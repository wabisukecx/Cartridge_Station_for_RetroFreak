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


class SramCartridge:
    """SRAM at 0x6000, four outer-bank lines and a 16 KiB ROM window."""
    mapper153_select_prg = NesRegisters.mapper153_select_prg

    def __init__(self, prg, fault=None):
        self.prg, self.fault = prg, fault
        self.outer, self.bank = [0, 0, 0, 0], 0
        self.sram = bytearray(bytes(range(256)) * 32)
        self.sram_enabled = True
        self.prg_reads = 0
        self.writes, self.modes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy:
            raise AssertionError('Reference requires dummy reads')
        self.writes.append((address, value))
        if address == 0x800d:
            if value != 0:
                raise AssertionError('ROM path must not enable SRAM')
            self.sram_enabled = False
        elif 0x8000 <= address <= 0x8003:
            if self.fault != 'outer':
                self.outer[address - 0x8000] = value
        elif address == 0x8008:
            self.bank = value & 15
        else:
            raise AssertionError('Unexpected high register')
        self.memory(0x11, 0x8000, 8)

    def _nes_bank_write(self, address, value, dummy):
        if address != 0x6008 or not dummy:
            raise AssertionError('Unexpected lower alias')
        self.writes.append((address, value))
        if self.sram_enabled:
            self.sram[8] = value
            raise AssertionError('Lower alias reached writable SRAM')
        # SRAM-disabled lower alias does not select ROM on this board.
        self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command != 0x11 or address != 0x8000:
            raise AssertionError('CHR RAM / SRAM reads must not be appended to ROM')
        if size == 8:
            return self.prg[:8]
        if size != 16384 or len(set(self.outer)) != 1:
            raise AssertionError('Invalid ROM window or incomplete outer-bank selection')
        self.prg_reads += 1
        number = (self.outer[0] << 4) | self.bank
        result = self.prg[number * size:(number + 1) * size]
        if self.fault == 'second' and self.prg_reads > len(self.prg) // size:
            result = bytes((result[0] ^ 1,)) + result[1:]
        return result

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(count=32, fault=None):
    prg = banks(count, 16384)
    return profile_for(153, prg, b''), SramCartridge(prg, fault), prg


class Mapper153Tests(unittest.TestCase):
    def test_wire_prefix_protects_sram_before_reference_bank_sequence(self):
        wire = Wire((bytes(16) + memory(b'P' * 8)) * 7, fragment=3)
        Adapter(wire).nes.mapper153_select_prg(31)
        expected = []
        for address, value in [(0x800d, 0)] + [(a, 1) for a in range(0x8000, 0x8004)] + [(0x6008, 31), (0x8008, 31)]:
            expected.extend([packet(0x12, 8, 1, address, 1), bytes((value,)),
                             packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4)])
        self.assertEqual(wire.writes, expected)
        self.assertFalse(wire.replies)

    def test_failed_disable_ack_stops_before_bank_and_lower_alias_writes(self):
        wire = Wire(packet(1, 0))
        with self.assertRaises(ProtocolError):
            Adapter(wire).nes.mapper153_select_prg(0)
        self.assertEqual(wire.writes, [packet(0x12, 8, 1, 0x800d, 1)])

    def test_invalid_banks_and_unguarded_lower_writes_are_rejected(self):
        for bank in (-1, 32, True, 1.0):
            wire = Wire(b'')
            with self.assertRaises(ValueError):
                Adapter(wire).nes.mapper153_select_prg(bank)
            self.assertFalse(wire.writes)
        wire = Wire(b'')
        with self.assertRaises(ValueError):
            Adapter(wire).nes.cpu_register_write(0x6008, 0)
        with self.assertRaises(ValueError):
            Adapter(wire).nes.bandai_rom_register(153, 0x6008, 0)
        self.assertFalse(wire.writes)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_passes_preserve_all_existing_sram_bytes(self, sleep):
        for count in (8, 32):
            profile, bus, prg = fixture(count)
            original = bytes(bus.sram)
            progress = []
            self.assertIsNone(profile.support())
            self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)), profile.header + prg)
            self.assertEqual(bus.prg_reads, count * 2)
            self.assertEqual(bytes(bus.sram), original)
            self.assertFalse(bus.sram_enabled)
            self.assertEqual(bus.modes, [1, 0])
            self.assertEqual(progress[-1][1], len(prg) * 2)
            for index in range(count * 2):
                bank = index % count
                self.assertEqual(bus.writes[index * 7:index * 7 + 7],
                                 [(0x800d, 0)] + [(a, bank // 16) for a in range(0x8000, 0x8004)]
                                 + [(0x6008, bank), (0x8008, bank)])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_missing_outer_bank_bit_cannot_pass_full_prg_hash(self, sleep):
        profile, bus, _ = fixture(fault='outer')
        original = bytes(bus.sram)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bytes(bus.sram), original)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_damage_never_returns_rom(self, sleep):
        profile, bus, _ = fixture(fault='second')
        original = bytes(bus.sram)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bytes(bus.sram), original)
        self.assertEqual(bus.modes, [1, 0])

    def test_chr_rom_unknown_submapper_and_extra_outer_banks_stop_before_io(self):
        profile, _, _ = fixture()
        for change in (dict(submapper=1), dict(chr_size=8192), dict(prg_size=16384),
                       dict(prg_size=540672), dict(prg_size=24576)):
            bus = Mock()
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, **change))
            self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_lower_transport_failure_keeps_sram_protected_and_exits(self, sleep):
        profile, bus, _ = fixture()
        original = bytes(bus.sram)
        bus._nes_bank_write = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus._nes_bank_write.call_count, 1)
        self.assertFalse(bus.sram_enabled)
        self.assertEqual(bytes(bus.sram), original)
        self.assertEqual(bus.modes, [1, 0])

    def test_one_embedded_profile_is_applicable(self):
        profiles = [p for p in Database.load(data('nes_profiles.reference.json')).profiles if p.mapper == 153]
        self.assertEqual(len(profiles), 1)
        self.assertEqual((profiles[0].prg_size, profiles[0].chr_size), (524288, 0))
        self.assertIsNone(profiles[0].support())

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_selection_and_saved_prg_hashes(self, sleep):
        profile, bus, prg = fixture()
        other = replace(profile, id='other', prg_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + prg)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
