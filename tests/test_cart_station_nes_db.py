from _paths import ROOT, data  # noqa: F401
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch

from cart_station_transport import Adapter
from cart_station_protocol import ProtocolError, packet
from cart_station_cli import main
from test_cart_station_mapper206 import MAPPER206_PROFILE
from cart_station_banks import read_banks
from cart_station_nes_db import (Database, Profile, dump_profile, identify, select_candidate)
from test_cart_station import Wire, memory


def sha(data):
    return hashlib.sha1(data).hexdigest()


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prg, self.chr = b'P' * 16384, b'C' * 8192
        self.header = b'NES\x1a' + bytes((1, 1, 0, 0)) + bytes(8)
        self.record = {'id': 'test', 'title': 'Synthetic test game', 'mapper': 0, 'submapper': 0,
                       'prg_size': 16384, 'chr_size': 8192, 'header': self.header.hex(),
                       'prg_sha1': sha(self.prg), 'chr_sha1': sha(self.chr),
                       'raw_sha1': sha(self.prg + self.chr),
                       'file_sha1': sha(self.header + self.prg + self.chr),
                       'probes': {'first1k_sha1': sha(self.prg[:1024]),
                                  'last1k_sha1': sha(self.prg[-1024:])}}
        self.profile = Profile.parse(self.record)

    def write_db(self, records):
        path = self.root / 'profiles.json'
        path.write_text(json.dumps({'version': 1, 'profiles': records}), encoding='utf-8')
        return path

    def test_default_database_preserves_verified_profile(self):
        profile = Database.load().by_id['610a40eb37b7a0c27d2fa02a19feeafeccfcdc18-m206-s1']
        self.assertIsNone(profile.support())
        self.assertEqual(profile.header, MAPPER206_PROFILE['header'])
        self.assertEqual(profile.file_sha1, MAPPER206_PROFILE['file_sha1'])

    def test_partial_matching_uses_union_not_intersection(self):
        db = Database.load(self.write_db([self.record]))
        self.assertEqual(db.candidates(self.prg[:1024], b'Z' * 1024), (self.profile,))
        self.assertEqual(db.candidates(b'Z' * 1024, self.prg[-1024:]), (self.profile,))
        self.assertEqual(db.candidates(b'Z' * 1024, b'Z' * 1024), ())

    def test_alternate_bank_indexes_and_deduplication(self):
        row = dict(self.record, probes={'lastprev16k_1k_sha1': sha(self.prg[:1024]),
                                      'halflast1k_sha1': sha(self.prg[-1024:])})
        db = Database([Profile.parse(row)])
        self.assertEqual(len(db.candidates(self.prg[:1024], self.prg[-1024:])), 1)
        with self.assertRaises(ProtocolError):
            db.candidates(b'P', b'C' * 1024)

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(ProtocolError):
            Database.load(self.write_db([self.record, self.record]))

    def test_invalid_hash_sizes_header_and_types_rejected_before_io(self):
        for change in ({'prg_sha1': 'broken'}, {'prg_size': True}, {'chr_size': -1},
                       {'mapper': 4}, {'submapper': 1}, {'header': '00'},
                       {'header': (self.header[:6] + b'\x04' + self.header[7:]).hex()},
                       {'probes': {'unknown': 'a' * 40}}, {'title': 'bad\nname'}):
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                Database.load(self.write_db([dict(self.record, **change)]))

    def test_no_unique_candidate_is_never_silently_selected(self):
        other = replace(self.profile, id='other')
        for candidates, identifier in (((), None), ((self.profile, other), None),
                                       ((self.profile,), 'not-present')):
            with self.assertRaises(ProtocolError):
                select_candidate(candidates, identifier)
        self.assertEqual(select_candidate((self.profile, other), 'other'), other)

    def test_database_presence_does_not_enable_unported_mapper(self):
        unsupported = replace(self.profile, mapper=4)
        with self.assertRaises(ProtocolError):
            select_candidate((unsupported,))
        adapter = Mock()
        with self.assertRaises(ProtocolError):
            dump_profile(adapter, unsupported)
        self.assertEqual(adapter.mock_calls, [])

    @patch('time.sleep')
    def test_identification_only_reads_initial_bank_no_register_writes(self, sleep):
        adapter = Mock()
        adapter.status.return_value = 3
        adapter.memory.side_effect = [b'X' * 1024, self.prg[:1024], self.prg[-1024:]]
        self.assertEqual(identify(adapter, Database([self.profile])), (self.profile,))
        self.assertEqual(adapter.memory.call_args_list,
                         [call(0x11, 0x8000, 1024), call(0x11, 0x8000, 1024), call(0x11, 0xfc00, 1024)])
        self.assertEqual(adapter.mode.call_args_list, [call(1), call(0)])
        adapter.nes.mapper206_register.assert_not_called()

    @patch('time.sleep')
    def test_identify_error_still_exits_cartridge_mode(self, sleep):
        adapter = Mock()
        adapter.status.return_value = 3
        adapter.memory.side_effect = ProtocolError('disconnected')
        with self.assertRaises(ProtocolError):
            identify(adapter, Database([self.profile]))
        self.assertEqual(adapter.mode.call_args_list, [call(1), call(0)])

    @patch('cart_station_nes_db.dump')
    def test_complete_hashes_required_after_partial_match(self, dumper):
        dumper.return_value = self.header + self.prg + self.chr
        self.assertEqual(dump_profile(Mock(), self.profile), dumper.return_value)
        for bad in (self.header + self.prg + b'Z' * 8192, self.header + self.prg[:-1] + self.chr):
            dumper.return_value = bad
            with self.assertRaises(ProtocolError):
                dump_profile(Mock(), self.profile)

    def test_database_filename_cannot_escape_destination(self):
        for title in ('../outside', 'a/b\\c', 'NUL', 'CON.test', '...'):
            name = replace(self.profile, title=title).filename()
            self.assertEqual(Path(name).name, name)
            self.assertNotIn('/', name)
            self.assertNotIn('\\', name)
            self.assertTrue(name.endswith('.nes'))

    @patch('cart_station_cli.open_transport')
    def test_db_dump_requires_interactive_confirmation_before_hardware(self, opener):
        with patch('sys.argv', ['cart_station.py', 'dump-db']), \
                patch('sys.stdin.isatty', return_value=False), patch('sys.stderr'), self.assertRaises(SystemExit):
            main()
        opener.assert_not_called()

    @patch('cart_station_nes_db.identify')
    @patch('cart_station_nes_db.dump_profile')
    @patch('cart_station_cli.open_transport')
    def test_no_confirmation_closes_transport_without_reading_or_saving(self, opener, dumper, identifier):
        identifier.return_value = (self.profile,)
        destination = self.root / 'do-not-create'
        with patch('sys.argv', ['cart_station.py', 'dump-db', '--output-dir', str(destination)]), \
                patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value=''), \
                patch('builtins.print'):
            main()
        dumper.assert_not_called()
        opener.return_value.close.assert_called_once()
        self.assertFalse(destination.exists())

    @patch('cart_station_nes_db.identify')
    @patch('cart_station_cli.open_transport')
    def test_cli_db_dump_saves_only_fully_verified_data_and_preserves_conflicts(self, opener, identifier):
        identifier.return_value = (self.profile,)
        destination = self.root / 'output'
        expected = self.header + self.prg + self.chr
        with patch('sys.argv', ['cart_station.py', 'dump-db', '--output-dir', str(destination)]), \
                patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='y'), \
                patch('builtins.print'), patch('cart_station_nes_db.dump', return_value=expected):
            main()
            target = destination / self.profile.filename()
            self.assertEqual(target.read_bytes(), expected)
            main()  # Exact duplicate reuses the same file.
            self.assertEqual(len(list(destination.iterdir())), 1)
            target.write_bytes(b'existing different file')
            main()
            self.assertEqual(target.read_bytes(), b'existing different file')
            copies = list(destination.glob('*.verified-*.nes'))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_bytes(), expected)
            main()
            self.assertEqual(len(list(destination.iterdir())), 2)
        self.assertEqual(opener.return_value.close.call_count, 4)

    @patch('cart_station_nes_db.identify')
    @patch('cart_station_cli.open_transport')
    def test_cli_hash_mismatch_creates_no_output(self, opener, identifier):
        identifier.return_value = (self.profile,)
        destination = self.root / 'output'
        with patch('sys.argv', ['cart_station.py', 'dump-db', '--output-dir', str(destination)]), \
                patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='y'), \
                patch('builtins.print'), patch('cart_station_nes_db.dump', return_value=self.header + b'X' * 24576), \
                self.assertRaises(ProtocolError):
            main()
        self.assertFalse(destination.exists())
        opener.return_value.close.assert_called_once()


class BankTests(unittest.TestCase):
    def test_bank_numbers_partial_last_and_progress(self):
        reader = Mock(side_effect=lambda bank, size: bytes([bank]) * size)
        report = Mock()
        self.assertEqual(read_banks(reader, 10, 4, report), b'\0' * 4 + b'\1' * 4 + b'\2' * 2)
        self.assertEqual(reader.call_args_list, [call(0, 4), call(1, 4), call(2, 2)])
        self.assertEqual(report.call_args_list, [call(4, 10), call(8, 10), call(10, 10)])

    def test_zero_chr_invalid_sizes_and_short_banks(self):
        reader = Mock()
        self.assertEqual(read_banks(reader, 0, 1024), b'')
        reader.assert_not_called()
        for total, bank in ((-1, 4), (True, 4), (1, 0)):
            with self.assertRaises(ValueError):
                read_banks(reader, total, bank)
        reader.return_value = b'x'
        with self.assertRaises(ValueError):
            read_banks(reader, 4, 4)

    def test_nrom_bus_sequence_is_preserved(self):
        prg, chr_data = b'P' * 16384, b'C' * 8192
        wire = Wire(memory(prg) + memory(prg) + memory(chr_data))
        adapter = Adapter(wire)
        self.assertEqual(adapter.nrom(32, 8), prg + prg + chr_data)
        self.assertEqual(wire.writes, [packet(0x11, 8, 16384, 0x8000, 16384), packet(6, 0, 4),
                                      packet(0x11, 8, 16384, 0xc000, 16384), packet(6, 0, 4),
                                      packet(0x13, 8, 8192, 0, 8192), packet(6, 0, 4)])


if __name__ == '__main__':
    unittest.main()
