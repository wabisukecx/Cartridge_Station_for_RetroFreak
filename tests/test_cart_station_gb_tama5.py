from _paths import ROOT  # noqa: F401
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


class ControlWire(Wire):
    """Decode actual command 1C packets into an independent TAMA5 model."""
    def __init__(self, cartridge):
        super().__init__(b'', fragment=3)
        self.cartridge, self.address = cartridge, None

    def write(self, data):
        self.writes.append(data)
        if self.address is None:
            if data not in (packet(0x1c, 8, 1, 0xa000, 1), packet(0x1c, 8, 1, 0xa001, 1)):
                raise AssertionError('Unexpected bank command')
            self.address = int.from_bytes(data[12:16], 'little')
        else:
            if len(data) != 1:
                raise AssertionError('Expected register byte')
            self.cartridge.write_register(self.address, data[0])
            self.address = None
        self.replies.extend(bytes(8))


class Cartridge(Adapter):
    def __init__(self, payload, fault=None):
        super().__init__(ControlWire(self))
        self.payload, self.fault = payload, fault
        # Start with a stale save command and nonzero ROM registers.
        self.selector, self.low, self.high = 7, 9, 1
        self.save, self.clock = bytes(range(32)), bytes(range(16))
        self.original_save, self.original_clock = self.save, self.clock
        self.events, self.modes, self.read_banks = [], [], []

    def status(self):
        return 5

    def mode(self, value):
        self.modes.append(value)

    def gb_register(self, address, value):
        raise AssertionError('Use the dedicated TAMA5 ROM selection transaction')

    def write_register(self, address, value):
        self.events.append(('W', address, value))
        if address == 0xa001:
            if value not in (0, 1):
                raise AssertionError('Save/RTC command selected')
            self.selector = value
        elif address == 0xa000:
            if self.selector not in (0, 1):
                raise AssertionError('Data written with stale save/RTC selector')
            if self.fault == 'transport' and self.selector == 0 and value == 1:
                raise OSError('transport stopped')
            if self.fault == 'bank':
                return
            if self.selector == 0:
                self.low = value & 15
            else:
                self.high = value & 1
        else:
            raise AssertionError('Unexpected address')

    def gb_read(self, address, size):
        self.events.append(('R', address, size))
        if not (0 <= address < 0x8000 and address + size <= 0x8000):
            raise AssertionError('Read outside ROM')
        bank = 0 if address < 0x4000 else (self.high << 4) | self.low
        start = bank * 16384 + (address & 16383)
        result = self.payload[start:start + size]
        if size == 16384:
            self.read_banks.append(bank)
            if self.fault == 'second' and len(self.read_banks) > len(self.payload) // 16384:
                result = result[:-1] + bytes((result[-1] ^ 1,))
        return result


@patch('cart_station_gb.time.sleep')
class Tama5Tests(unittest.TestCase):
    def test_header_probe_and_display(self, sleep):
        profile, payload, _ = fixture(0xfd, 4)
        self.assertEqual(profile.controller, 'TAMA5')
        self.assertIsNone(profile.support())
        self.assertIn('TAMA5', profile_details(profile))
        bus = Cartridge(payload)
        self.assertEqual(inspect_header(bus), profile)
        self.assertEqual(bus.events, [('R', 0x100, 80)] * 2)

    def test_unreviewed_geometry_or_type_stops_before_io(self, sleep):
        profile, _, _ = fixture(0xfd, 4)
        for size in (0, 16384, 49152, 1048576, 2097152, 8388608):
            bus = Mock()
            with self.assertRaises(ProtocolError):
                dump_gb(bus, replace(profile, prg_size=size), GbDatabase([]))
            self.assertEqual(bus.mock_calls, [])
        other, _, _ = fixture(1, 4)
        self.assertIsNotNone(replace(profile, header=other.header).support())

    def test_all_32_banks_twice_through_actual_control_packets(self, sleep):
        profile, payload, record = fixture(0xfd, 4)
        bus = Cartridge(payload)
        matched, result = dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(result, payload)
        self.assertEqual(matched.raw_sha1, record['sha1'])
        self.assertEqual(bus.read_banks, list(range(32)) * 2)
        self.assertEqual(bus.modes, [1, 0])
        self.assertFalse(bus.transport.replies)

    def test_smaller_reviewed_sizes(self, sleep):
        for code in range(4):
            profile, payload, record = fixture(0xfd, code)
            bus = Cartridge(payload)
            self.assertEqual(dump_gb(bus, profile, GbDatabase([record]))[1], payload)
            self.assertEqual(bus.read_banks, list(range(len(payload) // 16384)) * 2)

    def test_stale_selector_and_save_clock_preservation(self, sleep):
        profile, payload, record = fixture(0xfd, 4)
        bus = Cartridge(payload)
        dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(bus.save, bus.original_save)
        self.assertEqual(bus.clock, bus.original_clock)
        self.assertTrue(all(e[2] in (0, 1) for e in bus.events if e[:2] == ('W', 0xa001)))
        self.assertEqual(bus.events[1], ('W', 0xa001, 1))

    def test_boundary_bank_packets_at_15_16_31(self, sleep):
        for bank in (1, 15, 16, 31):
            wire = Wire(bytes(64), fragment=3)
            Adapter(wire).gb_tama5_bank(bank)
            expected = []
            for address, value in ((0xa001, 1), (0xa000, bank // 16),
                                   (0xa001, 0), (0xa000, bank % 16)):
                expected.extend((packet(0x1c, 8, 1, address, 1), bytes((value,))))
            self.assertEqual(wire.writes, expected)
            self.assertFalse(wire.replies)

    def test_bank_zero_literal_reference_sequence_and_fixed_window(self, sleep):
        _, payload, _ = fixture(0xfd, 4)
        bus = Cartridge(payload)
        bus.gb_tama5_bank(0)
        self.assertEqual(bus.events, [('W', 0xa001, 1), ('W', 0xa000, 0), ('W', 0xa001, 0)])
        # Low selection remains stale, so zero must be read from the fixed window.
        self.assertEqual(bus.low, 9)
        self.assertEqual(bus.gb_read(0, 16384), payload[:16384])

    def test_invalid_bank_values_send_nothing(self, sleep):
        for bank in (-1, 32, 255, True, 1.0, '1', None):
            wire = Wire(b'')
            with self.assertRaises(ValueError):
                Adapter(wire).gb_tama5_bank(bank)
            self.assertEqual(wire.writes, [])

    def test_generic_bank_controls_still_reject_tama_ports(self, sleep):
        for address in (0xa000, 0xa001):
            for value in (0, 1, 4, 7, 10):
                wire = Wire(b'')
                with self.assertRaises(ValueError):
                    Adapter(wire).gb_register(address, value)
                with self.assertRaises(ValueError):
                    Adapter(wire).gb_mbc6_register(address, value)
                self.assertEqual(wire.writes, [])

    def test_truncated_ack_stops_selection_without_retry(self, sleep):
        # Every ready/completion boundary of the four-write transaction.
        for complete_acks in range(8):
            wire = Wire(bytes(8 * complete_acks + 3), fragment=3)
            with self.assertRaises(ProtocolError):
                Adapter(wire).gb_tama5_bank(31)
            self.assertEqual(len(wire.writes), complete_acks + 1)

    def test_second_read_damage_stops(self, sleep):
        profile, payload, record = fixture(0xfd, 3)
        bus = Cartridge(payload, 'second')
        with self.assertRaisesRegex(ProtocolError, 'Two GB ROM reads differ'):
            dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(bus.modes, [1, 0])

    def test_unknown_hash_and_wrong_bank_stop_without_fallback(self, sleep):
        profile, payload, record = fixture(0xfd, 4)
        for fault, records in ((None, []), ('bank', [record])):
            bus = Cartridge(payload, fault)
            with self.assertRaisesRegex(ProtocolError, 'not in the database'):
                dump_gb(bus, profile, GbDatabase(records))
            self.assertEqual(len(bus.read_banks), 64)
            self.assertEqual(bus.modes, [1, 0])

    def test_transport_failure_does_not_continue_and_restores_mode(self, sleep):
        profile, payload, record = fixture(0xfd, 4)
        bus = Cartridge(payload, 'transport')
        with self.assertRaisesRegex(ProtocolError, 'transport stopped'):
            dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(bus.read_banks, [0])
        self.assertEqual(bus.modes, [1, 0])

    def test_cancellation_and_cartridge_change_cleanup(self, sleep):
        profile, payload, record = fixture(0xfd, 4)
        bus = Cartridge(payload)
        def progress(stage, done, total):
            if stage == 'Read 2/2':
                raise UserCancelled('cancel')
        with self.assertRaises(UserCancelled):
            dump_gb(bus, profile, GbDatabase([record]), progress)
        self.assertEqual(bus.read_banks, list(range(32)))
        self.assertEqual(bus.modes, [1, 0])
        bus = Cartridge(payload[:0x134] + b'X' + payload[0x135:])
        with self.assertRaisesRegex(ProtocolError, 'cartridge changed'):
            dump_gb(bus, profile, GbDatabase([record]))
        self.assertFalse(bus.transport.writes)

    def test_service_capture_saved_identity_and_reuse(self, sleep):
        _, payload, record = fixture(0xfd, 4)
        with tempfile.TemporaryDirectory() as folder:
            service = Service(Path(folder), database=Database([]), gb_database=GbDatabase([record]))
            service.adapter = Cartridge(payload)
            for _ in range(3):
                state = service.poll()
            self.assertTrue(state.supported)
            path, created = service.capture(lambda *args: None)
            self.assertTrue(created)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(path.suffix, '.gb')
            self.assertTrue(verified_file(path, service.profile))
            self.assertEqual(service.existing(), path)
            self.assertFalse(service.capture(lambda *args: None)[1])

    def test_bundled_osutchi_mesutchi_record_is_present(self, sleep):
        self.assertTrue(any(r['title'] == 'Game de Hakken!! Tamagotchi - Osutchi to Mesutchi (Japan) (SGB Enhanced).gb'
                            and r['size'] == 524288 for r in GbDatabase.load().index.values()))
