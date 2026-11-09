from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_nes_mappers import MapperReader
from cart_station_storage import save_verified_rom
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


# Independent bus wiring, not imported from the implementation's address table.
VARIANTS = ((21, 1, 2), (21, 2, 64), (23, 1, 1), (23, 2, 4),
            (23, 3, 1), (25, 1, 2), (25, 2, 8), (25, 3, 2))


def chr_banks(count):
    # Preserve bank identity above 255; an 8-bit pattern alone would repeat.
    return b''.join(bank.to_bytes(2, 'little') + bytes((bank & 255,)) * 1022
                    for bank in range(count))


class VrcCartridge:
    """ROM-only VRC bus with physical CHR address lines and separate nibbles."""
    def __init__(self, prg, chr_data, high_offset, fault=None):
        self.prg, self.chr, self.high_offset, self.fault = prg, chr_data, high_offset, fault
        self.prg_bank = self.low = self.high = 0
        self.prg_reads = self.chr_reads = 0
        self.writes, self.modes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy:
            raise AssertionError('VRC writes require the reference dummy read')
        self.writes.append((address, value))
        if address == 0xa000:
            self.prg_bank = value
        elif address == 0xb000 and value <= 15:
            self.low = value
        elif address in (0xb001, 0xb002, 0xb004, 0xb008, 0xb040) and value <= 31:
            if address == 0xb000 + self.high_offset:
                self.high = value & 15 if self.fault == 'high_bit' else value
        else:
            raise AssertionError('Unexpected control/IRQ/save address or register width')
        self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if address == 0x8000 and size == 8:
                return self.prg[:8]
            if address != 0xa000 or size != 8192:
                raise AssertionError('VRC PRG read must use the 0xA000 window')
            self.prg_reads += 1
            offset = self.prg_bank * 8192
            result = self.prg[offset:offset + size]
            if self.fault == 'second' and self.prg_reads > len(self.prg) // 8192:
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0 or size != 1024:
            raise AssertionError('VRC CHR read must use the 0x0000 window')
        self.chr_reads += 1
        offset = (self.low | self.high << 4) * 1024
        return self.chr[offset:offset + size]

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, submapper, offset, fault=None):
    prg = banks(32, 8192)
    count = 256 if mapper in (23, 25) and submapper == 3 else 512
    chr_data = chr_banks(count)
    profile = profile_for(mapper, prg, chr_data, submapper)
    return profile, VrcCartridge(prg, chr_data, offset, fault), prg + chr_data


class VrcWiringTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_all_eight_wirings_complete_two_independent_passes(self, sleep):
        for mapper, sub, offset in VARIANTS:
            with self.subTest(mapper=mapper, submapper=sub):
                profile, bus, raw = fixture(mapper, sub, offset)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)),
                                 profile.header + raw)
                self.assertEqual(bus.prg_reads, 64)
                self.assertEqual(bus.chr_reads, 2 * profile.chr_size // 1024)
                self.assertEqual(bus.modes, [1, 0])
                self.assertEqual(progress[-1][1], 2 * len(raw))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_chr_high_writes_use_each_physical_address(self, sleep):
        for mapper, sub, offset in VARIANTS:
            profile, bus, _ = fixture(mapper, sub, offset)
            reader = MapperReader(bus, profile)
            result = reader.chr_rom_bank(255, 1024)
            self.assertEqual(bus.writes, [(0xb000, 15), (0xb000 + offset, 15)])
            self.assertEqual(result, bus.chr[255 * 1024:256 * 1024])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_wrong_physical_wiring_fails_complete_chr_hash(self, sleep):
        for mapper, sub, offset in VARIANTS:
            profile, bus, _ = fixture(mapper, sub, 64 if offset != 64 else 2)
            with self.subTest(mapper=mapper, submapper=sub), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_vrc4_ninth_chr_bit_is_required_above_256_kib(self, sleep):
        for mapper, sub, offset in VARIANTS:
            if sub == 3:
                continue
            profile, bus, _ = fixture(mapper, sub, offset, 'high_bit')
            with self.subTest(mapper=mapper, submapper=sub), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertIn((0xb000 + offset, 16), bus.writes)
            self.assertIn((0xb000 + offset, 31), bus.writes)

    def test_unknown_wirings_and_geometry_stop_before_any_io(self):
        for mapper, sub, offset in VARIANTS:
            profile, _, _ = fixture(mapper, sub, offset)
            changes = [dict(submapper=0), dict(submapper=4), dict(prg_size=2129920),
                       dict(prg_size=24576), dict(chr_size=0), dict(chr_size=1025),
                       dict(chr_size=525312)]
            if sub == 3:
                changes.append(dict(chr_size=263168))
            if mapper == 21:
                changes.append(dict(submapper=3))
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, submapper=sub, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_read_corruption_never_returns_rom(self, sleep):
        for mapper, sub, offset in VARIANTS:
            profile, bus, _ = fixture(mapper, sub, offset, 'second')
            with self.subTest(mapper=mapper, submapper=sub), self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_is_not_retried_as_another_wiring(self, sleep):
        profile, bus, _ = fixture(25, 2, 8)
        bus.memory = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.memory.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_bundled_database_adds_18_profiles(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((21, 2), (23, 10), (25, 6)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_selection_uses_hashes_and_rejects_mixed_wirings(self, sleep):
        profile, bus, raw = fixture(23, 2, 4)
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        bus = Mock()
        with self.assertRaises(ProtocolError):
            dump_candidates(bus, (profile, replace(profile, submapper=1)))
        self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_saved_nes_header_keeps_mapper_and_submapper(self, sleep):
        for mapper, sub, offset in ((21, 2, 64), (23, 3, 1), (25, 1, 2)):
            profile, bus, _ = fixture(mapper, sub, offset)
            payload = dump_profile(bus, profile)
            with tempfile.TemporaryDirectory() as folder:
                path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
                self.assertTrue(created)
                self.assertTrue(verified_rom_file(path, profile))
                self.assertEqual(path.read_bytes()[:16], profile.header)
