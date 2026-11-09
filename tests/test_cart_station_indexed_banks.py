from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile, dump_candidates
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks


class IndexedCartridge:
    """Independent nibble, ROM-selection and indexed register models."""
    def __init__(self, mapper, prg, chr_data, lose_prg_high=False,
                 lose_chr_high=False, ignore_rom_enable=False,
                 ignore_command=False, corrupt_second=False):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.prg_bank = self.chr_bank = self.other_prg = 0
        self.rom_enable = False
        self.selector = 15
        self.lose_prg_high, self.lose_chr_high = lose_prg_high, lose_chr_high
        self.ignore_rom_enable, self.ignore_command = ignore_rom_enable, ignore_command
        self.corrupt_second = corrupt_second
        self.prg_reads = self.chr_reads = self.dummy_reads = 0
        self.modes, self.writes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def memory(self, command, address, size):
        if command == 0x11:
            if address == 0x8000 and size == 8:
                self.dummy_reads += 1
                return self.prg[:8]
            expected = 0xa000 if self.mapper == 19 else 0x8000
            if address != expected or size != 8192:
                raise AssertionError('Wrong PRG window')
            self.prg_reads += 1
            bank = self.prg_bank % (len(self.prg) // 8192)
            result = self.prg[bank * 8192:(bank + 1) * 8192]
            if self.corrupt_second and self.prg_reads > len(self.prg) // 8192:
                result = bytes((result[0] ^ 1,)) + result[1:]
            return result
        if command != 0x13 or address != 0 or size != 1024 or not self.chr:
            raise AssertionError('Unexpected CHR access, including CHR-RAM capture')
        self.chr_reads += 1
        if self.mapper == 19 and not self.rom_enable:
            return bytes(size)
        bank = self.chr_bank % (len(self.chr) // 1024)
        return self.chr[bank * 1024:(bank + 1) * 1024]

    def cpu_register_write(self, address, value, dummy=True):
        if not dummy:
            raise AssertionError('Expected reference dummy read')
        self.writes.append((address, value))
        if self.mapper == 18:
            if address == 0x8000:
                self.prg_bank = (self.prg_bank & 0x30) | (value & 15)
            elif address == 0x8001:
                high = 0 if self.lose_prg_high else value & 3
                self.prg_bank = (self.prg_bank & 15) | (high << 4)
            elif address == 0xa000:
                self.chr_bank = (self.chr_bank & 0xf0) | (value & 15)
            elif address == 0xa001:
                high = 0 if self.lose_chr_high else value & 15
                self.chr_bank = (self.chr_bank & 15) | (high << 4)
            else:
                raise AssertionError('Unexpected SS88006 register or save operation')
        elif self.mapper == 19:
            if address == 0xe800:
                self.prg_bank = value & 63
                self.rom_enable = not self.ignore_rom_enable and (value & 0xc0) == 0xc0
            elif address == 0xe000:
                self.other_prg = value & 63
            elif address == 0x8000:
                self.chr_bank = value
            else:
                raise AssertionError('Unexpected Namco register or save/audio operation')
        elif self.mapper == 69:
            if address == 0x8000:
                if value not in (0, 9):
                    raise AssertionError('Unexpected FME-7 command')
                if not self.ignore_command:
                    self.selector = value
            elif address == 0xa000:
                if self.selector == 9:
                    self.prg_bank = value
                elif self.selector == 0:
                    self.chr_bank = value
            else:
                raise AssertionError('Unexpected FME-7 save/audio operation')
        elif self.mapper == 85:
            if address == 0x8000:
                self.prg_bank = value
            elif address == 0xa000:
                self.chr_bank = value
            else:
                raise AssertionError('Unexpected VRC7 save/audio operation')
        self.memory(0x11, 0x8000, 8)

    nes = property(lambda self: self)
    write = NesRegisters.write


def fixture(mapper, prg_banks=64, chr_banks=256, submapper=0, **options):
    prg, chr_data = banks(prg_banks, 8192), banks(chr_banks, 1024, 43)
    return (profile_for(mapper, prg, chr_data, submapper=submapper),
            IndexedCartridge(mapper, prg, chr_data, **options), prg + chr_data)


class IndexedBankTests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_two_pass_roms_with_nibbles_commands_and_chr_ram_geometry(self, sleep):
        for mapper, prg_banks, chr_banks in ((18, 4, 8), (18, 64, 256),
                                           (19, 64, 256), (69, 64, 256),
                                           (85, 16, 128), (85, 64, 0)):
            with self.subTest(mapper=mapper, chr=chr_banks):
                profile, bus, raw = fixture(mapper, prg_banks, chr_banks)
                progress = []
                self.assertIsNone(profile.support())
                self.assertEqual(dump_profile(bus, profile, progress=lambda *p: progress.append(p)),
                                 profile.header + raw)
                self.assertEqual(bus.prg_reads, 2 * prg_banks)
                self.assertEqual(bus.chr_reads, 2 * chr_banks)
                self.assertEqual(bus.dummy_reads, len(bus.writes))
                self.assertEqual(progress[-1][1], 2 * len(raw))
                self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_known_db_submappers_keep_their_headers(self, sleep):
        for mapper, submappers in ((19, (0, 2, 3, 4, 5)), (85, (0, 1, 2))):
            for submapper in submappers:
                profile, bus, raw = fixture(mapper, 16, 128, submapper=submapper)
                self.assertEqual(dump_profile(bus, profile), profile.header + raw)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_split_prg_or_chr_nibble_failure_is_detected(self, sleep):
        for option in ('lose_prg_high', 'lose_chr_high'):
            profile, bus, _ = fixture(18, **{option: True})
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.chr_reads, 0 if option == 'lose_prg_high' else 256)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_namco_rom_selection_fault_cannot_be_saved_as_chr(self, sleep):
        profile, bus, _ = fixture(19, ignore_rom_enable=True)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.prg_reads, 64)
        self.assertEqual(bus.chr_reads, 256)
        self.assertIn((0xe800, 0xc0), bus.writes)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_ignored_fme7_selector_is_detected_before_chr_phase(self, sleep):
        profile, bus, _ = fixture(69, ignore_command=True)
        with self.assertRaises(ProtocolError):
            dump_profile(bus, profile)
        self.assertEqual(bus.chr_reads, 0)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_second_pass_corruption_returns_no_rom(self, sleep):
        for mapper in (18, 19, 69, 85):
            profile, bus, _ = fixture(mapper, 16, 128, corrupt_second=True)
            with self.assertRaises(ProtocolError):
                dump_profile(bus, profile)
            self.assertEqual(bus.prg_reads, 32)
            self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_same_geometry_candidates_use_complete_hashes(self, sleep):
        for mapper in (18, 19, 69, 85):
            profile, bus, raw = fixture(mapper, 16, 128)
            wrong = replace(profile_for(mapper, bus.prg, banks(128, 1024, 44)), id='wrong')
            matched, result = dump_candidates(bus, (wrong, profile))
            self.assertEqual(matched.id, profile.id)
            self.assertEqual(result, profile.header + raw)
            self.assertEqual(bus.prg_reads, 32)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_failure_does_not_retry_or_return_rom(self, sleep):
        for mapper in (18, 19, 69, 85):
            profile, bus, _ = fixture(mapper, 16, 128)
            bus.memory = Mock(side_effect=ProtocolError('transfer stopped'))
            with self.assertRaisesRegex(ProtocolError, 'transfer stopped'):
                dump_profile(bus, profile)
            self.assertEqual(bus.memory.call_count, 1)
            self.assertEqual(bus.modes, [1, 0])

    def test_unsupported_geometry_and_submappers_do_not_touch_device(self):
        for mapper in (18, 19, 69, 85):
            profile, _, _ = fixture(mapper, 16, 128)
            limit = 524288 if mapper in (18, 19) else 2097152
            changes = [{'prg_size': limit + 8192}, {'chr_size': 270336},
                       {'submapper': 1 if mapper == 19 else 3 if mapper == 85 else 1}]
            if mapper != 85:
                changes.append({'chr_size': 0})
            for change in changes:
                bus = Mock()
                with self.subTest(mapper=mapper, change=change), self.assertRaises(ProtocolError):
                    dump_profile(bus, replace(profile, **change))
                self.assertFalse(bus.mock_calls)

    def test_embedded_profiles_are_applicable_without_changing_db(self):
        db = Database.load(data('nes_profiles.reference.json'))
        for mapper, count in ((18, 16), (19, 21), (69, 12), (85, 2)):
            profiles = [p for p in db.profiles if p.mapper == mapper]
            self.assertEqual(len(profiles), count)
            self.assertTrue(all(p.support() is None for p in profiles))


if __name__ == '__main__':
    unittest.main()
