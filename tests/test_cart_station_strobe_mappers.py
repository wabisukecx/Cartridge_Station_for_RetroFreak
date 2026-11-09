from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import (Database, dump_profile, dump_candidates,
                                 verified_rom_file)
from cart_station_nes_mappers import MapperReader
from cart_station_storage import save_verified_rom
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class WindowCartridge:
    """Separate register windows; 72/92 commit banks on rising strobe bits.

    This models ROM access only. IRQ, audio and save memory are not simulated.
    """
    def __init__(self, mapper, prg, chr_data, corrupt_second=False, ignore_strobe=False):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.prg_unit = 8192 if mapper in (48, 210) else 16384
        self.chr_unit = 1024 if mapper in (48, 210) else 8192
        self.prg_bank = self.chr_bank = self.previous = 0
        self.prg_reads = self.chr_reads = 0
        self.corrupt_second, self.ignore_strobe = corrupt_second, ignore_strobe
        self.modes, self.writes, self.windows = [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def memory(self, command, address, size):
        if command == 0x11:
            base = 0xc000 if self.mapper == 92 else 0x8000
            # The other half remains fixed for 16 KiB banked boards.
            if self.prg_unit == 8192:
                offset = self.prg_bank * 8192 + (address - 0x8000) % 8192
            else:
                number = self.prg_bank if base <= address < base + 16384 else len(self.prg) // 16384 - 1
                offset = number * 16384 + (address & 16383)
            result = self.prg[offset:offset + size]
            if size == self.prg_unit:
                self.windows.append(address)
                self.prg_reads += 1
                if self.corrupt_second and self.prg_reads > len(self.prg) // self.prg_unit:
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        expected = 0x1000 if self.mapper == 48 else 0
        if command != 0x13 or address != expected or size != self.chr_unit or not self.chr:
            raise AssertionError('Wrong CHR window or unexpected CHR-RAM read')
        self.chr_reads += 1
        offset = self.chr_bank * self.chr_unit
        return self.chr[offset:offset + size]

    def cpu_register_write(self, address, value, dummy=True):
        self.writes.append((address, value, dummy))
        if self.mapper in (72, 92):
            if dummy or not 0x8000 <= address <= 0xffff:
                raise AssertionError('Strobe boards require conflict-search writes')
            effective = value & self.memory(0x11, address, 1)[0]
            if not self.ignore_strobe:
                if effective & 128 and not self.previous & 128:
                    self.prg_bank = (effective & 127) % (len(self.prg) // 16384)
                if effective & 64 and not self.previous & 64:
                    self.chr_bank = (effective & 63) % (len(self.chr) // 8192)
            self.previous = effective
        else:
            if not dummy:
                raise AssertionError('Direct writes require the reference dummy read')
            if (self.mapper, address) in ((48, 0x8000), (73, 0xf000), (210, 0xe000)):
                self.prg_bank = value % (len(self.prg) // self.prg_unit)
            elif (self.mapper, address) in ((48, 0xa000), (210, 0x8000)):
                self.chr_bank = value % (len(self.chr) // self.chr_unit)
            else:
                raise AssertionError('Unexpected register, including save/IRQ controls')
            self.memory(0x11, 0x8000, 8)

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, submapper=0, corrupt_second=False, ignore_strobe=False):
    prg_unit = 8192 if mapper in (48, 210) else 16384
    chr_unit = 1024 if mapper in (48, 210) else 8192
    prg = banks(64 if mapper == 210 else 16, prg_unit)
    chr_data = b'' if mapper == 73 else banks(256 if mapper in (48, 210) else 16, chr_unit, 37)
    profile = profile_for(mapper, prg, chr_data, submapper)
    return profile, WindowCartridge(mapper, prg, chr_data, corrupt_second, ignore_strobe), prg + chr_data


class StrobeMapperTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_full_two_pass_reads_cover_each_supported_submapper(self, sleep):
        for mapper, subs in ((48, (0, 1)), (72, (0,)), (73, (0,)),
                             (92, (0,)), (210, (0, 1, 2))):
            for sub in subs:
                with self.subTest(mapper=mapper, submapper=sub):
                    p, bus, raw = fixture(mapper, sub)
                    progress = []
                    self.assertIsNone(p.support())
                    self.assertEqual(dump_profile(bus, p, progress=lambda *v: progress.append(v)), p.header + raw)
                    self.assertEqual(bus.prg_reads, 2 * p.prg_size // bus.prg_unit)
                    self.assertEqual(bus.chr_reads, 2 * p.chr_size // bus.chr_unit)
                    self.assertEqual(bus.modes, [1, 0])
                    self.assertEqual(progress[-1][1], 2 * len(raw))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_strobe_transition_is_required_to_pass_full_hashes(self, sleep):
        for mapper in (72, 92):
            p, bus, _ = fixture(mapper, ignore_strobe=True)
            with self.subTest(mapper=mapper), self.assertRaises(ProtocolError):
                dump_profile(bus, p)
            self.assertEqual(bus.modes, [1, 0])
            self.assertFalse(bus.chr_reads)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_strobe_search_writes_match_current_rom_byte_and_both_edges(self, sleep):
        for mapper in (72, 92):
            p, bus, raw = fixture(mapper)
            self.assertEqual(dump_profile(bus, p), p.header + raw)
            values = [v for a, v, dummy in bus.writes]
            self.assertEqual(values[:32], [v for bank in range(16) for v in (bank, bank | 128)])
            self.assertEqual(values[32:64], [v for bank in range(16) for v in (bank, bank | 64)])
            self.assertTrue(all(not dummy for a, v, dummy in bus.writes))
            self.assertEqual(set(bus.windows), {0xc000 if mapper == 92 else 0x8000})

    @patch('cart_station_nes_mappers.time.sleep')
    def test_wrong_cpu_window_cannot_pass_mapper92_hash(self, sleep):
        p, bus, _ = fixture(92)
        memory = bus.memory
        bus.memory = lambda command, address, size: memory(command, 0x8000 if command == 0x11 and size == 16384 else address, size)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, p)
        self.assertFalse(bus.chr_reads)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper210_mask_and_registers_stay_out_of_save_memory(self, sleep):
        p, bus, _ = fixture(210, 2)
        reader = MapperReader(bus, p)
        self.assertEqual(reader.prg_bank(127, 8192), bus.prg[-8192:])
        self.assertEqual(bus.writes, [(0xe000, 63, True)])
        self.assertEqual(reader.chr_rom_bank(255, 1024), bus.chr[-1024:])
        self.assertEqual(bus.writes[-1], (0x8000, 255, True))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_corrupt_second_pass_stops_without_returning_rom(self, sleep):
        for mapper in (48, 72, 73, 92, 210):
            p, bus, _ = fixture(mapper, corrupt_second=True)
            with self.subTest(mapper=mapper), self.assertRaises(ProtocolError):
                dump_profile(bus, p)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_is_not_retried_and_restores_mode(self, sleep):
        p, bus, _ = fixture(210)
        bus.memory = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, p)
        self.assertEqual(bus.memory.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_unsupported_geometry_and_subvariants_stop_before_io(self):
        for mapper in (48, 72, 73, 92, 210):
            p, _, _ = fixture(mapper)
            changes = [dict(submapper=3), dict(prg_size=24576), dict(chr_size=1)]
            if mapper == 210:
                changes.append(dict(prg_size=532480))
            if mapper in (72, 92):
                changes.extend((dict(prg_size=2113536), dict(chr_size=532480)))
            if mapper == 73:
                changes.append(dict(chr_size=8192))
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(p, **change))
                self.assertFalse(bus.mock_calls)

    def test_bundled_profiles_gain_24_applicable_entries(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((48, 6), (72, 3), (73, 1), (92, 2), (210, 12)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_hash_selection_and_saved_header(self, sleep):
        p, bus, raw = fixture(210, 1)
        other = replace(p, id='other', prg_sha1='0' * 40, raw_sha1='0' * 40)
        matched, payload = dump_candidates(bus, (other, p))
        self.assertEqual(matched, p)
        self.assertEqual(payload, p.header + raw)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / p.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, p))
