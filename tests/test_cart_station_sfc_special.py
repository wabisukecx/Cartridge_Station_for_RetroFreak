"""Recovered SFC register sequences on an independent cartridge bus model."""
from _paths import ROOT  # noqa: F401
import hashlib
import unittest
from dataclasses import replace

from cart_station_protocol import ProtocolError, packet
from cart_station_sfc import BLOCK, SfcDatabase, dump_sfc, read_rom
from cart_station_transport import Adapter
from test_cart_station import Wire
from test_cart_station_sfc import Cartridge, fixture


class SpecialCartridge(Cartridge):
    def __init__(self, profile, payload):
        super().__init__(profile, payload)
        self.registers = {}
        self.writes = []

    def snes_rom_register(self, controller, address, value):
        self.registers[address] = value
        self.writes.append((controller, address, value))

    def snes_read(self, address, size):
        if self.profile.controller not in ('Sa1', 'Sdd1') or size != BLOCK:
            return super().snes_read(address, size)
        self.events.append('R,%d,%d' % (address, size))
        if self.profile.controller == 'Sdd1':
            # Each $C0/$D0/$E0/$F0 window maps one physical MiB.
            window = (address >> 20) - 12
            physical = self.registers[0x4804 + window] * 0x100000 + (address & 0xfffff)
        else:
            # SA-1 low-half banks $00/$20/$80/$A0 map four 1 MiB windows.
            bank = address >> 16
            window = ((bank >> 7) * 2) + ((bank & 0x3f) >> 5)
            physical = self.registers[0x2220 + window] * 0x100000
            physical += (bank & 31) * 0x8000 + (address & 0x7fff)
        return self.raw[physical:physical + size]


class SfcSpecialTests(unittest.TestCase):
    def test_special_reads_reconstruct_all_eight_mib_in_both_passes(self):
        for makeup, chip, controller, base in ((0x23, 0x35, 'Sa1', 0x2220),
                                               (0x32, 0x43, 'Sdd1', 0x4804)):
            with self.subTest(controller=controller):
                profile, _, record = fixture(8388608, makeup, chip)
                raw = bytearray(b''.join(bytes([bank]) * BLOCK for bank in range(256)))
                raw[0x7fb0:0x7fe0] = profile.header
                raw = bytes(raw)
                record.update(sha1=hashlib.sha1(raw).hexdigest(), sha256=hashlib.sha256(raw).hexdigest())
                bus = SpecialCartridge(profile, raw)
                self.assertIsNone(profile.support())
                matched, result = dump_sfc(bus, profile, SfcDatabase([record]))
                self.assertEqual(result, raw)
                self.assertEqual(matched.raw_sha1, record['sha1'])
                # Initial map plus one four-register transaction per bank/pass.
                low = [(controller, base + i, i) for i in range(4)]
                high = [(controller, base + i, i + 4) for i in range(4)]
                self.assertEqual(bus.writes, (low + low * 128 + high * 128) * 2)
                self.assertEqual(bus.modes, [1, 0, 1, 0])

    def test_only_recovered_rom_register_values_reach_the_wire(self):
        wire = Wire(bytes(16))
        Adapter(wire).snes_rom_register('Sa1', 0x2220, 4)
        self.assertEqual(wire.writes, [packet(8, 8, 1, 0x2220, 1), b'\x04'])
        for controller, address, value in (
                ('Sa1', 0x2226, 0x80), ('Sa1', 0x2220, 7),
                ('Sdd1', 0x4800, 1), ('Sdd1', 0x4807, 0),
                ('Spc7110', 0x4830, 0x80), ('CX4', 0x7f52, 1),
                ('Sa1', True, 0), ('Sa1', 0x2220, False)):
            wire = Wire(b'')
            with self.subTest(controller=controller, address=address, value=value):
                with self.assertRaises(ValueError):
                    Adapter(wire).snes_rom_register(controller, address, value)
                self.assertEqual(wire.writes, [])

    def test_cx4_rockman_x2_initialization_and_unknown_cx4_rejection(self):
        profile, raw, record = fixture(chip=0xf3, title=b'ROCKMAN X2')
        bus = SpecialCartridge(profile, raw)
        self.assertEqual(profile.chip, 'CX4')
        self.assertIsNone(profile.support())
        matched, result = dump_sfc(bus, profile, SfcDatabase([record]))
        self.assertEqual(result, raw)
        self.assertIsNone(matched.support())
        self.assertEqual(bus.writes, [('CX4', 0x7f52, 0)] * 2)
        self.assertIsNotNone(fixture(chip=0xf3)[0].support())

    def test_spc7110_remains_blocked_before_any_rom_register_access(self):
        profile, raw, record = fixture(makeup=0x3a, chip=0xf9)
        bus = SpecialCartridge(profile, raw)
        self.assertEqual(profile.chip, 'SPC7110 + RTC')
        with self.assertRaisesRegex(ProtocolError, 'dedicated reader'):
            dump_sfc(bus, profile, SfcDatabase([record]))
        self.assertEqual(bus.writes, [])
        self.assertEqual(bus.events, [])

    def test_direct_read_rejects_unsupported_profiles_before_register_writes(self):
        profile, raw, _ = fixture(chip=0xf3, title=b'ROCKMAN X2')
        for unsupported in (replace(profile, controller='HiRom'),
                            replace(profile, prg_size=4194304),
                            fixture(makeup=0x23, chip=0)[0]):
            with self.subTest(controller=unsupported.controller):
                bus = SpecialCartridge(unsupported, raw)
                with self.assertRaises(ProtocolError):
                    read_rom(bus, unsupported, BLOCK)
                self.assertEqual(bus.writes, [])
                self.assertEqual(bus.events, [])


if __name__ == '__main__':
    unittest.main()
