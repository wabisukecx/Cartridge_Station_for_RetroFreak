from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class WindowCartridge:
    """Independent fixed/banked CPU windows and optional wired-AND writes."""
    def __init__(self, mapper, prg, chr_data=b'', ignore_selection=False,
                 corrupt_second=False):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.unit = 32768 if mapper == 34 else 16384
        self.bank = self.chr_bank = 0
        self.ignore_selection = ignore_selection
        self.corrupt_second = corrupt_second
        self.full_reads = self.dummy_reads = 0
        self.modes, self.writes, self.full_addresses = [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_offset(self, address):
        if not 0x8000 <= address <= 0xffff:
            raise AssertionError('Unexpected CPU address')
        if self.mapper == 34:
            return self.bank * 32768 + address - 0x8000
        if self.mapper == 180:
            bank = 0 if address < 0xc000 else self.bank
        else:
            bank = self.bank if address < 0xc000 else len(self.prg) // 16384 - 1
        return bank * 16384 + (address & 0x3fff)

    def memory(self, command, address, size):
        if command == 0x11:
            result = bytes(self.prg[self.cpu_offset(address + i)] for i in range(size))
            if size == self.unit:
                self.full_reads += 1
                self.full_addresses.append(address)
                if self.corrupt_second and self.full_reads > len(self.prg) // self.unit:
                    result = bytes((result[0] ^ 1,)) + result[1:]
            elif size == 8:
                self.dummy_reads += 1
            return result
        if command != 0x13 or self.mapper != 70 or address != 0:
            raise AssertionError('Unexpected CHR access')
        offset = self.chr_bank * 8192
        return self.chr[offset:offset + size]

    def cpu_register_write(self, address, value, dummy=True):
        if self.mapper == 94:
            if address != 0x8000 or not dummy:
                raise AssertionError('Expected direct reference write')
            effective = value
        else:
            if not 0x8000 <= address <= 0xffff or dummy:
                raise AssertionError('Expected conflict-search reference write')
            effective = value & self.prg[self.cpu_offset(address)]
        self.writes.append((address, value, dummy))
        if not self.ignore_selection:
            if self.mapper == 34:
                self.bank = effective % (len(self.prg) // 32768)
            elif self.mapper == 70:
                self.bank = (effective >> 4) % (len(self.prg) // 16384)
                self.chr_bank = (effective & 15) % (len(self.chr) // 8192)
            elif self.mapper == 94:
                self.bank = (effective >> 2) % (len(self.prg) // 16384)
            elif self.mapper == 180:
                self.bank = effective % (len(self.prg) // 16384)
        if dummy:
            self.memory(0x11, 0x8000, 8)

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, prg_banks=8, chr_banks=0, submapper=0, **options):
    unit = 32768 if mapper == 34 else 16384
    prg = banks(prg_banks, unit)
    chr_data = banks(chr_banks, 8192, 43)
    profile = profile_for(mapper, prg, chr_data, submapper=submapper)
    return profile, WindowCartridge(mapper, prg, chr_data, **options), prg + chr_data


class BankWindowTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_independent_reads_use_correct_cpu_windows(self, sleep):
        for mapper, prg_banks, chr_banks in ((34, 1, 0), (34, 4, 0),
                                           (70, 1, 1), (70, 8, 16),
                                           (94, 1, 0), (94, 64, 0),
                                           (180, 1, 0), (180, 8, 0)):
            with self.subTest(mapper=mapper, banks=prg_banks):
                profile, bus, raw = fixture(mapper, prg_banks, chr_banks)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *p: progress.append(p)),
                                 profile.header + raw)
                self.assertEqual(bus.full_reads, prg_banks * 2)
                self.assertEqual(set(bus.full_addresses), {0xc000 if mapper == 180 else 0x8000})
                self.assertEqual(bus.dummy_reads, len(bus.writes) if mapper == 94 else 0)
                self.assertEqual(bus.modes, [1, 0])
                self.assertEqual(progress[-1][1], len(raw) * 2)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_bnrom_submappers_and_nina001_are_separate(self, sleep):
        for submapper in (0, 2):
            profile, bus, raw = fixture(34, 4, submapper=submapper)
            self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        # NINA-001 uses the dedicated lower registers; BNROM must reject its CHR layout.
        profile, _, _ = fixture(34, 2, 8, submapper=1)
        self.assertIsNone(profile.support())
        bus = Mock()
        with self.assertRaises(ProtocolError):
            dump_profile(bus, replace(profile, submapper=0))
        self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_wrong_bank_selection_never_returns_rom(self, sleep):
        for mapper in (34, 70, 94, 180):
            profile, bus, _ = fixture(mapper, chr_banks=4 if mapper == 70 else 0,
                                      ignore_selection=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_never_returns_rom(self, sleep):
        for mapper in (34, 70, 94, 180):
            profile, bus, _ = fixture(mapper, chr_banks=4 if mapper == 70 else 0,
                                      corrupt_second=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.full_reads, 16)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_same_geometry_candidates_are_verified_against_chr(self, sleep):
        profile, bus, raw = fixture(70, chr_banks=4)
        wrong = replace(profile_for(70, bus.prg, banks(4, 8192, 44)), id='wrong')
        matched, result = dump_candidates(bus, (wrong, profile))
        self.assertEqual(matched.id, profile.id)
        self.assertEqual(result, profile.header + raw)
        self.assertEqual(bus.full_reads, 16)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_error_is_not_retried(self, sleep):
        for mapper in (34, 70, 94, 180):
            profile, bus, _ = fixture(mapper, chr_banks=4 if mapper == 70 else 0)
            bus.memory = Mock(side_effect=ProtocolError('transport failure'))
            with self.assertRaisesRegex(ProtocolError, 'transport failure'):
                dump_profile(bus, profile)
            self.assertEqual(bus.memory.call_count, 1)
            self.assertEqual(bus.modes, [1, 0])

    def test_unaddressable_geometry_is_rejected_before_io(self):
        for mapper, maximum in ((34, 8388608), (70, 262144),
                                (94, 1048576), (180, 4194304)):
            profile, _, _ = fixture(mapper, chr_banks=4 if mapper == 70 else 0)
            changes = [{'submapper': 1}, {'prg_size': maximum + 32768}]
            changes += ([{'chr_size': 0}, {'chr_size': 139264}] if mapper == 70
                        else [{'chr_size': 8192}])
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_embedded_profiles_are_enabled_only_for_ported_layouts(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((34, 3), (70, 4), (94, 1), (180, 1)):
            profiles = [p for p in db.profiles if p.mapper == mapper and p.support() is None]
            self.assertEqual(len(profiles), count)
        nina = [p for p in db.profiles if p.mapper == 34 and p.submapper == 1]
        self.assertTrue(nina)
        self.assertTrue(all(p.support() is None for p in nina))


if __name__ == '__main__':
    unittest.main()
