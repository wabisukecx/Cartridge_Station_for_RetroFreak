from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class PairedCartridge:
    """Independent direct/indexed banks, including two different CHR windows."""
    def __init__(self, mapper, prg, chr_data, ignore_pair_second=False, corrupt_second=False):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.unit = 16384 if mapper in (67, 68) else 8192
        self.prg_bank = self.chr_bank = 0
        self.selector = -1
        self.registers = {0: 0, 2: 0, 3: 1}
        self.ignore_pair_second, self.corrupt_second = ignore_pair_second, corrupt_second
        self.prg_reads = self.chr_reads = self.dummy_reads = 0
        self.writes, self.modes, self.ppu_windows = [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def memory(self, command, address, size):
        if command == 0x11:
            if address == 0x8000 and size == 8:
                self.dummy_reads += 1
                return self.prg[:8]
            if address != 0x8000 or size != self.unit:
                raise AssertionError('Wrong CPU window')
            self.prg_reads += 1
            bank = self.prg_bank % (len(self.prg) // self.unit)
            result = self.prg[bank * self.unit:(bank + 1) * self.unit]
            if self.corrupt_second and self.prg_reads > len(self.prg) // self.unit:
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13:
            raise AssertionError('Unexpected bus command')
        self.chr_reads += 1
        self.ppu_windows.append(address)
        if self.mapper in (88, 154):
            if size != 2048:
                raise AssertionError('Wrong paired CHR size')
            if address == 0:
                offset = (self.registers[0] & 62) * 1024
                return self.chr[offset:offset + size]
            if address == 0x1000:
                first = ((self.registers[2] & 63) | 64) * 1024
                second = ((self.registers[3] & 63) | 64) * 1024
                return self.chr[first:first + 1024] + self.chr[second:second + 1024]
            raise AssertionError('Wrong Mapper 88 CHR window')
        unit, expected = (1024, 0x1000) if self.mapper == 95 else (2048, 0)
        if size != unit or address != expected:
            raise AssertionError('Wrong CHR bank/window')
        bank = self.chr_bank % (len(self.chr) // unit)
        return self.chr[bank * unit:(bank + 1) * unit]

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy:
            raise AssertionError('Reference requires dummy CPU read')
        self.writes.append((address, value))
        if self.mapper in (67, 68):
            prg_reg, chr_reg = (0xf800, 0x8800) if self.mapper == 67 else (0xf000, 0x8000)
            if address == prg_reg:
                if self.mapper == 68 and value >= 16:
                    raise AssertionError('Base Sunsoft-4 should not enable save/add-on paths')
                self.prg_bank = value
            elif address == chr_reg:
                self.chr_bank = value
            else:
                raise AssertionError('Unexpected Sunsoft register')
        elif address == 0xa001 and self.mapper in (88, 95, 154) and value == 0:
            pass
        elif address == 0x8000:
            self.selector = value
        elif address == 0x8001:
            if self.selector == 6:
                self.prg_bank = value
            elif self.mapper == 76 and self.selector == 2:
                self.chr_bank = value
            elif self.mapper == 95 and self.selector == 2:
                self.chr_bank = value
            elif self.mapper in (88, 154) and self.selector in (0, 2, 3):
                if not (self.ignore_pair_second and self.selector == 3):
                    self.registers[self.selector] = value
            else:
                raise AssertionError('Unexpected bank selector')
        else:
            raise AssertionError('Unexpected register/write outside ROM controls')
        self.memory(0x11, 0x8000, 8)

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, prg_banks=16, chr_size=131072, **options):
    unit = 16384 if mapper in (67, 68) else 8192
    prg, chr_data = banks(prg_banks, unit), banks(chr_size // 1024, 1024, 43)
    return (profile_for(mapper, prg, chr_data), PairedCartridge(mapper, prg, chr_data, **options),
            prg + chr_data)


class PairedChrTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_pass_roms_and_reference_dummy_reads(self, sleep):
        for mapper, prg_banks, chr_size in ((67, 1, 8192), (67, 8, 131072),
                                          (68, 8, 262144), (76, 16, 131072),
                                          (88, 16, 65536), (88, 16, 131072),
                                          (95, 16, 32768)):
            with self.subTest(mapper=mapper, chr=chr_size):
                profile, bus, raw = fixture(mapper, prg_banks, chr_size)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *p: progress.append(p)),
                                 profile.header + raw)
                self.assertEqual(bus.prg_reads, 2 * prg_banks)
                self.assertEqual(bus.dummy_reads, len(bus.writes))
                self.assertEqual(progress[-1][1], 2 * len(raw))
                self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper88_switches_chr_window_at_exact_64k_boundary(self, sleep):
        profile, bus, raw = fixture(88)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual(bus.ppu_windows, ([0] * 32 + [0x1000] * 32) * 2)
        self.assertEqual(sum(address == 0xa001 for address, _ in bus.writes), 32)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper88_missing_second_half_bank_is_detected(self, sleep):
        profile, bus, _ = fixture(88, ignore_pair_second=True)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertIn(0x1000, bus.ppu_windows)
        self.assertEqual(bus.prg_reads, 16)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_never_returns_rom(self, sleep):
        for mapper in (67, 68, 76, 88, 95):
            profile, bus, _ = fixture(mapper, corrupt_second=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.prg_reads, 32)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_same_geometry_candidates_use_complete_chr_hash(self, sleep):
        for mapper in (67, 68, 76, 88, 95):
            profile, bus, raw = fixture(mapper)
            wrong = replace(profile_for(mapper, bus.prg, banks(len(bus.chr) // 1024, 1024, 44)), id='wrong')
            matched, result = dump_candidates(bus, (wrong, profile))
            self.assertEqual(matched.id, profile.id)
            self.assertEqual(result, profile.header + raw)
            self.assertEqual(bus.prg_reads, 32)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_stops_without_retry(self, sleep):
        for mapper in (67, 68, 76, 88, 95):
            profile, bus, _ = fixture(mapper)
            bus.memory = Mock(side_effect=ProtocolError('transfer stopped'))
            with self.assertRaisesRegex(ProtocolError, 'transfer stopped'):
                dump_profile(bus, profile)
            self.assertEqual(bus.memory.call_count, 1)
            self.assertEqual(bus.modes, [1, 0])

    def test_unknown_submapper_and_unaddressable_geometry_are_rejected_before_io(self):
        for mapper in (67, 68, 76, 88, 95):
            profile, _, _ = fixture(mapper)
            prg_max = 4194304 if mapper == 67 else 262144 if mapper == 68 else 2097152
            chr_max = 131072 if mapper == 88 else 262144 if mapper == 95 else 524288
            for change in ({'submapper': 2 if mapper == 68 else 1}, {'prg_size': prg_max + 32768},
                           {'chr_size': 0}, {'chr_size': chr_max + 8192}):
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_embedded_profiles_keep_sunsoft_add_on_separate(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((67, 2), (68, 5), (76, 1), (88, 3), (95, 1)):
            profiles = [p for p in db.profiles if p.mapper == mapper and p.support() is None]
            self.assertEqual(len(profiles), count)
        add_on = [p for p in db.profiles if p.mapper == 68 and p.submapper == 1]
        self.assertEqual(len(add_on), 3)
        self.assertTrue(all(p.support() is None for p in add_on))


if __name__ == '__main__':
    unittest.main()
