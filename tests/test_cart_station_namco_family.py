from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_storage import save_verified_rom
from cart_station_nes_db import (Database, auto_read_group, dump_candidates,
                                 dump_profile, verified_rom_file)
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks
from test_cart_station_paired_chr import PairedCartridge


class NamcoCartridge:
    """Independent indexed PRG/CHR windows and fixed 32 KiB wiring."""
    def __init__(self, mapper, submapper, prg, chr_data, corrupt_second=False):
        self.mapper, self.submapper, self.prg, self.chr = mapper, submapper, prg, chr_data
        self.fixed = mapper == 206 and submapper == 1
        self.unit = 16384 if self.fixed else 8192
        self.selector = -1
        self.prg_bank = self.chr_bank = 0
        self.corrupt_second = corrupt_second
        self.prg_reads = self.chr_reads = 0
        self.trace, self.writes, self.modes = [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def memory(self, command, address, size):
        self.trace.append(('read', command, address, size))
        if command == 0x11:
            if address == 0x8000 and size == 8:
                return self.prg[:8]
            if size != self.unit or address not in ((0x8000, 0xc000) if self.fixed else (0x8000,)):
                raise AssertionError('Wrong PRG geometry/window')
            self.prg_reads += 1
            offset = address - 0x8000 if self.fixed else self.prg_bank * 8192
            result = self.prg[offset:offset + size]
            if self.corrupt_second and self.prg_reads > len(self.prg) // self.unit:
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0x1000 or size != 1024:
            raise AssertionError('Wrong CHR window')
        if self.mapper == 119 and self.chr_bank & 64:
            raise AssertionError('CHR RAM must not be appended to the ROM')
        self.chr_reads += 1
        bank = self.chr_bank % (len(self.chr) // 1024)
        return self.chr[bank * 1024:(bank + 1) * 1024]

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy:
            raise AssertionError('Reference requires dummy CPU read')
        self.writes.append((address, value))
        self.trace.append(('write', address, value, dummy))
        if address == 0xa001 and self.mapper == 206 and not self.fixed and value == 0:
            pass
        elif address == 0x8000:
            if value == 6 and self.fixed:
                raise AssertionError('Fixed PRG must never receive PRG bank operations')
            if value not in (2, 6):
                raise AssertionError('Unexpected selector')
            self.selector = value
        elif address == 0x8001 and self.selector == 6:
            self.prg_bank = value % (len(self.prg) // 8192)
        elif address == 0x8001 and self.selector == 2:
            self.chr_bank = value
        else:
            raise AssertionError('Unexpected register operation')
        self.memory(0x11, 0x8000, 8)

    def mapper206_register(self, address, value):
        self.cpu_register_write(address, value)

    def nrom(self, prg_kib, chr_kib):
        if not self.fixed or (prg_kib, chr_kib) != (32, 0):
            raise AssertionError('Unexpected legacy fixed-PRG request')
        return self.memory(0x11, 0x8000, 16384) + self.memory(0x11, 0xc000, 16384)

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, submapper=0, prg_banks=16, chr_size=65536, **options):
    unit = 16384 if mapper == 206 and submapper == 1 else 8192
    prg = banks(prg_banks, unit)
    chr_data = banks(chr_size // 1024, 1024, 43)
    return (profile_for(mapper, prg, chr_data, submapper=submapper),
            NamcoCartridge(mapper, submapper, prg, chr_data, **options), prg + chr_data)


class NamcoFamilyTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_complete_two_pass_roms_for_banked_and_fixed_variants(self, sleep):
        for mapper, submapper, count, chr_size in ((118, 0, 32, 131072),
                (119, 0, 16, 65536), (206, 0, 16, 65536), (206, 0, 4, 32768),
                (206, 1, 2, 16384), (206, 1, 2, 32768)):
            with self.subTest(mapper=mapper, submapper=submapper, prg=count):
                profile, bus, raw = fixture(mapper, submapper, count, chr_size)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *p: progress.append(p)),
                                 profile.header + raw)
                self.assertEqual(bus.prg_reads, count * 2)
                self.assertEqual(bus.chr_reads, 2 * chr_size // 1024)
                self.assertEqual(progress[-1][1], 2 * len(raw))
                self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper154_uses_pair_windows_and_complete_hashes(self, sleep):
        prg, chr_data = banks(16, 8192), banks(128, 1024, 43)
        profile = profile_for(154, prg, chr_data)
        bus = PairedCartridge(154, prg, chr_data)
        self.assertEqual(dump_profile(bus, profile), profile.header + prg + chr_data)
        self.assertEqual(bus.ppu_windows, ([0] * 32 + [0x1000] * 32) * 2)
        bus = PairedCartridge(154, prg, chr_data, ignore_pair_second=True)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_fixed_prg_hash_gate_precedes_all_chr_writes(self, sleep):
        profile, bus, _ = fixture(206, 1, 2, 32768)
        bus.prg = banks(2, 16384, 1)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.writes, [])
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_fixed_206_group_is_identified_without_title_specific_reader(self, sleep):
        profile, bus, raw = fixture(206, 1, 2, 32768)
        wrong = replace(profile_for(206, bus.prg, banks(32, 1024, 44), submapper=1), id='wrong')
        self.assertEqual(auto_read_group((wrong, profile)), (wrong, profile))
        matched, result = dump_candidates(bus, (wrong, profile))
        self.assertEqual(matched.id, profile.id)
        self.assertEqual(result, profile.header + raw)
        banked = fixture(206, 0, 4, 32768)[0]
        self.assertEqual(auto_read_group((banked, profile)), ())

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_returns_no_rom(self, sleep):
        for mapper, submapper, count in ((118, 0, 16), (119, 0, 16),
                                          (206, 0, 16), (206, 1, 2)):
            profile, bus, _ = fixture(mapper, submapper, count, corrupt_second=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.prg_reads, count * 2)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_is_not_retried(self, sleep):
        for mapper, submapper, count in ((118, 0, 16), (119, 0, 16),
                                          (206, 0, 16), (206, 1, 2)):
            profile, bus, _ = fixture(mapper, submapper, count)
            bus.memory = Mock(side_effect=ProtocolError('transfer stopped'))
            with self.assertRaisesRegex(ProtocolError, 'transfer stopped'):
                dump_profile(bus, profile)
            self.assertEqual(bus.memory.call_count, 1)
            self.assertEqual(bus.modes, [1, 0])

    def test_unported_geometry_and_chr_ram_selection_are_rejected_before_io(self):
        for mapper, submapper, change in (
                (118, 0, {'chr_size': 139264}), (119, 0, {'chr_size': 73728}),
                (118, 0, {'submapper': 1}), (119, 0, {'submapper': 1}),
                (206, 0, {'submapper': 2}), (206, 1, {'prg_size': 65536}),
                (206, 0, {'chr_size': 270336}), (206, 0, {'prg_size': 2105344})):
            profile = fixture(mapper, submapper, 2 if submapper else 16)[0]
            bus = Mock()
            with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, **change))
            self.assertFalse(bus.mock_calls)
        profile = profile_for(154, banks(16, 8192), banks(128, 1024))
        self.assertIsNotNone(replace(profile, chr_size=139264).support())

    def test_embedded_profiles_are_applicable(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((118, 7), (119, 4), (154, 1), (206, 34)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_generic_fixed_206_saved_file_keeps_db_header_and_full_hashes(self, sleep):
        profile, bus, _ = fixture(206, 1, 2, 32768)
        payload = dump_profile(bus, profile)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / profile.filename()
            path, created = save_verified_rom(target, payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
            self.assertEqual(path.read_bytes(), payload)


if __name__ == '__main__':
    unittest.main()
