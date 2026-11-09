from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class Cartridge:
    """ROM windows with VRC2 shifted CHR lines, VRC6 mode and RAMBO selector.

    Unrelated IRQ/audio/save registers deliberately fail the test.
    """
    def __init__(self, mapper, prg, chr_data, fault=None):
        self.mapper, self.prg, self.chr, self.fault = mapper, prg, chr_data, fault
        self.prg_unit = 16384 if mapper in (24, 26) else 8192
        self.prg_bank = self.chr_bank = self.low = self.high = 0
        self.selector = -1
        self.chr_mode = 3
        self.prg_reads = self.chr_reads = 0
        self.modes, self.writes, self.windows = [], [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)
        if value == 1:
            self.chr_mode = 3

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy or not 0x8000 <= address <= 0xffff:
            raise AssertionError('Reference requires direct register write and dummy read')
        self.writes.append((address, value))
        if self.mapper == 22:
            if address == 0xa000:
                self.prg_bank = value
            elif address == 0xb000 and value <= 15:
                self.low = value
            elif address == 0xb002 and value <= 31:
                self.high = value
            else:
                raise AssertionError('Wrong VRC2a register or nibble width')
            encoded = self.low | (self.high << 4)
            self.chr_bank = encoded if self.fault == 'unshifted' else encoded >> 1
        elif self.mapper in (24, 26):
            if address == 0x8000:
                self.prg_bank = value
            elif address == 0xb003 and value == 0:
                if self.fault != 'mode':
                    self.chr_mode = 0
            elif address == 0xd000:
                if self.chr_mode != 0:
                    self.chr_bank = 0
                else:
                    self.chr_bank = value
            else:
                raise AssertionError('Unexpected VRC6 control/save/audio register')
        else:
            if address == 0x8000 and value in (6, 32):
                self.selector = value
            elif address == 0x8001 and self.selector == 6:
                self.prg_bank = value
            elif address == 0x8001 and self.selector == 32:
                self.chr_bank = 0 if self.fault == 'selector' else value
            else:
                raise AssertionError('Wrong RAMBO register or CHR mode selector')
        self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if size == 8 and address == 0x8000:
                return self.prg[:8]
            expected = 0xa000 if self.mapper == 22 else 0x8000
            if size != self.prg_unit or address != expected:
                raise AssertionError('Wrong PRG window')
            self.windows.append(address)
            self.prg_reads += 1
            bank = self.prg_bank % (len(self.prg) // self.prg_unit)
            offset = bank * self.prg_unit
            result = self.prg[offset:offset + size]
            if self.fault == 'second' and self.prg_reads > len(self.prg) // size:
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0 or size != 1024:
            raise AssertionError('Wrong CHR window')
        self.chr_reads += 1
        bank = self.chr_bank % (len(self.chr) // 1024)
        return self.chr[bank * 1024:(bank + 1) * 1024]

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, fault=None):
    unit = 16384 if mapper in (24, 26) else 8192
    prg = banks(16, unit)
    chr_data = banks(128 if mapper == 158 else 256, 1024, 43)
    return profile_for(mapper, prg, chr_data), Cartridge(mapper, prg, chr_data, fault), prg + chr_data


class VrcRamboTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_complete_two_pass_reads_and_windows(self, sleep):
        for mapper in (22, 24, 26, 64, 158):
            with self.subTest(mapper=mapper):
                profile, bus, raw = fixture(mapper)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)),
                                 profile.header + raw)
                self.assertEqual(bus.prg_reads, 32)
                self.assertEqual(bus.chr_reads, 2 * profile.chr_size // 1024)
                self.assertEqual(set(bus.windows), {0xa000 if mapper == 22 else 0x8000})
                self.assertEqual(progress[-1][1], 2 * len(raw))
                self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_vrc2_shift_and_five_bit_high_register_are_needed(self, sleep):
        profile, bus, raw = fixture(22)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        writes = [pair for pair in bus.writes if pair[0] in (0xb000, 0xb002)]
        self.assertIn((0xb002, 16), writes)
        self.assertIn((0xb002, 31), writes)
        self.assertTrue(all(value % 2 == 0 for address, value in writes if address == 0xb000))
        profile, bus, _ = fixture(22, 'unshifted')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_vrc6_mode_is_reset_before_every_chr_bank(self, sleep):
        for mapper in (24, 26):
            profile, bus, raw = fixture(mapper)
            self.assertEqual(dump_profile(bus, profile), profile.header + raw)
            writes = [pair for pair in bus.writes if pair[0] != 0x8000]
            expected = [pair for bank in range(256) for pair in ((0xb003, 0), (0xd000, bank))]
            self.assertEqual(writes, expected * 2)
            profile, bus, _ = fixture(mapper, 'mode')
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_rambo_chr_mode_selector_must_take_effect(self, sleep):
        for mapper in (64, 158):
            profile, bus, _ = fixture(mapper, 'selector')
            with self.subTest(mapper=mapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertIn((0x8000, 32), bus.writes)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_never_returns_rom(self, sleep):
        for mapper in (22, 24, 26, 64, 158):
            profile, bus, _ = fixture(mapper, 'second')
            with self.subTest(mapper=mapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_error_restores_mode_without_retry(self, sleep):
        profile, bus, _ = fixture(26)
        bus.memory = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.memory.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_unknown_submapper_and_geometry_stop_before_io(self):
        for mapper in (22, 24, 26, 64, 158):
            profile, _, _ = fixture(mapper)
            upper = 4194304 if mapper in (24, 26) else 2097152
            for change in (dict(submapper=1), dict(prg_size=upper + 16384),
                           dict(prg_size=24576), dict(chr_size=0), dict(chr_size=1025),
                           dict(chr_size=139264 if mapper == 158 else 263168)):
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_bundled_database_adds_six_profiles_and_no_mapper158_entries(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((22, 2), (24, 1), (26, 2), (64, 1), (158, 0)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_same_geometry_candidates_are_selected_by_actual_hashes(self, sleep):
        profile, bus, raw = fixture(22)
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_db_header_and_full_hashes_survive_saving(self, sleep):
        profile, bus, _ = fixture(64)
        payload = dump_profile(bus, profile)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
            self.assertEqual(path.read_bytes(), payload)
