"""Fixed-PRG MMC1 board tests. All ROM bytes are generated test data."""
from _paths import ROOT, data  # noqa: F401
import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_manager import Service, UserCancelled
from cart_station_nes_db import Database, Profile, dump_candidates, dump_profile
from cart_station_nes_mappers import MapperReader
from test_cart_station_nes_mappers import Cartridge


def fixture(name='Fixed PRG MMC1', chr_size=32768, variation=0):
    prg = b'L' * 16384 + b'H' * 16384
    chr_data = b''.join(bytes(((bank + variation) & 255,)) * 4096
                        for bank in range(chr_size // 4096))
    header = bytearray(b'NES\x1a' + bytes(12))
    header[4:9] = bytes((2, chr_size // 8192, 0x10, 8, 0x50))
    sha = lambda value: hashlib.sha1(value).hexdigest()
    profile = Profile.parse(dict(id=name, title=name, mapper=1, submapper=5,
                                 prg_size=32768, chr_size=chr_size, header=header.hex(),
                                 prg_sha1=sha(prg), chr_sha1=sha(chr_data),
                                 raw_sha1=sha(prg + chr_data),
                                 file_sha1=sha(header + prg + chr_data),
                                 probes={'first1k_sha1': sha(prg[:1024])}))
    return profile, prg, chr_data


class FixedPrgCartridge(Cartridge):
    """Hardwired PRG address lines and MMC1-controlled 4/8 KiB CHR windows."""
    def __init__(self, prg, chr_data):
        super().__init__(1, prg, chr_data)
        self.reads = []

    def memory(self, command, address, size):
        self.reads.append((command, address, size))
        if command == 0x11:
            offset = address - 0x8000
            return self.prg[offset:offset + size]
        if self.registers[0] & 16:
            bank = self.registers[1 if address < 4096 else 2]
            offset = bank * 4096 + (address & 4095)
        else:
            offset = (self.registers[1] & ~1) * 4096 + (address & 8191)
        offset %= len(self.chr)
        return self.chr[offset:offset + size]


@patch('cart_station_nes_mappers.time.sleep')
class FixedPrgMmc1Tests(unittest.TestCase):
    def test_fixed_windows_and_all_chr_banks_are_read_twice(self, sleep):
        for size in (8192, 16384, 32768, 65536, 131072):
            with self.subTest(chr_size=size):
                profile, prg, chr_data = fixture(chr_size=size)
                bus = FixedPrgCartridge(prg, chr_data)
                self.assertEqual(dump_profile(bus, profile), profile.header + prg + chr_data)
                self.assertEqual(bus.modes, [1, 0])
                full_prg = [item for item in bus.reads if item[0] == 0x11 and item[2] == 16384]
                self.assertEqual(full_prg, [(0x11, 0x8000, 16384), (0x11, 0xc000, 16384)] * 2)
                full_chr = [item for item in bus.reads if item[0] == 0x13]
                self.assertEqual(len(full_chr), 2 * size // 4096)
                self.assertTrue(all(0x8000 <= address < 0xe000 for address, _, _ in bus.writes))

    def test_prg_reads_do_not_write_any_mmc1_register(self, sleep):
        profile, prg, chr_data = fixture()
        bus = FixedPrgCartridge(prg, chr_data)
        reader = MapperReader(bus, profile)
        self.assertEqual(reader.prg_bank(0, 16384), prg[:16384])
        self.assertEqual(reader.prg_bank(1, 16384), prg[16384:])
        self.assertEqual(bus.writes, [])

    def test_unknown_subvariants_and_wrong_geometries_never_touch_device(self, sleep):
        profile, _, _ = fixture()
        for changes in ({'prg_size': 16384}, {'prg_size': 65536}, {'chr_size': 0},
                        {'chr_size': 24576}, {'chr_size': 262144},
                        {'submapper': 1}, {'submapper': 4}, {'submapper': 6}):
            with self.subTest(changes=changes):
                bus = Mock()
                with self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **changes))
                self.assertFalse(bus.mock_calls)

    def test_wrong_prg_stops_before_chr_read_and_has_no_variant_retry(self, sleep):
        profile, prg, chr_data = fixture()
        bus = FixedPrgCartridge(b'X' + prg[1:], chr_data)
        with self.assertRaisesRegex(ProtocolError, 'hash mismatch'):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])
        self.assertFalse(any(command == 0x13 for command, _, _ in bus.reads))

    def test_wrong_chr_bank_mapping_is_detected_by_full_hash(self, sleep):
        profile, prg, chr_data = fixture()
        bus = FixedPrgCartridge(prg, chr_data)
        original = bus.memory
        def repeated_first_bank(command, address, size):
            return chr_data[:size] if command == 0x13 else original(command, address, size)
        bus.memory = repeated_first_bank
        with self.assertRaisesRegex(ProtocolError, 'hash mismatch'):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])

    def test_second_read_is_fresh_and_detects_changed_prg(self, sleep):
        profile, prg, chr_data = fixture()
        bus = FixedPrgCartridge(prg, chr_data)
        original = bus.memory
        count = 0
        def changing(command, address, size):
            nonlocal count
            if command == 0x11 and size == 16384:
                count += 1
                if count == 3:
                    bus.prg = b'X' + prg[1:]
            return original(command, address, size)
        bus.memory = changing
        with self.assertRaisesRegex(ProtocolError, 'hash mismatch'):
            dump_profile(bus, profile)
        self.assertGreaterEqual(count, 3)
        self.assertEqual(bus.modes, [1, 0])

    def test_transport_failure_exits_without_trying_other_mmc1_variants(self, sleep):
        profile, prg, chr_data = fixture()
        bus = FixedPrgCartridge(prg, chr_data)
        bus.memory = Mock(side_effect=ProtocolError('Reader disconnected'))
        with self.assertRaisesRegex(ProtocolError, 'Reader disconnected'):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])

    def test_same_prg_candidates_are_resolved_by_chr_and_full_hash(self, sleep):
        first, prg, _ = fixture('First revision')
        second, _, chr_data = fixture('Second revision', variation=40)
        bus = FixedPrgCartridge(prg, chr_data)
        selected, payload = dump_candidates(bus, (first, second))
        self.assertEqual(selected, second)
        self.assertEqual(payload, second.header + prg + chr_data)
        self.assertEqual(bus.modes, [1, 0])

    def test_file_hash_mismatch_is_not_saved(self, sleep):
        profile, prg, chr_data = fixture()
        bus = FixedPrgCartridge(prg, chr_data)
        with self.assertRaisesRegex(ProtocolError, 'hash mismatch'):
            dump_profile(bus, replace(profile, file_sha1='0' * 40))
        self.assertEqual(bus.modes, [1, 0])

    def test_real_db_fixed_prg_profiles_become_readable(self, sleep):
        database = Database.load(data('nes_profiles.reference.json'))
        profiles = [p for p in database.profiles if p.mapper == 1 and p.submapper == 5]
        self.assertEqual(len(profiles), 28)
        self.assertTrue(all(p.support() is None for p in profiles))
        japanese = [p for p in profiles if 'Dr. Mario (Japan, USA)' in p.title]
        self.assertEqual(len(japanese), 2)
        self.assertTrue(any('(Rev 1)' in p.title for p in japanese))

    def test_service_capture_preserves_full_validation_and_mgl_launch(self, sleep):
        profile, prg, chr_data = fixture()
        bus = FixedPrgCartridge(prg, chr_data)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '_Console').mkdir()
            (root / '_Console/NES_test.rbf').write_bytes(b'fake core')
            config = root / 'cores.json'
            config.write_text(json.dumps({'version': 1, 'NES': {
                'rbf': '_Console/NES', 'type': 'f', 'index': 1, 'delay': 2}}))
            service = Service(root / 'games/NES', core_path=config, sd_root=root,
                              database=Database([profile]))
            service.adapter, service.profile, service.selected_id = bus, profile, profile.id
            service.candidate = 3
            with patch('cart_station_service.identify', return_value=(profile,)):
                path, created = service.capture(lambda *args: None)
                self.assertTrue(created)
                self.assertEqual(path.read_bytes(), profile.header + prg + chr_data)
                self.assertFalse(service.capture(lambda *args: None)[1])
            self.assertEqual(service.existing(), path)
            mgl = service.prepare_game()
            self.assertEqual(ET.parse(mgl).find('file').get('path'), str(path.resolve()))

    def test_service_cancel_before_second_read_creates_no_file(self, sleep):
        profile, prg, chr_data = fixture()
        bus = FixedPrgCartridge(prg, chr_data)
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'output'
            service = Service(destination, database=Database([profile]))
            service.adapter, service.profile, service.selected_id = bus, profile, profile.id
            service.candidate = 3
            def cancel(stage, done, total):
                if stage == 'Read 2/2':
                    raise UserCancelled('Cancelled')
            with patch('cart_station_service.identify', return_value=(profile,)):
                with self.assertRaises(UserCancelled):
                    service.capture(cancel)
            self.assertFalse(destination.exists())
            self.assertEqual(bus.modes[-1], 0)


if __name__ == '__main__':
    unittest.main()
