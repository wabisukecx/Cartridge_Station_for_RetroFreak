from _paths import ROOT, data  # noqa: F401
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError, packet
from cart_station_nes_registers import NesRegisters
from cart_station_transport import Adapter
from cart_station_nes_mappers import MapperReader
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station import Wire, memory
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class Cartridge(Adapter):
    """Independent MMC3 inner-page and outer address-line decoder.

    The lower latch accepts writes only while the A001 write gate is open.
    These two boards have ROM and an outer latch, not writable cartridge RAM.
    Mapping equations are checked against Mesen's MMC3_37/MMC3_47 sources.
    """
    def __init__(self, mapper, prg, chr_data, fault=None):
        self.nes = NesRegisters(self)
        self.mapper, self.prg, self.chr, self.fault = mapper, prg, chr_data, fault
        self.block = 4 if mapper == 37 else 1
        self.gate = False
        self.selector = 0
        self.regs = [0] * 8
        self.writes, self.blocks, self.modes = [], [], []
        self.prg_reads = self.chr_reads = 0

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def _nes_bank_write(self, address, value, dummy):
        self.writes.append((address, value))
        if self.fault == 'transport' and address == 0x6000:
            raise OSError('transport stopped')
        if address == 0xa001:
            self.gate = bool(value & 128) and not bool(value & 64)
            if self.fault == 'gate':
                self.gate = False
        elif address == 0x6000:
            if self.gate:
                self.block = value & (7 if self.mapper == 37 else 1)
                self.blocks.append(self.block)
        elif address == 0x8000:
            self.selector = value & 7
        elif address == 0x8001:
            self.regs[self.selector] = value
        else:
            raise AssertionError('Unexpected register or RAM write')
        if dummy:
            self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if address != 0x8000 or size not in (8, 8192):
                raise AssertionError('Wrong PRG window')
            page = self.regs[6]
            if self.mapper == 37:
                if self.block <= 2:
                    page &= 7
                elif self.block == 3:
                    page = (page & 7) | 8
                elif self.block == 7:
                    page = (page & 7) | 32
                else:
                    page = (page & 15) | 16
            else:
                page = (page & 15) | (self.block << 4)
            offset = (page * 8192) % len(self.prg)
            result = self.prg[offset:offset + size]
            if size == 8192:
                self.prg_reads += 1
                if self.fault == 'prg' or (self.fault == 'second' and self.prg_reads > 32):
                    result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0x1000 or size != 1024:
            raise AssertionError('Wrong CHR window')
        self.chr_reads += 1
        page = self.regs[2]
        if self.mapper == 37:
            if self.block >= 4:
                page |= 128
        else:
            page = (page & 127) | (self.block << 7)
        offset = page * 1024
        result = self.chr[offset:offset + size]
        if self.fault == 'chr':
            result = bytes((result[0] ^ 1,)) + result[1:]
        return result


def fixture(mapper, fault=None):
    prg, chr_data = banks(32, 8192), banks(256, 1024, 43)
    return profile_for(mapper, prg, chr_data), Cartridge(mapper, prg, chr_data, fault), prg + chr_data


class MulticartTests(unittest.TestCase):
    def test_compound_wire_enables_latch_selects_then_disables(self):
        for mapper, block in ((37, 8), (37, 11), (37, 12), (47, 0), (47, 1)):
            wire = Wire((bytes(16) + memory(b'P' * 8)) * 3, fragment=3)
            Adapter(wire).nes.mmc3_multicart_select(mapper, block)
            expected = []
            for address, value in ((0xa001, 128), (0x6000, block), (0xa001, 0)):
                expected += [packet(0x12, 8, 1, address, 1), bytes((value,)),
                             packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4)]
            self.assertEqual(wire.writes, expected)
            self.assertFalse(wire.replies)

    def test_guard_rejects_arbitrary_mapper_or_outer_block(self):
        for mapper, block in ((4, 0), (37, 0), (37, 15), (47, 2), (47, -1),
                              (True, 0), (47, True), (37, 256)):
            wire = Wire(b'')
            with self.assertRaises(ValueError):
                Adapter(wire).nes.mmc3_multicart_select(mapper, block)
            self.assertFalse(wire.writes)
        with self.assertRaises(ValueError):
            Adapter(Wire(b'')).nes.cpu_register_write(0x6000, 0)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper37_complete_two_reads(self, sleep):
        profile, bus, raw = fixture(37)
        self.assertIsNone(profile.support())
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual((bus.prg_reads, bus.chr_reads), (64, 512))
        self.assertEqual(bus.modes, [1, 0])
        self.assertFalse(bus.gate)
        self.assertEqual(set(bus.blocks), {0, 3, 4})

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper47_complete_two_reads(self, sleep):
        profile, bus, raw = fixture(47)
        self.assertIsNone(profile.support())
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual((bus.prg_reads, bus.chr_reads), (64, 512))
        self.assertEqual(bus.modes, [1, 0])
        self.assertFalse(bus.gate)
        self.assertEqual(set(bus.blocks), {0, 1})

    def test_mapper37_prg_and_chr_boundary_mapping(self):
        profile, bus, _ = fixture(37)
        reader = MapperReader(bus, profile)
        for bank, outer in ((0, 0), (7, 0), (8, 3), (15, 3), (16, 4), (31, 4)):
            self.assertEqual(reader.prg_bank(bank, 8192), bus.prg[bank*8192:(bank+1)*8192])
            self.assertEqual(bus.block, outer)
        for bank, outer in ((0, 0), (127, 0), (128, 4), (255, 4)):
            self.assertEqual(reader.chr_rom_bank(bank, 1024), bus.chr[bank*1024:(bank+1)*1024])
            self.assertEqual(bus.block, outer)

    def test_mapper47_128k_boundaries_keep_middle_banks_in_first_block(self):
        profile, bus, _ = fixture(47)
        reader = MapperReader(bus, profile)
        for bank, outer in ((7, 0), (8, 0), (15, 0), (16, 1), (31, 1)):
            self.assertEqual(reader.prg_bank(bank, 8192), bus.prg[bank*8192:(bank+1)*8192])
            self.assertEqual(bus.block, outer)
        for bank, outer in ((63, 0), (64, 0), (127, 0), (128, 1), (255, 1)):
            self.assertEqual(reader.chr_rom_bank(bank, 1024), bus.chr[bank*1024:(bank+1)*1024])
            self.assertEqual(bus.block, outer)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_original_mapper47_prg_threshold_fails_full_hash(self, sleep):
        profile, bus, _ = fixture(47)
        def original(reader, bank, size):
            reader.registers.mmc3_multicart_select(47, int(bank >= 8))
            reader.write(0x8000, 6)
            reader.write(0x8001, bank)
            return reader.cpu(0x8000, size)
        with patch.object(MapperReader, 'prg_bank', original), self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 0)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_original_mapper47_chr_threshold_fails_full_hash(self, sleep):
        profile, bus, _ = fixture(47)
        def original(reader, bank, size):
            reader.registers.mmc3_multicart_select(47, int(bank >= 64))
            reader.write(0x8000, 2)
            reader.write(0x8001, bank)
            return reader.ppu(0x1000, size)
        with patch.object(MapperReader, 'chr_rom_bank', original), self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 256)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_disabled_write_gate_prevents_outer_selection_and_fails_hash(self, sleep):
        for mapper in (37, 47):
            profile, bus, _ = fixture(mapper, 'gate')
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.blocks, [])
            self.assertEqual(bus.chr_reads, 0)

    def test_unreviewed_geometries_and_ram_metadata_stop_before_io(self):
        for mapper in (37, 47):
            profile, _, _ = fixture(mapper)
            ram = bytearray(profile.header)
            ram[10] = 7
            battery = bytearray(profile.header)
            battery[6] |= 2
            for changes in (dict(submapper=1), dict(prg_size=131072), dict(prg_size=270336),
                            dict(chr_size=0), dict(chr_size=263168), dict(header=bytes(ram)),
                            dict(header=bytes(battery))):
                bus = Mock()
                with self.subTest(mapper=mapper, changes=changes), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **changes))
                self.assertFalse(bus.mock_calls)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_prg_damage_stops_before_chr(self, sleep):
        for mapper in (37, 47):
            profile, bus, _ = fixture(mapper, 'prg')
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.chr_reads, 0)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_chr_and_second_read_damage_cannot_produce_rom(self, sleep):
        for mapper in (37, 47):
            for fault in ('chr', 'second'):
                profile, bus, _ = fixture(mapper, fault)
                with self.subTest(mapper=mapper, fault=fault), self.assertRaises(ProtocolError):
                    dump_profile(bus, profile)
                self.assertEqual(bus.modes, [1, 0])
                self.assertFalse(bus.gate)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_error_stops_without_retries_or_further_registers(self, sleep):
        for mapper in (37, 47):
            profile, bus, _ = fixture(mapper, 'transport')
            with self.assertRaisesRegex(OSError, 'transport stopped'):
                dump_profile(bus, profile)
            self.assertEqual(bus.writes, [(0xa001, 128), (0x6000, 8 if mapper == 37 else 0)])
            self.assertEqual(bus.prg_reads, 0)
            self.assertEqual(bus.modes, [1, 0])
        adapter = Adapter(Wire(b''))
        adapter.nes.cpu_register_write = Mock(side_effect=OSError('enable failed'))
        adapter._nes_bank_write = Mock()
        with self.assertRaises(OSError):
            adapter.nes.mmc3_multicart_select(47, 0)
        adapter._nes_bank_write.assert_not_called()

    @patch('cart_station_nes_mappers.time.sleep')
    def test_cancellation_stops_and_restores_reader_mode(self, sleep):
        profile, bus, _ = fixture(37)
        def progress(stage, done, total):
            if done >= 8192:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            dump_profile(bus, profile, progress=progress)
        self.assertEqual(bus.prg_reads, 1)
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])
        self.assertFalse(bus.gate)

    def test_two_embedded_profiles_are_applicable(self):
        profiles = [p for p in Database.load(data('nes_profiles.reference.json')).profiles if p.mapper in (37, 47)]
        self.assertEqual(len(profiles), 2)
        self.assertTrue(all(p.support() is None for p in profiles))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidates_and_saved_files_keep_mapper_identity(self, sleep):
        for mapper in (37, 47):
            profile, bus, raw = fixture(mapper)
            other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
            selected, payload = dump_candidates(bus, (other, profile))
            self.assertEqual(selected, profile)
            self.assertEqual(payload, profile.header + raw)
            self.assertEqual((payload[6] >> 4) | (payload[7] & 0xf0), mapper)
            with tempfile.TemporaryDirectory() as folder:
                path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
                self.assertTrue(created)
                self.assertTrue(verified_rom_file(path, profile))
