from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station_nes_mappers import profile_for, Cartridge as MmcCartridge
from test_cart_station_simple_mappers import banks


class Cartridge:
    """CPROM fixed PRG, UNROM fixed search window and mixed CHR-ROM/RAM bus."""
    def __init__(self, mapper, prg, chr_data, fault=None):
        self.mapper, self.prg, self.chr, self.fault = mapper, prg, chr_data, fault
        self.prg_bank = self.chr_bank = 0
        self.unit = 32768 if mapper == 77 else 16384
        self.prg_reads = self.chr_reads = 0
        self.writes, self.searches, self.modes = [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def offset(self, address):
        if self.mapper == 13:
            return address - 0x8000
        if self.mapper == 30:
            bank = self.prg_bank if address < 0xc000 else len(self.prg) // 16384 - 1
            return bank * 16384 + (address & 16383)
        return self.prg_bank * 32768 + address - 0x8000

    def memory(self, command, address, size):
        if command == 0x11:
            offset = self.offset(address)
            result = self.prg[offset:offset + size]
            if size == 1024:
                self.searches.append(address)
            if size == self.unit:
                self.prg_reads += 1
                if self.fault == 'second' and self.prg_reads > len(self.prg) // self.unit:
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or self.mapper != 77 or address != 0 or size != 2048:
            raise AssertionError('Do not append CHR RAM or read beyond the 2 KiB ROM window')
        self.chr_reads += 1
        offset = self.chr_bank * 2048
        return self.chr[offset:offset + size]

    def cpu_register_write(self, address, value, dummy=True):
        if self.mapper == 13 or dummy:
            raise AssertionError('Fixed CPROM needs no writes; others use conflict searches')
        if self.mapper == 30 and not 0xc000 <= address <= 0xffff:
            raise AssertionError('UNROM512 search must stay in the fixed upper window')
        self.writes.append((address, value))
        effective = value & self.prg[self.offset(address)]
        if self.mapper == 30:
            self.prg_bank = effective & 31
        else:
            self.prg_bank = effective & 15
            self.chr_bank = 0 if self.fault == 'chr' else effective >> 4

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, fault=None):
    prg = banks({13: 2, 30: 32, 77: 16}[mapper], 32768 if mapper == 77 else 16384)
    chr_data = banks(16, 2048, 43) if mapper == 77 else b''
    return profile_for(mapper, prg, chr_data), Cartridge(mapper, prg, chr_data, fault), prg + chr_data


class Mmc3VariantTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_complete_roms_for_new_mapper_numbers(self, sleep):
        for mapper in (13, 30, 77):
            profile, bus, raw = fixture(mapper)
            progress = []
            self.assertIsNone(profile.support())
            self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)), profile.header + raw)
            self.assertEqual(bus.prg_reads, 2 * profile.prg_size // bus.unit)
            self.assertEqual(bus.chr_reads, 32 if mapper == 77 else 0)
            self.assertEqual(bus.modes, [1, 0])
            self.assertEqual(progress[-1][1], 2 * len(raw))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper4_subvariants_keep_common_rom_protocol_and_db_header(self, sleep):
        prg, chr_data = banks(32, 8192), banks(256, 1024, 43)
        for submapper in (0, 1, 3):
            profile = profile_for(4, prg, chr_data, submapper)
            bus = MmcCartridge(4, prg, chr_data)
            self.assertIsNone(profile.support())
            payload = dump_profile(bus, profile)
            self.assertEqual(payload, profile.header + prg + chr_data)
            self.assertEqual(payload[8] >> 4, submapper)
            self.assertEqual(bus.modes, [1, 0])
            self.assertTrue(all(address in (0x8000, 0x8001) for address, value, dummy in bus.writes))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_cprom_reads_only_fixed_prg_and_never_touches_chr_ram(self, sleep):
        profile, bus, raw = fixture(13)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertFalse(bus.writes)
        self.assertFalse(bus.chr_reads)
        self.assertEqual(bus.prg_reads, 4)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_unrom512_uses_fixed_search_window_and_no_flash_unlock_sequence(self, sleep):
        profile, bus, raw = fixture(30)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertTrue(all(0xc000 <= address <= 0xffff for address in bus.searches))
        self.assertEqual([value for address, value in bus.writes], list(range(32)) * 2)
        self.assertTrue(all(value <= 31 for address, value in bus.writes))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper77_uses_chr_upper_nibble_and_rom_window_only(self, sleep):
        profile, bus, raw = fixture(77)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual([v for a, v in bus.writes[16:32]], [b << 4 for b in range(16)])
        profile, bus, _ = fixture(77, 'chr')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])

    def test_unsupported_layouts_stop_before_any_io(self):
        for mapper in (13, 30, 77):
            profile, _, _ = fixture(mapper)
            changes = [dict(submapper=1), dict(prg_size=24576), dict(chr_size=1)]
            changes.append(dict(prg_size={13: 16384, 30: 540672, 77: 557056}[mapper]))
            changes.append(dict(chr_size=8192 if mapper in (13, 30) else 34816))
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)
        profile = profile_for(4, banks(16, 8192), banks(8, 1024))
        for submapper in (2, 4, 15):
            bus = Mock()
            with self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, submapper=submapper))
            self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_stops_for_fixed_and_banked_roms(self, sleep):
        for mapper in (13, 30, 77):
            profile, bus, _ = fixture(mapper, 'second')
            with self.subTest(mapper=mapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])
        profile = profile_for(4, banks(16, 8192), banks(8, 1024), 1)
        bus = MmcCartridge(4, banks(16, 8192), banks(8, 1024))
        actual_memory = bus.memory
        full_reads = [0]
        def corrupt_second(command, address, size):
            result = actual_memory(command, address, size)
            if command == 0x11 and size == 8192:
                full_reads[0] += 1
                if full_reads[0] > 16:
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        bus.memory = corrupt_second
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_restores_mode_without_retry(self, sleep):
        profile, bus, _ = fixture(77)
        bus.cpu_register_write = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.cpu_register_write.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_bundled_database_gains_19_applicable_profiles(self):
        db = Database.load(data('nes_profiles.reference.json'))
        new_mmc = [p for p in db.profiles if p.mapper == 4 and p.submapper in (1, 3)]
        self.assertEqual(len(new_mmc), 16)
        self.assertTrue(all(p.support() is None for p in new_mmc))
        for mapper in (13, 30, 77):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), 1)
            self.assertIsNone(profiles[0].support())

    @patch('cart_station_nes_mappers.time.sleep')
    def test_full_hash_candidate_selection_and_saved_header(self, sleep):
        profile, bus, raw = fixture(77)
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
        bus = Mock()
        mmc = profile_for(4, banks(16, 8192), banks(8, 1024), 1)
        with self.assertRaises(ProtocolError):
            dump_candidates(bus, (mmc, replace(mmc, submapper=3)))
        self.assertFalse(bus.mock_calls)
