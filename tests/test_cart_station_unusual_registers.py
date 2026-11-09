from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter
from cart_station_nes_mappers import MapperReader
from cart_station_nes_db import Database, dump_profile, dump_candidates, verified_rom_file
from cart_station_storage import save_verified_rom
from test_cart_station import Wire, memory
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class Cartridge:
    """NINA lower windows, StudyBox fixed bank zero, and two 406 wire layouts."""
    def __init__(self, mapper, prg, chr_data, layout='AXR', fault=None):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.layout, self.fault = layout, fault
        self.prg_unit = {34: 32768, 186: 16384, 406: 8192}[mapper]
        self.chr_unit = 4096 if mapper == 34 else 1024
        self.prg_bank = self.chr_bank = 0
        self.selector = -1
        self.active_layout = False
        self.prg_reads = self.chr_reads = self.good_prg_reads = 0
        self.modes, self.writes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)
        if value == 1:
            self.active_layout = False

    def nina001_rom_register(self, address, value):
        if self.mapper != 34 or address not in (0x7ffd, 0x7ffe):
            raise AssertionError('Wrong NINA register')
        self.writes.append((address, value))
        if address == 0x7ffd:
            self.prg_bank = value
        else:
            self.chr_bank = value
        self.memory(0x11, 0x8000, 8)

    def studybox_select_prg(self, bank):
        if self.mapper != 186 or not 1 <= bank <= 255:
            raise AssertionError('StudyBox fixed bank zero must not be selected through IO')
        self.writes.append((0x4201, bank))
        self.prg_bank = bank
        self.memory(0x11, 0x8000, 8)

    def cpu_register_write(self, address, value, dummy=True):
        if self.mapper != 406 or not dummy or address not in (0x8000, 0x8002, 0xe000, 0xe001):
            raise AssertionError('Unexpected register, RAM, or flash-program address')
        self.writes.append((address, value))
        select, register = (0x8000, 0x8002) if self.layout == 'AXR' else (0xe000, 0xe001)
        if address == select:
            if value not in (2, 6):
                raise AssertionError('Unexpected selector')
            self.selector, self.active_layout = value, True
        elif address == register:
            if self.selector == 6:
                self.prg_bank = value
            elif self.selector == 2:
                self.chr_bank = value
        self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if address == 0x8000 and size == 8:
                return self.prg[:8]
            if size != self.prg_unit:
                raise AssertionError('Wrong PRG unit')
            if self.mapper == 186:
                bank = 0 if address == 0xc000 else self.prg_bank
                if address not in (0x8000, 0xc000):
                    raise AssertionError('Wrong StudyBox window')
            else:
                if address != 0x8000:
                    raise AssertionError('Wrong PRG window')
                bank = self.prg_bank
            self.prg_reads += 1
            result = self.prg[bank * size:(bank + 1) * size]
            if self.mapper != 406 or self.active_layout:
                self.good_prg_reads += 1
            if self.fault == 'always' or (self.fault == 'second' and self.good_prg_reads > len(self.prg) // size):
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or self.mapper == 186 or size != self.chr_unit:
            raise AssertionError('Wrong CHR unit or CHR-RAM read')
        if address != (0 if self.mapper == 34 else 0x1000):
            raise AssertionError('Wrong CHR window')
        self.chr_reads += 1
        offset = self.chr_bank * self.chr_unit
        return self.chr[offset:offset + size]

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, layout='AXR', fault=None):
    unit = {34: 32768, 186: 16384, 406: 8192}[mapper]
    count = {34: 2, 186: 16, 406: 64}[mapper]
    prg = banks(count, unit)
    chr_data = b'' if mapper == 186 else banks(16 if mapper == 34 else 256, 4096 if mapper == 34 else 1024, 43)
    return (profile_for(mapper, prg, chr_data, 1 if mapper == 34 else 0),
            Cartridge(mapper, prg, chr_data, layout, fault), prg + chr_data)


class UnusualRegisterTests(unittest.TestCase):
    def test_nina_and_studybox_wire_commands_include_dummy_reads(self):
        for name, args, address, value in (('nina001_rom_register', (0x7ffd, 1), 0x7ffd, 1),
                ('nina001_rom_register', (0x7ffe, 15), 0x7ffe, 15), ('studybox_select_prg', (255,), 0x4201, 255)):
            wire = Wire(bytes(16) + memory(b'P' * 8), fragment=3)
            getattr(Adapter(wire).nes, name)(*args)
            self.assertEqual(wire.writes, [packet(0x12, 8, 1, address, 1), bytes((value,)),
                                          packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4)])
            self.assertFalse(wire.replies)

    def test_lower_guards_do_not_expose_general_ram_or_io_writes(self):
        for name, args in (('nina001_rom_register', (0x7fff, 0)), ('nina001_rom_register', (0x6000, 0)),
                ('nina001_rom_register', (0x7ffd, 2)), ('nina001_rom_register', (0x7ffe, 16)),
                ('nina001_rom_register', (True, 0)), ('nina001_rom_register', (0x7ffd, True)),
                ('studybox_select_prg', (0,)), ('studybox_select_prg', (256,)), ('studybox_select_prg', (True,))):
            wire = Wire(b'')
            with self.assertRaises(ValueError):
                getattr(Adapter(wire).nes, name)(*args)
            self.assertFalse(wire.writes)
        wire = Wire(b'')
        for address in (0x4201, 0x7ffd, 0x7ffe):
            with self.assertRaises(ValueError):
                Adapter(wire).nes.cpu_register_write(address, 0)
        self.assertFalse(wire.writes)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_nina_two_passes_use_32k_prg_and_4k_chr(self, sleep):
        profile, bus, raw = fixture(34)
        self.assertIsNone(profile.support())
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual((bus.prg_reads, bus.chr_reads), (4, 32))
        self.assertEqual(bus.writes[:2], [(0x7ffd, 0), (0x7ffd, 1)])
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_studybox_fixed_first_bank_has_no_io_write(self, sleep):
        profile, bus, raw = fixture(186)
        self.assertEqual(MapperReader(bus, profile).prg_bank(0, 16384), bus.prg[:16384])
        self.assertFalse(bus.writes)
        profile, bus, raw = fixture(186)
        self.assertEqual(dump_profile(bus, profile), profile.header + raw)
        self.assertEqual([v for a, v in bus.writes], list(range(1, 16)) * 2)
        self.assertEqual(bus.prg_reads, 32)
        self.assertFalse(bus.chr_reads)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapper406_full_hashes_choose_axr_or_inl_and_read_twice(self, sleep):
        for layout in ('AXR', 'INL'):
            profile, bus, raw = fixture(406, layout)
            progress = []
            self.assertIsNone(profile.support())
            self.assertEqual(dump_profile(bus, profile, progress=lambda *v: progress.append(v)), profile.header + raw)
            self.assertEqual(bus.good_prg_reads, 128)
            self.assertEqual(bus.chr_reads, 512)
            self.assertEqual(bus.modes, [1, 0] if layout == 'AXR' else [1, 0, 1, 0])
            self.assertEqual(progress[-1][1], 2 * len(raw))
            self.assertTrue(all(a in (0x8000, 0x8002, 0xe000, 0xe001) for a, v in bus.writes))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_hash_damage_and_second_pass_corruption_cannot_return_a_rom(self, sleep):
        for mapper in (34, 186, 406):
            for fault in ('always', 'second'):
                profile, bus, _ = fixture(mapper, fault=fault)
                with self.subTest(mapper=mapper, fault=fault), self.assertRaises(ProtocolError):
                    dump_profile(bus, profile)
                self.assertEqual(bus.modes[-1], 0)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_error_is_never_retried_as_another_406_layout(self, sleep):
        profile, bus, _ = fixture(406, 'INL')
        bus.cpu_register_write = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.cpu_register_write.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])

    def test_unsupported_geometries_and_submappers_stop_before_io(self):
        for mapper in (34, 186, 406):
            profile, _, _ = fixture(mapper)
            changes = [dict(submapper=3), dict(prg_size=24576), dict(chr_size=1)]
            changes.append(dict(prg_size={34: 98304, 186: 4210688, 406: 2105344}[mapper]))
            changes.append(dict(chr_size={34: 69632, 186: 8192, 406: 263168}[mapper]))
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_unknown_internal_406_variant_is_rejected_before_writing(self):
        profile, _, _ = fixture(406)
        bus = Mock()
        reader = MapperReader(bus, profile)
        reader.register_variant = 'unknown'
        with self.assertRaises(ProtocolError):
            reader.prg_bank(0, 8192)
        self.assertFalse(bus.mock_calls)

    def test_three_embedded_profiles_are_newly_applicable(self):
        db = Database.load(data('nes_profiles.reference.json'))
        nina = [p for p in db.profiles if p.mapper == 34 and p.submapper == 1]
        for profiles in (nina, [p for p in db.profiles if p.mapper == 186],
                         [p for p in db.profiles if p.mapper == 406]):
            self.assertEqual(len(profiles), 1)
            self.assertIsNone(profiles[0].support())

    @patch('cart_station_nes_mappers.time.sleep')
    def test_candidate_and_saved_file_keep_mapper406_high_header_bits(self, sleep):
        profile, bus, raw = fixture(406, 'INL')
        other = replace(profile, id='other', chr_sha1='0' * 40, raw_sha1='0' * 40)
        selected, payload = dump_candidates(bus, (other, profile))
        self.assertEqual(selected, profile)
        self.assertEqual(payload, profile.header + raw)
        self.assertEqual(payload[8] & 15, 1)
        with tempfile.TemporaryDirectory() as folder:
            path, created = save_verified_rom(Path(folder) / profile.filename(), payload)
            self.assertTrue(created)
            self.assertTrue(verified_rom_file(path, profile))

    @patch('cart_station_nes_mappers.time.sleep')
    def test_studybox_transport_failure_exits_without_more_banks(self, sleep):
        profile, bus, _ = fixture(186)
        bus.studybox_select_prg = Mock(side_effect=OSError('transport stopped'))
        with self.assertRaisesRegex(OSError, 'transport stopped'):
            dump_profile(bus, profile)
        self.assertEqual(bus.studybox_select_prg.call_count, 1)
        self.assertEqual(bus.modes, [1, 0])
