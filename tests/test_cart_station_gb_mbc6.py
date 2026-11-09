from _paths import ROOT  # noqa: F401
import hashlib
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
from test_cart_station import Wire
from test_cart_station_gb import fixture


def cartridge_fixture(size_code=5):
    profile, original, record = fixture(0x20, size_code, 0x80)
    # Each 8 KiB half is distinct: reversing or aliasing the two windows
    # must not accidentally pass a test based on repeated 16 KiB patterns.
    payload = bytearray().join(bytes((bank, bank ^ 0xa5)) * 4096
                               for bank in range(len(original) // 8192))
    payload[0x100:0x150] = profile.header
    payload = bytes(payload)
    record.update(sha1=hashlib.sha1(payload).hexdigest(),
                  sha256=hashlib.sha256(payload).hexdigest())
    return profile, payload, record


class Cartridge:
    """Independent MBC6 decoder with two ROM/flash and two RAM windows."""
    def __init__(self, payload, fault=None):
        self.payload, self.fault = payload, fault
        self.banks, self.flash_selected = [7, 11], [True, True]
        self.flash = bytes((0xed,)) * 1048576
        self.ram = bytes(range(256)) * 128
        self.original_flash, self.original_ram = self.flash, self.ram
        self.events, self.modes, self.read_banks = [], [], []

    def status(self):
        return 5

    def mode(self, value):
        self.modes.append(value)

    def gb_register(self, address, value):
        raise AssertionError('MBC6 must use dedicated ROM-only controls')

    def gb_mbc6_register(self, address, value):
        self.events.append(('W', address, value))
        if self.fault == 'transport' and address == 0x3000 and value == 3:
            raise OSError('transport stopped')
        if address in (0x2800, 0x3800):
            if value != 0:
                raise AssertionError('Flash selected')
            self.flash_selected[address == 0x3800] = False
        elif address in (0x2000, 0x3000):
            self.banks[address == 0x3000] = value & 127
        else:
            raise AssertionError('Unexpected RAM/flash access')

    def gb_read(self, address, size):
        self.events.append(('R', address, size))
        if address == 0x100 and size == 80:
            return self.payload[0x100:0x150]
        if address != 0x4000 or size != 16384:
            raise AssertionError('Unexpected ROM read')
        parts = []
        for window in (0, 1):
            bank = self.banks[window ^ (self.fault == 'swap')]
            self.read_banks.append(bank)
            data = self.flash if self.flash_selected[window] else self.payload
            start = bank * 8192
            parts.append(data[start:start + 8192])
        result = b''.join(parts)
        if self.fault == 'second' and len(self.read_banks) > len(self.payload) // 8192:
            result = result[:-1] + bytes((result[-1] ^ 1,))
        return result


@patch('cart_station_gb.time.sleep')
class Mbc6Tests(unittest.TestCase):
    def test_header_and_probe_without_register_writes(self, sleep):
        profile, payload, _ = cartridge_fixture()
        self.assertEqual(profile.controller, 'MBC6')
        self.assertIsNone(profile.support())
        bus = Cartridge(payload)
        self.assertEqual(inspect_header(bus), profile)
        self.assertEqual(bus.events, [('R', 0x100, 80)] * 2)

    def test_geometry_and_header_type_stop_before_io(self, sleep):
        profile, _, _ = cartridge_fixture()
        for size in (0, 16384, 49152, 2097152, 4194304, 8388608):
            bus = Mock()
            with self.assertRaises(ProtocolError):
                dump_gb(bus, replace(profile, prg_size=size), GbDatabase([]))
            self.assertEqual(bus.mock_calls, [])
        other, _, _ = fixture(1, 5)
        self.assertIsNotNone(replace(profile, header=other.header).support())

    def test_all_128_eight_kib_banks_in_order_twice(self, sleep):
        profile, payload, record = cartridge_fixture()
        bus = Cartridge(payload)
        matched, result = dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(result, payload)
        self.assertEqual(matched.raw_sha1, record['sha1'])
        self.assertEqual(bus.read_banks, list(range(128)) * 2)
        self.assertEqual(bus.modes, [1, 0])
        self.assertEqual(bus.events[1:6], [('W', 0x2800, 0), ('W', 0x3800, 0),
                         ('W', 0x2000, 0), ('W', 0x3000, 1), ('R', 0x4000, 16384)])

    def test_minimum_and_intermediate_supported_sizes(self, sleep):
        for code in range(5):
            profile, payload, record = cartridge_fixture(code)
            bus = Cartridge(payload)
            self.assertEqual(dump_gb(bus, profile, GbDatabase([record]))[1], payload)
            self.assertEqual(bus.read_banks, list(range(len(payload) // 8192)) * 2)

    def test_stale_flash_selection_is_cleared_and_data_preserved(self, sleep):
        profile, payload, record = cartridge_fixture(3)
        bus = Cartridge(payload)
        dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(bus.flash_selected, [False, False])
        self.assertEqual(bus.flash, bus.original_flash)
        self.assertEqual(bus.ram, bus.original_ram)
        self.assertTrue(all(e[1] in (0x2800, 0x3800, 0x2000, 0x3000)
                            for e in bus.events if e[0] == 'W'))

    def test_reference_two_mib_limit_would_wrap_seven_bit_banks(self, sleep):
        # Reproduce the original bank mask at the first out-of-range pair.
        _, payload, _ = cartridge_fixture()
        bus = Cartridge(payload)
        for address, value in ((0x2800, 0), (0x3800, 0),
                               (0x2000, (64 * 2) & 127),
                               (0x3000, (64 * 2 + 1) & 127)):
            bus.gb_mbc6_register(address, value)
        self.assertEqual(bus.gb_read(0x4000, 16384), payload[:16384])
        self.assertEqual(bus.read_banks, [0, 1])

    def test_wire_commands_consume_ack_without_dummy_read(self, sleep):
        for address, value in ((0x2800, 0), (0x3800, 0), (0x2000, 126), (0x3000, 127)):
            wire = Wire(bytes(16), fragment=3)
            Adapter(wire).gb_mbc6_register(address, value)
            self.assertEqual(wire.writes, [packet(0x1c, 8, 1, address, 1), bytes((value,))])
            self.assertFalse(wire.replies)

    def test_dedicated_controls_reject_flash_ram_and_invalid_types(self, sleep):
        pairs = [(a, 0) for a in (0, 0x400, 0x800, 0xc00, 0x1000, 0x4000, 0x6000, 0xa000)]
        pairs += [(0x2800, 8), (0x3800, 8), (0x2000, 128), (0x3000, 128),
                  (0x2000, -1), (0x2000, True), (0x2800, 0.0), ('8192', 0)]
        for address, value in pairs:
            wire = Wire(b'')
            with self.assertRaises(ValueError):
                Adapter(wire).gb_mbc6_register(address, value)
            self.assertEqual(wire.writes, [])

    def test_generic_gb_register_limits_remain_unchanged(self, sleep):
        for address, value in ((0x2800, 0), (0x3800, 0), (0x2000, 1), (0x3000, 2)):
            wire = Wire(b'')
            with self.assertRaises(ValueError):
                Adapter(wire).gb_register(address, value)
            self.assertEqual(wire.writes, [])

    def test_second_pass_damage_stops(self, sleep):
        profile, payload, record = cartridge_fixture(3)
        bus = Cartridge(payload, 'second')
        with self.assertRaisesRegex(ProtocolError, 'Two GB ROM reads differ'):
            dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(bus.modes, [1, 0])

    def test_unknown_hash_and_reversed_windows_stop_without_variant_retry(self, sleep):
        profile, payload, record = cartridge_fixture(3)
        for fault, records in ((None, []), ('swap', [record])):
            bus = Cartridge(payload, fault)
            # A swapped first pair fails the header check before DB lookup.
            with self.assertRaises(ProtocolError):
                dump_gb(bus, profile, GbDatabase(records))
            self.assertEqual(len(bus.read_banks), len(payload) // 8192 * (2 if fault is None else 1))
            self.assertEqual(bus.modes, [1, 0])

    def test_transport_failure_and_cancel_restore_mode(self, sleep):
        profile, payload, record = cartridge_fixture(3)
        bus = Cartridge(payload, 'transport')
        with self.assertRaises(OSError):
            dump_gb(bus, profile, GbDatabase([record]))
        self.assertEqual(bus.read_banks, [0, 1])
        self.assertEqual(bus.modes, [1, 0])
        bus = Cartridge(payload)
        def progress(stage, done, total):
            if stage == 'Read 2/2':
                raise UserCancelled('cancel')
        with self.assertRaises(UserCancelled):
            dump_gb(bus, profile, GbDatabase([record]), progress)
        self.assertEqual(len(bus.read_banks), len(payload) // 8192)
        self.assertEqual(bus.modes, [1, 0])

    def test_changed_cartridge_stops_before_register_write(self, sleep):
        profile, payload, record = cartridge_fixture(3)
        bus = Cartridge(payload[:0x134] + b'X' + payload[0x135:])
        with self.assertRaisesRegex(ProtocolError, 'cartridge changed'):
            dump_gb(bus, profile, GbDatabase([record]))
        self.assertFalse(any(e[0] == 'W' for e in bus.events))

    def test_service_capture_saved_hash_reuse_and_bundled_record(self, sleep):
        _, payload, record = cartridge_fixture(3)
        with tempfile.TemporaryDirectory() as folder:
            service = Service(Path(folder), database=Database([]), gb_database=GbDatabase([record]))
            service.adapter = Cartridge(payload)
            for _ in range(3):
                state = service.poll()
            self.assertTrue(state.supported)
            path, created = service.capture(lambda *args: None)
            self.assertTrue(created)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(path.suffix, '.gbc')
            self.assertTrue(verified_file(path, service.profile))
            self.assertEqual(service.existing(), path)
            self.assertFalse(service.capture(lambda *args: None)[1])
        self.assertTrue(any(r['title'] == 'Net de Get - Minigame @ 100 (Japan).gbc'
                            and r['size'] == 1048576 for r in GbDatabase.load().index.values()))
