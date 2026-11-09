from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class SmallBankCartridge:
    """Independent 8 KiB PRG and 1/4 KiB CHR windows with mode/register state."""
    def __init__(self, mapper, prg, chr_data, ignore_mode=False,
                 ignore_chr_upper=False, corrupt_second=False):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.chr_unit = 4096 if mapper == 75 else 1024
        self.prg_bank = self.chr_bank = self.chr_upper = 0
        self.swapped = True
        self.ignore_mode = ignore_mode
        self.ignore_chr_upper = ignore_chr_upper
        self.corrupt_second = corrupt_second
        self.prg_reads = self.chr_reads = self.dummy_reads = 0
        self.modes, self.writes, self.cpu_windows, self.ppu_windows = [], [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def memory(self, command, address, size):
        if command == 0x11:
            if size == 8 and address == 0x8000:
                self.dummy_reads += 1
                return self.prg[:8]
            expected = 0xa000 if self.mapper == 65 else 0x8000
            if address != expected or size != 8192:
                raise AssertionError('Wrong PRG window')
            self.prg_reads += 1
            self.cpu_windows.append(address)
            bank = (len(self.prg) // 8192 - 2
                    if self.mapper == 32 and self.swapped else self.prg_bank)
            result = self.prg[bank * 8192:(bank + 1) * 8192]
            if self.corrupt_second and self.prg_reads > len(self.prg) // 8192:
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        expected = 0x1000 if self.mapper == 33 else 0
        if command != 0x13 or address != expected or size != self.chr_unit:
            raise AssertionError('Wrong CHR window')
        self.chr_reads += 1
        self.ppu_windows.append(address)
        bank = self.chr_bank
        if self.mapper == 75:
            bank |= self.chr_upper << 4
        bank %= len(self.chr) // self.chr_unit
        return self.chr[bank * self.chr_unit:(bank + 1) * self.chr_unit]

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy:
            raise AssertionError('Reference requires dummy read')
        self.writes.append((address, value))
        if self.mapper == 32:
            if address == 0x9000:
                if not self.ignore_mode:
                    self.swapped = value != 0
            elif address == 0x8000:
                self.prg_bank = value % (len(self.prg) // 8192)
            elif address == 0xb000:
                self.chr_bank = value
            else:
                raise AssertionError('Unexpected G-101 register')
        elif self.mapper == 33:
            if address == 0x8000:
                self.prg_bank = value % (len(self.prg) // 8192)
            elif address == 0xa000:
                self.chr_bank = value
            else:
                raise AssertionError('Unexpected TC0190 register')
        elif self.mapper == 65:
            if address == 0xa000:
                self.prg_bank = value % (len(self.prg) // 8192)
            elif address == 0xb000:
                self.chr_bank = value
            else:
                raise AssertionError('Unexpected H3001 register')
        elif self.mapper == 75:
            if address == 0x8000:
                self.prg_bank = value % (len(self.prg) // 8192)
            elif address == 0x9000:
                self.chr_upper = 0 if self.ignore_chr_upper else (value >> 1) & 1
            elif address == 0xe000:
                self.chr_bank = value & 15
            else:
                raise AssertionError('Unexpected VRC1 register')
        self.memory(0x11, 0x8000, 8)

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, prg_banks=16, chr_banks=None, **options):
    unit = 4096 if mapper == 75 else 1024
    if chr_banks is None:
        chr_banks = 32 if mapper == 75 else 128
    prg, chr_data = banks(prg_banks, 8192), banks(chr_banks, unit, 43)
    return (profile_for(mapper, prg, chr_data),
            SmallBankCartridge(mapper, prg, chr_data, **options), prg + chr_data)


class SmallBankTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_pass_roms_preserve_small_bank_boundaries_and_windows(self, sleep):
        for mapper, prg_banks, chr_banks in ((32, 4, 8), (32, 32, 128),
                                           (33, 4, 8), (33, 32, 256),
                                           (65, 32, 256), (75, 4, 2), (75, 16, 32)):
            with self.subTest(mapper=mapper, prg=prg_banks, chr=chr_banks):
                profile, bus, raw = fixture(mapper, prg_banks, chr_banks)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *p: progress.append(p)),
                                 profile.header + raw)
                self.assertEqual(bus.prg_reads, prg_banks * 2)
                self.assertEqual(bus.chr_reads, chr_banks * 2)
                self.assertEqual(bus.dummy_reads, len(bus.writes))
                self.assertEqual(set(bus.cpu_windows), {0xa000 if mapper == 65 else 0x8000})
                self.assertEqual(set(bus.ppu_windows), {0x1000 if mapper == 33 else 0})
                self.assertEqual(progress[-1][1], len(raw) * 2)
                self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_g101_mode_register_fault_is_detected(self, sleep):
        profile, bus, _ = fixture(32, ignore_mode=True)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_vrc1_chr_high_bit_fault_is_detected(self, sleep):
        profile, bus, _ = fixture(75, ignore_chr_upper=True)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertIn((0x9000, 2), bus.writes)
        self.assertEqual(bus.chr_reads, 32)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_returns_no_rom(self, sleep):
        for mapper in (32, 33, 65, 75):
            profile, bus, _ = fixture(mapper, corrupt_second=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.prg_reads, 32)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_same_geometry_candidates_require_complete_chr_match(self, sleep):
        for mapper in (32, 33, 65, 75):
            profile, bus, raw = fixture(mapper)
            wrong_chr = banks(len(bus.chr) // bus.chr_unit, bus.chr_unit, 44)
            wrong = replace(profile_for(mapper, bus.prg, wrong_chr), id='wrong')
            matched, result = dump_candidates(bus, (wrong, profile))
            self.assertEqual(matched.id, profile.id)
            self.assertEqual(result, profile.header + raw)
            self.assertEqual(bus.prg_reads, 32)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_is_not_retried(self, sleep):
        for mapper in (32, 33, 65, 75):
            profile, bus, _ = fixture(mapper)
            bus.memory = Mock(side_effect=ProtocolError('transfer failed'))
            with self.assertRaisesRegex(ProtocolError, 'transfer failed'):
                dump_profile(bus, profile)
            self.assertEqual(bus.memory.call_count, 1)
            self.assertEqual(bus.modes, [1, 0])

    def test_unsupported_submappers_and_geometries_do_not_touch_device(self):
        for mapper in (32, 33, 65, 75):
            profile, _, _ = fixture(mapper)
            limit = 131072 if mapper == 75 else 262144
            for change in ({'submapper': 2 if mapper == 32 else 1}, {'prg_size': 2097152 + 8192},
                           {'prg_size': 16384}, {'chr_size': 0},
                           {'chr_size': limit + 8192}):
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_embedded_profiles_include_reviewed_g101_submapper1(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((32, 7), (33, 9), (65, 3), (75, 6)):
            applicable = [p for p in db.profiles if p.mapper == mapper and p.support() is None]
            self.assertEqual(len(applicable), count)
        special = [p for p in db.profiles if p.mapper == 32 and p.submapper == 1]
        self.assertEqual(len(special), 1)
        self.assertIsNone(special[0].support())


if __name__ == '__main__':
    unittest.main()
