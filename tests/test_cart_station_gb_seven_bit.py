from _paths import ROOT  # noqa: F401
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_gb import GbDatabase, dump_gb, inspect_header, verified_file
from cart_station_nes_db import Database
from cart_station_protocol import ProtocolError, packet
from cart_station_service import Service, UserCancelled
from cart_station_transport import Adapter
from cart_station_presentation import profile_details
from test_cart_station import Wire
from test_cart_station_gb import fixture


class Cartridge:
    """Seven-bit ROM address decoder; RAM/RTC/EEPROM are separate spaces."""
    def __init__(self, payload, fault=None):
        self.payload, self.fault = payload, fault
        self.cart = payload[0x147]
        self.bank, self.ram_bank = 1, 3
        self.peripherals = bytes(range(256)) * 128
        self.original_peripherals = self.peripherals
        self.events, self.modes, self.read_banks = [], [], []

    def status(self):
        return 5

    def mode(self, value):
        self.modes.append(value)

    def gb_register(self, address, value):
        self.events.append(('W', address, value))
        if self.fault == 'transport' and value == 1:
            raise OSError('transport stopped')
        if 0x2000 <= address <= 0x3fff:
            if self.fault != 'bank':
                self.bank = value & 127
        elif 0x4000 <= address <= 0x5fff and self.cart == 0xfe:
            self.ram_bank = value & 3
        else:
            raise AssertionError('Unexpected peripheral enable or data write')

    def gb_read(self, address, size):
        self.events.append(('R', address, size))
        if not (0 <= address < 0x8000 and address + size <= 0x8000):
            raise AssertionError('Read outside ROM')
        bank = 0 if address < 0x4000 else self.bank
        start = bank * 16384 + (address & 16383)
        result = self.payload[start:start + size]
        if size == 16384:
            self.read_banks.append(bank)
            if self.fault == 'second' and len(self.read_banks) > len(self.payload) // 16384:
                result = result[:-1] + bytes((result[-1] ^ 1,))
        return result


@patch('cart_station_gb.time.sleep')
class SevenBitTests(unittest.TestCase):
    def test_header_types_and_presentation(self, sleep):
        for cart, name in ((0xfe, 'HuC3'), (0x22, 'MBC7')):
            p, _, _ = fixture(cart, 6)
            self.assertEqual(p.controller, name)
            self.assertIsNone(p.support())
            self.assertIn(name, profile_details(p))

    def test_reviewed_geometry_only_before_io(self, sleep):
        for cart in (0xfe, 0x22):
            p, _, _ = fixture(cart, 6)
            for size in (0, 16384, 49152, 4194304, 8388608):
                bus = Mock()
                with self.assertRaises(ProtocolError):
                    dump_gb(bus, replace(p, prg_size=size), GbDatabase([]))
                self.assertEqual(bus.mock_calls, [])
            other, _, _ = fixture(1, 6)
            self.assertIsNotNone(replace(p, header=other.header).support())

    def test_huc3_all_128_banks_twice(self, sleep):
        p, payload, record = fixture(0xfe, 6)
        bus = Cartridge(payload)
        matched, result = dump_gb(bus, p, GbDatabase([record]))
        self.assertEqual(result, payload)
        self.assertEqual(matched.raw_sha1, record['sha1'])
        self.assertEqual(bus.read_banks, list(range(128)) * 2)
        self.assertIn(('W', 0x2100, 64), bus.events)
        self.assertEqual(bus.modes, [1, 0])

    def test_mbc7_all_128_banks_twice_without_mbc5_high_write(self, sleep):
        p, payload, record = fixture(0x22, 6, 0x80)
        bus = Cartridge(payload)
        _, result = dump_gb(bus, p, GbDatabase([record]))
        self.assertEqual(result, payload)
        self.assertEqual(bus.read_banks, list(range(128)) * 2)
        self.assertTrue(all(e[1] == 0x2100 for e in bus.events if e[0] == 'W'))

    def test_inherited_huc1_trace_loses_bank_64(self, sleep):
        self.check_original_trace(0xfe, 'HuC3', 64)

    def test_inherited_mbc5_trace_overwrites_bank_one(self, sleep):
        self.check_original_trace(0x22, 'MBC7', 1)

    def check_original_trace(self, cart, name, boundary):
        _, payload, _ = fixture(cart, 6)
        reference = json.loads((ROOT / 'tests/fixtures/gb_reference_traces.json').read_text())
        case = next(c for c in reference['cases'] if c['controller'] == name)
        bus, parts = Cartridge(payload), []
        for entry in case['trace']:
            operation, address, value = entry.split(',')
            if operation == 'W':
                bus.gb_register(int(address), int(value))
            else:
                parts.append(bus.gb_read(int(address), int(value)))
        self.assertNotEqual(bus.read_banks[boundary], boundary)
        self.assertNotEqual(hashlib.sha1(b''.join(parts)).digest(), hashlib.sha1(payload).digest())

    def test_peripherals_untouched_and_bank_zero_uses_fixed_window(self, sleep):
        for cart in (0xfe, 0x22):
            p, payload, record = fixture(cart, 3)
            bus = Cartridge(payload)
            dump_gb(bus, p, GbDatabase([record]))
            self.assertEqual(bus.peripherals, bus.original_peripherals)
            self.assertFalse(any(e[0] == 'W' and e[1] < 0x2000 for e in bus.events))
            self.assertEqual(bus.events[1], ('W', 0x2100, 0))
            if cart == 0xfe:
                self.assertTrue(all(e[2] == 0 for e in bus.events if e[:2] == ('W', 0x4000)))

    def test_header_probe_has_no_bank_or_peripheral_write(self, sleep):
        for cart in (0xfe, 0x22):
            p, payload, _ = fixture(cart, 3)
            bus = Cartridge(payload)
            self.assertEqual(inspect_header(bus), p)
            self.assertEqual(bus.events, [('R', 0x100, 80)] * 2)

    def test_wire_controls_and_forbidden_peripherals(self, sleep):
        for address, value in ((0x2100, 64), (0x2100, 127), (0x4000, 0)):
            wire = Wire(bytes(16), fragment=3)
            Adapter(wire).gb_register(address, value)
            self.assertEqual(wire.writes, [packet(0x1c, 8, 1, address, 1), bytes((value,))])
            self.assertFalse(wire.replies)
        for address in (0, 0xa000, 0xa080):
            with self.assertRaises(ValueError):
                Adapter(Wire(b'')).gb_register(address, 0)

    def test_second_pass_corruption_stops(self, sleep):
        for cart in (0xfe, 0x22):
            p, payload, record = fixture(cart, 3)
            bus = Cartridge(payload, 'second')
            with self.assertRaisesRegex(ProtocolError, 'Two GB ROM reads differ'):
                dump_gb(bus, p, GbDatabase([record]))
            self.assertEqual(bus.modes, [1, 0])

    def test_unknown_hash_and_wrong_banks_stop_without_variant_retry(self, sleep):
        for cart in (0xfe, 0x22):
            p, payload, record = fixture(cart, 3)
            for fault, records in ((None, []), ('bank', [record])):
                bus = Cartridge(payload, fault)
                with self.assertRaisesRegex(ProtocolError, 'not in the database'):
                    dump_gb(bus, p, GbDatabase(records))
                self.assertEqual(len(bus.read_banks), len(payload) // 16384 * 2)
                self.assertEqual(bus.modes, [1, 0])

    def test_transport_failure_and_cancellation_cleanup(self, sleep):
        for cart in (0xfe, 0x22):
            p, payload, record = fixture(cart, 3)
            bus = Cartridge(payload, 'transport')
            with self.assertRaises(OSError):
                dump_gb(bus, p, GbDatabase([record]))
            self.assertEqual(bus.read_banks, [0])
            self.assertEqual(bus.modes, [1, 0])
            bus = Cartridge(payload)
            def progress(stage, done, total):
                if stage == 'Read 2/2':
                    raise UserCancelled('cancel')
            with self.assertRaises(UserCancelled):
                dump_gb(bus, p, GbDatabase([record]), progress)
            self.assertEqual(len(bus.read_banks), len(payload) // 16384)
            self.assertEqual(bus.modes, [1, 0])

    def test_changed_header_stops_before_bank_write(self, sleep):
        for cart in (0xfe, 0x22):
            p, payload, record = fixture(cart, 3)
            bus = Cartridge(payload[:0x134] + b'X' + payload[0x135:])
            with self.assertRaisesRegex(ProtocolError, 'cartridge changed'):
                dump_gb(bus, p, GbDatabase([record]))
            self.assertFalse(any(e[0] == 'W' for e in bus.events))

    def test_service_capture_save_identity_and_reuse(self, sleep):
        for cart, cgb in ((0xfe, 0), (0x22, 0x80)):
            _, payload, record = fixture(cart, 3, cgb)
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
