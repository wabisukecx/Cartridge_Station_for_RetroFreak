"""Runtime packages stay independent of the developer/test packages."""
from _paths import ROOT
import re
import tempfile
import unittest
from pathlib import Path

from _bundle import project_sources


class DistributionTests(unittest.TestCase):
    def test_mister_runtime_has_application_databases_and_config_but_no_tools_or_tests(self):
        sources = project_sources(ROOT)
        for name in ('cart_station.py', 'Cartridge_Station_for_RetroFreak.sh',
                     'src/cart_station_transport.py', 'src/cart_station_service.py',
                     'data/nes_profiles.reference.json', 'config/output_paths.example.json'):
            self.assertIn(name, sources)
        self.assertFalse(any(name.startswith(('tools/', 'tests/', 'windows_tests/')) for name in sources))
        self.assertNotIn('src/cart_station_windows.py', sources)

    def test_windows_runtime_keeps_gui_launcher_and_dependency_requirements(self):
        sources = project_sources(ROOT, windows=True)
        for name in ('src/cart_station_windows.py',
                     'requirements.txt', 'requirements-windows.txt', 'Cartridge_Station_for_RetroFreak_Windows.cmd'):
            self.assertIn(name, sources)
        self.assertFalse(any(name.startswith(('tools/', 'tests/', 'windows_tests/')) for name in sources))

    def test_mister_validation_keeps_all_common_tests_fixtures_and_index_tools(self):
        sources = project_sources(ROOT, validation=True)
        for folder, pattern in (('tests', '*.py'), ('tests/fixtures', '*.json'), ('tools', '*.py')):
            expected = {path.relative_to(ROOT).as_posix() for path in (ROOT / folder).glob(pattern)}
            self.assertTrue(expected)
            self.assertTrue(expected.issubset(sources))
        self.assertIn('tools/build_windows_bundle.py', sources)
        self.assertIn('src/cart_station_windows.py', sources)
        self.assertIn('windows_tests/test_cart_station_windows.py', sources)

    def test_windows_validation_keeps_common_and_gui_test_suites(self):
        sources = project_sources(ROOT, windows=True, validation=True)
        self.assertIn('tests/test_cart_station_distribution.py', sources)
        self.assertIn('windows_tests/test_cart_station_windows.py', sources)
        self.assertIn('tools/build_mister_bundle.py', sources)
        self.assertIn('src/cart_station_windows.py', sources)

    def test_runtime_payload_is_identical_in_validation_package(self):
        for windows in (False, True):
            with self.subTest(windows=windows):
                runtime = project_sources(ROOT, windows=windows)
                validation = project_sources(ROOT, windows=windows, validation=True)
                self.assertTrue(runtime.keys() <= validation.keys())
                self.assertTrue(all(path == validation[name] for name, path in runtime.items()))

    def test_personal_and_research_data_are_excluded_from_every_package(self):
        for windows in (False, True):
            for validation in (False, True):
                with self.subTest(windows=windows, validation=validation):
                    sources = project_sources(ROOT, windows=windows, validation=validation)
                    self.assertNotIn('data/nes_catalog.reference.json', sources)
                    self.assertFalse(any(name.startswith(('research/', 'runtime/', 'logs/', 'backup/',
                                                         '.venv/', 'dist/', 'mapper/')) for name in sources))
                    self.assertTrue(all(name.endswith('.example.json') for name in sources
                                        if name.startswith('config/')))

    def test_extra_local_files_are_not_picked_up_by_the_package_patterns(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('config/nes_profiles.json', 'logs/trace.jsonl', 'runtime/manager.log',
                         'research/private.json', 'data/nes_catalog.reference.json',
                         'data/game.nes', 'tests/__pycache__/leftover.pyc', 'tools/local.exe'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'private')
            for validation in (False, True):
                sources = project_sources(root, validation=validation)
                self.assertFalse(any(path.is_file() for path in sources.values()))

    def test_dependency_licenses_ship_in_all_packages(self):
        for windows in (False, True):
            for validation in (False, True):
                sources = project_sources(ROOT, windows=windows, validation=validation)
                for name in ('licenses/pyserial-3.5.txt', 'licenses/pyusb-1.3.1.txt',
                             'docs/LICENSES.md'):
                    self.assertIn(name, sources)
                    self.assertTrue(sources[name].is_file())
        for name in ('pyserial-3.5.txt', 'pyusb-1.3.1.txt'):
            text = (ROOT / 'licenses' / name).read_text(encoding='utf-8')
            self.assertIn('Redistribution and use', text)
            self.assertIn('DISCLAIMED', text)

    def test_readmes_and_user_guides_ship_in_every_package_and_their_links_resolve(self):
        for windows in (False, True):
            for validation in (False, True):
                with self.subTest(windows=windows, validation=validation):
                    sources = project_sources(ROOT, windows=windows, validation=validation)
                    for readme in ('README.md', 'USER_GUIDE.md', 'README.ja.md'):
                        self.assertIn(readme, sources)
                        text = sources[readme].read_text(encoding='utf-8')
                        for target in re.findall(r'\]\(([^)#\s]+)(?:#[^)]*)?\)', text):
                            if target.startswith(('http://', 'https://')):
                                continue
                            path = target.rstrip('/')
                            self.assertTrue(path in sources or any(name.startswith(path + '/') for name in sources),
                                            (readme, target))

    def test_all_selected_source_files_exist(self):
        for windows in (False, True):
            for validation in (False, True):
                with self.subTest(windows=windows, validation=validation):
                    sources = project_sources(ROOT, windows=windows, validation=validation)
                    self.assertTrue(all(path.is_file() for path in sources.values()))


if __name__ == '__main__':
    unittest.main()
