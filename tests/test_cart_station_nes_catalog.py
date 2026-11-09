from _paths import ROOT  # noqa: F401
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_cli import main
from cart_station_nes_db import Database, select_candidate
from cart_station_nes_mappers import MapperReader
from test_cart_station_nes_mappers import Cartridge, profile_for


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prg, self.chr = b'P' * 65536, b'C' * 8192
        self.profile = profile_for(1, self.prg, self.chr)
        self.payload = self.profile.header + self.prg + self.chr
        self.row = asdict(self.profile)
        self.row.update(header=self.profile.header.hex(), probes={})
        self.path = self.root / 'metadata.json'
        self.path.write_text(json.dumps({'version': 1, 'profiles': [self.row]}), encoding='utf-8')
        self.database = Database.load(self.path)
        self.destination = self.root / 'output'

    def cli(self, *args, answer='y', adapter=None):
        wire = Mock()
        with patch('sys.argv', ['cart_station.py'] + list(args)), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value=answer), \
                patch('cart_station_cli.open_transport', return_value=wire) as opener, \
                patch('cart_station_cli.Adapter', return_value=adapter or Cartridge(1, self.prg, self.chr)), \
                patch('cart_station_nes_mappers.time.sleep'), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        return output.getvalue(), wire, opener

    def dump_args(self):
        return ('dump-db', '--db', str(self.path), '--catalog-profile', self.profile.id,
                '--output-dir', str(self.destination))

    def test_summary_separates_probe_presence_from_reader_support_and_hardware(self):
        other = replace(self.profile, id='unsupported', mapper=112,
                        probes=(('first1k_sha1', '0' * 40),))
        summary = Database([self.profile, other]).summary()
        self.assertEqual((summary['profiles'], summary['with_partial_hashes'],
                          summary['full_hash_only'], summary['reader_supported']), (2, 1, 1, 1))
        self.assertEqual(summary['hardware_verified'], 'not inferred from database membership')

    def test_search_filters_title_id_and_mapper_without_device(self):
        self.assertEqual(self.database.search('SYNTHETIC MAPPER', 1), (self.profile,))
        self.assertEqual(self.database.search(self.profile.id), (self.profile,))
        self.assertEqual(self.database.search('', 2), ())
        self.assertEqual(self.database.search('not present'), ())

    def test_list_db_never_opens_reader_and_limits_results(self):
        output, _, opener = self.cli('list-db', '--db', str(self.path), '--query', 'synthetic',
                                     '--mapper', '1', '--limit', '1')
        opener.assert_not_called()
        result = json.loads(output)
        self.assertEqual(result['state'], 'catalog_only')
        self.assertEqual(result['matches'], 1)
        self.assertFalse(result['profiles'][0]['partial_hashes'])

    def test_unknown_or_unsupported_manual_plan_rejected_before_open(self):
        for identifier in ('missing', self.profile.id):
            with self.subTest(identifier=identifier):
                db = self.database if identifier == 'missing' else Database([replace(self.profile, submapper=15)])
                with patch('cart_station_nes_db.Database.load', return_value=db), \
                        patch('sys.argv', ['cart_station.py', *self.dump_args(), '--catalog-profile', identifier]), \
                        patch('sys.stdin.isatty', return_value=True), \
                        patch('cart_station_cli.open_transport') as opener:
                    with self.assertRaises(ProtocolError):
                        main()
                opener.assert_not_called()

    def test_manual_plan_requires_confirmation_and_does_not_probe_or_write_banks(self):
        bus = Cartridge(1, self.prg, self.chr)
        output, wire, _ = self.cli(*self.dump_args(), answer='', adapter=bus)
        self.assertIn('manual_unverified', output)
        self.assertIn('title/revision is unconfirmed', output)
        self.assertEqual(bus.modes, [])
        self.assertEqual(bus.writes, [])
        wire.close.assert_called_once()
        self.assertFalse(self.destination.exists())

    def test_manual_plan_real_reader_verifies_twice_and_preserves_name_conflict(self):
        self.destination.mkdir()
        target = self.destination / self.profile.filename()
        target.write_bytes(b'original')
        for _ in range(2):
            bus = Cartridge(1, self.prg, self.chr)
            original_read = MapperReader.read
            with patch('cart_station_nes_mappers.MapperReader.read', autospec=True) as read:
                # Use the production reader for both independent reads.
                read.side_effect = original_read
                self.cli(*self.dump_args(), adapter=bus)
                self.assertEqual(read.call_count, 2)
            self.assertEqual(bus.modes, [1, 0])
        self.assertEqual(target.read_bytes(), b'original')
        copies = list(self.destination.glob('*.verified-*.nes'))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), self.payload)

    def test_manual_wrong_full_hash_creates_no_file(self):
        bus = Cartridge(1, b'X' * 65536, self.chr)
        with self.assertRaises(ProtocolError):
            self.cli(*self.dump_args(), adapter=bus)
        self.assertFalse(self.destination.exists())

    def test_manual_plan_refuses_empty_multiple_or_other_console(self):
        for code in (0, 255, 254, 5):
            with self.subTest(code=code):
                bus = Mock()
                bus.status.return_value = code
                with self.assertRaisesRegex(ProtocolError, 'one FC/NES'):
                    self.cli(*self.dump_args(), adapter=bus)
                bus.mode.assert_not_called()
                bus.memory.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_manual_and_partial_selection_options_are_mutually_exclusive(self):
        with patch('sys.argv', ['cart_station.py', *self.dump_args(), '--profile', self.profile.id]), \
                patch('sys.stderr'), patch('cart_station_cli.open_transport') as opener:
            with self.assertRaises(SystemExit):
                main()
        opener.assert_not_called()

    def test_catalog_dump_still_requires_interactive_terminal(self):
        with patch('sys.argv', ['cart_station.py', *self.dump_args()]), \
                patch('sys.stdin.isatty', return_value=False), patch('sys.stderr'), \
                patch('cart_station_cli.open_transport') as opener:
            with self.assertRaises(SystemExit):
                main()
        opener.assert_not_called()

    def test_existing_partial_candidate_selection_is_not_relaxed(self):
        with self.assertRaises(ProtocolError):
            select_candidate((), self.profile.id)
        self.assertEqual(self.database.select_catalog_profile(self.profile.id), self.profile)



if __name__ == '__main__':
    unittest.main()
