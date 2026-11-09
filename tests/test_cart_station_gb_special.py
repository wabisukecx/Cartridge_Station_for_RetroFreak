from _paths import ROOT, data  # noqa: F401
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter
from cart_station_gb import GbDatabase, inspect_header, dump_gb, verified_file
from cart_station_service import Service, UserCancelled
from cart_station_nes_db import Database
from cart_station_presentation import profile_details
from test_cart_station_gb import fixture
from test_cart_station import Wire


class Cartridge:
    """Six-bit ROM window independent of RAM bank and camera registers."""
    def __init__(self, payload, fault=None):
        self.payload, self.fault = payload, fault
        self.cart = payload[0x147]
        self.bank, self.ram_bank = 1, 3
        self.ram = bytearray(bytes(range(256)) * (128 if self.cart == 255 else 512))
        self.camera = bytearray(bytes(range(128)))
        self.original_ram, self.original_camera = bytes(self.ram), bytes(self.camera)
        self.events, self.modes, self.read_banks = [], [], []

    def status(self):
        return 5

    def mode(self, value):
        self.modes.append(value)

    def gb_register(self, address, value):
        self.events.append(('W', address, value))
        if self.fault == 'transport' and address == 0x2100 and value == 1:
            raise OSError('transport stopped')
        if address == 0x2100:
            if self.fault != 'bank':
                self.bank = value & 63
        elif address == 0x4000 and self.cart == 255 and value == 0:
            self.ram_bank = value
        else:
            raise AssertionError('Unexpected RAM, IR or camera access')

    def gb_read(self, address, size):
        self.events.append(('R', address, size))
        if address < 0x4000:
            bank = 0
        elif address == 0x4000:
            bank = self.bank
        else:
            raise AssertionError('Read outside ROM windows')
        offset = bank * 16384 + (address & 16383)
        result = self.payload[offset:offset + size]
        if size == 16384:
            self.read_banks.append(bank)
            if self.fault == 'second' and len(self.read_banks) > len(self.payload) // 16384:
                result = result[:-1] + bytes((result[-1] ^ 1,))
        return result


@patch('cart_station_gb.time.sleep')
class GbSpecialTests(unittest.TestCase):
    def test_header_recognizes_only_the_two_new_types(self, sleep):
        for cart, name in ((255, 'HuC1'), (252, 'PocketCamera')):
            profile, _, _ = fixture(cart, 5)
            self.assertEqual(profile.controller, name)
            self.assertIsNone(profile.support())
            self.assertIn(name, profile_details(profile))

    def test_geometry_and_mismatched_type_stop_before_io(self, sleep):
        for cart in (255, 252):
            profile, _, _ = fixture(cart, 5)
            for size in (0, 16384, 49152, 2097152, 4194304):
                bus = Mock()
                with self.assertRaises(ProtocolError):
                    dump_gb(bus, replace(profile, prg_size=size), GbDatabase([]))
                self.assertEqual(bus.mock_calls, [])
            other, _, _ = fixture(1, 5)
            self.assertIsNotNone(replace(profile, header=other.header).support())

    def test_huc1_full_one_mib_two_reads(self, sleep):
        profile, payload, record = fixture(255, 5)
        bus = Cartridge(payload)
        matched, result = dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(result, payload)
        self.assertEqual(matched.controller, 'HuC1')
        self.assertEqual(bus.read_banks, list(range(64)) * 2)
        self.assertEqual(bus.modes, [1, 0])
        self.assertEqual(sum(e == ('W', 0x4000, 0) for e in bus.events), 128)

    def test_camera_full_one_mib_two_reads(self, sleep):
        profile, payload, record = fixture(252, 5)
        bus = Cartridge(payload)
        matched, result = dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(result, payload)
        self.assertEqual(matched.controller, 'PocketCamera')
        self.assertEqual(bus.read_banks, list(range(64)) * 2)
        self.assertTrue(all(e[1] == 0x2100 for e in bus.events if e[0] == 'W'))
        self.assertEqual(bus.modes, [1, 0])

    def test_huc1_sram_and_camera_pictures_are_preserved(self, sleep):
        for cart in (255, 252):
            profile, payload, record = fixture(cart, 5)
            bus = Cartridge(payload)
            dump_gb(bus, profile, GbDatabase([record]))
            self.assertEqual(bytes(bus.ram), bus.original_ram)
            self.assertEqual(bytes(bus.camera), bus.original_camera)
            self.assertTrue(all(e[1] < 0x8000 for e in bus.events))
            self.assertFalse(any(e[0] == 'W' and e[1] < 0x2000 for e in bus.events))

    def test_header_inspection_never_changes_banks(self, sleep):
        for cart in (255, 252):
            profile, payload, _ = fixture(cart, 5)
            bus = Cartridge(payload)
            self.assertEqual(inspect_header(bus), profile)
            self.assertEqual(bus.events, [('R', 0x100, 80)] * 2)
            self.assertEqual(bus.modes, [1, 0])

    def test_wire_bank_controls_and_prohibited_peripheral_addresses(self, sleep):
        for address, value in ((0x2100, 0), (0x2100, 63), (0x4000, 0)):
            wire = Wire(bytes(16), fragment=3)
            Adapter(wire).gb_register(address, value)
            self.assertEqual(wire.writes, [packet(0x1c, 8, 1, address, 1), bytes((value,))])
            self.assertFalse(wire.replies)
        for address in (0x0000, 0xa000, 0xa001):
            with self.assertRaises(ValueError):
                Adapter(Wire(b'')).gb_register(address, 0)

    def test_mmm01_still_stops(self, sleep):
        for cart in (0x0b, 0x0c, 0x0d):
            profile, _, _ = fixture(cart, 3)
            bus = Mock()
            with self.assertRaises(ProtocolError):
                dump_gb(bus, profile, GbDatabase([]))
            self.assertEqual(bus.mock_calls, [])

    def test_second_pass_damage_produces_no_file(self, sleep):
        for cart in (255, 252):
            profile, payload, record = fixture(cart, 3)
            bus = Cartridge(payload, 'second')
            with self.assertRaisesRegex(ProtocolError, 'Two GB ROM reads differ'):
                dump_gb(bus, profile, GbDatabase([record]))
            self.assertEqual(bus.modes, [1, 0])

    def test_wrong_bank_or_unknown_full_hash_has_no_variant_retry(self, sleep):
        for cart in (255, 252):
            profile, payload, record = fixture(cart, 3)
            for fault, records in (('bank', [record]), (None, [])):
                bus = Cartridge(payload, fault)
                with self.assertRaisesRegex(ProtocolError, 'not in the database'):
                    dump_gb(bus, profile, GbDatabase(records))
                self.assertEqual(len(bus.read_banks), len(payload) // 16384 * 2)
                self.assertEqual(bus.modes, [1, 0])

    def test_changed_header_stops_before_register_write(self, sleep):
        for cart in (255, 252):
            profile, payload, record = fixture(cart, 3)
            bus = Cartridge(payload[:0x134] + b'X' + payload[0x135:])
            with self.assertRaisesRegex(ProtocolError, 'cartridge changed'):
                dump_gb(bus, profile, GbDatabase([record]))
            self.assertFalse(any(e[0] == 'W' for e in bus.events))
            self.assertEqual(bus.modes, [1, 0])

    def test_transport_failure_exits_without_retry(self, sleep):
        for cart in (255, 252):
            profile, payload, record = fixture(cart, 3)
            bus = Cartridge(payload, 'transport')
            with self.assertRaisesRegex(OSError, 'transport stopped'):
                dump_gb(bus, profile, GbDatabase([record]))
            self.assertEqual(bus.read_banks, [0])
            self.assertEqual(bus.modes, [1, 0])

    def test_cancel_at_second_read_restores_reader_mode(self, sleep):
        for cart in (255, 252):
            profile, payload, record = fixture(cart, 3)
            bus = Cartridge(payload)
            def progress(stage, done, total):
                if stage == 'Read 2/2':
                    raise UserCancelled('cancel')
            with self.assertRaises(UserCancelled):
                dump_gb(bus, profile, GbDatabase([record]), progress)
            self.assertEqual(len(bus.read_banks), len(payload) // 16384)
            self.assertEqual(bus.modes, [1, 0])

    def test_service_capture_reuse_save_verification_and_bundled_titles(self, sleep):
        for cart, cgb in ((255, 0x80), (252, 0)):
            profile, payload, record = fixture(cart, 3, cgb)
            with tempfile.TemporaryDirectory() as folder:
                service = Service(Path(folder), database=Database([]), gb_database=GbDatabase([record]))
                service.adapter = Cartridge(payload)
                for _ in range(3):
                    state = service.poll()
                self.assertTrue(state.supported)
                path, created = service.capture(lambda *args: None)
                self.assertTrue(created)
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(path.suffix, '.gbc' if cgb else '.gb')
                self.assertTrue(verified_file(path, service.profile))
                self.assertEqual(service.existing(), path)
                self.assertFalse(service.capture(lambda *args: None)[1])
        records = GbDatabase.load().index.values()
        self.assertTrue(any('Pocket Camera (Japan)' in r['title'] and r['size'] == 1048576 for r in records))
        self.assertTrue(any('Pokemon Card GB (Japan)' in r['title'] and r['size'] == 1048576 for r in records))
