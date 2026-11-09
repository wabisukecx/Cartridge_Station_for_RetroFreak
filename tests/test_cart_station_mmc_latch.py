from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_candidates, dump_profile
from test_cart_station_nes_mappers import profile_for


def rom_banks(count, unit, seed=0):
    return b''.join(bytes((bank, seed)) * (unit // 2) for bank in range(count))


class LatchCartridge:
    """Independent ROM windows with a latch that changes during PPU reads."""
    def __init__(self, mapper, prg, chr_data, initial_fe=False,
                 ignore_fe=False, corrupt_second=False):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.prg_unit = 8192 if mapper == 9 else 16384
        self.prg_bank = 0
        self.fd_bank, self.fe_bank = 0, 0
        self.fe = initial_fe
        self.ignore_fe = ignore_fe
        self.corrupt_second = corrupt_second
        self.prg_reads, self.chr_reads, self.latch_changes = 0, 0, 0
        self.modes, self.writes, self.dummy_reads = [], [], 0

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy or address not in (0xa000, 0xb000, 0xc000):
            raise AssertionError('Unexpected ROM register operation')
        self.writes.append((address, value))
        if address == 0xa000:
            self.prg_bank = value % (len(self.prg) // self.prg_unit)
        elif address == 0xb000:
            self.fd_bank = value % (len(self.chr) // 4096)
        elif not self.ignore_fe:
            self.fe_bank = value % (len(self.chr) // 4096)
        # The adapter performs this short CPU read after every register write.
        self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if address != 0x8000:
                raise AssertionError('Unexpected CPU window')
            offset = self.prg_bank * self.prg_unit
            if size == 8:
                self.dummy_reads += 1
            else:
                self.prg_reads += 1
            return self.prg[offset:offset + size]
        if command != 0x13 or address != 0 or size != 4096:
            raise AssertionError('Unexpected PPU window')
        self.chr_reads += 1
        result = bytearray()
        for position in range(size):
            bank = self.fe_bank if self.fe else self.fd_bank
            result.append(self.chr[bank * 4096 + position])
            # Both possible latched banks are encountered in the read window.
            if position in (0xfd8, 0xfe8):
                self.fe = position == 0xfe8
                self.latch_changes += 1
        if self.corrupt_second and self.chr_reads > len(self.chr) // 4096:
            result[0] ^= 1
        return bytes(result)

    nes = property(lambda self: self)
    write = NesRegisters.write


class MmcLatchTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_pass_rom_and_latch_switches_from_either_initial_state(self, sleep):
        for mapper, prg_size, chr_size in ((9, 32768, 8192), (9, 131072, 131072),
                                           (10, 131072, 65536), (10, 262144, 131072)):
            unit = 8192 if mapper == 9 else 16384
            prg = rom_banks(prg_size // unit, unit, 17)
            chr_data = rom_banks(chr_size // 4096, 4096, 43)
            profile = profile_for(mapper, prg, chr_data)
            for initial in (False, True):
                with self.subTest(mapper=mapper, size=prg_size, initial_fe=initial):
                    bus = LatchCartridge(mapper, prg, chr_data, initial_fe=initial)
                    progress = []
                    self.assertIsNone(profile.support())
                    self.assertEqual(dump_profile(bus, profile, progress=lambda *p: progress.append(p)),
                                     profile.header + prg + chr_data)
                    self.assertEqual(bus.prg_reads, 2 * prg_size // unit)
                    self.assertEqual(bus.chr_reads, 2 * chr_size // 4096)
                    self.assertEqual(bus.latch_changes, 2 * bus.chr_reads)
                    self.assertEqual(bus.dummy_reads, len(bus.writes))
                    self.assertEqual(bus.modes, [1, 0])
                    self.assertEqual(progress[-1][1], 2 * (prg_size + chr_size))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_latch_bank_write_fault_fails_complete_hash(self, sleep):
        for mapper in (9, 10):
            unit = 8192 if mapper == 9 else 16384
            prg, chr_data = rom_banks(16, unit), rom_banks(32, 4096)
            bus = LatchCartridge(mapper, prg, chr_data, initial_fe=True, ignore_fe=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile_for(mapper, prg, chr_data))
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_chr_corruption_returns_no_rom(self, sleep):
        for mapper in (9, 10):
            unit = 8192 if mapper == 9 else 16384
            prg, chr_data = rom_banks(16, unit), rom_banks(32, 4096)
            bus = LatchCartridge(mapper, prg, chr_data, corrupt_second=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile_for(mapper, prg, chr_data))
            self.assertEqual(bus.prg_reads, 32)
            self.assertEqual(bus.chr_reads, 64)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_wrong_prg_hash_never_starts_chr_register_writes(self, sleep):
        prg, chr_data = rom_banks(16, 8192), rom_banks(32, 4096)
        profile = profile_for(9, prg, chr_data)
        bus = LatchCartridge(9, rom_banks(16, 8192, 1), chr_data)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertTrue(all(address == 0xa000 for address, _ in bus.writes))
        self.assertEqual(bus.chr_reads, 0)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_same_geometry_candidates_use_complete_chr_and_raw_hash(self, sleep):
        for mapper in (9, 10):
            unit = 8192 if mapper == 9 else 16384
            prg, chr_data = rom_banks(16, unit), rom_banks(32, 4096)
            profile = profile_for(mapper, prg, chr_data)
            wrong = replace(profile_for(mapper, prg, rom_banks(32, 4096, 1)), id='wrong')
            bus = LatchCartridge(mapper, prg, chr_data)
            matched, result = dump_candidates(bus, (wrong, profile))
            self.assertEqual(matched.id, profile.id)
            self.assertEqual(result, profile.header + prg + chr_data)
            self.assertEqual(bus.prg_reads, 32)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_error_stops_and_restores_mode_without_retry(self, sleep):
        for mapper in (9, 10):
            unit = 8192 if mapper == 9 else 16384
            prg, chr_data = rom_banks(16, unit), rom_banks(32, 4096)
            bus = LatchCartridge(mapper, prg, chr_data)
            bus.memory = Mock(side_effect=ProtocolError('read failed'))
            with self.assertRaisesRegex(ProtocolError, 'read failed'):
                dump_profile(bus, profile_for(mapper, prg, chr_data))
            self.assertEqual(bus.memory.call_count, 1)
            self.assertEqual(bus.modes, [1, 0])

    def test_submapper_and_geometry_limits_reject_before_io(self):
        for mapper in (9, 10):
            unit = 8192 if mapper == 9 else 16384
            profile = profile_for(mapper, rom_banks(16, unit), rom_banks(32, 4096))
            for change in ({'submapper': 1}, {'prg_size': unit * 257},
                           {'prg_size': 16384}, {'chr_size': 0},
                           {'chr_size': 4096 * 257}):
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_embedded_mmc2_and_mmc4_profiles_are_applicable(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((9, 6), (10, 3)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))


if __name__ == '__main__':
    unittest.main()
