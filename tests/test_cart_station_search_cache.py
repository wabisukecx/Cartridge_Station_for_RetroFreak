"""Synthetic fixed/switchable windows and real hash gates; no physical ROMs."""
from _paths import ROOT  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_service import UserCancelled
from cart_station_nes_db import dump_profile
from cart_station_nes_mappers import MapperReader
from test_cart_station_nes_mappers import profile_for


class Cartridge:
    def __init__(self, mapper):
        self.mapper = mapper
        fixed = b'\x80' * (16384 - 1024) + bytes(range(256)) * 4
        self.prg = (b''.join(bytes((bank,)) * 16384 for bank in range(7)) + fixed
                    if mapper == 2 else b'\x80' * 16384 + fixed)
        self.chr = b'' if mapper == 2 else b''.join(bytes((bank + 40,)) * 8192 for bank in range(8))
        self.bank = self.chr_bank = 0
        self.search_reads = self.full_prg_reads = self.full_chr_reads = 0
        self.writes, self.modes = [], []
        self.fail_search = None

    def status(self): return 3
    def mode(self, value): self.modes.append(value)

    def cpu_offset(self, address):
        if self.mapper == 2:
            bank = 7 if address >= 0xc000 else self.bank
            return bank * 16384 + (address & 0x3fff)
        return address - 0x8000

    def memory(self, command, address, size):
        if command == 0x13:
            self.full_chr_reads += 1
            return self.chr[self.chr_bank * 8192:self.chr_bank * 8192 + size]
        assert command == 0x11
        if size == 1024:
            self.search_reads += 1
            if self.search_reads == self.fail_search:
                raise OSError('search transfer failed')
        else:
            self.full_prg_reads += 1
        offset = self.cpu_offset(address)
        return self.prg[offset:offset + size]

    def cpu_register_write(self, address, value, dummy=True):
        assert not dummy
        effective = value & self.prg[self.cpu_offset(address)]
        self.writes.append((address, value, effective))
        if self.mapper == 2: self.bank = effective & 7
        else: self.chr_bank = effective & 7

    def profile(self):
        return profile_for(self.mapper, self.prg, self.chr)

    nes = property(lambda self: self)
    write = NesRegisters.write


class LegacySearchReader(MapperReader):
    def search_write(self, start, end, value):
        self._fixed_search_blocks = self._fixed_search_addresses = None
        return super().search_write(start, end, value)


@patch('cart_station_nes_mappers.time.sleep')
class SearchCacheTests(unittest.TestCase):
    def compare_legacy(self, mapper):
        legacy, cached = Cartridge(mapper), Cartridge(mapper)
        with patch('cart_station_nes_mappers.MapperReader', LegacySearchReader):
            expected = dump_profile(legacy, legacy.profile())
        actual = dump_profile(cached, cached.profile())
        self.assertEqual(actual, expected)
        self.assertEqual(actual[16:], cached.prg + cached.chr)
        self.assertEqual(cached.writes, legacy.writes)
        self.assertEqual(cached.full_prg_reads, legacy.full_prg_reads)
        self.assertEqual(cached.full_chr_reads, legacy.full_chr_reads)
        self.assertEqual(cached.full_prg_reads, 16 if mapper == 2 else 4)
        self.assertEqual(cached.full_chr_reads, 0 if mapper == 2 else 16)
        self.assertEqual(cached.search_reads, 32 if mapper == 2 else 64)
        self.assertEqual(legacy.search_reads, 256 if mapper == 2 else 512)
        self.assertEqual(cached.modes, [1, 0])

    def test_mapper2_same_bank_writes_hashes_and_two_passes_with_fewer_search_reads(self, sleep):
        self.compare_legacy(2)

    def test_mapper3_fixed_prg_same_chr_writes_and_two_passes_with_fewer_search_reads(self, sleep):
        self.compare_legacy(3)

    def test_cache_is_discarded_after_each_read_and_explicit_initialization(self, sleep):
        bus = Cartridge(2)
        reader = MapperReader(bus, bus.profile())
        self.assertEqual(reader.read(), bus.prg)
        self.assertIsNone(reader._fixed_search_blocks)
        self.assertEqual(reader.read(), bus.prg)
        self.assertEqual(bus.search_reads, 32)
        reader._fixed_search_blocks, reader._fixed_search_addresses = {1: b'old'}, {1: 2}
        reader.initialize()
        self.assertIsNone(reader._fixed_search_blocks)
        self.assertIsNone(reader._fixed_search_addresses)
        replacement = Cartridge(2)
        reader.adapter = replacement
        self.assertEqual(reader.read(), replacement.prg)
        self.assertEqual(replacement.search_reads, 16)

    def test_second_pass_search_transfer_failure_is_not_hidden_or_retried(self, sleep):
        bus = Cartridge(2)
        bus.fail_search = 17
        with self.assertRaisesRegex(OSError, 'search transfer failed'):
            dump_profile(bus, bus.profile())
        self.assertEqual(bus.modes, [1, 0])
        self.assertEqual(bus.search_reads, 17)
        self.assertEqual(bus.full_prg_reads, 8)

    def test_cancel_discards_cache_and_next_read_does_not_reuse_partial_search(self, sleep):
        bus = Cartridge(2)
        reader = MapperReader(bus, bus.profile())
        def progress(done, total):
            if done >= 16384: raise UserCancelled('cancel')
        with self.assertRaises(UserCancelled): reader.read(progress)
        self.assertIsNone(reader._fixed_search_blocks)
        self.assertEqual(bus.search_reads, 16)
        self.assertEqual(reader.read(), bus.prg)
        self.assertEqual(bus.search_reads, 32)

    def test_incomplete_search_block_is_not_cached_or_followed_by_register_write(self, sleep):
        bus = Cartridge(2)
        bus.memory = Mock(return_value=b'')
        reader = MapperReader(bus, bus.profile())
        with self.assertRaisesRegex(ProtocolError, 'Incomplete bus-conflict'):
            reader.read()
        self.assertEqual(bus.writes, [])
        self.assertIsNone(reader._fixed_search_blocks)

    def test_switchable_search_windows_of_other_mappers_are_never_cached(self, sleep):
        bus = Mock()
        bus.nes = bus
        bus.memory.side_effect = [b'\x09\x01' + bytes(1022), b'\x09\x09\x02' + bytes(1021)]
        reader = MapperReader(bus, SimpleNamespace(mapper=7))
        reader._fixed_search_blocks, reader._fixed_search_addresses = {}, {}
        reader.search_write(0x8000, 0x83ff, 1)
        reader.search_write(0x8000, 0x83ff, 2)
        self.assertEqual(bus.memory.call_count, 2)
        self.assertEqual([c.args[:2] for c in bus.cpu_register_write.call_args_list],
                         [(0x8001, 1), (0x8002, 2)])
        self.assertEqual(reader._fixed_search_blocks, {})

    def test_second_pass_chr_corruption_still_fails_full_hash_and_exits_mode(self, sleep):
        bus = Cartridge(3)
        profile = bus.profile()
        memory = bus.memory
        def corrupt(command, address, size):
            second = command == 0x13 and bus.full_chr_reads >= 8
            data = memory(command, address, size)
            return bytes((data[0] ^ 1,)) + data[1:] if second else data
        bus.memory = corrupt
        with self.assertRaises(ProtocolError): dump_profile(bus, profile)
        self.assertEqual(bus.full_prg_reads, 4)
        self.assertEqual(bus.full_chr_reads, 16)
        self.assertEqual(bus.modes, [1, 0])

    def test_absent_value_preserves_legacy_fallback_and_does_not_cache_rom_output(self, sleep):
        bus = Mock()
        bus.nes = bus
        bus.memory.return_value = bytes(1024)
        reader = MapperReader(bus, SimpleNamespace(mapper=2))
        reader._fixed_search_blocks, reader._fixed_search_addresses = {}, {}
        for _ in range(2): reader.search_write(0xc000, 0xffff, 7)
        self.assertEqual(bus.memory.call_count, 16)
        self.assertEqual(bus.cpu_register_write.call_count, 2)
        bus.cpu_register_write.assert_called_with(0xc000, 7, dummy=False)


if __name__ == '__main__':
    unittest.main()
