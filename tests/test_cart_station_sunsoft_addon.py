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


class AddonCartridge:
    """Eight internal 16 KiB banks, one external bank and a protected SRAM window."""
    def __init__(self, prg, chr_data, fault=None):
        self.prg, self.chr, self.fault = prg, chr_data, fault
        self.prg_bank = self.chr_bank = 0
        self.prg_reads = self.chr_reads = 0
        self.sram = bytearray(b'S' * 8192)
        self.sram_enabled = True
        self.mode_register = 16
        self.writes, self.modes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy or address not in (0xf000, 0x8000):
            raise AssertionError('Unexpected Sunsoft control address')
        self.writes.append((address, value))
        if address == 0xf000:
            if value not in (0, *range(8, 16)):
                raise AssertionError('ROM path must not enable SRAM')
            self.mode_register = value
            self.sram_enabled = bool(value & 16)
            self.prg_bank = value & 7
        else:
            self.chr_bank = value
        self.memory(0x11, 0x8000, 8)

    def _nes_bank_write(self, address, value, dummy):
        if (address, value, dummy) != (0x6000, 1, True):
            raise AssertionError('Only the fixed external-ROM selection is allowed')
        self.writes.append((address, value))
        if self.sram_enabled:
            self.sram[0] = value
            raise AssertionError('External selection must not reach enabled SRAM')
        if self.mode_register != 0:
            raise AssertionError('Disable internal ROM/SRAM before external selection')
        if self.fault != 'external':
            self.prg_bank = 8
        self.memory(0x11, 0x8000, 8)

    def sunsoft4_addon_select(self):
        # Execute the production compound guard against independent chip state.
        NesRegisters.sunsoft4_addon_select(self)

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
        if command != 0x13 or address != 0 or size != 2048:
            raise AssertionError('Wrong CHR window')
        self.chr_reads += 1
        offset = self.chr_bank * 2048
        return self.chr[offset:offset + size]

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(count=9, fault=None):
    prg, chr_data = banks(count, 16384), banks(64, 2048, 43)
    return profile_for(68, prg, chr_data, 1), AddonCartridge(prg, chr_data, fault), prg + chr_data


class SunsoftAddonTests(unittest.TestCase):
    def test_wire_compound_selection_disables_sram_before_lower_write(self):
        reply = bytes(16) + memory(b'P' * 8)
        wire = Wire(reply * 2, fragment=3)
        Adapter(wire).nes.sunsoft4_addon_select()
        self.assertEqual(wire.writes, [packet(0x12, 8, 1, 0xf000, 1), b'\0',
                                      packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4),
                                      packet(0x12, 8, 1, 0x6000, 1), b'\1',
                                      packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4)])
        self.assertFalse(wire.replies)

    def test_failed_disable_ack_never_sends_lower_write(self):
        wire = Wire(packet(1, 0))
        with self.assertRaises(ProtocolError):
            Adapter(wire).nes.sunsoft4_addon_select()
        self.assertEqual(wire.writes, [packet(0x12, 8, 1, 0xf000, 1)])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_base_only_and_external_rom_complete_two_passes(self, sleep):
        for count in (8, 9):
            profile, bus, raw = fixture(count)
            progress = []
            self.assertIsNone(profile.support())
            self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)), profile.header + raw)
            self.assertEqual(bus.prg_reads, count * 2)
            self.assertEqual(bus.chr_reads, 128)
            self.assertEqual(bus.modes, [1, 0])
            self.assertEqual(progress[-1][1], 2 * len(raw))
            self.assertEqual(sum(a == 0x6000 for a, v in bus.writes), 2 if count == 9 else 0)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_internal_external_boundary_preserves_preexisting_sram(self, sleep):
        profile, bus, raw = fixture()
        original = bytes(bus.sram)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual(bus.writes[:10], [(0xf000, n) for n in range(8, 16)] + [(0xf000, 0), (0x6000, 1)])
        self.assertEqual(bytes(bus.sram), original)
        self.assertFalse(bus.sram_enabled)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_absent_or_wrong_external_bank_stops_before_chr_read(self, sleep):
        profile, bus, _ = fixture(fault='external')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertFalse(bus.chr_reads)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_never_returns_rom(self, sleep):
        profile, bus, _ = fixture(fault='second')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 64)
        self.assertEqual(bus.modes, [1, 0])

    def test_unknown_submapper_and_extra_external_banks_stop_before_io(self):
        profile, _, _ = fixture()
        for change in (dict(submapper=2), dict(prg_size=163840), dict(prg_size=114688),
                       dict(chr_size=0), dict(chr_size=526336)):
            bus = Mock()
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, **change))
            self.assertFalse(bus.mock_calls)
        wire = Wire(b'')
        with self.assertRaises(ValueError):
            Adapter(wire).nes.cpu_register_write(0x6000, 1)
        self.assertFalse(wire.writes)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_external_transport_failure_restores_mode_without_retry(self, sleep):
        profile, bus, _ = fixture()
        bus.sunsoft4_addon_select = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.sunsoft4_addon_select.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_three_embedded_addon_profiles_are_now_applicable(self):
        db = Database.load(data('nes_profiles.reference.json'))
        addon = [p for p in db.profiles if p.mapper == 68 and p.submapper == 1]
        self.assertEqual(len(addon), 3)
        self.assertTrue(all(p.support() is None for p in addon))
        # The later dedicated multicart port now covers the two other boards.
        self.assertTrue(all(p.support() is None for p in db.profiles if p.mapper in (37, 47)))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_hash_candidate_selection_and_saved_submapper_header(self, sleep):
        profile, bus, raw = fixture()
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
            self.assertEqual(path.read_bytes()[8] >> 4, 1)
