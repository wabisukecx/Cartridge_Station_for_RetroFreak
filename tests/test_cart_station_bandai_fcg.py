from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station import Wire, memory
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class FcgCartridge:
    """Independent lower/upper register decoding; no EEPROM or save accesses."""
    def __init__(self, mapper, submapper, prg, chr_data, fault=None):
        self.mapper, self.submapper = mapper, submapper
        self.prg, self.chr, self.fault = prg, chr_data, fault
        self.lower_decode = mapper == 16 and submapper == 4
        self.prg_bank = self.chr_bank = 0
        self.prg_reads = self.chr_reads = 0
        self.writes, self.modes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def register(self, address, value):
        self.writes.append((address, value))
        prg_address = 0x6008 if self.lower_decode else 0x8008
        chr_address = 0x6000 if self.lower_decode else 0x8000
        if address == prg_address and self.fault != 'prg_alias':
            self.prg_bank = value & 15
        elif address == chr_address:
            self.chr_bank = value & 127 if self.fault == 'chr_bit' else value
        self.memory(0x11, 0x8000, 8)

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy or address not in (0x8000, 0x8001, 0x8002, 0x8003, 0x8008):
            raise AssertionError('Unexpected Bandai high control/EEPROM address')
        self.register(address, value)

    def bandai_rom_register(self, mapper, address, value):
        if mapper != self.mapper or address not in (0x6000, 0x6008):
            raise AssertionError('Unexpected lower address or mapper')
        self.register(address, value)

    def memory(self, command, address, size):
        if command == 0x11:
            if address == 0x8000 and size == 8:
                return self.prg[:8]
            if address != 0x8000 or size != 16384:
                raise AssertionError('Wrong PRG window or save read')
            self.prg_reads += 1
            offset = self.prg_bank * 16384
            result = self.prg[offset:offset + size]
            if self.fault == 'second' and self.prg_reads > len(self.prg) // 16384:
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0 or size != 1024:
            raise AssertionError('Wrong CHR window or EEPROM operation')
        self.chr_reads += 1
        offset = self.chr_bank * 1024
        return self.chr[offset:offset + size]

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, submapper, fault=None):
    prg, chr_data = banks(16, 16384), banks(256, 1024, 43)
    return profile_for(mapper, prg, chr_data, submapper), FcgCartridge(mapper, submapper, prg, chr_data, fault), prg + chr_data


class BandaiFcgTests(unittest.TestCase):
    def test_wire_lower_aliases_use_command12_and_dummy_read(self):
        for mapper in (16, 159):
            for address, value in ((0x6008, 15), (0x6000, 255)):
                wire = Wire(bytes(16) + memory(b'P' * 8), fragment=3)
                Adapter(wire).nes.bandai_rom_register(mapper, address, value)
                self.assertEqual(wire.writes, [packet(0x12, 8, 1, address, 1), bytes((value,)),
                                              packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4)])
                self.assertFalse(wire.replies)

    def test_guard_blocks_sram_mapper_eeprom_register_and_invalid_values(self):
        cases = [(m, a, 0) for m in (16, 159) for a in (0x6001, 0x600d, 0x7fff, 0x800d)]
        cases += [(153, 0x6008, 0), (157, 0x6008, 0), (16, 0x6008, 16),
                  (159, 0x6000, 256), (16.0, 0x6000, 0), (16, True, 0),
                  (16, 0x6000, True), (16, 0x6000, -1)]
        for mapper, address, value in cases:
            wire = Wire(b'')
            with self.subTest(mapper=mapper, address=address, value=value), self.assertRaises(ValueError):
                Adapter(wire).nes.bandai_rom_register(mapper, address, value)
            self.assertFalse(wire.writes)
        wire = Wire(b'')
        with self.assertRaises(ValueError):
            Adapter(wire).nes.cpu_register_write(0x6008, 0)
        self.assertFalse(wire.writes)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_complete_two_pass_reads_for_all_supported_boards(self, sleep):
        for mapper, submapper in ((16, 4), (16, 5), (159, 0)):
            profile, bus, raw = fixture(mapper, submapper)
            progress = []
            self.assertIsNone(profile.support())
            self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)), profile.header + raw)
            self.assertEqual(bus.prg_reads, 32)
            self.assertEqual(bus.chr_reads, 512)
            self.assertEqual(bus.modes, [1, 0])
            self.assertEqual(progress[-1][1], 2 * len(raw))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_prg_sequence_and_chr_aliases_keep_reference_order(self, sleep):
        profile, bus, raw = fixture(16, 4)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        for bank in range(16):
            self.assertEqual(bus.writes[bank * 6:bank * 6 + 6],
                             [(a, 0) for a in range(0x8000, 0x8004)] + [(0x6008, bank), (0x8008, bank)])
        self.assertEqual(bus.writes[96:96 + 512],
                         [pair for bank in range(256) for pair in ((0x6000, bank), (0x8000, bank))])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_required_lower_or_upper_prg_decoder_cannot_be_ignored(self, sleep):
        for mapper, submapper in ((16, 4), (16, 5), (159, 0)):
            profile, bus, _ = fixture(mapper, submapper, 'prg_alias')
            with self.subTest(mapper=mapper, submapper=submapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertFalse(bus.chr_reads)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_chr_high_bit_loss_is_detected_by_full_hash(self, sleep):
        for mapper, submapper in ((16, 4), (16, 5), (159, 0)):
            profile, bus, _ = fixture(mapper, submapper, 'chr_bit')
            with self.subTest(mapper=mapper, submapper=submapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    def test_unsupported_submapper_and_geometry_stop_before_io(self):
        for mapper, submapper in ((16, 4), (16, 5), (159, 0)):
            profile, _, _ = fixture(mapper, submapper)
            for change in (dict(submapper=0 if mapper == 16 else 1), dict(submapper=6),
                           dict(prg_size=278528), dict(prg_size=24576),
                           dict(chr_size=0), dict(chr_size=263168)):
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_never_returns_rom(self, sleep):
        for mapper, submapper in ((16, 4), (16, 5), (159, 0)):
            profile, bus, _ = fixture(mapper, submapper, 'second')
            with self.subTest(mapper=mapper, submapper=submapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_restores_mode_without_variant_retry(self, sleep):
        profile, bus, _ = fixture(16, 4)
        bus.bandai_rom_register = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.bandai_rom_register.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_bundled_fcg_profiles_and_separate_mapper153_path(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((16, 15), (159, 6)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))
        self.assertTrue(all(p.support() is None for p in db.profiles if p.mapper == 153))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_selection_uses_full_hash_and_preserves_submapper(self, sleep):
        profile, bus, raw = fixture(16, 5)
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
            self.assertEqual(path.read_bytes()[8] >> 4, 5)

    def test_different_bandai_submappers_are_not_auto_grouped(self):
        profile, _, _ = fixture(16, 4)
        bus = Mock()
        with self.assertRaises(ProtocolError):
            dump_candidates(bus, (profile, replace(profile, submapper=5)))
        self.assertFalse(bus.mock_calls)
