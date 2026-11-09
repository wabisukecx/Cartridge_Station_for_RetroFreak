from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter
from cart_station_nes_mappers import MapperReader
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station import Wire, memory
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class Cartridge:
    """Discrete 0x6000 register models with fixed UxROM upper window for 93."""
    def __init__(self, mapper, prg, chr_data, fault=None):
        self.mapper, self.prg, self.chr, self.fault = mapper, prg, chr_data, fault
        self.unit = 32768 if mapper in (86, 140) else 16384
        self.chr_unit = 4096 if mapper == 184 else 8192
        self.prg_bank = self.chr_bank = self.lower = 0
        self.prg_reads = self.chr_reads = 0
        self.writes, self.modes, self.searches = [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def offset(self, address):
        if self.mapper == 184:
            return (address - 0x8000) % len(self.prg)
        if self.mapper == 93:
            bank = self.prg_bank if address < 0xc000 else len(self.prg) // 16384 - 1
            return bank * 16384 + (address & 16383)
        return self.prg_bank * 32768 + address - 0x8000

    def memory(self, command, address, size):
        if command == 0x11:
            result = self.prg[self.offset(address):self.offset(address) + size]
            if size == 1024:
                self.searches.append(address)
            if size == self.unit:
                self.prg_reads += 1
                if self.fault == 'second' and self.prg_reads > len(self.prg) // self.unit:
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or self.mapper == 93 or address != 0 or size != self.chr_unit:
            raise AssertionError('Wrong CHR window or unexpected CHR-RAM read')
        self.chr_reads += 1
        offset = self.chr_bank * self.chr_unit
        return self.chr[offset:offset + size]

    def discrete_rom_register(self, mapper, value):
        if mapper != self.mapper:
            raise AssertionError('Wrong mapper for the lower register')
        self.writes.append((0x6000, value, True))
        if mapper == 86:
            self.prg_bank = (value >> 4) & 3
            self.chr_bank = (value & 3) | (0 if self.fault == 'chr_bit' else (value >> 4) & 4)
        elif mapper == 140:
            self.prg_bank, self.chr_bank = value >> 4, value & 15
        elif mapper == 184:
            self.chr_bank = value & 7
        elif mapper == 93:
            if self.fault != 'lower':
                self.lower = value
        else:
            raise AssertionError('Unexpected mapper')
        self.memory(0x11, 0x8000, 8)

    def cpu_register_write(self, address, value, dummy=True):
        if self.mapper != 93 or not 0xd000 <= address <= 0xffff or dummy:
            raise AssertionError('Only Mapper93 upper search writes are expected')
        effective = value & self.prg[self.offset(address)]
        self.writes.append((address, value, False))
        bank = effective >> 4
        if self.lower == bank:
            self.prg_bank = bank

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, fault=None, fixed_size=32768):
    unit = 32768 if mapper in (86, 140) else 16384
    size = {86: 131072, 93: 131072, 140: 524288, 184: fixed_size}[mapper]
    prg = banks(size // unit, unit)
    chr_unit = 4096 if mapper == 184 else 8192
    count = {86: 8, 93: 0, 140: 16, 184: 8}[mapper]
    chr_data = banks(count, chr_unit, 37)
    return profile_for(mapper, prg, chr_data), Cartridge(mapper, prg, chr_data, fault), prg + chr_data


class DiscreteLowerTests(unittest.TestCase):
    def test_wire_uses_only_6000_command12_and_reference_dummy_read(self):
        for mapper, value in ((86, 48), (86, 67), (93, 7), (140, 240), (140, 15), (184, 7)):
            wire = Wire(bytes(16) + memory(b'P' * 8), fragment=3)
            Adapter(wire).nes.discrete_rom_register(mapper, value)
            self.assertEqual(wire.writes, [packet(0x12, 8, 1, 0x6000, 1), bytes((value,)),
                                          packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4)])
            self.assertFalse(wire.replies)

    def test_lower_register_guard_rejects_other_bits_and_mapper_types(self):
        for mapper, value in ((86, 4), (86, 128), (86, 0x50), (93, 8), (140, 17),
                              (184, 8), (184, 255), (80, 0), (86.0, 0), (86, True),
                              (93, -1), (140, 256)):
            wire = Wire(b'')
            with self.subTest(mapper=mapper, value=value), self.assertRaises(ValueError):
                Adapter(wire).nes.discrete_rom_register(mapper, value)
            self.assertFalse(wire.writes)
        wire = Wire(b'')
        with self.assertRaises(ValueError):
            Adapter(wire).nes.cpu_register_write(0x6000, 0)
        self.assertFalse(wire.writes)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_complete_two_pass_reads_for_all_four_mappers(self, sleep):
        for mapper, fixed_size in ((86, 32768), (93, 32768), (140, 32768),
                                   (184, 16384), (184, 32768)):
            profile, bus, raw = fixture(mapper, fixed_size=fixed_size)
            progress = []
            self.assertIsNone(profile.support())
            self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)), profile.header + raw)
            self.assertEqual(bus.prg_reads, 2 * profile.prg_size // bus.unit)
            self.assertEqual(bus.chr_reads, 2 * profile.chr_size // bus.chr_unit)
            self.assertEqual(bus.modes, [1, 0])
            self.assertEqual(progress[-1][1], 2 * len(raw))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper86_chr_bit2_moves_to_register_bit6(self, sleep):
        profile, bus, raw = fixture(86)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual([v for a, v, d in bus.writes[4:12]], [0, 1, 2, 3, 64, 65, 66, 67])
        profile, bus, _ = fixture(86, 'chr_bit')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper93_low_write_and_upper_fixed_search_are_both_required(self, sleep):
        profile, bus, raw = fixture(93)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertTrue(all(0xd000 <= a <= 0xffff for a in bus.searches))
        self.assertEqual([v for a, v, d in bus.writes if a == 0x6000], list(range(8)) * 2)
        self.assertTrue(all(not d and 0xd000 <= a <= 0xffff for a, v, d in bus.writes if a != 0x6000))
        profile, bus, _ = fixture(93, 'lower')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_bad_fixed_prg_never_reaches_mapper184_register(self, sleep):
        profile, bus, _ = fixture(184)
        bus.prg = bytes((bus.prg[0] ^ 1,)) + bus.prg[1:]
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertFalse(bus.writes)
        self.assertFalse(bus.chr_reads)
        self.assertEqual(bus.modes, [1, 0])
        untouched = Mock()
        with self.assertRaisesRegex(ProtocolError, 'verified fixed PRG'):
            MapperReader(untouched, profile).chr_rom_bank(0, 4096)
        self.assertFalse(untouched.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_rechecks_fixed_prg_before_more_chr_writes(self, sleep):
        profile, bus, _ = fixture(184, 'second')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 8)
        self.assertEqual(len(bus.writes), 8)
        self.assertEqual(bus.modes, [1, 0])

    def test_unknown_submapper_and_unrepresentable_geometry_stop_before_io(self):
        for mapper in (86, 93, 140, 184):
            profile, _, _ = fixture(mapper)
            changes = [dict(submapper=1), dict(prg_size=24576), dict(chr_size=1)]
            changes.append(dict(prg_size={86: 163840, 93: 147456, 140: 557056, 184: 49152}[mapper]))
            changes.append(dict(chr_size={86: 73728, 93: 8192, 140: 139264, 184: 36864}[mapper]))
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_never_returns_rom(self, sleep):
        for mapper in (86, 93, 140):
            profile, bus, _ = fixture(mapper, 'second')
            with self.subTest(mapper=mapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_restores_mode_without_retry(self, sleep):
        profile, bus, _ = fixture(86)
        bus.discrete_rom_register = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.discrete_rom_register.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_bundled_database_adds_twelve_profiles(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((86, 4), (93, 2), (140, 3), (184, 3)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_selection_and_saved_header_full_hashes(self, sleep):
        profile, bus, raw = fixture(140)
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
