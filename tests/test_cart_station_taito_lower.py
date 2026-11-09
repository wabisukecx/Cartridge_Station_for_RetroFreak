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


class TaitoCartridge:
    """Independent lower-register ROM windows, save locks and CHR pair state."""
    def __init__(self, mapper, prg, chr_data, fault=None):
        self.mapper, self.prg, self.chr, self.fault = mapper, prg, chr_data, fault
        self.prg_bank = self.chr_bank = 0
        self.next_chr = -1
        self.locks = [0, 0, 0]
        self.chr_mode = -1
        self.prg_reads = self.chr_reads = 0
        self.writes, self.modes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_register_write(self, *args, **kwargs):
        raise AssertionError('General high-register API must not perform lower writes')

    def taito_rom_register(self, mapper, address, value):
        if mapper != self.mapper:
            raise AssertionError('Wrong mapper transport selection')
        self.writes.append((address, value))
        if self.mapper == 82 and address in (0x7ef7, 0x7ef8, 0x7ef9):
            if value != 255:
                raise AssertionError('Save unlock must not be used')
            if self.fault != 'locks':
                self.locks[address - 0x7ef7] = value
        elif self.mapper == 82 and address == 0x7ef6:
            self.chr_mode = value
        elif address == 0x7efa:
            if self.mapper == 82:
                if self.locks != [255] * 3 or self.chr_mode != 0:
                    raise AssertionError('Reference initialization must precede bank reads')
                self.prg_bank = value if self.fault == 'shift' else value >> 2
            else:
                self.prg_bank = value
        elif address == 0x7ef2:
            self.chr_bank = value
        elif self.mapper == 82 and address == 0x7ef3:
            if self.fault != 'pair':
                self.next_chr = value
        else:
            raise AssertionError('Unexpected lower control or save memory write')
        self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if address == 0x8000 and size == 8:
                return self.prg[:8]
            if address != 0x8000 or size != 8192:
                raise AssertionError('Wrong Taito PRG window')
            self.prg_reads += 1
            bank = self.prg_bank % (len(self.prg) // 8192)
            result = self.prg[bank * 8192:(bank + 1) * 8192]
            if self.fault == 'second' and self.prg_reads > len(self.prg) // 8192:
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0x1000 or size != 1024:
            raise AssertionError('Wrong Taito CHR window')
        self.chr_reads += 1
        bank = self.chr_bank
        if self.mapper == 82 and self.next_chr != (bank + 1) & 255:
            return bytes(size)
        return self.chr[bank * 1024:(bank + 1) * 1024]

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, fault=None):
    prg = banks(64 if mapper == 82 else 32, 8192)
    chr_data = banks(256, 1024, 43)
    return profile_for(mapper, prg, chr_data), TaitoCartridge(mapper, prg, chr_data, fault), prg + chr_data


class TaitoLowerTests(unittest.TestCase):
    def test_wire_ex_write_uses_command15_and_other_mappers_use12(self):
        for mapper in (80, 82, 207):
            wire = Wire(bytes(16) + memory(b'P' * 8), fragment=3)
            adapter = Adapter(wire)
            adapter.nes.taito_rom_register(mapper, 0x7efa, 4)
            self.assertEqual(wire.writes, [packet(0x15 if mapper == 80 else 0x12, 8, 1, 0x7efa, 1),
                                          b'\x04', packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4)])
            self.assertFalse(wire.replies)

    def test_lower_register_guard_blocks_save_unlocks_and_other_addresses(self):
        invalid = [(82, a, v) for a, v in ((0x7ef7, 202), (0x7ef8, 105),
                    (0x7ef9, 132), (0x7ef6, 1), (0x7efa, 3))]
        invalid += [(m, a, 0) for m in (80, 82, 207) for a in (0x6000, 0x7f00, 0x8000)]
        invalid += [(80, 0x7ef8, 163), (207, 0x7ef3, 0), (81, 0x7efa, 0),
                    (80.0, 0x7efa, 0), (80, True, 0), (80, 0x7efa, True),
                    (80, 0x7efa, -1), (80, 0x7efa, 256)]
        for mapper, address, value in invalid:
            wire = Wire(b'')
            with self.subTest(mapper=mapper, address=address, value=value), self.assertRaises(ValueError):
                Adapter(wire).nes.taito_rom_register(mapper, address, value)
            self.assertFalse(wire.writes)

    def test_general_nes_write_guard_still_rejects_lower_addresses(self):
        for address in (0x6000, 0x7ef2, 0x7ef6, 0x7ef7, 0x7efa, 0x7f00):
            wire = Wire(b'')
            with self.assertRaises(ValueError):
                Adapter(wire).nes.cpu_register_write(address, 0)
            self.assertFalse(wire.writes)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_all_three_mappers_complete_two_reads_and_hash_checks(self, sleep):
        for mapper in (80, 82, 207):
            profile, bus, raw = fixture(mapper)
            progress = []
            self.assertIsNone(profile.support())
            self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)), profile.header + raw)
            self.assertEqual(bus.prg_reads, 2 * profile.prg_size // 8192)
            self.assertEqual(bus.chr_reads, 512)
            self.assertEqual(bus.modes, [1, 0])
            self.assertEqual(progress[-1][1], 2 * len(raw))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper82_initialization_precedes_each_pass(self, sleep):
        profile, bus, raw = fixture(82)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        initial = [(0x7ef7, 255), (0x7ef8, 255), (0x7ef9, 255), (0x7ef6, 0)]
        self.assertEqual(bus.writes[:4], initial)
        per_pass = 4 + 64 + 256 * 2
        self.assertEqual(bus.writes[per_pass:per_pass + 4], initial)
        profile, bus, _ = fixture(82, 'locks')
        with self.assertRaisesRegex(AssertionError, 'initialization'):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper82_shift_and_chr_pair_are_not_optional(self, sleep):
        for fault in ('shift', 'pair'):
            profile, bus, _ = fixture(82, fault)
            with self.subTest(fault=fault), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper82_chr_pair_wraps_like_a_byte_cast(self, sleep):
        profile, bus, raw = fixture(82)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual(bus.writes[-2:], [(0x7ef2, 255), (0x7ef3, 0)])
        self.assertIn((0x7efa, 252), bus.writes)

    def test_unknown_submapper_and_unrepresentable_geometry_stop_before_io(self):
        for mapper in (80, 82, 207):
            profile, _, _ = fixture(mapper)
            for change in (dict(submapper=1), dict(prg_size=24576), dict(chr_size=0),
                           dict(chr_size=263168), dict(prg_size=532480 if mapper == 82 else 2105344)):
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_never_returns_rom(self, sleep):
        for mapper in (80, 82, 207):
            profile, bus, _ = fixture(mapper, 'second')
            with self.subTest(mapper=mapper), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_restores_mode_without_retry(self, sleep):
        profile, bus, _ = fixture(80)
        bus.taito_rom_register = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.taito_rom_register.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_bundled_database_adds_twelve_profiles(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((80, 7), (82, 0), (207, 1), (552, 4)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_selection_and_saved_header_full_hashes(self, sleep):
        profile, bus, raw = fixture(207)
        other = replace(profile, id='other', prg_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
