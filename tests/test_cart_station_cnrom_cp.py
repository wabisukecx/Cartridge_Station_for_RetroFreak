from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
import zlib
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, Profile, dump_profile, dump_candidates, verified_rom_file
from cart_station_nes_mappers import MapperReader
from cart_station_storage import save_verified_rom
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


# Expected physical sequences kept independent of the implementation's table.
PAIRS = {'2A629F7D': ((240, 15),), '725D5CC4': ((0, 51),),
         'AA755715': ((0, 17),), '87A3F91E': ((32, 34),),
         'DAADEBFA': ((32, 34),), '81A743F5': ((32, 34),),
         '73DE64AD': ((0, 42),), 'B36457C7': ((19, 33),),
         'A03C98A7': ((33, 32),), '9CF17FAB': ((16, 18), (0, 19))}


def synthetic_crc(payload, expected):
    """Solve four synthetic suffix bytes over GF(2); contains no real ROM data."""
    prefix = payload[:-4]
    base = zlib.crc32(prefix + bytes(4))
    basis = {}
    for bit in range(32):
        vector = zlib.crc32(prefix + (1 << bit).to_bytes(4, 'little')) ^ base
        coefficient = 1 << bit
        while vector:
            pivot = vector.bit_length() - 1
            if pivot not in basis:
                basis[pivot] = vector, coefficient
                break
            other, code = basis[pivot]
            vector ^= other
            coefficient ^= code
    target, answer = int(expected, 16) ^ base, 0
    while target:
        vector, coefficient = basis[target.bit_length() - 1]
        target ^= vector
        answer ^= coefficient
    result = prefix + answer.to_bytes(4, 'little')
    assert '%08X' % zlib.crc32(result) == expected
    return result


class ProtectedCartridge:
    """Fixed PRG, actual wired-AND writes and locked 8 KiB CHR windows."""
    def __init__(self, prg, chr_data, sequences, fault=None):
        self.prg, self.chr, self.sequences, self.fault = prg, chr_data, sequences, fault
        self.unlocked, self.bank = False, 0
        self.previous = []
        self.prg_reads = self.chr_reads = 0
        self.writes, self.modes = [], []

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
                if (self.prg_reads - 1) % (len(self.prg) // 16384) == 0:
                    self.unlocked, self.previous = False, []
                if self.fault == 'second' and self.prg_reads > len(self.prg) // 16384:
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0 or size != 8192:
            raise AssertionError('Wrong protected CHR window')
        self.chr_reads += 1
        if not self.unlocked:
            return b'\xff' * size
        return self.chr[self.bank * 8192:(self.bank + 1) * 8192]

    def cpu_register_write(self, address, value, dummy=True):
        if dummy or not 0x8000 <= address <= 0xffff:
            raise AssertionError('Unlock must use conflict-search writes')
        self.writes.append((address, value))
        effective = value & self.memory(0x11, address, 1)[0]
        self.previous.append(effective)
        if self.fault != 'unlock':
            for bank, pair in enumerate(self.sequences):
                if tuple(self.previous[-2:]) == pair:
                    self.unlocked, self.bank = True, bank

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(crc='2A629F7D', size=32768, fault=None):
    prg = synthetic_crc(banks(size // 16384, 16384), crc)
    chr_data = banks(len(PAIRS[crc]), 8192, 43)
    profile = replace(profile_for(185, prg, chr_data, 7), prg_crc32=crc)
    return profile, ProtectedCartridge(prg, chr_data, PAIRS[crc], fault), prg + chr_data


class CnromCpTests(unittest.TestCase):
    def test_crc_metadata_is_optional_and_strictly_validated(self):
        profile, _, _ = fixture()
        record = asdict(profile)
        record.update(header=profile.header.hex(), probes=dict(profile.probes))
        record['prg_crc32'] = '2a629f7d'
        self.assertEqual(Profile.parse(record).prg_crc32, '2A629F7D')
        for bad in ('123', 'x' * 8, 123, True):
            with self.assertRaises(ValueError):
                Profile.parse(dict(record, prg_crc32=bad))
        record.pop('prg_crc32')
        old = Profile.parse(record)
        self.assertIsNone(old.prg_crc32)
        self.assertIsNotNone(old.support())
        ordinary = profile_for(13, banks(2, 16384), b'')
        self.assertIsNone(ordinary.support())

    @patch('cart_station_nes_mappers.time.sleep')
    def test_all_reference_crc_sequences_complete_two_passes(self, sleep):
        for crc in PAIRS:
            for size in (16384, 32768):
                profile, bus, raw = fixture(crc, size)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)), profile.header + raw)
                self.assertEqual(bus.prg_reads, 2 * size // 16384)
                self.assertEqual(bus.chr_reads, len(PAIRS[crc]) * 2)
                self.assertEqual(bus.modes, [1, 0])
                self.assertEqual(progress[-1][1], 2 * len(raw))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_wrong_fixed_prg_stops_before_any_unlock_write(self, sleep):
        profile, bus, _ = fixture()
        bus.prg = bytes((bus.prg[0] ^ 1,)) + bus.prg[1:]
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertFalse(bus.writes)
        self.assertFalse(bus.chr_reads)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_correct_sha1_but_wrong_crc_metadata_stops_before_writes(self, sleep):
        profile, bus, _ = fixture()
        with self.assertRaisesRegex(ProtocolError, 'CRC32'):
            dump_profile(bus, replace(profile, prg_crc32='725D5CC4'))
        self.assertFalse(bus.writes)
        self.assertFalse(bus.chr_reads)

    def test_missing_unknown_crc_or_submapper_stops_before_io(self):
        profile, _, _ = fixture()
        for change in (dict(prg_crc32=None), dict(prg_crc32='DEADBEEF'), dict(submapper=0),
                       dict(submapper=8), dict(prg_size=49152), dict(chr_size=16384)):
            bus = Mock()
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, **change))
            self.assertFalse(bus.mock_calls)

    def test_chr_primitive_requires_verified_fixed_prg(self):
        profile, _, _ = fixture()
        bus = Mock()
        with self.assertRaisesRegex(ProtocolError, 'verified fixed PRG'):
            MapperReader(bus, profile).chr_rom_bank(0, 8192)
        self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_prg_damage_stops_further_unlocks(self, sleep):
        profile, bus, _ = fixture(fault='second')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(len(bus.writes), 2)
        self.assertEqual(bus.chr_reads, 1)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_unlock_failure_cannot_pass_chr_hash(self, sleep):
        profile, bus, _ = fixture(fault='unlock')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 1)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_reference_second_bank_assignment_is_preserved_literally(self, sleep):
        profile, bus, raw = fixture('9CF17FAB')
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual([value for address, value in bus.writes], [16, 18, 0, 19] * 2)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_exits_without_another_unlock_variant(self, sleep):
        profile, bus, _ = fixture()
        bus.cpu_register_write = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.cpu_register_write.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_bundled_profiles_include_documented_prg1_unlock(self):
        profiles = [p for p in Database.load(data('nes_profiles.reference.json')).profiles if p.mapper == 185]
        self.assertEqual(len(profiles), 10)
        self.assertTrue(all(p.prg_crc32 for p in profiles))
        self.assertEqual(sum(p.support() is None for p in profiles), 10)
        self.assertEqual([p.prg_crc32 for p in profiles if p.support() is not None], [])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidates_are_filtered_by_actual_crc_before_unlock(self, sleep):
        profile, bus, raw = fixture()
        other = replace(profile, id='other', prg_crc32='725D5CC4')
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        self.assertEqual([v for a, v in bus.writes], [240, 15] * 2)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_saved_file_keeps_db_header_and_all_hashes(self, sleep):
        profile, bus, _ = fixture()
        payload = dump_profile(bus, profile)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
            self.assertEqual(path.read_bytes(), payload)
