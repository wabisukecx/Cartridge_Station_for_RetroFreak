from _paths import ROOT, data  # noqa: F401
import hashlib
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

from cart_station_manager import (PollState, Screen, Service, UserCancelled, create_mgl,
                        render, run_manager, verified_file)
from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, Profile, auto_read_group, verified_rom_file


def synthetic(name, value, mapper=0):
    prg, chr_data = bytes((value,)) * 16384, bytes((value + 1,)) * 8192
    header = b'NES\x1a' + bytes((1, 1, mapper << 4, 8)) + bytes(8)
    payload = header + prg + chr_data
    sha = lambda data: hashlib.sha1(data).hexdigest()
    profile = Profile.parse({'id': name, 'title': name, 'mapper': mapper, 'submapper': 0,
                             'prg_size': len(prg), 'chr_size': len(chr_data), 'header': header.hex(),
                             'prg_sha1': sha(prg), 'chr_sha1': sha(chr_data),
                             'raw_sha1': sha(prg + chr_data), 'probes': {'first1k_sha1': sha(prg[:1024])}})
    return profile, payload


class ImmediateExecutor:
    def submit(self, function, *arguments):
        future = Future()
        try:
            future.set_result(function(*arguments))
        except Exception as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait):
        pass


class ManagerDbTests(unittest.TestCase):
    def setUp(self):
        # Menu-flow fakes inspect the full layout. Do not inherit the SSH/CI
        # terminal height; compact layouts are exercised with explicit sizes.
        self.terminal_size = patch('cart_station_manager.shutil.get_terminal_size',
                                   return_value=os.terminal_size((80, 25)))
        self.terminal_size.start()
        self.addCleanup(self.terminal_size.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / 'games' / 'NES'
        self.first, self.first_data = synthetic('First game', 32)
        self.second, self.second_data = synthetic('Second game', 64)
        self.unported, self.unported_data = synthetic('Unported game', 96, mapper=7)
        self.database = Database([self.first, self.second, self.unported])
        (self.root / '_Console').mkdir()
        (self.root / '_Console' / 'NES_20260823.rbf').write_bytes(b'fake core')
        self.config = self.root / 'cores.json'
        self.core = {'rbf': '_Console/NES', 'type': 'f', 'index': 1, 'delay': 2}
        self.config.write_text(json.dumps({'version': 1, 'NES': self.core}))
        self.service = Service(self.destination, core_path=self.config,
                               sd_root=self.root, database=self.database)

    def prime(self, candidates=None):
        self.service.adapter = Mock()
        self.service.candidate, self.service.samples = 3, 3
        self.service.identification_done = True
        self.service.candidates = tuple(candidates or (self.first, self.second, self.unported))

    def save(self, profile, data):
        self.destination.mkdir(parents=True, exist_ok=True)
        path = self.destination / profile.filename()
        path.write_bytes(data)
        return path

    def test_multiple_candidates_require_selection_and_unported_reason_is_visible(self):
        self.prime()
        state = self.service.snapshot('Choose from 3 candidates')
        self.assertFalse(state.supported)
        self.assertTrue(state.selection_pending)
        screen = Screen()
        screen.apply(state)
        self.assertEqual(screen.view, 'main')
        state = self.service.select_profile(self.unported.id)
        self.assertFalse(state.supported)
        screen.view = 'main'
        screen.apply(state)
        self.assertIn('not been ported', render(screen, self.destination))
        self.assertFalse(screen.enabled()[2])
        with self.assertRaises(ProtocolError):
            self.service.capture(Mock())
        self.assertFalse(self.service.adapter.mock_calls)

    def test_switching_candidate_never_uses_previous_game_rom(self):
        self.save(self.first, self.first_data)
        self.prime()
        self.assertTrue(self.service.select_profile(self.first.id).saved)
        state = self.service.select_profile(self.second.id)
        self.assertFalse(state.saved)
        self.assertEqual(state.profile, self.second)
        self.assertIsNone(self.service.existing())
        with self.assertRaisesRegex(ProtocolError, 'selected game'):
            self.service.prepare_game()

    def test_complete_different_rom_is_preserved_and_alternate_survives_restart(self):
        original = self.save(self.first, self.second_data)
        self.prime((self.first,))
        self.service.select_profile(self.first.id)
        with patch('cart_station_service.identify', return_value=(self.first,)), \
                patch('cart_station_service.dump_profile', return_value=self.first_data):
            saved, created = self.service.capture(Mock())
        self.assertTrue(created)
        self.assertEqual(original.read_bytes(), self.second_data)
        self.assertEqual(saved.read_bytes(), self.first_data)
        restarted = Service(self.destination, core_path=self.config,
                            sd_root=self.root, database=self.database)
        restarted.profile = self.first
        self.assertEqual(restarted.existing(), saved)
        self.assertEqual(ET.parse(restarted.prepare_game()).find('file').get('path'), str(saved.resolve()))
        saved.write_bytes(b'corrupted alternate')
        self.assertIsNone(restarted.existing())
        with self.assertRaises(ProtocolError):
            restarted.prepare_game()

    def test_stale_or_non_candidate_selection_is_rejected(self):
        self.prime()
        with self.assertRaises(ProtocolError):
            self.service.select_profile('not-in-current-candidates')
        self.service.close()
        with self.assertRaises(ProtocolError):
            self.service.select_profile(self.first.id)

    @patch('cart_station_service.identify')
    @patch('cart_station_service.dump_profile')
    def test_cartridge_change_after_selection_blocks_mapper_execution(self, dumper, identifier):
        self.prime()
        self.service.select_profile(self.first.id)
        identifier.return_value = (self.second,)
        with self.assertRaisesRegex(ProtocolError, 'Cartridge changed'):
            self.service.capture(Mock())
        dumper.assert_not_called()
        self.assertFalse(self.destination.exists())

    @patch('cart_station_service.identify')
    @patch('cart_station_service.dump_profile')
    def test_selected_db_title_and_payload_used_for_capture(self, dumper, identifier):
        self.prime()
        self.service.select_profile(self.second.id)
        identifier.return_value = (self.first, self.second)
        dumper.return_value = self.second_data
        report = Mock()
        target, created = self.service.capture(report)
        self.assertTrue(created)
        self.assertEqual(target.name, self.second.filename())
        self.assertEqual(target.read_bytes(), self.second_data)
        self.assertEqual(dumper.call_args.args[1], self.second)
        # Capture timing wraps progress; arguments and cancellation still reach the UI.
        report.reset_mock()
        dumper.call_args.kwargs['progress']('Read 1/2', 12, 24)
        report.assert_called_once_with('Read 1/2', 12, 24)
        self.assertTrue(verified_file(target, self.second))
        self.assertFalse(self.service.capture(report)[1])

    @patch('cart_station_service.identify')
    @patch('cart_station_service.dump_profile')
    def test_cancel_before_dump_creates_no_output(self, dumper, identifier):
        self.prime()
        self.service.select_profile(self.second.id)
        with self.assertRaises(UserCancelled):
            self.service.capture(Mock(side_effect=UserCancelled('cancelled')))
        identifier.assert_not_called()
        dumper.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_selected_game_launch_uses_verified_absolute_path(self):
        first_path = self.save(self.first, self.first_data)
        second_path = self.save(self.second, self.second_data)
        self.prime()
        self.service.select_profile(self.second.id)
        transport = self.service.transport = Mock()
        mgl = self.service.prepare_game()
        transport.close.assert_called_once()
        self.assertEqual(ET.parse(mgl).find('file').get('path'), str(second_path.resolve()))
        self.assertNotEqual(ET.parse(mgl).find('file').get('path'), str(first_path.resolve()))

    def test_launch_revalidates_file_after_cached_verification(self):
        path = self.save(self.first, self.first_data)
        self.prime()
        self.assertTrue(self.service.select_profile(self.first.id).saved)
        path.write_bytes(self.first_data[:-1] + b'X')
        with self.assertRaises(ProtocolError):
            self.service.prepare_game()
        self.assertFalse((self.root / 'Scripts' / 'Cartridge_Station_for_RetroFreak' / 'runtime').exists())

    def test_file_verification_requires_header_and_complete_sections_without_file_hash(self):
        path = self.save(self.first, self.first_data)
        self.assertIsNone(self.first.file_sha1)
        self.assertTrue(verified_rom_file(path, self.first))
        for value in (self.first_data[:6] + b'\x01' + self.first_data[7:],
                      self.first_data[:-1], self.first_data + b'extra',
                      self.first_data[:100] + b'X' + self.first_data[101:]):
            path.write_bytes(value)
            self.assertFalse(verified_rom_file(path, self.first))

    def test_distinct_profiles_generate_distinct_safe_mgl_names(self):
        first_path = self.save(self.first, self.first_data)
        second_path = self.save(self.second, self.second_data)
        runtime = self.root / 'runtime'
        first_mgl = create_mgl(first_path, self.core, runtime, self.root, profile=self.first)
        second_mgl = create_mgl(second_path, self.core, runtime, self.root, profile=self.second)
        self.assertNotEqual(first_mgl.name, second_mgl.name)
        self.assertNotIn(' ', first_mgl.name)

    def test_small_candidate_list_scrolls_and_sanitizes_control_sequences(self):
        candidates = tuple(replace(self.first, id=str(i), title='Game %d' % i) for i in range(25))
        candidates = candidates[:20] + (replace(self.first, title='bad\x1b[Htitle'),) + candidates[21:]
        screen = Screen(view='candidates', candidates=candidates, choice=20)
        picture = render(screen, self.destination, width=40, height=16)
        self.assertLessEqual(len(picture.split('\r\n')), 15)
        self.assertIn('Game 21', picture)
        self.assertIn('bad [Htitle', picture)
        self.assertNotIn('bad\x1b', picture)
        self.assertTrue(all(len(line) <= 38 for line in picture.replace('\x1b[7m', '').replace('\x1b[0m', '').split('\r\n')))

    def test_picker_separates_reader_support_from_title_and_revision_match(self):
        profiles = (replace(self.first, title='Game (Japan)'),
                    replace(self.second, title='Game (Japan) (Rev 1)'))
        screen = Screen(view='candidates', candidates=profiles, selection_pending=True)
        picture = render(screen, self.destination)
        self.assertIn('[READER SUPPORTED]', picture)
        self.assertNotIn('[READABLE]', picture)
        self.assertIn('Partial hashes can match different titles/revisions.', picture)
        self.assertIn('Title/revision unconfirmed until full ROM hashes match.', picture)
        self.assertIn('Saved only if full ROM hashes match the selected title.', picture)
        screen.saved, screen.profile = True, profiles[0]
        self.assertIn('Saved ROM verified; re-reading checks hashes again.', render(screen, self.destination))
        screen.choice = 1
        self.assertIn('Title/revision unconfirmed until full ROM hashes match.', render(screen, self.destination))

    def test_header_based_candidate_does_not_claim_a_partial_hash_match(self):
        from test_cart_station_gb import fixture
        profile, _, _ = fixture()
        screen = Screen(view='candidates', candidates=(profile,))
        picture = render(screen, self.destination)
        self.assertIn('A candidate title alone does not verify the ROM.', picture)
        self.assertNotIn('Partial hashes', picture)

    def test_super_chinese_mixed_geometry_stays_main_and_requires_selection(self):
        profiles = tuple(p for p in Database.load().profiles if p.title in
                         ('Super Chinese (Japan)', 'Kung-Fu Heroes (USA).nes'))
        self.assertEqual(len(profiles), 2)
        self.assertEqual(auto_read_group(profiles), ())
        self.prime(profiles)
        screen = Screen()
        screen.apply(self.service.snapshot('FC/NES: choose from 2 DB candidates'))
        self.assertEqual(screen.view, 'main')
        self.assertFalse(screen.enabled()[2])
        self.assertTrue(screen.enabled()[1])
        self.assertIn('Choose SELECT CARTRIDGE before DUMP.', render(screen, self.destination))
        selected = self.service.select_profile(profiles[0].id)
        screen.apply(selected)
        self.assertTrue(screen.enabled()[2])
        self.assertEqual(screen.profile, profiles[0])

    def test_auto_group_picker_is_optional_and_main_dump_remains_enabled(self):
        self.prime((self.first, self.second))
        self.service.auto_group = (self.first, self.second)
        screen = Screen()
        screen.apply(self.service.snapshot('Auto-detect'))
        self.assertEqual(screen.view, 'main')
        self.assertTrue(screen.enabled()[2])
        screen.view = 'candidates'
        picture = render(screen, self.destination, width=100)
        self.assertIn('Optional choice; DUMP can identify these titles automatically.', picture)
        self.assertNotIn('Partial matches only.', picture)
        self.assertIn('Saved only after a full ROM hash match.', picture)

    def test_poll_preserves_explicitly_opened_picker_for_same_candidates(self):
        for auto in (False, True):
            self.prime((self.first, self.second))
            self.service.auto_group = (self.first, self.second) if auto else ()
            state = self.service.snapshot('Candidates')
            screen = Screen()
            screen.apply(state)
            screen.view, screen.choice = 'candidates', 1
            screen.apply(state)
            self.assertEqual((screen.view, screen.choice), ('candidates', 1))

    def test_changed_candidates_close_old_picker_for_both_selection_modes(self):
        for auto in (False, True):
            screen = Screen(view='candidates', candidates=(self.unported,), choice=9)
            self.prime((self.first, self.second))
            self.service.auto_group = (self.first, self.second) if auto else ()
            screen.apply(self.service.snapshot('New cartridge'))
            self.assertEqual((screen.view, screen.choice), ('main', 0))

    def test_initial_focus_is_select_for_multiple_and_dump_for_single_candidate(self):
        for auto in (False, True):
            self.prime((self.first, self.second))
            self.service.auto_group = (self.first, self.second) if auto else ()
            screen = Screen()
            screen.apply(self.service.snapshot('Detected'))
            self.assertEqual(screen.selected, 1)
            self.assertEqual(screen.view, 'main')
        self.prime((self.first,))
        self.service.auto_group = ()
        state = self.service.select_profile(self.first.id)
        screen = Screen()
        screen.apply(state)
        self.assertEqual(screen.selected, 2)
        self.assertTrue(screen.enabled()[1])  # Single-item picker stays visible.
        self.assertIn('SELECT CARTRIDGE', render(screen, self.destination))

    def test_unsupported_or_removed_cartridge_focuses_check(self):
        self.prime((self.unported,))
        state = self.service.select_profile(self.unported.id)
        screen = Screen(selected=3)
        screen.apply(state)
        self.assertEqual(screen.selected, 0)
        screen.selected = 2
        screen.apply(PollState('Connected', 'No cartridge', False, (), None, False))
        self.assertEqual(screen.selected, 0)

    def test_periodic_poll_does_not_move_user_cursor_after_initial_focus(self):
        self.prime((self.first, self.second))
        state = self.service.snapshot('Detected')
        screen = Screen()
        screen.apply(state)
        for selected in range(5):
            screen.selected = selected
            screen.apply(state)
            self.assertEqual(screen.selected, selected)

    def test_completed_auto_dump_keeps_game_start_focus_when_candidates_collapse(self):
        screen = Screen(candidates=(self.first, self.second), profile=self.second,
                        saved=True, selected=3)
        state = PollState('Connected', 'Verified', True, (self.second,), self.second, True)
        screen.apply(state)
        self.assertEqual(screen.selected, 3)

    def test_poll_does_not_reopen_cancelled_picker_or_erase_save_result(self):
        screen = Screen()
        state = PollState('Connected', 'Choose', False, (self.first, self.second), None, False, True)
        screen.apply(state)
        self.assertEqual(screen.view, 'main')
        screen.view = 'main'
        screen.apply(state)
        self.assertEqual(screen.view, 'main')
        selected = PollState('Connected', 'DB candidate selected', True, (self.first, self.second), self.second, True)
        screen.apply(selected)
        screen.message = 'Saved and verified.'
        screen.apply(selected)
        self.assertEqual(screen.message, 'Saved and verified.')

    def test_removal_disables_dump_but_keeps_correct_saved_game_available(self):
        self.save(self.second, self.second_data)
        self.prime()
        self.service.select_profile(self.second.id)
        self.service.adapter.status.return_value = 0
        self.service.poll()
        self.service.poll()
        state = self.service.poll()
        self.assertFalse(state.supported)
        self.assertEqual(state.candidates, ())
        self.assertTrue(state.saved)
        self.assertEqual(state.profile, self.second)

    def test_new_insertion_never_launches_the_previous_game(self):
        self.save(self.second, self.second_data)
        self.prime()
        self.service.select_profile(self.second.id)
        self.service.adapter.status.return_value = 0
        for _ in range(3):
            self.service.poll()
        self.service.adapter.status.return_value = 3
        with patch('cart_station_service.identify',
                   return_value=(self.first, self.second, self.unported)):
            for _ in range(3):
                state = self.service.poll()
        self.assertTrue(state.selection_pending)
        self.assertIsNone(state.profile)
        self.assertFalse(state.saved)
        screen = Screen(saved=True, core_ready=True)
        screen.apply(state)
        self.assertFalse(screen.enabled()[3])
        with self.assertRaisesRegex(ProtocolError, 'No game selected'):
            self.service.prepare_game()

    def test_reinserted_header_cartridge_keeps_its_verified_profile(self):
        from cart_station_service import HeaderSystem
        from cart_station_gb import GbProfile, GbDatabase
        header = bytes(80)
        detected = GbProfile(header, 'HEADER', 'MBC1', 65536, 'GB')
        verified = replace(detected, title='DB title', raw_sha1='a' * 40)
        system = HeaderSystem(5, GbProfile, 'GB/GBC', 'gb_database', GbDatabase,
                              lambda adapter, progress: detected, Mock(), Mock(), Mock())
        service = SimpleNamespace(adapter=Mock(), identification_progress=None, recent_profile=verified)
        self.assertIs(system.identify_profile(service), verified)
        service.recent_profile = replace(verified, header=b'\1' * 80)
        self.assertIs(system.identify_profile(service), detected)

    def test_polling_reuses_the_conflict_listing_briefly(self):
        self.prime()
        self.service.select_profile(self.first.id)
        self.destination.mkdir(parents=True)
        self.service.file_cache.clear()
        with patch.object(Path, 'iterdir', autospec=True, side_effect=lambda path: iter(())) as listing:
            self.service.existing(cached=True)
            self.service.existing(cached=True)
            self.assertEqual(listing.call_count, 1)
            self.service.existing()
            self.assertEqual(listing.call_count, 2)
            with patch('cart_station_service.time.monotonic', return_value=float('inf')):
                self.service.existing(cached=True)
            self.assertEqual(listing.call_count, 3)

    def test_error_log_keeps_one_previous_generation(self):
        from cart_station_manager import LOG_LIMIT_BYTES, log_error
        path = self.root / 'runtime' / 'manager.log'
        path.parent.mkdir()
        path.write_bytes(b'x' * LOG_LIMIT_BYTES)
        log_error(ValueError('new entry'), path)
        self.assertEqual(path.with_name('manager.log.1').stat().st_size, LOG_LIMIT_BYTES)
        self.assertIn('ValueError: new entry', path.read_text(encoding='utf-8'))
        log_error(ValueError('second'), path)
        self.assertEqual(len(path.read_text(encoding='utf-8').splitlines()), 2)

    @patch('time.sleep')
    def test_nrom_cancel_mid_read_restores_cartridge_mode(self, sleep):
        from cart_station_transport import Adapter
        from cart_station_nes_core import dump
        from test_cart_station import Wire, memory, status
        wire = Wire(status() + bytes(16) + memory(self.first_data[16:16400]) + bytes(16))
        adapter = Adapter(wire)

        def cancel_after_first_bank(stage, done, total):
            if done:
                raise UserCancelled('stop')

        with self.assertRaises(UserCancelled):
            dump(adapter, 16, 8, 'horizontal', progress=cancel_after_first_bank)
        self.assertEqual(len(adapter.control_replies), 4)
        self.assertFalse(wire.replies)

    @patch('cart_station_nes_mappers.time.sleep')
    def test_mapped_cancel_mid_read_restores_cartridge_mode(self, sleep):
        from cart_station_nes_db import dump_profile
        from test_cart_station_nes_mappers import Cartridge, profile_for
        prg = b'P' * 32768
        profile = profile_for(2, prg, b'')
        adapter = Cartridge(2, prg, b'')

        def cancel_after_first_bank(stage, done, total):
            if done:
                raise UserCancelled('stop')

        with self.assertRaises(UserCancelled):
            dump_profile(adapter, profile, progress=cancel_after_first_bank)
        self.assertEqual(adapter.modes, [1, 0])
        self.assertEqual(len(adapter.writes), 1)

    def test_compact_pipe_error_keeps_recovery_hint_and_disables_dump(self):
        screen = Screen(profile=self.first, reader='Not connected / select CHECK to retry',
                        supported=False, error=True, message='[Errno 32] Pipe error',
                        recovery_hint='Check cartridge/USB connection; select CHECK / RETRY.')
        for height in (16, 20, 21, 25):
            with self.subTest(height=height):
                picture = render(screen, self.destination, width=80, height=height)
                self.assertIn('ERROR: [Errno 32] Pipe error', picture)
                self.assertIn('Check cartridge/USB connection; select CHECK / RETRY.', picture)
                self.assertIn('DUMP CARTRIDGE  [unavailable]', picture)
                self.assertLessEqual(len(picture.split('\r\n')), height - 1)
                if height >= 21:
                    self.assertIn('Reader: Not connected', picture)
                else:
                    self.assertNotIn('Reader:', picture)

    def test_capture_pipe_error_updates_reader_and_explicit_retry_restores_menu(self):
        self.prime((self.first,))
        self.service.select_profile(self.first.id)
        self.service.product = 'Connected'
        test = self

        class DisconnectTerminal:
            text = ''
            reads = 0
            stopped = False
            recovered = False

            def resume(self): pass
            def pause(self): pass
            def discard(self): pass
            def draw(self, text): self.text = text

            def read(self):
                self.reads += 1
                if self.reads > 300:
                    raise AssertionError('USB recovery menu flow did not finish')
                if 'Exit to MiSTer?' in self.text:
                    return ['left' if '[NO]' in self.text else 'select']
                if 'ERROR: [Errno 32] Pipe error' in self.text:
                    test.assertIn('Reader: Not connected', self.text)
                    test.assertNotIn('Reader: Connected', self.text)
                    test.assertIn('Check cartridge/USB connection; select CHECK / RETRY.', self.text)
                    test.assertIn('DUMP CARTRIDGE  [unavailable]', self.text)
                    self.stopped = True
                    return ['select']  # CHECK / RETRY receives focus after failure.
                if self.stopped and 'Reader: Connected' in self.text:
                    self.recovered = True
                    test.assertNotIn('Pipe error', self.text)
                    test.assertNotIn('Check cartridge/USB connection;', self.text)
                    test.assertNotIn('DUMP CARTRIDGE  [unavailable]', self.text)
                    return ['back']
                if 'Start reading the selected cartridge?' in self.text:
                    return ['left' if '[NO]' in self.text else 'select']
                return ['select'] if '\x1b[7m > DUMP CARTRIDGE' in self.text else ['down']

        def poll():
            self.service.adapter = Mock()
            self.service.product = 'Connected'
            self.service.candidates = (self.first,)
            self.service.profile = self.first
            self.service.selected_id = self.first.id
            return self.service.snapshot('Synthetic cartridge')

        def disconnect(progress):
            self.service.close()
            raise OSError(32, 'Pipe error')

        terminal = DisconnectTerminal()
        args = SimpleNamespace(output_dir=self.destination, paths_config=None, preview=False,
                               port=None, usb=True, core_config=self.config)
        fake_fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4)
        with patch.dict('sys.modules', {'fcntl': fake_fcntl}), \
                patch('cart_station_manager.open', mock_open(), create=True), \
                patch('cart_station_manager.sys.stdin.isatty', return_value=True), \
                patch('cart_station_manager.sys.stdout.isatty', return_value=True), \
                patch('cart_station_manager.Service', return_value=self.service), \
                patch.object(self.service, 'poll', side_effect=poll), \
                patch.object(self.service, 'capture', side_effect=disconnect) as capture, \
                patch('cart_station_manager.log_error'), \
                patch('cart_station_manager.Terminal', return_value=terminal), \
                patch('cart_station_manager.ThreadPoolExecutor', return_value=ImmediateExecutor()), \
                patch('cart_station_manager.time.monotonic', side_effect=[n * 0.2 for n in range(1000)]):
            run_manager(args, 'mister')
        self.assertTrue(terminal.stopped)
        self.assertTrue(terminal.recovered)
        capture.assert_called_once()
        self.assertFalse(self.destination.exists())

    def test_pad_can_back_out_of_picker_and_choose_no_without_dumping(self):
        self.prime((self.first, self.second))

        class CancelPad:
            text = ''
            reads = 0
            step = 0

            def resume(self):
                pass

            def pause(self):
                pass

            def discard(self):
                pass

            def draw(self, text):
                self.text = text

            def read(self):
                self.reads += 1
                if self.reads > 300:
                    raise AssertionError('Cancel flow did not finish')
                if 'SELECT CARTRIDGE /' in self.text:
                    if self.step == 0:
                        self.step = 1
                        return ['back']
                    self.step = 2
                    return ['select']
                if 'Start reading the selected cartridge?' in self.text:
                    self.step = 3
                    return ['select']  # NO is the default.
                if 'Exit to MiSTer?' in self.text:
                    return ['left' if '[NO]' in self.text else 'select']
                if self.step == 3:
                    return ['back']
                label = 'SELECT CARTRIDGE' if self.step in (0, 1) else 'DUMP CARTRIDGE'
                return ['select'] if '\x1b[7m > ' + label in self.text else ['down']

        args = SimpleNamespace(output_dir=self.destination, paths_config=None, preview=False,
                               port=None, usb=True, core_config=self.config)
        fake_fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4)
        with patch.dict('sys.modules', {'fcntl': fake_fcntl}), \
                patch('cart_station_manager.open', mock_open(), create=True), \
                patch('cart_station_manager.sys.stdin.isatty', return_value=True), \
                patch('cart_station_manager.sys.stdout.isatty', return_value=True), \
                patch('cart_station_manager.Service', return_value=self.service), \
                patch.object(self.service, 'poll', side_effect=lambda: self.service.snapshot('Candidates ready')), \
                patch.object(self.service, 'capture') as capture, \
                patch('cart_station_manager.Terminal', return_value=CancelPad()), \
                patch('cart_station_manager.ThreadPoolExecutor', return_value=ImmediateExecutor()), \
                patch('cart_station_manager.time.monotonic', side_effect=[n * 0.2 for n in range(1000)]), \
                patch('cart_station_manager.send_launch') as send:
            run_manager(args, 'mister')
        capture.assert_not_called()
        send.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_pad_flow_selects_second_candidate_confirms_dumps_and_launches_it(self):
        destination = self.destination
        second_path = destination / self.second.filename()

        class PadTerminal:
            text = ''
            reads = 0

            def resume(self):
                pass

            def pause(self):
                pass

            def discard(self):
                pass

            def draw(self, text):
                self.text = text

            def read(self):
                self.reads += 1
                if self.reads > 300:
                    raise AssertionError('Pad flow did not finish')
                if 'SELECT CARTRIDGE /' in self.text:
                    return ['select'] if '\x1b[7m > Second game' in self.text else ['down']
                if 'Start reading the selected cartridge?' in self.text or 'Start the verified game?' in self.text:
                    return ['left' if '[NO]' in self.text else 'select']
                if 'Game: Second game' not in self.text:
                    if 'Game: Select the cartridge you inserted' in self.text:
                        return ['select'] if '\x1b[7m > SELECT CARTRIDGE' in self.text else ['down']
                    return []
                if not getattr(self, 'selected_default_checked', False):
                    assert '\x1b[7m > DUMP CARTRIDGE' in self.text
                    self.selected_default_checked = True
                if second_path.exists() and not getattr(self, 'saved_default_checked', False):
                    assert '\x1b[7m > GAME START' in self.text
                    self.saved_default_checked = True
                label = 'GAME START' if second_path.exists() else 'DUMP CARTRIDGE'
                if '\x1b[7m > ' + label in self.text:
                    return [] if label + '  [unavailable]' in self.text else ['select']
                return ['down']

        args = SimpleNamespace(output_dir=self.destination, paths_config=None, preview=False,
                               port=None, usb=True, core_config=self.config, db=None)
        fake_fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4)
        adapter, transport = Mock(), Mock()
        adapter.status.return_value = 3
        with patch.dict('sys.modules', {'fcntl': fake_fcntl}), \
                patch('cart_station_manager.open', mock_open(), create=True), \
                patch('cart_station_manager.sys.stdin.isatty', return_value=True), \
                patch('cart_station_manager.sys.stdout.isatty', return_value=True), \
                patch('cart_station_manager.Service', return_value=self.service), \
                patch('cart_station_service.open_transport', return_value=transport), \
                patch('cart_station_service.Adapter', return_value=adapter), \
                patch('cart_station_service.identify', return_value=(self.first, self.second)), \
                patch('cart_station_service.auto_read_group', return_value=()), \
                patch('cart_station_service.dump_profile', return_value=self.second_data) as dumper, \
                patch('cart_station_manager.Terminal', return_value=PadTerminal()), \
                patch('cart_station_manager.ThreadPoolExecutor', return_value=ImmediateExecutor()), \
                patch('cart_station_manager.time.monotonic', side_effect=[n * 0.2 for n in range(1000)]), \
                patch('cart_station_manager.send_launch') as send:
            run_manager(args, 'mister')
        self.assertEqual(second_path.read_bytes(), self.second_data)
        self.assertFalse((destination / self.first.filename()).exists())
        self.assertEqual(dumper.call_args.args[1], self.second)
        self.assertEqual(ET.parse(send.call_args.args[0]).find('file').get('path'), str(second_path.resolve()))
        transport.close.assert_called_once()

    def test_same_geometry_readable_candidates_are_read_together_and_hashes_pick_the_title(self):
        self.prime((self.first, self.second))
        self.service.auto_group = (self.first, self.second)
        state = self.service.snapshot('x')
        self.assertEqual(state.auto, 2)
        self.assertFalse(state.selection_pending)
        screen = Screen()
        screen.apply(state)
        self.assertTrue(screen.enabled()[2])
        self.assertFalse(screen.enabled()[3])
        self.assertIn('Auto-detect among 2', render(screen, self.destination))
        with patch('cart_station_service.identify', return_value=(self.first, self.second)), \
                patch('cart_station_service.dump_candidates', return_value=(self.second, self.second_data)) as dumper:
            target, created = self.service.capture(lambda *args: None)
        self.assertTrue(created)
        self.assertEqual(target, self.destination / self.second.filename())
        self.assertEqual(target.read_bytes(), self.second_data)
        self.assertEqual(dumper.call_args.args[1], (self.first, self.second))

    def test_auto_read_is_blocked_when_the_cartridge_candidates_changed(self):
        self.prime((self.first, self.second))
        self.service.auto_group = (self.first, self.second)
        with patch('cart_station_service.identify', return_value=(self.first,)), \
                patch('cart_station_service.dump_candidates') as dumper:
            with self.assertRaisesRegex(ProtocolError, 'Cartridge changed'):
                self.service.capture(lambda *args: None)
        dumper.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_unported_or_mixed_candidates_stay_a_manual_choice(self):
        self.assertEqual(auto_read_group((self.first, self.second, self.unported)), ())
        self.assertEqual(auto_read_group((self.first,)), ())
        self.assertEqual(auto_read_group((self.first, self.second)), (self.first, self.second))


class RetryCleanupTests(unittest.TestCase):
    def test_retry_logs_close_failure_and_reconnects(self):
        service = Service(Path('unused'), database=Database([]))
        transport = Mock()
        cleanup_error = OSError('reader cleanup failed')
        transport.close.side_effect = cleanup_error
        service.transport = transport
        service.adapter = Mock()
        service.file_cache['stale'] = True
        state = object()
        with patch.object(service, 'poll', return_value=state) as poll:
            with self.assertLogs('cart_station_service', level='WARNING') as logs:
                self.assertIs(service.retry(), state)
        transport.close.assert_called_once()
        poll.assert_called_once_with()
        self.assertIsNone(service.transport)
        self.assertIsNone(service.adapter)
        self.assertEqual(service.product, 'Not connected')
        self.assertEqual(service.snapshot('Stopped').reader, 'Not connected')
        self.assertEqual(service.file_cache, {})
        self.assertIs(logs.records[0].exc_info[1], cleanup_error)
        self.assertIn('reader cleanup failed', logs.records[0].getMessage())

    def test_retry_preserves_reconnection_error_after_close_failure(self):
        service = Service(Path('unused'), database=Database([]))
        reconnect_error = ProtocolError('reader reconnect failed')
        with patch.object(service, 'close', side_effect=OSError('cleanup failed')):
            with patch.object(service, 'poll', side_effect=reconnect_error):
                with self.assertLogs('cart_station_service', level='WARNING'):
                    with self.assertRaises(ProtocolError) as raised:
                        service.retry()
        self.assertIs(raised.exception, reconnect_error)


if __name__ == '__main__':
    unittest.main()
