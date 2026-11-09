"""Normal automatic recognition/candidate workflows, without full-catalog UI."""
from _paths import ROOT  # noqa: F401
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

from cart_station_manager import MENU_CHECK, MENU_DUMP, MENU_SELECT, Screen, render, run_manager
from cart_station_nes_db import Database
from cart_station_nes_mappers import MapperReader
from cart_station_protocol import ProtocolError
from cart_station_service import Service
from test_cart_station_manager_db import ImmediateExecutor
from test_cart_station_nes_mappers import Cartridge, profile_for


class CandidateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / 'output'
        self.prg, self.chr = b'P' * 32768, b'C' * 8192
        self.profile = replace(profile_for(1, self.prg, self.chr), title='Inserted game')
        self.other = replace(self.profile, id='other', title='Other revision')
        self.service = Service(self.destination, database=Database((self.profile, self.other)),
                               sd_root=Path(self.temp.name))
        self.bus = Cartridge(1, self.prg, self.chr)
        self.service.adapter, self.service.product = self.bus, 'Connected'
        self.service.candidate, self.service.samples = 3, 3
        self.service.identification_done = True

    def select(self):
        self.service.candidates = (self.profile, self.other)
        return self.service.select_profile(self.profile.id)

    def test_no_candidates_disables_select_and_dump_without_catalog_fallback(self):
        screen = Screen()
        screen.apply(self.service.snapshot('FC/NES: no DB candidates'))
        self.assertFalse(screen.enabled()[MENU_SELECT])
        self.assertFalse(screen.enabled()[MENU_DUMP])
        self.assertEqual(screen.selected, MENU_CHECK)
        self.assertIn('No DB match', screen.message)
        self.assertNotIn('full DB', render(screen, self.destination))

    def test_multiple_candidates_keep_select_and_unconfirmed_title_guidance(self):
        self.service.candidates = (self.profile, self.other)
        screen = Screen()
        screen.apply(self.service.snapshot('FC/NES: choose from 2 DB candidates'))
        self.assertEqual(screen.selected, MENU_SELECT)
        self.assertTrue(screen.enabled()[MENU_SELECT])
        self.assertFalse(screen.enabled()[MENU_DUMP])
        screen.view = 'candidates'
        picture = render(screen, self.destination)
        self.assertIn('Inserted game', picture)
        self.assertIn('Other revision', picture)
        self.assertIn('Title/revision unconfirmed', picture)
        self.assertNotIn('Right: Full DB', picture)

    def test_unique_candidate_focuses_dump(self):
        self.select()
        self.service.candidates = (self.profile,)
        screen = Screen()
        screen.apply(self.service.snapshot('FC/NES: DB candidate selected'))
        self.assertEqual(screen.selected, MENU_DUMP)
        self.assertTrue(screen.enabled()[MENU_DUMP])

    def test_removal_clears_candidate_selection_and_returns_to_check(self):
        screen = Screen()
        screen.apply(self.select())
        self.bus.status = Mock(return_value=255)
        state = self.service.poll()
        screen.apply(state)
        self.assertFalse(screen.enabled()[MENU_SELECT])
        self.assertFalse(screen.enabled()[MENU_DUMP])
        self.assertEqual(screen.selected, MENU_CHECK)

    def test_candidate_list_still_fits_short_terminals(self):
        screen = Screen(candidates=(self.profile, self.other), view='candidates')
        for height in (16, 20, 25):
            with self.subTest(height=height):
                picture = render(screen, self.destination, width=78, height=height)
                self.assertLessEqual(len(picture.split('\r\n')), height - 1)
                self.assertIn('OK: Choose', picture)

    def test_cannot_select_full_database_entry_outside_detected_candidates(self):
        self.service.candidates = (self.profile,)
        with self.assertRaisesRegex(ProtocolError, 'no longer available'):
            self.service.select_profile(self.other.id)
        self.assertFalse(self.bus.writes or self.bus.modes)
        self.assertFalse(self.destination.exists())

    @patch('cart_station_nes_mappers.time.sleep')
    def test_selected_candidate_rechecks_membership_and_reads_twice(self, sleep):
        self.select()
        original = MapperReader.read
        with patch('cart_station_service.identify', return_value=(self.profile, self.other)) as identify, \
                patch.object(MapperReader, 'read', autospec=True, side_effect=original) as read:
            target, created = self.service.capture(Mock())
        self.assertTrue(created)
        self.assertEqual(read.call_count, 2)
        identify.assert_called_once_with(self.bus, self.service.database)
        self.assertEqual(target.read_bytes(), self.profile.header + self.prg + self.chr)

    def test_changed_candidates_before_capture_fail_without_bank_writes(self):
        self.select()
        with patch('cart_station_service.identify', return_value=(self.other,)):
            with self.assertRaisesRegex(ProtocolError, 'Cartridge changed'):
                self.service.capture(Mock())
        self.assertFalse(self.bus.writes)
        self.assertFalse(self.destination.exists())
        self.assertIsNone(self.service.adapter)

    def test_wrong_slot_before_capture_fails_without_bank_writes(self):
        self.select()
        self.bus.status = Mock(return_value=5)
        with self.assertRaisesRegex(ProtocolError, 'Insert one FC/NES'):
            self.service.capture(Mock())
        self.assertFalse(self.bus.writes)
        self.assertFalse(self.destination.exists())

    @patch('cart_station_nes_mappers.time.sleep')
    def test_gamepad_right_stays_in_candidates_then_choose_dump_and_exit(self, sleep):
        self.service.candidates = (self.profile, self.other)
        test = self

        class Pad:
            text = ''
            reads = 0
            right_pressed = False
            def resume(self): pass
            def pause(self): pass
            def discard(self): pass
            def draw(self, text): self.text = text
            def read(self):
                self.reads += 1
                if self.reads > 200:
                    raise AssertionError('Normal candidate workflow did not finish')
                test.assertNotIn('FULL DATABASE', self.text)
                if 'Exit to MiSTer?' in self.text or 'Start reading the selected cartridge?' in self.text:
                    return ['left' if '[NO]' in self.text else 'select']
                if 'Saved and verified.' in self.text:
                    return ['back']
                if 'SELECT CARTRIDGE /' in self.text:
                    if not self.right_pressed:
                        self.right_pressed = True
                        return ['right']
                    return ['select']
                if '\x1b[7m > SELECT CARTRIDGE' in self.text or '\x1b[7m > DUMP CARTRIDGE' in self.text:
                    return ['select']
                return ['down']

        args = SimpleNamespace(output_dir=self.destination, paths_config=None, preview=False,
                               port=None, usb=True, core_config=None)
        pad = Pad()
        with patch.dict('sys.modules', {'fcntl': SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4)}), \
                patch('cart_station_manager.open', mock_open(), create=True), \
                patch('cart_station_manager.sys.stdin.isatty', return_value=True), \
                patch('cart_station_manager.sys.stdout.isatty', return_value=True), \
                patch('cart_station_manager.Service', return_value=self.service), \
                patch('cart_station_manager.Terminal', return_value=pad), \
                patch('cart_station_manager.ThreadPoolExecutor', return_value=ImmediateExecutor()), \
                patch('cart_station_manager.shutil.get_terminal_size', return_value=os.terminal_size((80, 25))), \
                patch('cart_station_manager.time.monotonic', side_effect=[n * 0.2 for n in range(1000)]), \
                patch('cart_station_service.identify', return_value=(self.profile, self.other)):
            run_manager(args, 'mister')
        self.assertTrue(pad.right_pressed)
        self.assertEqual((self.destination / self.profile.filename()).read_bytes(),
                         self.profile.header + self.prg + self.chr)


if __name__ == '__main__':
    unittest.main()
