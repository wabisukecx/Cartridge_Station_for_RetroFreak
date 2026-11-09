from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_nes_mappers import MapperReader, RomMismatch
from cart_station_storage import save_verified_rom
from test_cart_station_nes_mappers import profile_for, Cartridge as SerialCartridge
from test_cart_station_simple_mappers import banks


class Cartridge:
    """Independent 32/16 KiB windows, Datach outer lines and karaoke selection."""
    def __init__(self, mapper, prg, chr_data, fault=None):
        self.mapper, self.prg, self.chr, self.fault = mapper, prg, chr_data, fault
        self.unit = 32768 if mapper in (96, 148) else 16384
        self.bank = self.chr_bank = 0
        self.outer = [0, 0, 0, 0]
        self.prg_reads = self.chr_reads = 0
        self.modes, self.writes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_offset(self, address):
        if self.unit == 32768:
            return self.bank * self.unit + address - 0x8000
        number = self.bank if address < 0xc000 else len(self.prg) // 16384 - 1
        return number * 16384 + (address & 16383)

    def memory(self, command, address, size):
        if command == 0x11:
            offset = self.cpu_offset(address)
            result = self.prg[offset:offset + size]
            if size == self.unit:
                if address != 0x8000:
                    raise AssertionError('Wrong PRG window')
                self.prg_reads += 1
                if self.fault == 'second' and self.prg_reads > len(self.prg) // self.unit:
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or self.mapper != 148 or address != 0 or size != 8192:
            raise AssertionError('Unexpected CHR-RAM/PPU read')
        self.chr_reads += 1
        return self.chr[self.chr_bank * 8192:(self.chr_bank + 1) * 8192]

    def cpu_register_write(self, address, value, dummy=True):
        self.writes.append((address, value, dummy))
        if self.mapper in (96, 188):
            if dummy or not 0x8000 <= address <= 0xffff:
                raise AssertionError('Expected conflict-search write')
            effective = value & self.prg[self.cpu_offset(address)]
            if self.mapper == 96:
                self.bank = effective & 3
            else:
                external = not effective & 16
                if self.fault == 'internal_only':
                    external = False
                self.bank = ((effective & 7) + (8 if external else 0)) % (len(self.prg) // 16384)
        elif self.mapper == 148:
            if address != 0x8000 or not dummy:
                raise AssertionError('Wrong Mapper148 register')
            self.bank, self.chr_bank = (value >> 3) & 1, value & 7
        elif self.mapper == 157:
            if not dummy:
                raise AssertionError('Datach requires dummy reads')
            if 0x8000 <= address <= 0x8003:
                if self.fault != 'outer':
                    self.outer[address - 0x8000] = value
            elif address == 0x8008:
                if len(set(self.outer)) != 1:
                    raise AssertionError('All four outer-bank lines must be programmed')
                self.bank = (self.outer[0] * 16 + (value & 15)) % (len(self.prg) // 16384)
            else:
                raise AssertionError('Unexpected Datach/EEPROM/save register')
        else:
            raise AssertionError('Unexpected mapper')
        if dummy:
            self.memory(0x11, 0x8000, 8)

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, fault=None):
    unit = 32768 if mapper in (96, 148) else 16384
    count = {96: 4, 148: 2, 157: 32, 188: 16}[mapper]
    prg = banks(count, unit)
    chr_data = banks(8, 8192, 37) if mapper == 148 else b''
    return profile_for(mapper, prg, chr_data), Cartridge(mapper, prg, chr_data, fault), prg + chr_data


class DatachMmc1aTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_rom_reads_cover_direct_conflict_outer_and_karaoke_banks(self, sleep):
        for mapper in (96, 148, 157, 188):
            profile, bus, raw = fixture(mapper)
            progress = []
            with self.subTest(mapper=mapper):
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)),
                                 profile.header + raw)
                self.assertEqual(bus.prg_reads, 2 * profile.prg_size // bus.unit)
                self.assertEqual(bus.chr_reads, 16 if mapper == 148 else 0)
                self.assertEqual(bus.modes, [1, 0])
                self.assertEqual(progress[-1][1], 2 * len(raw))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mmc1a_serial_model_full_rom_and_fixed_last_bank(self, sleep):
        for size in (131072, 524288):
            prg, chr_data = banks(size // 16384, 16384), banks(32, 4096, 37)
            profile = profile_for(155, prg, chr_data)
            bus = SerialCartridge(1, prg, chr_data)
            self.assertEqual(dump_profile(bus, profile), profile.header + prg + chr_data)
            self.assertEqual(bus.writes[:8], [(0x8000, 128 + n, True) for n in range(8)])
            reader = MapperReader(bus, profile)
            before = len(bus.writes)
            self.assertEqual(reader.prg_bank(size // 16384 - 1, 16384), prg[-16384:])
            self.assertEqual(len(bus.writes), before)
            self.assertTrue(all(a >= 0x8000 for a, v, d in bus.writes))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mmc1a_does_not_guess_mmc1_special_variants(self, sleep):
        profile = profile_for(155, banks(4, 16384), banks(1, 8192))
        bus = Mock()
        bus.status.return_value = 3
        with patch('cart_station_nes_mappers.MapperReader') as factory:
            factory.return_value.read.side_effect = RomMismatch('wrong ROM')
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(factory.call_args.args[2:], (4096, 0))
        self.assertEqual(bus.mode.call_count, 2)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_datach_outer_bank_bit_is_required_above_bank15(self, sleep):
        profile, bus, _ = fixture(157, 'outer')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.writes[16 * 5:16 * 5 + 5],
                         [(a, 1, True) for a in range(0x8000, 0x8004)] + [(0x8008, 16, True)])
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_karaoke_internal_external_selection_is_hash_checked(self, sleep):
        profile, bus, raw = fixture(188)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual([v for a, v, d in bus.writes[:16]], list(range(16, 24)) + list(range(8)))
        profile, bus, _ = fixture(188, 'internal_only')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)

    def test_unknown_submapper_and_geometry_stop_before_io(self):
        for mapper in (96, 148, 155, 157, 188):
            if mapper == 155:
                profile = profile_for(155, banks(8, 16384), banks(8, 4096))
            else:
                profile, _, _ = fixture(mapper)
            changes = [dict(submapper=1), dict(prg_size=24576), dict(chr_size=1)]
            if mapper in (96, 157, 188):
                changes.append(dict(chr_size=8192))
            changes.append(dict(prg_size={96: 163840, 148: 98304, 155: 540672,
                                          157: 4210688, 188: 278528}[mapper]))
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_stops_without_returning_rom(self, sleep):
        for mapper in (96, 148, 157, 188):
            profile, bus, _ = fixture(mapper, 'second')
            with self.subTest(mapper=mapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_restores_mode_and_does_not_retry(self, sleep):
        profile, bus, _ = fixture(157)
        bus.memory = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.memory.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_bundled_database_adds_17_profiles(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((96, 2), (148, 3), (155, 2), (157, 7), (188, 3)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_hash_selection_and_saved_db_header(self, sleep):
        profile, bus, raw = fixture(148)
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
