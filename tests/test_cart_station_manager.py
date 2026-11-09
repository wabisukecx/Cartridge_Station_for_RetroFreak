from _paths import ROOT, data  # noqa: F401
import hashlib
import json
import stat
import tempfile
import unittest
from concurrent.futures import Future
from unittest.mock import mock_open
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from test_cart_station_mapper206 import MAPPER206_PROFILE
from cart_station_manager import (KeyDecoder, PollState, Screen, Service, core_available, create_mgl,
                        read_core_config, render, run_manager, send_launch, verified_file)
from cart_station_nes_db import Database, Profile


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = {'rbf': '_Console/NES', 'type': 'f', 'index': 1, 'delay': 2}
        self.core_dir = self.root / '_Console'
        self.core_dir.mkdir()
        (self.core_dir / 'NES_20260823.rbf').write_bytes(b'fake core')
        self.config = self.root / 'cores.json'
        self.config.write_text(json.dumps({'version': 1, 'NES': self.core}), encoding='utf-8')
        prg, chr_data = bytes(range(256)) * 128, b'C' * 16384
        self.payload = MAPPER206_PROFILE['header'] + prg + chr_data
        self.hash_patch = patch.dict(MAPPER206_PROFILE, {'file_sha1': hashlib.sha1(self.payload).hexdigest(),
                                                   'prg_sha1': hashlib.sha1(prg).hexdigest(),
                                                   'chr_sha1': hashlib.sha1(chr_data).hexdigest(),
                                                   'raw_sha1': hashlib.sha1(prg + chr_data).hexdigest()})
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)
        self.destination = self.root / 'games' / 'NES'
        self.profile = Profile.parse({'id': '610a40eb37b7a0c27d2fa02a19feeafeccfcdc18-m206-s1', 'title': MAPPER206_PROFILE['title'],
                                      'mapper': 206, 'submapper': 1, 'prg_size': 32768, 'chr_size': 16384,
                                      'header': MAPPER206_PROFILE['header'].hex(),
                                      **{key: MAPPER206_PROFILE[key] for key in
                                         ('prg_sha1', 'chr_sha1', 'raw_sha1', 'file_sha1')}})

    def rom(self, directory=None):
        directory = directory or self.destination
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / MAPPER206_PROFILE['rom_name']
        path.write_bytes(self.payload)
        return path

    def service(self):
        return Service(self.destination, core_path=self.config, sd_root=self.root,
                       database=Database([self.profile]))

    def test_menu_follows_check_select_dump_start_workflow(self):
        screen = Screen(profile=self.profile, candidates=(self.profile,), supported=True,
                        saved=True, core_ready=True)
        labels = ('CHECK / RETRY', 'SELECT CARTRIDGE', 'DUMP CARTRIDGE',
                  'GAME START', 'EXIT TO MiSTer')
        picture = render(screen, self.destination)
        menu = [line.replace('\x1b[7m', '').replace('\x1b[0m', '').strip().lstrip('> ').strip()
                for line in picture.splitlines()]
        self.assertEqual([line for line in menu if line in labels], list(labels))

    def test_workflow_actions_keep_separate_selection_dump_and_start_gates(self):
        screen = Screen(candidates=(self.profile,), selection_pending=True)
        self.assertEqual(screen.enabled(), [True, True, False, False, True])
        screen.selection_pending, screen.supported = False, True
        self.assertEqual(screen.enabled(), [True, True, True, False, True])
        screen.saved, screen.core_ready = True, True
        self.assertTrue(all(screen.enabled()))
        screen.busy = True
        self.assertFalse(any(screen.enabled()))

    def test_core_settings_and_dated_core_discovery(self):
        self.assertEqual(read_core_config(self.config), self.core)
        self.assertTrue(core_available(self.core, self.root))
        self.assertFalse(core_available(dict(self.core, rbf='_Console/SNES'), self.root))

    def test_invalid_core_settings_are_rejected(self):
        for changes in ({'rbf': '../NES'}, {'rbf': '/root/NES'}, {'index': 0}, {'type': 's'},
                        {'delay': 0}, {'rbf': '_Console/NES\nload_core other'}, {'delay': True}):
            self.config.write_text(json.dumps({'version': 1, 'NES': dict(self.core, **changes)}), encoding='utf-8')
            with self.assertRaises(ProtocolError):
                read_core_config(self.config)

    def test_mgl_uses_verified_absolute_rom_and_nes_slot_one(self):
        rom = self.rom()
        launch = create_mgl(rom, self.core, self.root / 'runtime', self.root, self.profile)
        xml = ET.parse(launch).getroot()
        self.assertEqual(xml.tag, 'mistergamedescription')
        self.assertEqual(xml.findtext('rbf'), '_Console/NES')
        self.assertEqual(xml.find('file').attrib, {'type': 'f', 'index': '1', 'delay': '2', 'path': str(rom.resolve())})
        self.assertEqual(rom.read_bytes(), self.payload)

    def test_bad_rom_never_creates_launch(self):
        rom = self.rom()
        rom.write_bytes(self.payload[:-1] + b'Z')
        self.assertFalse(verified_file(rom, self.profile))
        with self.assertRaises(ProtocolError):
            create_mgl(rom, self.core, self.root / 'runtime', self.root, self.profile)
        self.assertFalse((self.root / 'runtime').exists())

    def test_conflicting_target_never_overwrites(self):
        target = self.rom()
        target.write_bytes(b'keep this file')
        with self.assertRaises(ProtocolError):
            self.service().prepare_game()
        self.assertEqual(target.read_bytes(), b'keep this file')

    @patch('cart_station_service.open_transport')
    @patch('cart_station_service.Adapter')
    @patch('cart_station_service.identify')
    def test_cartridge_detection_waits_for_three_samples(self, identifier, adapter_factory, opener):
        service = self.service()
        adapter = adapter_factory.return_value
        adapter.product.return_value = {'build': 10}
        adapter.status.return_value = 3
        identifier.return_value = (self.profile,)
        self.assertFalse(service.poll().supported)
        self.assertFalse(service.poll().supported)
        identifier.assert_not_called()
        self.assertTrue(service.poll().supported)
        self.assertTrue(service.poll().supported)
        identifier.assert_called_once_with(adapter, service.database)

    @patch('cart_station_service.open_transport')
    @patch('cart_station_service.Adapter')
    @patch('cart_station_service.identify', return_value=())
    def test_unknown_cartridge_is_identified_without_mapper_writes(self, identifier, adapter_factory, opener):
        service = self.service()
        adapter = adapter_factory.return_value
        adapter.product.return_value = {'build': 10}
        adapter.status.return_value = 3
        adapter.nrom.return_value = b'other game'
        service.poll()
        service.poll()
        self.assertFalse(service.poll().supported)
        adapter.nes.mapper206_register.assert_not_called()

    @patch('cart_station_service.open_transport')
    @patch('cart_station_service.Adapter')
    def test_poll_error_closes_session_for_explicit_retry(self, adapter_factory, opener):
        service = self.service()
        adapter_factory.return_value.product.side_effect = OSError('unplugged')
        with self.assertRaises(OSError):
            service.poll()
        opener.return_value.close.assert_called_once()
        self.assertIsNone(service.adapter)

    @patch('cart_station_service.identify')
    @patch('cart_station_service.dump_profile')
    def test_capture_verifies_saved_file(self, dumper, identifier):
        service = self.service()
        service.adapter, service.profile, service.selected_id = Mock(), self.profile, self.profile.id
        identifier.return_value = (self.profile,)
        dumper.return_value = self.payload
        target, created = service.capture(Mock())
        self.assertTrue(created)
        self.assertTrue(verified_file(target, self.profile))
        self.assertFalse(service.capture(Mock())[1])

    def test_capture_requires_identified_cartridge(self):
        with self.assertRaises(ProtocolError):
            self.service().capture(Mock())

    @patch('cart_station_service.identify')
    @patch('cart_station_service.dump_profile')
    def test_capture_preserves_broken_file_and_launches_verified_alternate(self, dumper, identifier):
        service = self.service()
        service.adapter, service.profile, service.selected_id = Mock(), self.profile, self.profile.id
        identifier.return_value = (self.profile,)
        dumper.return_value = self.payload
        original = self.rom()
        original.write_bytes(self.payload[:100])  # partial save from an older version
        target, created = service.capture(Mock())
        self.assertTrue(created)
        self.assertTrue(verified_file(target, self.profile))
        self.assertEqual(original.read_bytes(), self.payload[:100])
        self.assertNotEqual(target, original)
        self.assertEqual(service.existing(), target)
        self.assertFalse(service.capture(Mock())[1])
        mgl = service.prepare_game()
        self.assertIn(str(target.resolve()), mgl.read_text())

    @patch('cart_station_service.open_transport')
    @patch('cart_station_service.Adapter')
    def test_status_messages_accept_observed_empty_and_keep_other_codes(self, adapter_factory, opener):
        adapter = adapter_factory.return_value
        adapter.product.return_value = {'build': 10}
        for value, text in ((0, 'No cartridge detected'),
                            (255, 'No cartridge detected'),
                            (254, 'Multiple cartridges detected; leave only one inserted'),
                            (42, 'Different console / type 42'),
                            (7, 'Different console / type 7')):
            service = self.service()
            adapter.status.return_value = value
            states = [service.poll() for _ in range(3)]
            self.assertEqual(states[-1].cartridge, text)
            self.assertFalse(states[-1].supported)

    @patch('cart_station_service.open_transport')
    @patch('cart_station_service.Adapter')
    def test_multiple_insertion_disables_capture_and_remaining_fc_is_identified(self, adapter_factory, opener):
        service = self.service()
        adapter = adapter_factory.return_value
        adapter.product.return_value = {'build': 10}
        adapter.status.return_value = 254
        service.selected_id = self.profile.id
        service.candidates = (self.profile,)
        service.auto_group = (self.profile,)
        with patch('cart_station_service.identify') as identify:
            for _ in range(3):
                state = service.poll()
            self.assertFalse(state.supported)
            self.assertFalse(state.auto)
            self.assertEqual(state.candidates, ())
            self.assertIn('leave only one', state.cartridge)
            identify.assert_not_called()
        adapter.status.return_value = 3
        with patch('cart_station_service.identify', return_value=(self.profile,)) as identify:
            for _ in range(3):
                service.poll()
            identify.assert_called_once()

    def test_buttons_enabled_only_in_valid_states(self):
        screen = Screen()
        self.assertEqual(screen.enabled(), [True, False, False, False, True])
        screen.supported = screen.saved = screen.core_ready = True
        screen.candidates = (self.profile,)
        self.assertTrue(all(screen.enabled()))
        screen.busy = True
        self.assertFalse(any(screen.enabled()))

    def test_render_shows_safe_default_confirmation_and_progress(self):
        screen = Screen(confirm='Start reading?', reader='bad\x1b[Hname')
        picture = render(screen, self.destination)
        self.assertIn('Yes   [NO]', picture)
        self.assertNotIn('bad\x1b', picture)
        screen.busy, screen.percent, screen.stage = True, 50, 'Read 1/2'
        self.assertIn('50%', render(screen, self.destination))
        self.assertIn('Read 1/2', render(screen, self.destination))

    def test_small_tv_layout_keeps_actions_and_confirmation_visible(self):
        screen = Screen(confirm='Start reading?')
        picture = render(screen, self.destination, width=80, height=16)
        self.assertLessEqual(len(picture.split('\r\n')), 15)
        self.assertIn('GAME START', picture)
        self.assertIn('EXIT TO MiSTer', picture)
        self.assertIn('Yes   [NO]', picture)

    def test_partial_terminal_arrows_and_standalone_back(self):
        decoder = KeyDecoder()
        self.assertEqual(decoder.feed(b'\x1b[', 10), [])
        self.assertEqual(decoder.feed(b'A\r', 10.01), ['up', 'select'])
        self.assertEqual(decoder.feed(b'\x1b', 11), [])
        self.assertEqual(decoder.feed(b'', 11.2), ['back'])

    @patch('cart_station_manager.os.open', return_value=999)
    @patch('cart_station_manager.os.write')
    @patch('cart_station_manager.os.close')
    def test_launch_is_one_nonblocking_fifo_write(self, close, write, opener):
        launch = self.root / 'A & B.mgl'
        fifo = Mock()
        fifo.stat.return_value.st_mode = stat.S_IFIFO
        expected = ('load_core ' + str(launch.resolve()) + '\n').encode()
        write.return_value = len(expected)
        # Windows lacks O_CLOEXEC; the production call runs only on Linux.
        with patch('cart_station_manager.os.O_CLOEXEC', 0, create=True), patch('cart_station_manager.os.O_NONBLOCK', 2048, create=True):
            send_launch(launch, fifo)
        write.assert_called_once_with(999, expected)
        close.assert_called_once_with(999)
        self.assertTrue(opener.call_args.args[1] & 2048)

    @patch('cart_station_service.open_transport')
    @patch('builtins.print')
    def test_preview_does_not_open_hardware(self, output, opener):
        args = SimpleNamespace(output_dir=self.destination, paths_config=None, preview=True)
        run_manager(args, 'pc')
        opener.assert_not_called()
        self.assertIn('GAME START', output.call_args.args[0])

    def test_menu_game_start_confirmation_and_handoff(self):
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

        class MenuTerminal:
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
                if self.reads > 100:
                    raise AssertionError('Menu did not hand off to the game')
                if 'Start the verified game?' in self.text:
                    return ['left' if '[NO]' in self.text else 'select']
                if '\x1b[7m > GAME START' in self.text:
                    return ['select']
                return ['down']

        service = Mock()
        service.profile = self.profile
        service.sd_root = self.root
        service.existing.return_value = self.rom()
        service.poll.return_value = PollState('Connected', 'FC/NES: DB candidate selected', True,
                                             (self.profile,), self.profile, True)
        service.prepare_game.return_value = self.root / 'launch.mgl'
        args = SimpleNamespace(output_dir=self.destination, paths_config=None, preview=False,
                               port=None, usb=True, core_config=self.config)
        fake_fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4)
        with patch.dict('sys.modules', {'fcntl': fake_fcntl}), \
                patch('cart_station_manager.open', mock_open(), create=True), \
                patch('cart_station_manager.sys.stdin.isatty', return_value=True), \
                patch('cart_station_manager.sys.stdout.isatty', return_value=True), \
                patch('cart_station_manager.Service', return_value=service), \
                patch('cart_station_manager.Terminal', return_value=MenuTerminal()), \
                patch('cart_station_manager.ThreadPoolExecutor', return_value=ImmediateExecutor()), \
                patch('cart_station_manager.time.monotonic', side_effect=[n * 0.1 for n in range(200)]), \
                patch('cart_station_manager.send_launch') as send:
            run_manager(args, 'mister')
        service.capture.assert_not_called()
        service.prepare_game.assert_called_once()
        send.assert_called_once_with(self.root / 'launch.mgl')
        service.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
