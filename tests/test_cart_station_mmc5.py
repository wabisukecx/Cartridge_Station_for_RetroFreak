from _paths import ROOT, data  # noqa: F401
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError, packet
from cart_station_nes_registers import NesRegisters
from cart_station_transport import Adapter
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station import Wire, memory
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class Cartridge(Adapter):
    """Independent MMC5 address decoder: modes 0/3, 2 KiB CHR and SRAM.

    Register mapping follows Mesen MMC5 UpdatePrgBanks/UpdateChrBanks.
    Uses the production register whitelist; no transport or real ROM data.
    """
    def __init__(self, prg, chr_data, fault=None):
        self.nes = NesRegisters(self)
        self.prg, self.chr, self.fault = prg, chr_data, fault
        self.regs = {0x5102: 2, 0x5103: 1, 0x5100: 0, 0x5117: 255}
        self.sram = bytearray(bytes(range(256)) * 128)
        self.original_sram = bytes(self.sram)
        self.modes, self.writes = [], []
        self.prg_reads = self.chr_reads = 0

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def _nes_bank_write(self, address, value, dummy):
        self.writes.append((address, value))
        if self.fault == 'transport' and address == 0x5114 and value >= 128:
            raise OSError('transport stopped')
        if 0x6000 <= address < 0x8000:
            if self.regs.get(0x5102) == 2 and self.regs.get(0x5103) == 1:
                self.sram[address - 0x6000] = value
        else:
            self.regs[address] = value
        if dummy:
            self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if address != 0x8000 or size not in (8, 8192):
                raise AssertionError('Unexpected PRG window')
            mode = self.regs.get(0x5100, 0)
            if mode == 0:
                bank = self.regs[0x5117] & 0x7c
            elif mode == 3:
                value = self.regs.get(0x5114, 0)
                if not value & 128:
                    return bytes(self.sram[:size])
                bank = value & 127
            else:
                raise AssertionError('Unexpected PRG mode')
            offset = (bank * 8192) % len(self.prg)
            result = self.prg[offset:offset + size]
            if size == 8192:
                self.prg_reads += 1
                if self.fault == 'prg' or (self.fault == 'second' and self.prg_reads > len(self.prg) // 8192):
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0 or size != 2048 or self.regs.get(0x5101) != 2:
            raise AssertionError('Unexpected CHR window/mode')
        self.chr_reads += 1
        bank = self.regs.get(0x5121, 0) | (self.regs.get(0x5130, 0) << 8)
        offset = bank * 2048
        result = self.chr[offset:offset + size]
        if self.fault == 'chr':
            result = bytes((result[0] ^ 1,)) + result[1:]
        return result


def fixture(prg_count=128, chr_count=256, fault=None):
    prg, chr_data = banks(prg_count, 8192), banks(chr_count, 2048, 41)
    return profile_for(5, prg, chr_data), Cartridge(prg, chr_data, fault), prg + chr_data


class Mmc5Tests(unittest.TestCase):
    def test_register_wire_uses_command12_ack_and_dummy(self):
        for address, value in ((0x5102, 0), (0x5103, 0), (0x5100, 3), (0x5101, 2),
                               (0x5114, 255), (0x5115, 0), (0x5116, 0),
                               (0x5117, 255), (0x5130, 0), (0x5121, 255)):
            wire = Wire(bytes(16) + memory(b'P' * 8), fragment=3)
            Adapter(wire).nes.mmc5_rom_register(address, value)
            self.assertEqual(wire.writes, [packet(0x12, 8, 1, address, 1), bytes((value,)),
                                          packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4)])
            self.assertFalse(wire.replies)

    def test_register_guard_blocks_unlock_and_ram(self):
        for address, value in ((0x5102, 2), (0x5103, 1), (0x5113, 0), (0x5c00, 0),
                               (0x6000, 0), (0x5100, 0), (0x5101, 3), (0x5130, 3),
                               (0x5114, 127), (0x5121, 256), (True, 0), (0x5121, True)):
            wire = Wire(b'')
            with self.assertRaises(ValueError):
                Adapter(wire).nes.mmc5_rom_register(address, value)
            self.assertFalse(wire.writes)
        with self.assertRaises(ValueError):
            Adapter(Wire(b'')).nes.cpu_register_write(0x5114, 128)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_passes_cover_highest_banks_and_preserve_sram(self, sleep):
        profile, bus, raw = fixture()
        self.assertIsNone(profile.support())
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual((bus.prg_reads, bus.chr_reads), (256, 512))
        self.assertEqual(bytes(bus.sram), bus.original_sram)
        self.assertEqual(bus.writes[:3], [(0x5102, 0), (0x5103, 0), (0x5100, 3)])
        self.assertEqual(bus.writes.count((0x5102, 0)), 2)
        self.assertEqual(bus.writes.count((0x5114, 255)), 2)
        self.assertEqual(bus.writes.count((0x5121, 255)), 2)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_original_mode0_cannot_select_8k_prg(self, sleep):
        profile, bus, _ = fixture(32, 64)
        real_write = bus.nes.mmc5_rom_register
        def original_mode(address, value):
            if address == 0x5100:
                bus._nes_bank_write(address, 0, True)
            else:
                real_write(address, value)
        bus.nes.mmc5_rom_register = original_mode
        with self.assertRaisesRegex(ProtocolError, 'PRG.*mismatch'):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_prg_damage_stops_before_chr(self, sleep):
        profile, bus, _ = fixture(4, 4, 'prg')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_chr_damage_cannot_produce_rom(self, sleep):
        profile, bus, _ = fixture(4, 4, 'chr')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_read_damage_cannot_produce_rom(self, sleep):
        profile, bus, _ = fixture(4, 4, 'second')
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.prg_reads, 8)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_error_has_no_retry_and_sram_stays_protected(self, sleep):
        profile, bus, _ = fixture(4, 4, 'transport')
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])
        self.assertEqual(bus.prg_reads, 0)
        self.assertEqual(bytes(bus.sram), bus.original_sram)
        self.assertEqual((bus.regs[0x5102], bus.regs[0x5103]), (0, 0))

    def test_unreviewed_geometries_stop_before_io(self):
        profile, _, _ = fixture(4, 4)
        for changes in (dict(submapper=1), dict(prg_size=24576), dict(prg_size=1056768),
                        dict(chr_size=0), dict(chr_size=1024), dict(chr_size=526336)):
            bus = Mock()
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, **changes))
            self.assertFalse(bus.mock_calls)

    def test_all_25_embedded_mmc5_profiles_are_applicable(self):
        profiles = [p for p in Database.load(data('nes_profiles.reference.json')).profiles if p.mapper == 5]
        self.assertEqual(len(profiles), 25)
        self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_resolution_and_saved_file_verification(self, sleep):
        profile, bus, raw = fixture(4, 4)
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_initial_protection_failure_stops_before_bank_selection(self, sleep):
        profile, bus, _ = fixture(4, 4)
        bus._nes_bank_write = Mock(side_effect=OSError('protection failed'))
        with self.assertRaisesRegex(OSError, 'protection failed'):
            dump_profile(bus, profile)
        bus._nes_bank_write.assert_called_once_with(0x5102, 0, dummy=True)
        self.assertEqual(bus.prg_reads, 0)
        self.assertEqual(bus.modes, [1, 0])
