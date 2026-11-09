from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_mappers import MapperReader
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station_nes_mappers import profile_for, TraceBus
from test_cart_station_simple_mappers import banks


class FixedG101:
    """Independent fixed 8+8+16F PRG mapping, five PRG bits, eight CHR bits.

    Submapper 1 ignores the mode bit; window $8000 remains switchable.
    The fixed last 16 KiB window is also decoded, rather than treating the
    bank register as an arbitrary linear offset.
    """
    def __init__(self, prg, chr_data, fault=None):
        self.prg, self.chr, self.fault = prg, chr_data, fault
        self.regs = [0, 0]
        self.chr_regs = [0] * 8
        self.modes, self.writes = [], []
        self.prg_reads = self.chr_reads = 0

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_register_write(self, address, value, dummy=True):
        self.writes.append((address, value))
        if self.fault == 'transport' and address == 0x8000:
            raise OSError('transport stopped')
        if address & 0xf000 == 0x8000:
            self.regs[0] = value & 31
        elif address & 0xf000 == 0xa000:
            self.regs[1] = value & 31
        elif address & 0xf000 == 0xb000:
            self.chr_regs[address & 7] = value
        elif address & 0xf000 != 0x9000:
            raise AssertionError('Unexpected register')
        if dummy:
            self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if not 0x8000 <= address <= 0xffff or size not in (8, 8192):
                raise AssertionError('Wrong PRG window')
            slot = (address - 0x8000) // 8192
            bank = self.regs[slot] if slot < 2 else len(self.prg) // 8192 - 4 + slot
            offset = (bank * 8192) % len(self.prg) + (address & 8191)
            result = self.prg[offset:offset + size]
            if size == 8192:
                self.prg_reads += 1
                if self.fault == 'prg' or (self.fault == 'second' and self.prg_reads > len(self.prg) // 8192):
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0 or size != 1024:
            raise AssertionError('Wrong CHR window')
        self.chr_reads += 1
        offset = self.chr_regs[0] * 1024
        result = self.chr[offset:offset + size]
        if self.fault == 'chr':
            result = bytes((result[0] ^ 1,)) + result[1:]
        return result

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(prg_count=16, chr_count=128, fault=None):
    prg, chr_data = banks(prg_count, 8192), banks(chr_count, 1024, 43)
    return profile_for(32, prg, chr_data, 1), FixedG101(prg, chr_data, fault), prg + chr_data


class G101FixedTests(unittest.TestCase):
    def test_submapper1_matches_reference_mode_zero_trace(self):
        case = next(c for c in json.loads(data('mapper_reference_traces.json').read_text())['cases']
                    if c['variant'] == 'mapper032')
        prg, chr_data = banks(32, 8192), banks(128, 1024, 43)
        profile = profile_for(32, prg, chr_data, 1)
        bus = TraceBus()
        reader = MapperReader(bus, profile)
        reader.initialize()
        for bank in case['prg_banks']:
            reader.prg_bank(bank, case['prg_bank_size'])
        for bank in case['chr_banks']:
            reader.chr_rom_bank(bank, case['chr_bank_size'])
        self.assertEqual(bus.trace, case['trace'])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_actual_database_geometry_reads_twice(self, sleep):
        profile, bus, raw = fixture()
        self.assertIsNone(profile.support())
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual((bus.prg_reads, bus.chr_reads), (32, 256))
        self.assertEqual(bus.modes, [1, 0])
        self.assertTrue(all(value == 0 for address, value in bus.writes if address == 0x9000))
        self.assertTrue(all(address in (0x9000, 0x8000, 0xb000) for address, value in bus.writes))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_highest_five_bit_prg_and_eight_bit_chr_banks(self, sleep):
        profile, bus, raw = fixture(32, 256)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual(bus.writes.count((0x8000, 31)), 2)
        self.assertEqual(bus.writes.count((0xb000, 255)), 2)

    def test_model_fixed_windows_and_ignored_mode_bit(self):
        profile, bus, _ = fixture()
        for value in (0, 2, 3):
            bus.cpu_register_write(0x9000, value)
            bus.cpu_register_write(0x8000, 5)
            self.assertEqual(bus.memory(0x11, 0x8000, 8192), bus.prg[5*8192:6*8192])
            self.assertEqual(bus.memory(0x11, 0xc000, 8192), bus.prg[-16384:-8192])
            self.assertEqual(bus.memory(0x11, 0xe000, 8192), bus.prg[-8192:])
        bus.cpu_register_write(0x8000, 37)
        self.assertEqual(bus.regs[0], 5)

    def test_database_profile_and_unreviewed_geometries(self):
        db = Database.load(data('nes_profiles.reference.json'))
        special = [p for p in db.profiles if p.mapper == 32 and p.submapper == 1]
        normal = [p for p in db.profiles if p.mapper == 32 and p.submapper == 0]
        self.assertEqual((len(special), len(normal)), (1, 6))
        self.assertTrue(all(p.support() is None for p in special + normal))
        profile, _, _ = fixture()
        for changes in (dict(submapper=2), dict(prg_size=24576), dict(prg_size=270336),
                        dict(chr_size=0), dict(chr_size=263168)):
            bus = Mock()
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, **changes))
            self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_prg_corruption_stops_before_chr(self, sleep):
        profile, bus, _ = fixture(fault='prg')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_chr_corruption_cannot_produce_rom(self, sleep):
        profile, bus, _ = fixture(fault='chr')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_cannot_produce_rom(self, sleep):
        profile, bus, _ = fixture(fault='second')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.prg_reads, 32)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_has_no_retry(self, sleep):
        profile, bus, _ = fixture(fault='transport')
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.prg_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_and_saved_header_keep_submapper1(self, sleep):
        profile, bus, raw = fixture()
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        self.assertEqual(payload[8] >> 4, 1)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))
