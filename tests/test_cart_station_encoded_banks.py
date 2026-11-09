from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks
from test_cart_station_bank_windows import WindowCartridge


class EncodedCartridge(WindowCartridge):
    """Independent register decoding, including CHR bit 7 and fixed low PRG."""
    def __init__(self, mapper, prg, chr_data, ignore_chr_high=False, **options):
        super().__init__(mapper, prg, chr_data, **options)
        self.ignore_chr_high = ignore_chr_high

    def cpu_offset(self, address):
        if self.mapper != 97:
            return super().cpu_offset(address)
        if not 0x8000 <= address <= 0xffff:
            raise AssertionError('Unexpected CPU address')
        bank = len(self.prg) // 16384 - 1 if address < 0xc000 else self.bank
        return bank * 16384 + (address & 0x3fff)

    def memory(self, command, address, size):
        if command != 0x13:
            return super().memory(command, address, size)
        if self.mapper in (71, 97) or address != 0:
            raise AssertionError('Unexpected PPU read')
        start = self.chr_bank * 8192
        return self.chr[start:start + size]

    def cpu_register_write(self, address, value, dummy=True):
        direct = self.mapper in (71, 97)
        if direct:
            expected = 0xc000 if self.mapper == 71 else 0x8000
            if address != expected or not dummy:
                raise AssertionError('Expected direct write and dummy CPU read')
            effective = value
        else:
            if dummy or not 0x8000 <= address <= 0xffff:
                raise AssertionError('Expected conflict-search write')
            effective = value & self.prg[self.cpu_offset(address)]
        self.writes.append((address, value, dummy))
        if direct:
            self.bank = effective % (len(self.prg) // 16384)
        elif self.mapper == 78:
            self.bank = (effective & 7) % (len(self.prg) // 16384)
            self.chr_bank = (effective >> 4) % (len(self.chr) // 8192)
        elif self.mapper == 89:
            self.bank = ((effective >> 4) & 7) % (len(self.prg) // 16384)
            chr_number = (effective & 7) + (8 if effective & 128 else 0)
            if self.ignore_chr_high:
                chr_number &= 7
            self.chr_bank = chr_number % (len(self.chr) // 8192)
        elif self.mapper == 152:
            self.bank = ((effective >> 4) & 7) % (len(self.prg) // 16384)
            self.chr_bank = (effective & 15) % (len(self.chr) // 8192)
        if dummy:
            self.memory(0x11, 0x8000, 8)

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, prg_banks=8, chr_banks=None, submapper=0, **options):
    if chr_banks is None:
        chr_banks = 0 if mapper in (71, 97) else 16
    prg, chr_data = banks(prg_banks, 16384), banks(chr_banks, 8192, 43)
    return (profile_for(mapper, prg, chr_data, submapper=submapper),
            EncodedCartridge(mapper, prg, chr_data, **options), prg + chr_data)


class EncodedBankTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_pass_complete_roms_use_correct_windows_and_write_modes(self, sleep):
        for mapper, count in ((71, 1), (71, 16), (78, 1), (78, 8),
                               (89, 8), (97, 1), (97, 16), (152, 8)):
            with self.subTest(mapper=mapper, banks=count):
                profile, bus, raw = fixture(mapper, count)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *p: progress.append(p)),
                                 profile.header + raw)
                self.assertEqual(bus.full_reads, count * 2)
                self.assertEqual(set(bus.full_addresses), {0xc000 if mapper == 97 else 0x8000})
                self.assertEqual(bus.dummy_reads, len(bus.writes) if mapper in (71, 97) else 0)
                self.assertEqual(bus.modes, [1, 0])
                self.assertEqual(progress[-1][1], len(raw) * 2)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper78_submappers_keep_db_header_and_full_hash(self, sleep):
        for submapper in (0, 1, 3):
            profile, bus, raw = fixture(78, submapper=submapper)
            self.assertEqual(dump_profile(bus, profile), profile.header + raw)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_sunsoft_chr_high_bit_fault_is_caught_by_complete_hash(self, sleep):
        profile, bus, _ = fixture(89, ignore_chr_high=True)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertTrue(any(value & 128 for _, value, _ in bus.writes))
        self.assertEqual(bus.full_reads, 8)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_is_not_returned(self, sleep):
        for mapper in (71, 78, 89, 97, 152):
            profile, bus, _ = fixture(mapper, corrupt_second=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.full_reads, 16)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_same_geometry_candidates_require_complete_chr_match(self, sleep):
        for mapper in (78, 89, 152):
            profile, bus, raw = fixture(mapper)
            wrong = replace(profile_for(mapper, bus.prg, banks(16, 8192, 44)), id='wrong')
            matched, result = dump_candidates(bus, (wrong, profile))
            self.assertEqual(matched.id, profile.id)
            self.assertEqual(result, profile.header + raw)
            self.assertEqual(bus.full_reads, 16)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_stops_without_retry(self, sleep):
        for mapper in (71, 78, 89, 97, 152):
            profile, bus, _ = fixture(mapper)
            bus.memory = Mock(side_effect=ProtocolError('transfer stopped'))
            with self.assertRaisesRegex(ProtocolError, 'transfer stopped'):
                dump_profile(bus, profile)
            self.assertEqual(bus.memory.call_count, 1)
            self.assertEqual(bus.modes, [1, 0])

    def test_unsupported_geometries_and_submappers_do_not_touch_device(self):
        for mapper in (71, 78, 89, 97, 152):
            profile, _, _ = fixture(mapper)
            limit = 4194304 if mapper in (71, 97) else 131072
            changes = [{'submapper': 2 if mapper == 78 else 1},
                       {'prg_size': limit + 16384}]
            changes += ([{'chr_size': 8192}] if mapper in (71, 97) else
                        [{'chr_size': 0}, {'chr_size': 139264}])
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_embedded_profiles_and_reader_only_mapper71_are_distinguished(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((78, 2), (89, 1), (97, 1), (152, 4)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))
        self.assertFalse([p for p in db.profiles if p.mapper == 71])
        profile, _, _ = fixture(71)
        self.assertIsNone(Database((profile,)).by_id[profile.id].support())


if __name__ == '__main__':
    unittest.main()
