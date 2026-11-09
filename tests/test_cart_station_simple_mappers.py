from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates
from test_cart_station_nes_mappers import profile_for


def banks(count, size, seed=0):
    return b''.join(bytes((value + bank * 17 + seed) & 255 for value in range(256))
                    * (size // 256) for bank in range(count))


class ConflictCartridge:
    """Independent 32 KiB CPU/8 KiB PPU windows with wired-AND bus conflicts."""
    def __init__(self, mapper, prg, chr_data, corrupt_second=False):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.prg_bank = self.chr_bank = 0
        self.modes, self.writes = [], []
        self.full_reads = 0
        self.corrupt_second = corrupt_second

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def memory(self, command, address, size):
        if command == 0x11:
            offset = self.prg_bank * 32768 + address - 0x8000
            result = self.prg[offset:offset + size]
            if size == 32768:
                self.full_reads += 1
                if self.corrupt_second and self.full_reads > len(self.prg) // 32768:
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or self.mapper == 7:
            raise AssertionError('Unexpected bus read')
        offset = self.chr_bank * 8192 + address
        return self.chr[offset:offset + size]

    def cpu_register_write(self, address, value, dummy=True):
        if dummy or not 0x8000 <= address <= 0xffff:
            raise AssertionError('Unexpected register write')
        self.writes.append((address, value))
        # Model contention with the currently mapped PRG byte, not the profile.
        effective = value & self.prg[self.prg_bank * 32768 + address - 0x8000]
        if self.mapper == 7:
            self.prg_bank = effective % (len(self.prg) // 32768)
        elif self.mapper == 11:
            self.prg_bank = (effective & 3) % (len(self.prg) // 32768)
            self.chr_bank = (effective >> 4) % (len(self.chr) // 8192)
        elif self.mapper == 66:
            self.prg_bank = (effective >> 4) % (len(self.prg) // 32768)
            self.chr_bank = (effective & 15) % (len(self.chr) // 8192)

    nes = property(lambda self: self)
    write = NesRegisters.write


class SimpleMapperTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_axrom_known_submappers_keep_bus_conflict_search_and_db_header(self, sleep):
        prg = banks(8, 32768)
        for submapper in (0, 1, 2):
            profile = profile_for(7, prg, b'', submapper=submapper)
            bus = ConflictCartridge(7, prg, b'')
            self.assertEqual(dump_profile(bus, profile), profile.header + prg)
            self.assertEqual(bus.full_reads, 16)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_pass_roms_with_real_bus_conflict_model(self, sleep):
        for mapper, prg_banks, chr_banks in (
                (7, 1, 0), (7, 8, 0), (11, 1, 1), (11, 4, 16),
                (66, 1, 1), (66, 4, 4), (66, 16, 16)):
            with self.subTest(mapper=mapper, prg_banks=prg_banks, chr_banks=chr_banks):
                prg, chr_data = banks(prg_banks, 32768), banks(chr_banks, 8192, 43)
                profile = profile_for(mapper, prg, chr_data)
                bus = ConflictCartridge(mapper, prg, chr_data)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *p: progress.append(p)),
                                 profile.header + prg + chr_data)
                self.assertEqual(bus.full_reads, 2 * prg_banks)
                self.assertEqual(bus.modes, [1, 0])
                self.assertEqual(progress[-1][1], 2 * len(prg + chr_data))
                self.assertTrue(bus.writes)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_same_geometry_candidates_are_decided_by_full_chr_hash(self, sleep):
        prg, chr_data = banks(4, 32768), banks(4, 8192, 43)
        for mapper in (11, 66):
            profile = profile_for(mapper, prg, chr_data)
            other = replace(profile_for(mapper, prg, banks(4, 8192, 44)), id='other')
            bus = ConflictCartridge(mapper, prg, chr_data)
            matched, result = dump_candidates(bus, (other, profile))
            self.assertEqual(matched.id, profile.id)
            self.assertEqual(result, profile.header + prg + chr_data)
            self.assertEqual(bus.full_reads, 8)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_corrupt_second_pass_returns_no_rom_and_restores_mode(self, sleep):
        for mapper in (7, 11, 66):
            prg = banks(4, 32768)
            chr_data = b'' if mapper == 7 else banks(4, 8192, 43)
            profile = profile_for(mapper, prg, chr_data)
            bus = ConflictCartridge(mapper, prg, chr_data, corrupt_second=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_is_not_retried(self, sleep):
        for mapper in (7, 11, 66):
            prg, chr_data = banks(4, 32768), b'' if mapper == 7 else banks(4, 8192)
            bus = ConflictCartridge(mapper, prg, chr_data)
            bus.memory = Mock(side_effect=ProtocolError('transport stopped'))
            with self.assertRaisesRegex(ProtocolError, 'transport stopped'):
                dump_profile(bus, profile_for(mapper, prg, chr_data))
            self.assertEqual(bus.memory.call_count, 1)
            self.assertEqual(bus.modes, [1, 0])

    def test_subvariants_and_unaddressable_geometry_do_not_touch_device(self):
        for mapper in (7, 11, 66):
            prg, chr_data = banks(4, 32768), b'' if mapper == 7 else banks(4, 8192)
            profile = profile_for(mapper, prg, chr_data)
            changes = [{'submapper': 3 if mapper == 7 else 1}, {'prg_size': 16384}]
            if mapper == 7:
                changes += [{'chr_size': 8192}, {'prg_size': 8388608 + 32768}]
            else:
                changes += [{'chr_size': 0}, {'chr_size': 131072 + 8192},
                            {'prg_size': (131072 if mapper == 11 else 524288) + 32768}]
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_embedded_profiles_use_shared_supported_route(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper in (7, 66):
            applicable = [p for p in db.profiles if p.mapper == mapper and p.support() is None]
            self.assertTrue(applicable, mapper)
        # The supplied partial-hash catalog has no Mapper 11 entries. Do not
        # pretend that adding a reader also adds missing identification data.
        self.assertFalse([p for p in db.profiles if p.mapper == 11])
        prg, chr_data = banks(4, 32768), banks(4, 8192)
        profile = profile_for(11, prg, chr_data)
        self.assertIsNone(Database((profile,)).by_id[profile.id].support())


if __name__ == '__main__':
    unittest.main()
