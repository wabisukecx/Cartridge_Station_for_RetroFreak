from _paths import ROOT, data
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cart_station_nes_db as nes


def profile_row(prg_char, raw_char, mapper=0):
    raw = raw_char * 40
    header = '4e45531a010000080000000000000000' if mapper == 0 else '4e45531a010060280000000000000000'
    return {'id': raw + '-m%d-s0' % mapper, 'title': 'NES_' + raw, 'mapper': mapper, 'submapper': 0,
            'header': header, 'prg_size': 16384, 'chr_size': 0, 'prg_sha1': prg_char * 40,
            'chr_sha1': None, 'raw_sha1': raw, 'probes': {}, 'catalog': []}


class SupplementalCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base_row = profile_row('a', 'b')
        self.base_row['probes'] = {'first1k_sha1': hashlib.sha1(bytes(1024)).hexdigest()}
        self.extra_row = profile_row('c', 'd')
        self.base = self.root / 'nes_profiles.reference.json'
        self.write(self.base, {'version': 1, 'profiles': [self.base_row]})
        self.extra = self.root / 'nes_catalog.reference.json'
        self.document = {'version': 1, 'base_sha256': hashlib.sha256(self.base.read_bytes()).hexdigest(),
                         'profiles': [self.extra_row]}
        self.write(self.extra, self.document)
        self.paths = patch.object(nes, 'data_path', side_effect=lambda name: self.root / name)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.personal = patch.object(nes, 'user_nes_database', return_value=None)
        self.personal.start()
        self.addCleanup(self.personal.stop)

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_default_ignores_unclassified_catalog_and_keeps_candidates(self):
        database = nes.Database.load()
        self.assertEqual(len(database.profiles), 1)
        self.assertEqual(database.summary()['full_hash_only'], 0)
        self.assertEqual(database.candidates(bytes(1024), bytes(1024)), (nes.Profile.parse(self.base_row),))
        with self.assertRaisesRegex(nes.ProtocolError, 'Unknown'):
            database.select_catalog_profile(self.extra_row['id'])

    def test_explicit_database_is_not_supplemented(self):
        self.assertEqual(len(nes.Database.load(self.base).profiles), 1)

    def test_personal_override_is_not_supplemented(self):
        with patch.object(nes, 'user_nes_database', return_value=self.base):
            self.assertEqual(len(nes.Database.load().profiles), 1)

    def test_missing_supplement_keeps_base(self):
        self.extra.unlink()
        self.assertEqual(len(nes.Database.load().profiles), 1)

    def test_legacy_stale_supplement_does_not_block_standard_database(self):
        self.document['base_sha256'] = '0'*64
        self.write(self.extra, self.document)
        self.assertEqual(len(nes.Database.load().profiles), 1)

    def test_legacy_supplement_cannot_inject_partial_candidates(self):
        self.document['profiles'][0]['probes'] = self.base_row['probes']
        self.write(self.extra, self.document)
        self.assertEqual(nes.Database.load().candidates(bytes(1024), bytes(1024)),
                         (nes.Profile.parse(self.base_row),))

    def test_legacy_duplicate_id_cannot_overwrite_base(self):
        self.document['profiles'] = [dict(self.base_row, probes={})]
        self.write(self.extra, self.document)
        self.assertEqual(nes.Database.load().profiles, (nes.Profile.parse(self.base_row),))

    def test_invalid_legacy_supplement_is_not_loaded(self):
        for value in ([], {'version': 1, 'profiles': ['bad'], 'base_sha256': self.document['base_sha256']}):
            with self.subTest(value=value):
                self.write(self.extra, value)
                self.assertEqual(len(nes.Database.load().profiles), 1)

    def test_unported_catalog_row_is_searchable_but_cannot_be_selected(self):
        row = profile_row('c', 'd', mapper=38)
        self.document['profiles'] = [row]
        self.write(self.extra, self.document)
        database = nes.Database.load(self.extra)
        self.assertEqual(len(database.search(mapper=38)), 1)
        with self.assertRaisesRegex(nes.ProtocolError, 'not been ported'):
            database.select_catalog_profile(row['id'])

    def test_distribution_excludes_unclassified_metadata_and_keeps_base(self):
        from _bundle import project_sources
        database = nes.Database.load(data('nes_profiles.reference.json'))
        self.assertEqual(len(database.profiles), 2148)
        self.assertEqual(database.summary()['with_partial_hashes'], 2148)
        for windows in (False, True):
            sources = project_sources(ROOT, windows=windows)
            self.assertNotIn('data/nes_catalog.reference.json', sources)
            self.assertFalse(any(name.startswith('research/') for name in sources))


if __name__ == '__main__':
    unittest.main()
