"""Product/edition naming, existing settings and launch-directory compatibility."""
from _paths import ROOT  # noqa: F401
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from cart_station_branding import (PRODUCT_NAME, WINDOWS_EDITION, MISTER_EDITION,
    INSTALL_DIRECTORY, WINDOWS_LAUNCHER, MISTER_LAUNCHER, WINDOWS_ARCHIVE, MISTER_ARCHIVE,
    MISTER_LOCK_PATH)
from cart_station_paths import mister_runtime_directory
from cart_station_manager import Screen, render
from cart_station_service import Service
from cart_station_nes_db import Database
from cart_station_cli import main
from test_cart_station_nes_mappers import profile_for
from _bundle import project_sources


class BrandingTests(unittest.TestCase):
    def test_common_product_and_explicit_platform_editions(self):
        self.assertEqual(PRODUCT_NAME, 'Cartridge Station for RetroFreak')
        self.assertEqual(WINDOWS_EDITION, 'Windows Edition')
        self.assertEqual(MISTER_EDITION, 'MiSTer FPGA Edition')
        self.assertEqual(WINDOWS_ARCHIVE, INSTALL_DIRECTORY + '_Windows')
        self.assertEqual(MISTER_ARCHIVE, INSTALL_DIRECTORY + '_MiSTer')
        self.assertEqual(MISTER_LOCK_PATH, '/tmp/mister-cart-station.lock')

    def test_manager_header_shows_product_and_mister_edition_without_extra_rows(self):
        picture = render(Screen(), Path('/media/fat/games/NES'), width=76, height=16)
        self.assertIn(PRODUCT_NAME + ' / ' + MISTER_EDITION, picture)
        self.assertNotIn('MiSTer Cart Station', picture)
        self.assertLessEqual(len(picture.split('\r\n')), 15)
        self.assertIn('GAME START', picture)

    def test_runtime_uses_active_or_custom_scripts_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            sd = Path(folder).resolve()
            for name in (INSTALL_DIRECTORY, 'Custom folder'):
                active = sd / 'Scripts' / name
                self.assertEqual(mister_runtime_directory(sd, active), active / 'runtime')
            self.assertEqual(mister_runtime_directory(sd, sd / 'development'),
                             sd / 'Scripts' / INSTALL_DIRECTORY / 'runtime')

    def test_launchers_ship(self):
        for validation in (False, True):
            windows = project_sources(ROOT, windows=True, validation=validation)
            mister = project_sources(ROOT, validation=validation)
            self.assertIn(WINDOWS_LAUNCHER, windows)
            self.assertIn(MISTER_LAUNCHER, mister)
        for name in (MISTER_LAUNCHER,):
            text = (ROOT / name).read_text(encoding='utf-8')
            self.assertIn('/' + INSTALL_DIRECTORY + '/cart_station.py', text)
            self.assertIn('manager "$@"', text)
            self.assertNotIn('\r', (ROOT / name).read_bytes().decode('utf-8'))

    def test_cli_help_names_the_common_product_and_both_editions(self):
        output = io.StringIO()
        with patch('sys.argv', ['cart_station.py', '--help']), patch('sys.stdout', output):
            with self.assertRaises(SystemExit) as stopped: main()
        self.assertEqual(stopped.exception.code, 0)
        normalized = ' '.join(output.getvalue().split())
        for value in (PRODUCT_NAME, WINDOWS_EDITION, MISTER_EDITION): self.assertIn(value, normalized)

    def test_verified_game_launch_uses_active_runtime_and_same_rom(self):
        prg, chr_data = b'P' * 16384, b'C' * 8192
        profile = profile_for(0, prg, chr_data)
        with tempfile.TemporaryDirectory() as folder:
            sd = Path(folder).resolve()
            core = sd / '_Console';core.mkdir()
            (core / 'NES_20261025.rbf').write_bytes(b'synthetic core')
            destination = sd / 'games/NES';destination.mkdir(parents=True)
            rom = destination / profile.filename();rom.write_bytes(profile.header + prg + chr_data)
            for name in (INSTALL_DIRECTORY,):
                active = sd / 'Scripts' / name
                (active / 'config').mkdir(parents=True)
                (active / 'config/mister_cores.example.json').write_text(json.dumps({
                    'version': 1, 'NES': {'rbf': '_Console/NES', 'type': 'f', 'index': 1, 'delay': 2}}))
                service = Service(destination, database=Database([profile]), sd_root=sd)
                service.profile = profile
                with patch('cart_station_paths.ROOT', active): launch = service.prepare_game()
                self.assertEqual(launch.parent, active / 'runtime')
                self.assertEqual(ET.parse(launch).getroot().find('file').get('path'), str(rom.resolve()))
                self.assertEqual(rom.read_bytes(), profile.header + prg + chr_data)


if __name__ == '__main__':
    unittest.main()
