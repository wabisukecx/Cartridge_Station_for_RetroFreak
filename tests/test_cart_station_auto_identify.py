"""Post-read full-hash identification among same-geometry DB candidates."""
from _paths import ROOT, data  # noqa: F401
import hashlib
import random
import unittest
from unittest.mock import patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Profile, auto_read_group, dump_candidates
from cart_station_nes_mappers import dump_mapped, dump_mapped_group
from test_cart_station_nes_mappers import Cartridge


def sha(data):
    return hashlib.sha1(data).hexdigest()


def make(name, mapper, prg, chr_data, seed):
    rng = random.Random(seed)
    prg_data = bytes(rng.randrange(256) for _ in range(prg))
    chr_bytes = bytes(rng.randrange(256) for _ in range(chr_data))
    header = b'NES\x1a' + bytes((prg // 16384, chr_data // 8192, (mapper & 15) << 4, (mapper & 240) | 8, mapper >> 8)) \
        + bytes(7)
    profile = Profile.parse({
        'id': name, 'title': name, 'mapper': mapper, 'submapper': 0, 'prg_size': prg, 'chr_size': chr_data,
        'header': header.hex(), 'prg_sha1': sha(prg_data), 'chr_sha1': sha(chr_bytes) if chr_data else None,
        'raw_sha1': sha(prg_data + chr_bytes), 'probes': {'first1k_sha1': sha(prg_data[:1024])}})
    return profile, prg_data, chr_bytes


class AutoIdentifyTests(unittest.TestCase):
    def test_group_rules(self):
        a, _, _ = make('a', 2, 65536, 0, 1)
        b, _, _ = make('b', 2, 65536, 0, 2)
        c, _, _ = make('c', 2, 131072, 0, 3)
        d, _, _ = make('d', 7, 65536, 0, 4)
        self.assertEqual(auto_read_group((a, b)), (a, b))
        self.assertEqual(auto_read_group((a,)), ())
        self.assertEqual(auto_read_group((a, c)), ())
        self.assertEqual(auto_read_group((a, b, d)), ())

    def test_mapper_group_identifies_the_inserted_game(self):
        for mapper, prg, chr_size in ((1, 131072, 32768), (2, 65536, 0), (4, 65536, 16384), (3, 32768, 16384)):
            first = make('first', mapper, prg, chr_size, 10 + mapper)
            second = make('second', mapper, prg, chr_size, 20 + mapper)
            for inserted, other in ((first, second), (second, first)):
                with self.subTest(mapper=mapper, inserted=inserted[0].id):
                    bus = Cartridge(mapper, inserted[1], inserted[2])
                    profile, data = dump_candidates(bus, (other[0], inserted[0]))
                    self.assertEqual(profile, inserted[0])
                    self.assertEqual(data, inserted[0].header + inserted[1] + inserted[2])
                    self.assertEqual(bus.modes[-1], 0)

    def test_unknown_cartridge_matches_no_candidate_and_returns_nothing(self):
        first, second = make('first', 2, 65536, 0, 1), make('second', 2, 65536, 0, 2)
        stranger = make('stranger', 2, 65536, 0, 3)
        bus = Cartridge(2, stranger[1], stranger[2])
        with self.assertRaisesRegex(ProtocolError, 'No mapper variant matched'):
            dump_candidates(bus, (first[0], second[0]))
        self.assertEqual(bus.modes[-1], 0)

    def test_mapper0_group_uses_raw_hash_and_rejects_strangers(self):
        first, second = make('first', 0, 16384, 8192, 1), make('second', 0, 16384, 8192, 2)
        stranger = make('stranger', 0, 16384, 8192, 3)
        for inserted, ok in ((second, True), (stranger, False)):
            raw = inserted[1] + inserted[2]
            with patch('cart_station_nes_db.dump', return_value=b'NES\x1a' + bytes(12) + raw):
                if ok:
                    profile, data = dump_candidates(object(), (first[0], second[0]))
                    self.assertEqual(profile, second[0])
                    self.assertEqual(data, second[0].header + raw)
                else:
                    with self.assertRaisesRegex(ProtocolError, 'matches 0 of 2'):
                        dump_candidates(object(), (first[0], second[0]))

    def test_group_reader_still_rejects_mixed_input_and_single_path_is_unchanged(self):
        a, b = make('a', 2, 65536, 0, 1), make('b', 2, 131072, 0, 2)
        with self.assertRaises(ProtocolError):
            dump_mapped_group(Cartridge(2, a[1], a[2]), (a[0], b[0]))
        with self.assertRaises(ProtocolError):
            dump_mapped_group(Cartridge(2, a[1], a[2]), (a[0],))
        self.assertEqual(dump_mapped(Cartridge(2, a[1], a[2]), a[0]), a[0].header + a[1])

    def test_two_reads_must_agree_even_in_group_mode(self):
        a, b = make('a', 2, 65536, 0, 1), make('b', 2, 65536, 0, 2)
        bus = Cartridge(2, a[1], a[2])
        with patch('cart_station_nes_mappers.MapperReader.read', side_effect=[a[1], b[1]]):
            with self.assertRaisesRegex(ProtocolError, 'differ'):
                dump_mapped_group(bus, (a[0], b[0]))


if __name__ == '__main__':
    unittest.main()
