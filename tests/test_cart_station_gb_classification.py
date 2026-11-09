"""Full-hash classification diagnostics must not reroute or publish ROMs."""
from _paths import ROOT  # noqa: F401
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cart_station_gb import GbDatabase, GbUnknownRom, GbClassificationMismatch, dump_gb
from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database
from cart_station_service import Service
from test_cart_station_gb import Cartridge, fixture
from test_cart_station_gb_mbc1 import Mbc1Cartridge


class GbClassificationTests(unittest.TestCase):
    def test_classification_difference_is_reported_in_both_directions(self):
        for flag, expected in ((0, 'GBC'), (0x80, 'GB'), (0xc0, 'GB')):
            with self.subTest(flag=flag):
                profile, payload, record = fixture(cart=0, size_code=0, cgb=flag)
                database = GbDatabase([dict(record, system=expected)])
                with self.assertRaises(GbClassificationMismatch) as raised:
                    database.match(profile, payload)
                self.assertIn('header %s, DB %s' % (profile.system, expected), str(raised.exception))
                self.assertIn('no ROM saved', str(raised.exception))
                self.assertIn(hashlib.sha1(payload).hexdigest(), str(raised.exception))
                self.assertEqual(profile.raw_sha1, '')

    def test_wrong_hash_or_wrong_size_is_unknown_not_a_classification_difference(self):
        profile, payload, record = fixture(cart=0, size_code=0)
        for changes in ({'sha1': '0' * 40}, {'size': 65536}):
            with self.subTest(changes=changes):
                database = GbDatabase([dict(record, system='GBC', **changes)])
                with self.assertRaises(GbUnknownRom):
                    database.match(profile, payload)

    def test_records_in_both_systems_keep_the_header_system_match(self):
        profile, payload, record = fixture(cart=0, size_code=0, cgb=0x80)
        other = dict(record, system='GB', title='Other classification.gb')
        for records in ([record, other], [other, record]):
            with self.subTest(order=records[0]['system']):
                matched = GbDatabase(records).match(profile, payload)
                self.assertEqual(matched.system, 'GBC')
                self.assertEqual(matched.title, record['title'])
                self.assertTrue(matched.filename().endswith('.gbc'))

    def test_bad_alternate_sha256_is_an_integrity_error_not_a_classification_claim(self):
        profile, payload, record = fixture(cart=0, size_code=0)
        database = GbDatabase([dict(record, system='GBC', sha256='0' * 64)])
        with self.assertRaises(ProtocolError) as raised:
            database.match(profile, payload)
        self.assertNotIsInstance(raised.exception, (GbClassificationMismatch, GbUnknownRom))
        self.assertIn('SHA-256 mismatch', str(raised.exception))

    def test_bad_primary_sha256_does_not_fall_back_to_the_other_system(self):
        profile, payload, record = fixture(cart=0, size_code=0)
        database = GbDatabase([dict(record, sha256='0' * 64), dict(record, system='GBC')])
        with self.assertRaises(ProtocolError) as raised:
            database.match(profile, payload)
        self.assertNotIsInstance(raised.exception, (GbClassificationMismatch, GbUnknownRom))
        self.assertIn('SHA-256 mismatch', str(raised.exception))

    def test_conflicting_titles_for_one_identity_are_rejected_before_matching(self):
        _, _, record = fixture(cart=0, size_code=0)
        with self.assertRaisesRegex(ProtocolError, 'Conflicting GB database identity'):
            GbDatabase([record, dict(record, title='Conflicting title.gb')])

    @patch('cart_station_gb.time.sleep')
    def test_mbc1_classification_difference_stops_after_two_reads_and_restores_banks(self, sleep):
        profile, payload, record = fixture(cart=1, size_code=5)
        bus = Mbc1Cartridge(payload)
        database = GbDatabase([dict(record, system='GBC')])
        with self.assertRaises(GbClassificationMismatch):
            dump_gb(bus, profile, database)
        reads = [bank for address, size, bank in bus.bank_reads if size == 16384]
        self.assertEqual(reads, list(range(64)) * 2)
        self.assertEqual(bus.events[-3:], [('W', 0x6000, 0), ('W', 0x2100, 0), ('W', 0x4000, 0)])
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_gb.time.sleep')
    def test_service_does_not_save_change_destination_or_reclassify_on_mismatch(self, sleep):
        profile, payload, record = fixture(cart=0x1b, size_code=1, cgb=0x80)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            destination = root / 'games/GBC'
            destination.mkdir(parents=True)
            original = destination / 'existing.gbc'
            original.write_bytes(b'keep original')
            service = Service(destination, database=Database([]),
                              gb_database=GbDatabase([dict(record, system='GB')]), sd_root=root)
            service.profile, service.selected_id = profile, profile.id
            service.candidates = (profile,)
            service.adapter = Cartridge(payload)
            stages = []
            with self.assertRaises(GbClassificationMismatch):
                service.capture(lambda stage, done, total: stages.append(stage))
            self.assertEqual(service.destination, destination)
            self.assertEqual(service.profile, profile)
            self.assertEqual(service.profile.system, 'GBC')
            self.assertIsNone(service.adapter)
            self.assertEqual(original.read_bytes(), b'keep original')
            self.assertEqual(list(destination.iterdir()), [original])
            self.assertFalse((root / 'games/GB').exists())
            self.assertNotIn('Verifying and saving...', stages)


if __name__ == '__main__':
    unittest.main()
