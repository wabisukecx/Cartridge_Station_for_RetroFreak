from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_mappers import MapperReader
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station_nes_mappers import profile_for
from test_cart_station_cnrom_cp import synthetic_crc
from test_cart_station_simple_mappers import banks


class Cs1Cartridge:
    """Fixed PRG and a level-sensitive two-bit CS latch, not a pair matcher.

    Submapper 5 selects CHR only when (written value AND PRG byte) & 3 == 1.
    All test data is synthetic; CRC suffix construction does not use real ROM.
    """
    def __init__(self, prg, chr_data, fault=None):
        self.prg, self.chr, self.fault = prg, chr_data, fault
        self.latch = 0
        self.prg_reads = self.chr_reads = 0
        self.writes, self.effective, self.modes = [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def memory(self, command, address, size):
        if command == 0x11:
            offset = (address - 0x8000) % len(self.prg)
            result = self.prg[offset:offset + size]
            if size == 16384:
                self.prg_reads += 1
                if self.fault == 'prg' or (self.fault == 'second' and self.prg_reads > 2):
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0 or size != 8192:
            raise AssertionError('Wrong CHR window')
        self.chr_reads += 1
        if self.latch != 1:
            return bytes([255]) * size
        if self.fault == 'chr':
            return bytes((self.chr[0] ^ 1,)) + self.chr[1:]
        return self.chr

    def cpu_register_write(self, address, value, dummy=True):
        if dummy or not 0x8000 <= address <= 0xffff:
            raise AssertionError('Must use conflict-search writes')
        self.writes.append((address, value))
        if self.fault == 'transport' and value == 17:
            raise OSError('transport stopped')
        effective = value & self.prg[(address - 0x8000) % len(self.prg)]
        if self.fault == 'conflict':
            effective &= 254
        self.effective.append(effective)
        self.latch = effective & 3

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(fault=None):
    prg = synthetic_crc(banks(2, 16384), '49FF2E0B')
    chr_data = banks(1, 8192, 43)
    profile = replace(profile_for(185, prg, chr_data, 5), prg_crc32='49FF2E0B')
    return profile, Cs1Cartridge(prg, chr_data, fault), prg + chr_data


class CnromPrg1Tests(unittest.TestCase):
    def test_bundled_revision_and_level_sensitive_chip_select(self):
        profiles = [p for p in Database.load(data('nes_profiles.reference.json')).profiles
                    if p.prg_crc32 == '49FF2E0B']
        self.assertEqual(len(profiles), 1)
        p = profiles[0]
        self.assertEqual((p.mapper, p.submapper, p.prg_size, p.chr_size), (185, 5, 32768, 8192))
        self.assertIn('Rev 1', p.title)
        self.assertIsNone(p.support())
        _, bus, _ = fixture()
        for value in (0, 1, 2, 3, 17, 33):
            address = 0x8000 + bus.prg.index(value)
            bus.cpu_register_write(address, value, dummy=False)
            self.assertEqual(bus.memory(0x13, 0, 8192), bus.chr if value & 3 == 1 else b'\xff' * 8192)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_fresh_reads_use_documented_unlock_with_real_crc_and_sha1(self, sleep):
        profile, bus, raw = fixture()
        self.assertIsNone(profile.support())
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual(bus.prg_reads, 4)
        self.assertEqual(bus.chr_reads, 2)
        self.assertEqual([v for a, v in bus.writes], [0, 17, 0, 17])
        self.assertEqual(bus.effective, [0, 17, 0, 17])
        self.assertEqual(bus.modes, [1, 0])

    def test_only_reviewed_submapper_geometry_and_known_crc_are_allowed(self):
        profile, _, _ = fixture()
        for changes in (dict(submapper=4), dict(submapper=6), dict(submapper=7),
                        dict(prg_size=16384), dict(chr_size=16384),
                        dict(prg_crc32='DEADBEEF'), dict(prg_crc32=None)):
            bus = Mock()
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, **changes))
            self.assertFalse(bus.mock_calls)
        bus = Mock()
        with self.assertRaises(ProtocolError):
            MapperReader(bus, profile).chr_rom_bank(0, 8192)
        self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_wrong_fixed_prg_blocks_every_unlock(self, sleep):
        profile, bus, _ = fixture('prg')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.writes, [])
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_correct_sha1_but_different_actual_crc_blocks_every_unlock(self, sleep):
        profile, bus, _ = fixture()
        # Keep the alternate PRG's real SHA1 and all its other hashes; lie only
        # about CRC metadata, so rejection must come from the runtime CRC gate.
        alternate = synthetic_crc(bus.prg, 'DEADBEEF')
        profile = replace(profile_for(185, alternate, bus.chr, 5), prg_crc32='49FF2E0B')
        bus.prg = alternate
        with self.assertRaisesRegex(ProtocolError, 'CRC32'):
            dump_profile(bus, profile)
        self.assertEqual(bus.writes, [])
        self.assertEqual(bus.chr_reads, 0)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_prg_damage_stops_before_second_unlock(self, sleep):
        profile, bus, _ = fixture('second')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual([v for a, v in bus.writes], [0, 17])
        self.assertEqual(bus.chr_reads, 1)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_chr_damage_cannot_produce_rom(self, sleep):
        profile, bus, _ = fixture('chr')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 1)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_lost_chip_select_bit_fails_full_chr_hash(self, sleep):
        profile, bus, _ = fixture('conflict')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.effective, [0, 16])
        self.assertEqual(bus.latch, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_never_tries_other_unlock_values(self, sleep):
        profile, bus, _ = fixture('transport')
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual([v for a, v in bus.writes], [0, 17])
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidates_saved_header_and_all_embedded_applicability(self, sleep):
        profile, bus, raw = fixture()
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        self.assertEqual(payload[8] >> 4, 5)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
        profiles = Database.load(data('nes_profiles.reference.json')).profiles
        self.assertEqual(len(profiles), 2148)
        self.assertTrue(all(p.support() is None for p in profiles))
