"""Installation layout and compatibility checks without device access."""
from _paths import ROOT
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cart_station_paths import config_path, data_path, user_nes_database
from cart_station_environment import default_output_directory
from cart_station_service import read_core_config


class LayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch = patch('cart_station_paths.ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.root / 'config').mkdir()
        (self.root / 'data').mkdir()

    def write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_personal_configuration_beats_example(self):
        self.write('config/output_paths.example.json', dict(version=1, mister=dict(NES='/example')))
        personal = self.write('config/output_paths.json', dict(version=1, mister=dict(NES='/personal')))
        self.assertEqual(config_path('output_paths.json'), personal)
        self.assertEqual(default_output_directory('mister'), Path('/personal'))

    def test_core_configuration_uses_new_example_when_no_personal_file(self):
        self.write('config/mister_cores.example.json', dict(version=1, NES=dict(
            rbf='_Console/NES', type='f', index=1, delay=2)))
        self.assertEqual(read_core_config()['rbf'], '_Console/NES')

    def test_database_locations(self):
        self.assertEqual(data_path('gb_profiles.reference.json'), self.root / 'data/gb_profiles.reference.json')
        self.assertIsNone(user_nes_database())
        personal = self.write('config/nes_profiles.json', {})
        self.assertEqual(user_nes_database(), personal)

    def test_entry_point_works_outside_installation_directory(self):
        result = subprocess.run([sys.executable, '-S', str(ROOT / 'cart_station.py'), '--help'],
                                cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'identify-sfc', result.stdout)

    def test_imported_entry_point_keeps_its_module_identity(self):
        result = subprocess.run([
            sys.executable, '-S', '-c',
            'import cart_station, cart_station_cli; '
            'assert cart_station is not cart_station_cli; '
            'assert cart_station.__name__ == "cart_station"; '
            'assert not hasattr(cart_station, "Adapter")',
        ], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
