from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from cart_station_transport import Adapter
from cart_station_protocol import ProtocolError, packet
from cart_station_nes_db import Database, Profile, dump_profile
from cart_station_nes_mappers import MapperReader, RomMismatch, dump_mapped
from test_cart_station import Wire, memory


class TraceBus:
    """Same synthetic bus model as the one used to record the reference traces."""
    def __init__(self):
        self.trace = []

    def memory(self, command, address, size):
        kind = 'R' if command == 0x11 else 'P'
        self.trace.append('%s,%d,%d' % (kind, address, size))
        extra = 128 if kind == 'P' else 0
        return bytes(((address >> 8) + index + extra) & 255 for index in range(size))

    def cpu_register_write(self, address, value, dummy=True):
        self.trace.append('W,%d,%d' % (address, value))
        if dummy:
            self.memory(0x11, 0x8000, 8)


    def taito_rom_register(self, mapper, address, value):
        self.trace.append('%s,%d,%d' % ('X' if mapper == 80 else 'W', address, value))
        self.memory(0x11, 0x8000, 8)

    def discrete_rom_register(self, mapper, value):
        self.cpu_register_write(0x6000, value)

    def bandai_rom_register(self, mapper, address, value):
        self.cpu_register_write(address, value)

    def sunsoft4_addon_select(self):
        self.cpu_register_write(0xf000, 0)
        self.cpu_register_write(0x6000, 1)

    def mapper153_select_prg(self, bank):
        self.cpu_register_write(0x800d, 0)
        for address in range(0x8000, 0x8004):
            self.cpu_register_write(address, bank // 16)
        self.cpu_register_write(0x6008, bank)
        self.cpu_register_write(0x8008, bank)

    def nina001_rom_register(self, address, value):
        self.cpu_register_write(address, value)

    def mmc3_multicart_select(self, mapper, block):
        self.cpu_register_write(0xa001, 128)
        self.cpu_register_write(0x6000, block)
        self.cpu_register_write(0xa001, 0)

    def mmc5_rom_register(self, address, value):
        self.cpu_register_write(address, value)

    def studybox_select_prg(self, bank):
        self.cpu_register_write(0x4201, bank)

    def mapper87_register(self, address, value):
        self.cpu_register_write(address, value, dummy=False)

    nes = property(lambda self: self)
    write = NesRegisters.write


class Cartridge:
    """Synthetic chip model; independent bank windows and MMC1 serial latch."""
    def __init__(self, mapper, prg, chr_data):
        self.mapper, self.prg, self.chr = mapper, prg, chr_data
        self.bank, self.chr_bank, self.selector = 0, 0, 0
        self.registers = [12, 0, 0, 0]
        self.bits, self.shift = 0, 0
        self.modes, self.writes = [], []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def cpu_register_write(self, address, value, dummy=True):
        self.writes.append((address, value, dummy))
        if self.mapper == 1:
            if value & 128:
                self.bits = self.shift = 0
                self.registers[0] |= 12
            else:
                self.shift |= (value & 1) << self.bits
                self.bits += 1
                if self.bits == 5:
                    self.registers[(address >> 13) & 3] = self.shift
                    self.bits = self.shift = 0
        elif self.mapper == 2:
            self.bank = value
        elif self.mapper == 3:
            self.chr_bank = value % (len(self.chr) // 8192)
        elif self.mapper == 4:
            if address == 0x8000:
                self.selector = value & 7
            elif self.selector == 6:
                self.bank = value
            elif self.selector == 2:
                self.chr_bank = value
        if dummy:
            self.memory(0x11, 0x8000, 8)

    def memory(self, command, address, size):
        if command == 0x11:
            if self.mapper == 1:
                outer = (self.registers[1] & 16) if len(self.prg) > 262144 else 0
                bank = outer + (self.registers[3] & 15 if address < 0xc000 else 15)
                bank %= len(self.prg) // 16384
                offset = bank * 16384 + (address & 16383)
            elif self.mapper == 2:
                bank = self.bank if address < 0xc000 else len(self.prg) // 16384 - 1
                offset = bank * 16384 + (address & 16383)
            elif self.mapper == 3:
                offset = (address - 0x8000) % len(self.prg)
            else:
                offset = self.bank * 8192 + (address & 8191)
            return self.prg[offset:offset + size]
        if self.mapper == 1:
            data = bytearray()
            for index in range(size):
                position = address + index
                bank = self.registers[1 if position < 4096 else 2]
                data.append(self.chr[(bank * 4096 + position % 4096) % len(self.chr)])
            return bytes(data)
        unit = 1024 if self.mapper == 4 else 8192
        offset = self.chr_bank * unit + (address % unit)
        return self.chr[offset:offset + size]

    nes = property(lambda self: self)
    write = NesRegisters.write


def profile_for(mapper, prg, chr_data, submapper=0):
    header = bytearray(b'NES\x1a' + bytes(12))
    header[4], header[5], header[6], header[7] = (len(prg) // 16384,
        len(chr_data) // 8192, (mapper & 15) << 4, (mapper & 240) | 8)
    header[8] = (submapper << 4) | (mapper >> 8)
    sha = lambda data: hashlib.sha1(data).hexdigest()
    return Profile.parse({'id': 'synthetic-%d' % mapper, 'title': 'Synthetic mapper %d' % mapper,
                          'mapper': mapper, 'submapper': submapper, 'header': header.hex(),
                          'prg_size': len(prg), 'chr_size': len(chr_data), 'prg_sha1': sha(prg),
                          'chr_sha1': sha(chr_data) if chr_data else None,
                          'raw_sha1': sha(prg + chr_data), 'file_sha1': sha(bytes(header) + prg + chr_data)})


class MapperTests(unittest.TestCase):
    def test_rom_variants_match_reference_traces(self):
        fixture = json.loads(data('mapper_reference_traces.json').read_text())
        for case in fixture['cases']:
            with self.subTest(case=case['variant']):
                bus = TraceBus()
                profile = SimpleNamespace(mapper=case['mapper'], submapper=case.get('submapper', 0),
                                          prg_size=case['prg_size'], chr_size=case['chr_size'],
                                          prg_crc32=case.get('prg_crc32'))
                reader = MapperReader(bus, profile, 8192 if case['variant'] == 'mapper001_chr8k' else 4096,
                                      4 if case['variant'] == 'mapper001_prg64k' else 0)
                # Primitive trace comparison only; full dump tests enforce the PRG gate.
                reader._mapper87_prg_verified = case['mapper'] == 87
                reader._mapper184_prg_verified = case['mapper'] == 184
                reader._cnrom_cp_crc = case.get('prg_crc32') if case['mapper'] == 185 else None
                if case['variant'] == 'mapper406_inl':
                    reader.register_variant = 'INL'
                with patch('cart_station_nes_mappers.time.sleep', side_effect=lambda seconds: bus.trace.append('T,%d' % (seconds * 1000))):
                    reader.initialize()
                    for bank in case['prg_banks']:
                        reader.prg_bank(bank, case['prg_bank_size'])
                    for bank in case['chr_banks']:
                        reader.chr_rom_bank(bank, case['chr_bank_size'])
                expected = case['trace']
                if case['mapper'] == 5:
                    # Explicit deviations from the UNMODIFIED baseline fixture:
                    # protect SRAM first, and use 8 KiB PRG mode for $5114.
                    expected = ['W,20738,0', 'R,32768,8', 'W,20739,0', 'R,32768,8'] + [
                        'W,20736,3' if entry == 'W,20736,0' else entry for entry in expected]
                if case['mapper'] in (37, 47):
                    # Preserve the baseline fixture. Explicitly account for
                    # the added write-gate wrapper and corrected 47 boundaries.
                    expected = []
                    requests = [(bank, 16) for bank in case['prg_banks']] + [
                        (bank, 128) for bank in case['chr_banks']]
                    for index, (bank, threshold) in enumerate(requests):
                        chunk = list(case['trace'][index * 7:(index + 1) * 7])
                        self.assertEqual(len(chunk), 7)
                        self.assertTrue(chunk[0].startswith('W,24576,'))
                        if case['mapper'] == 47:
                            chunk[0] = 'W,24576,%d' % int(bank >= threshold)
                        expected += ['W,40961,128', 'R,32768,8'] + chunk[:2] + [
                            'W,40961,0', 'R,32768,8'] + chunk[2:]
                self.assertEqual(bus.trace, expected)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_complete_roms_match_for_four_chip_models(self, sleep):
        for mapper, prg_banks, chr_banks, prg_unit, chr_unit in (
                (1, 32, 16, 16384, 4096), (2, 16, 0, 16384, 8192),
                (3, 2, 4, 16384, 8192), (3, 2, 8, 16384, 8192),
                (4, 64, 32, 8192, 1024)):
            with self.subTest(mapper=mapper, chr_banks=chr_banks):
                prg = b''.join(bytes((128 + bank,)) * prg_unit for bank in range(prg_banks))
                chr_data = b''.join(bytes((bank,)) * chr_unit for bank in range(chr_banks))
                profile = profile_for(mapper, prg, chr_data)
                adapter = Cartridge(mapper, prg, chr_data)
                self.assertIsNone(profile.support())
                data = dump_profile(adapter, profile)
                self.assertEqual(data, profile.header + prg + chr_data)
                self.assertEqual(adapter.modes, [1, 0])
                self.assertTrue(all(0x8000 <= address <= 0xffff for address, value, dummy in adapter.writes))

    def test_register_wire_acknowledgements_and_optional_dummy(self):
        wire = Wire(bytes(16) + memory(b'P' * 8) + bytes(16), fragment=3)
        adapter = Adapter(wire)
        adapter.nes.cpu_register_write(0xa000, 0x1c)
        adapter.nes.cpu_register_write(0xc010, 2, dummy=False)
        self.assertEqual(wire.writes, [packet(0x12, 8, 1, 0xa000, 1), b'\x1c',
                                      packet(0x11, 8, 8, 0x8000, 8), packet(6, 0, 4),
                                      packet(0x12, 8, 1, 0xc010, 1), b'\x02'])
        self.assertFalse(wire.replies)
        for address, value in ((0x6000, 0), (0x10000, 0), (0x8000, 256), (True, 0)):
            with self.assertRaises(ValueError):
                adapter.nes.cpu_register_write(address, value)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_bus_conflict_search_hits_and_fallback_match_reference(self, sleep):
        bus = Mock()
        bus.nes = bus
        bus.memory.side_effect = [b'\0' * 1024, b'\0' * 17 + b'\x25' + b'\0' * 1006]
        reader = MapperReader(bus, SimpleNamespace(mapper=2))
        reader.search_write(0xc000, 0xffff, 0x25)
        bus.cpu_register_write.assert_called_once_with(0xc411, 0x25, dummy=False)
        sleep.assert_called_once_with(0.002)
        bus.reset_mock()
        bus.memory.side_effect = None
        bus.memory.return_value = b'\0' * 1024
        reader.search_write(0xc000, 0xffff, 99)
        self.assertEqual(bus.memory.call_count, 16)
        bus.cpu_register_write.assert_called_once_with(0xc000, 99, dummy=False)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mmc1_hash_mismatch_tries_only_matching_reference_variants(self, sleep):
        profile = profile_for(1, b'P' * 65536, b'C' * 8192)
        bus = Mock()
        bus.nes = bus
        bus.status.return_value = 3
        raw = b'P' * 65536 + b'C' * 8192
        with patch('cart_station_nes_mappers.MapperReader') as factory:
            factory.side_effect = [SimpleNamespace(read=Mock(side_effect=RomMismatch('first variant'))),
                                   SimpleNamespace(read=Mock(return_value=raw))]
            self.assertEqual(dump_mapped(bus, profile), profile.header + raw)
            self.assertEqual([c.args[2:] for c in factory.call_args_list], [(4096, 0), (8192, 0)])
        self.assertEqual(bus.mode.call_args_list, [call(1), call(0), call(1), call(0)])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_transport_error_exits_without_variant_retry(self, sleep):
        profile = profile_for(1, b'P' * 65536, b'C' * 8192)
        bus = Mock()
        bus.nes = bus
        bus.status.return_value = 3
        with patch('cart_station_nes_mappers.MapperReader') as factory:
            factory.return_value.read.side_effect = ProtocolError('USB unplugged')
            with self.assertRaisesRegex(ProtocolError, 'USB unplugged'):
                dump_mapped(bus, profile)
            factory.assert_called_once()
        self.assertEqual(bus.mode.call_args_list, [call(1), call(0)])

    def test_unsupported_geometries_never_touch_device(self):
        profile = profile_for(2, b'P' * 32768, b'')
        for changes in ({'submapper': 3}, {'chr_size': 8192}, {'prg_size': 4194304 + 16384}):
            bus = Mock()
            bus.nes = bus
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                dump_profile(bus, replace(profile, **changes))
            self.assertFalse(bus.mock_calls)

    def test_expanded_database_keeps_known_profile_and_rejects_unported_mappers(self):
        database = Database.load(data('nes_profiles.reference.json'))
        self.assertGreater(len(database.profiles), 2000)
        self.assertIsNone(database.by_id['610a40eb37b7a0c27d2fa02a19feeafeccfcdc18-m206-s1'].support())
        self.assertTrue(all(p.support() is not None for p in database.profiles if p.mapper not in (37, 47, 0, 1, 2, 3, 4, 5, 7, 9, 10, 11, 18, 19, 32, 33, 34, 65, 66, 67, 68, 69, 70, 71, 75, 76, 78, 85, 87, 88, 89, 94, 95, 97, 118, 119, 152, 154, 180, 206, 48, 72, 73, 92, 210, 22, 24, 26, 64, 158, 21, 23, 25, 96, 148, 155, 157, 188, 80, 82, 207, 552, 86, 93, 140, 184, 16, 159, 13, 30, 77, 185, 153, 186, 406)))

    def test_uxrom_cnrom_existing_db_variants_use_reference_conflict_search(self):
        database = Database.load(data('nes_profiles.reference.json'))
        for mapper in (2, 3):
            profiles = [p for p in database.profiles if p.mapper == mapper]
            self.assertTrue(profiles)
            self.assertTrue(any(p.submapper == 2 and p.support() is None for p in profiles))
            for profile in profiles:
                self.assertIn(profile.submapper, (0, 1, 2))

    def test_expanded_db_partial_collision_needs_explicit_profile(self):
        from cart_station_nes_db import select_candidate
        database = Database.load(data('nes_profiles.reference.json'))
        seed = database.by_id['610a40eb37b7a0c27d2fa02a19feeafeccfcdc18-m206-s1']
        first = dict(seed.probes)['first1k_sha1']
        # The two initial probe index buckets can collide across regional ROMs.
        self.assertGreater(len(database.index['first'][first]), 1)
        candidates = tuple(database.by_id[key] for key in database.index['first'][first])
        with self.assertRaises(ProtocolError):
            select_candidate(candidates)
        self.assertEqual(select_candidate(candidates, seed.id), seed)


if __name__ == '__main__':
    unittest.main()
