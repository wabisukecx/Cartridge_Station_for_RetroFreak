"""Fixed Mapper 206.1 profile metadata and ROM naming helper."""
from _paths import ROOT, data
import contextlib
import hashlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_cli import main
from cart_station_nes_db import Database
from test_cart_station_mapper206 import MAPPER206_PROFILE, fixed_mapper206_profile
from test_cart_station_namco_family import fixture


class LegacyCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile, self.bus, self.raw = fixture(206, 1, 2, 16384)
        self.payload = MAPPER206_PROFILE['header'] + self.raw
        self.bus.control_replies = []
        self.transport = Mock()
        self.metadata = {
            'prg_sha1': hashlib.sha1(self.bus.prg).hexdigest(),
            'chr_sha1': hashlib.sha1(self.bus.chr).hexdigest(),
            'raw_sha1': hashlib.sha1(self.raw).hexdigest(),
            'file_sha1': hashlib.sha1(self.payload).hexdigest(),
        }

    def invoke(self, *arguments):
        self.output, self.errors = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['cart_station.py', '--environment', 'pc', '--port', 'COM5', *map(str, arguments)]), \
                patch('cart_station_cli.open_transport', return_value=self.transport), \
                patch('cart_station_cli.Adapter', return_value=self.bus), \
                patch('time.sleep'), patch.dict(self.metadata), \
                contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors):
            main()

    def test_compatibility_metadata_matches_the_shipped_database(self):
        profile = fixed_mapper206_profile()
        database = Database.load(data('nes_profiles.reference.json'))
        expected = database.by_id['610a40eb37b7a0c27d2fa02a19feeafeccfcdc18-m206-s1']
        # The seed has only the original two probes; the reference DB also
        # retains alternate power-on windows. ROM identity must be identical.
        self.assertEqual(replace(profile, probes=expected.probes), expected)
        self.assertTrue(set(profile.probes).issubset(expected.probes))
        self.assertEqual(profile.filename(), 'Super Chinese (Japan).nes')



if __name__ == '__main__':
    unittest.main()
