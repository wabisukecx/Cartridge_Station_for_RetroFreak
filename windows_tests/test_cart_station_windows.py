"""Desktop tests use synthetic carts only; never enumerate or open real readers."""
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from PyQt6.QtTest import QTest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from cart_station_protocol import ProtocolError
from cart_station_storage import save_verified_rom
from cart_station_manager import PollState
from cart_station_nes_db import Database, Profile
from cart_station_windows import CartStationWindow


def synthetic(name, value):
    prg, chr_data = bytes((value,)) * 16384, bytes((value + 1,)) * 8192
    header = b'NES\x1a' + bytes((1, 1, 0, 8)) + bytes(8)
    raw = prg + chr_data
    sha = lambda value: hashlib.sha1(value).hexdigest()
    profile = Profile.parse(dict(id=name, title=name, mapper=0, submapper=0,
                                 prg_size=len(prg), chr_size=len(chr_data), header=header.hex(),
                                 prg_sha1=sha(prg), chr_sha1=sha(chr_data), raw_sha1=sha(raw)))
    return profile, header + raw


class FakeService:
    def __init__(self, destination, **options):
        self.destination, self.options = destination, options
        self.first, self.first_data = synthetic('First game', 20)
        self.second, self.second_data = synthetic('Second game', 40)
        self.candidates = (self.first, self.second)
        self.profile = None
        self.auto = 0
        self.calls = [('create', threading.get_ident())]
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False
        self.error = None
        self.captures = 0

    def poll(self):
        self.calls.append(('poll', threading.get_ident()))
        if self.error:
            raise self.error
        return self.snapshot('FC/NES: choose a candidate')

    def select_profile(self, identifier):
        self.calls.append(('select', threading.get_ident()))
        self.profile = next(p for p in self.candidates if p.id == identifier)
        return self.snapshot('FC/NES: DB candidate selected')

    def snapshot(self, message):
        return PollState('Connected', message, self.profile is not None and self.profile.support() is None, self.candidates,
                         self.profile, False, self.profile is None and not self.auto, self.auto)

    def capture(self, progress):
        self.calls.append(('capture', threading.get_ident()))
        self.captures += 1
        self.entered.set()
        progress('Read 1/2', 0, 100)
        while self.block and not self.release.wait(0.01):
            progress('Read 1/2', 20, 100)
        progress('Read 2/2', 80, 100)
        if self.profile is None and self.auto:
            self.profile = self.second
            self.auto = 0
        payload = self.second_data if self.profile == self.second else self.first_data
        progress('Verifying and saving...', 100, 100)
        self.destination.mkdir(parents=True, exist_ok=True)
        return save_verified_rom(self.destination / self.profile.filename(), payload)

    def close(self):
        self.calls.append(('close', threading.get_ident()))


class WindowsGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.destination = Path(self.temporary.name) / 'output'
        self.services = []
        self.ports = patch('serial.tools.list_ports.comports', return_value=[])
        self.ports.start()

        def factory(**values):
            service = FakeService(**values)
            self.services.append(service)
            return service

        self.window = CartStationWindow(self.destination, port='COM5', service_factory=factory)
        self.window.timer.stop()
        self.window.show()

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            self.app.processEvents()
            if time.monotonic() >= deadline:
                self.fail('Desktop worker did not finish in time')
            QTest.qWait(5)

    def tearDown(self):
        for service in self.services:
            service.release.set()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.window.close()
        self.wait_for(lambda: not self.window.thread.isRunning())
        self.app.processEvents()
        self.window.deleteLater()
        self.ports.stop()
        self.temporary.cleanup()

    def connect_reader(self):
        self.window.toggle_connection()
        self.wait_for(lambda: not self.window.task)
        self.assertTrue(self.window.connected)
        return self.services[-1]

    def test_branding_names_windows_as_an_edition_of_the_common_product(self):
        from PyQt6.QtWidgets import QLabel
        self.assertEqual(self.window.windowTitle(),
                         'Cartridge Station for RetroFreak — Windows Edition')
        self.assertEqual(self.window.findChild(QLabel, 'title').text(),
                         'Cartridge Station for RetroFreak')
        self.assertTrue(any('Windows Edition' in label.text()
                            for label in self.window.findChildren(QLabel)))

    def test_normal_window_has_no_full_database_search(self):
        from PyQt6.QtWidgets import QPushButton
        self.connect_reader()
        labels = [button.text() for button in self.window.findChildren(QPushButton)]
        self.assertNotIn('Search FC database…', labels)
        self.assertEqual(self.window.candidates.rowCount(), 2)
        self.select_second()
        self.assertTrue(self.window.dump.isEnabled())

    def test_no_detected_candidates_does_not_offer_manual_catalog_or_reading(self):
        service = self.connect_reader()
        service.candidates, service.profile = (), None
        self.window._apply_state(service.snapshot('FC/NES: no DB candidates'))
        self.window._enabled()
        self.assertEqual(self.window.candidates.rowCount(), 0)
        self.assertFalse(self.window.dump.isEnabled())
        self.assertEqual(self.window.title.text(), 'No cartridge selected')
        self.assertEqual(service.captures, 0)

    def test_removed_catalog_worker_command_cannot_select_or_capture(self):
        service = self.connect_reader()
        self.window._send('catalog_select', ('outside-candidates', 1))
        self.wait_for(lambda: not self.window.task and not self.window.connected)
        self.assertIn('Unknown reader operation', self.window.result.text())
        self.assertEqual(service.captures, 0)
        self.assertFalse(self.destination.exists())

    def select_second(self):
        self.window.candidates.selectRow(1)
        self.wait_for(lambda: not self.window.task)
        self.assertTrue(self.window.dump.isEnabled())

    def confirm(self, answer=QMessageBox.StandardButton.Yes):
        with patch.object(QMessageBox, 'question', return_value=answer):
            self.window.confirm_capture()

    def test_startup_opens_no_reader_and_never_starts_capture(self):
        self.app.processEvents()
        self.assertEqual(self.services, [])
        self.assertFalse(self.window.dump.isEnabled())
        self.assertFalse(self.destination.exists())

    def test_select_no_confirmation_saves_nothing(self):
        service = self.connect_reader()
        self.select_second()
        self.confirm(QMessageBox.StandardButton.No)
        self.assertEqual(service.captures, 0)
        self.assertFalse(self.destination.exists())

    def test_poll_keeps_selected_row_and_keyboard_focus(self):
        self.connect_reader()
        self.select_second()
        self.window.activateWindow()
        self.window.candidates.setFocus(Qt.FocusReason.OtherFocusReason)
        self.app.processEvents()
        self.assertTrue(self.window.candidates.hasFocus())
        self.window.poll()
        self.assertTrue(self.window.candidates.isEnabled())
        self.assertTrue(self.window.candidates.hasFocus())
        self.wait_for(lambda: not self.window.task)
        self.assertTrue(self.window.candidates.hasFocus())
        self.assertEqual(self.window.candidates.currentRow(), 1)
        self.assertEqual(len(self.window.candidates.selectedItems()), 5)

    def test_keyboard_choice_during_poll_is_applied_after_worker_finishes(self):
        service = self.connect_reader()
        self.select_second()
        entered, release = threading.Event(), threading.Event()
        def slow_poll():
            entered.set()
            release.wait(3)
            return service.snapshot('Polling')
        with patch.object(service, 'poll', side_effect=slow_poll):
            self.window.poll()
            self.wait_for(entered.is_set)
            try:
                self.window.candidates.setFocus()
                QTest.keyClick(self.window.candidates, Qt.Key.Key_Up)
                self.assertEqual(self.window.pending_selection, service.first.id)
            finally:
                release.set()
            self.wait_for(lambda: not self.window.task and service.profile == service.first)
        self.assertEqual(self.window.candidates.currentRow(), 0)
        self.assertEqual(self.window.title.text(), 'First game')
        self.assertEqual(service.captures, 0)

    def test_disconnect_during_poll_waits_then_restores_settings_controls(self):
        service = self.connect_reader()
        entered, release = threading.Event(), threading.Event()
        def slow_poll():
            entered.set()
            release.wait(3)
            return service.snapshot('Polling')
        with patch.object(service, 'poll', side_effect=slow_poll):
            self.window.poll()
            self.wait_for(entered.is_set)
            try:
                self.assertTrue(self.window.connect_button.isEnabled())
                self.window.connect_button.click()
                self.assertTrue(self.window.pending_disconnect)
                self.assertNotIn('close', [name for name, _ in service.calls])
            finally:
                release.set()
            self.wait_for(lambda: not self.window.task and not self.window.connected)
        self.assertIn('close', [name for name, _ in service.calls])
        self.assertTrue(self.window.port.isVisible())
        self.assertTrue(self.window.db_browse.isVisible())
        self.assertTrue(self.window.db_browse.isEnabled())
        self.assertFalse(self.window.connection_value.isVisible())
        self.assertFalse(self.window.database_value.isVisible())

    def test_unsupported_candidate_cannot_start_reading(self):
        service = self.connect_reader()
        unsupported = replace(service.first, id='unsupported', mapper=7)
        service.candidates = (unsupported, service.second)
        self.window.poll()
        self.wait_for(lambda: not self.window.task)
        self.window.candidates.selectRow(0)
        self.wait_for(lambda: not self.window.task)
        self.assertFalse(self.window.dump.isEnabled())
        self.assertIn('not been ported', self.window.details.text())
        self.confirm()
        self.assertEqual(service.captures, 0)

    def test_failed_connect_allows_retry_without_leaving_a_worker_running_io(self):
        with patch.object(self.window.worker, 'factory', side_effect=ProtocolError('No reader found')):
            self.window.toggle_connection()
            self.wait_for(lambda: not self.window.task)
        self.assertFalse(self.window.connected)
        self.assertTrue(self.window.connect_button.isEnabled())
        self.assertIn('No reader found', self.window.result.text())
        self.connect_reader()

    def test_reader_error_clears_connection_and_shows_reconnect_guidance(self):
        self.connect_reader()
        self.window._failed('capture', '[Errno 32] Pipe error', False)
        self.assertFalse(self.window.connected)
        self.assertIsNone(self.window.state)
        self.assertEqual(self.window.connect_button.text(), 'Connect')
        self.assertFalse(self.window.dump.isEnabled())
        self.assertIn('cartridge/USB connection', self.window.status.text())
        self.assertIn('click Connect', self.window.status.text())
        self.assertEqual(self.window.result.text(), '[Errno 32] Pipe error')

    def test_selected_title_is_saved_and_all_io_stays_on_one_worker(self):
        service = self.connect_reader()
        self.select_second()
        self.confirm()
        self.wait_for(lambda: not self.window.task)
        target = self.destination / service.second.filename()
        self.assertEqual(target.read_bytes(), service.second_data)
        self.assertIn(str(target), self.window.result.text())
        self.assertEqual(self.window.progress.value(), 100)
        self.window.toggle_connection()
        self.wait_for(lambda: not self.window.task)
        ids = {identity for _, identity in service.calls}
        self.assertEqual(len(ids), 1)
        self.assertNotIn(threading.get_ident(), ids)

    def test_confirmation_blocks_timer_polling(self):
        service = self.connect_reader()
        self.select_second()
        before = len(service.calls)
        def dialog(*args):
            self.window.poll()
            self.assertFalse(self.window.task)
            return QMessageBox.StandardButton.No
        with patch.object(QMessageBox, 'question', side_effect=dialog):
            self.window.confirm_capture()
        self.assertEqual(len(service.calls), before)

    def test_busy_capture_disables_reentry_and_can_cancel_without_saving(self):
        service = self.connect_reader()
        self.select_second()
        service.block = True
        self.confirm()
        self.wait_for(service.entered.is_set)
        self.assertFalse(self.window.connect_button.isEnabled())
        self.assertFalse(self.window.dump.isEnabled())
        self.assertFalse(self.window.browse.isEnabled())
        self.assertFalse(self.window._send('capture'))
        self.window.cancel_capture()
        self.wait_for(lambda: not self.window.task)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.window.connected)
        self.assertIn('safe', self.window.status.text())
        self.assertIn('close', [name for name, _ in service.calls])

    def test_close_while_reading_waits_for_safe_worker_cleanup(self):
        service = self.connect_reader()
        self.select_second()
        service.block = True
        self.confirm()
        self.wait_for(service.entered.is_set)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.window.close()
        self.wait_for(lambda: not self.window.thread.isRunning())
        self.assertTrue(self.window.allow_close)
        self.assertFalse(self.destination.exists())
        self.assertIn('close', [name for name, _ in service.calls])

    def test_declining_close_keeps_capture_running(self):
        service = self.connect_reader()
        self.select_second()
        service.block = True
        self.confirm()
        self.wait_for(service.entered.is_set)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            self.window.close()
        self.assertFalse(self.window.closing)
        self.assertFalse(self.window.worker.cancelled.is_set())
        self.assertTrue(self.window.thread.isRunning())
        self.window.cancel_capture()
        self.wait_for(lambda: not self.window.task)

    def test_poll_failure_disconnects_and_requires_explicit_reconnect(self):
        service = self.connect_reader()
        service.error = ProtocolError('Reader disconnected')
        self.window.poll()
        self.wait_for(lambda: not self.window.task)
        self.assertFalse(self.window.connected)
        self.assertFalse(self.window.dump.isEnabled())
        self.assertIn('Reader disconnected', self.window.result.text())
        self.window.poll()
        self.assertFalse(self.window.task)

    def test_conflicting_rom_is_preserved_and_result_shows_alternate(self):
        service = self.connect_reader()
        self.select_second()
        self.destination.mkdir()
        original = self.destination / service.second.filename()
        original.write_bytes(b'user ROM')
        self.confirm()
        self.wait_for(lambda: not self.window.task)
        self.assertEqual(original.read_bytes(), b'user ROM')
        self.assertIn('preserved', self.window.result.text())
        self.assertIn('.verified-', self.window.saved_path.name)

    def test_auto_group_resolves_without_asking_for_manual_title_selection(self):
        service = self.connect_reader()
        service.auto = 2
        self.window.poll()
        self.wait_for(lambda: not self.window.task)
        self.assertTrue(self.window.dump.isEnabled())
        self.confirm()
        self.wait_for(lambda: not self.window.task)
        self.assertEqual(self.window.title.text(), 'Second game')
        self.assertEqual(self.window.saved_path.read_bytes(), service.second_data)


    def test_gb_header_and_verified_title_refresh_in_place(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))
        from test_cart_station_gb import fixture
        from cart_station_gb import GbDatabase
        from cart_station_service import PollState
        profile, payload, record = fixture(0x1b, 3, 0x80)
        state = PollState('Connected', 'GBC: header detected', True, (profile,), profile, False)
        self.window._apply_state(state)
        self.assertIn('MBC5', self.window.details.text())
        self.assertEqual(self.window.candidates.horizontalHeaderItem(1).text(), 'Controller')
        self.assertEqual(self.window.candidates.item(0, 2).text(), '256 KiB')
        matched = GbDatabase([record]).match(profile, payload)
        self.assertEqual(profile.id, matched.id)
        self.window._apply_state(PollState('Connected', 'Verified', True, (matched,), matched, True))
        self.assertEqual(self.window.title.text(), record['title'])
        self.assertEqual(self.window.candidates.item(0, 0).text(), record['title'])
        self.assertIn('DB verified', self.window.details.text())

    def test_gba_unknown_size_then_verified_title_and_size_refresh(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))
        from test_cart_station_gba import fixture
        from cart_station_gba import GbaDatabase
        from cart_station_service import PollState
        profile,payload,record=fixture()
        self.window.connected=True
        self.window._apply_state(PollState('Connected','GBA header detected',True,(profile,),profile,False))
        self.window._enabled()
        self.assertTrue(self.window.dump.isEnabled())
        self.assertEqual(self.window.candidates.item(0,2).text(),'Auto (DB)')
        self.assertIn('TSTJ',self.window.details.text())
        matched=GbaDatabase([record]).match(profile,payload)
        self.window._apply_state(PollState('Connected','GBA verified',True,(matched,),matched,True))
        self.assertEqual(self.window.candidates.item(0,2).text(),'1024 KiB')
        self.assertEqual(self.window.title.text(),record['title'])
        self.assertIn('DB verified',self.window.details.text())

    def test_md_header_db_title_and_unsupported_geometry(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'tests'))
        from test_cart_station_md import fixture
        from cart_station_md import MdDatabase
        from cart_station_service import PollState
        profile,payload,record=fixture()
        self.window.connected=True
        self.window._apply_state(PollState('Connected','MD header detected',True,(profile,),profile,False))
        self.window._enabled()
        self.assertTrue(self.window.dump.isEnabled())
        self.assertEqual(self.window.candidates.item(0,1).text(),'None')
        self.assertEqual(self.window.candidates.item(0,2).text(),'1024 KiB')
        matched=MdDatabase([record]).match(profile,payload)
        self.window._apply_state(PollState('Connected','MD verified',True,(matched,),matched,True))
        self.assertEqual(self.window.title.text(),record['title'])
        self.assertIn('DB verified',self.window.details.text())
        unsupported=replace(profile,prg_size=8388608)
        self.window._apply_state(PollState('Connected','MD header',False,(unsupported,),unsupported,False))
        self.window._enabled()
        self.assertFalse(self.window.dump.isEnabled())

    def test_sfc_superfx_header_verified_title_and_dedicated_reader_disabled(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'tests'))
        from test_cart_station_sfc import fixture
        from cart_station_sfc import SfcDatabase
        from cart_station_service import PollState
        profile,payload,record=fixture(chip=0x13)
        self.window.connected=True
        self.window._apply_state(PollState('Connected','SFC header detected',True,(profile,),profile,False))
        self.window._enabled()
        self.assertTrue(self.window.dump.isEnabled())
        self.assertIn('SuperFX',self.window.details.text())
        self.assertEqual(self.window.candidates.item(0,1).text(),'LoRom')
        self.assertEqual(self.window.candidates.item(0,3).text(),'SFC')
        matched=SfcDatabase([record]).match(profile,payload)
        self.window._apply_state(PollState('Connected','SFC verified',True,(matched,),matched,True))
        self.assertEqual(self.window.title.text(),record['title'])
        self.assertIn('DB verified',self.window.details.text())
        unsupported=replace(profile,controller='Sa1')
        self.window._apply_state(PollState('Connected','SFC header',False,(unsupported,),unsupported,False))
        self.window._enabled()
        self.assertFalse(self.window.dump.isEnabled())

    def test_pce_unknown_title_size_then_db_verified_display(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'tests'))
        from test_cart_station_pce import fixture
        from cart_station_pce import PceDatabase
        from cart_station_service import PollState
        profile,raw,record,payload=fixture()
        self.window.connected=True
        self.window._apply_state(PollState('Connected','PCE sample detected',True,(profile,),profile,False))
        self.window._enabled()
        self.assertTrue(self.window.dump.isEnabled())
        self.assertEqual(self.window.candidates.item(0,2).text(),'Auto (DB)')
        self.assertIn('Sample detected',self.window.details.text())
        matched=replace(PceDatabase([record]).match(profile,payload),data_order='native')
        self.window._apply_state(PollState('Connected','PCE verified',True,(matched,),matched,True))
        self.assertEqual(self.window.title.text(),record['title'])
        self.assertEqual(self.window.candidates.item(0,2).text(),'256 KiB')
        self.assertIn('DB verified',self.window.details.text())

    def test_pce_worker_identifies_before_read_button_without_creating_rom(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'tests'))
        from test_cart_station_pce import fixture,Cartridge
        from cart_station_pce import PceDatabase
        from cart_station_service import Service
        from cart_station_windows import ReaderWorker
        profile,raw,record,payload=fixture()
        with tempfile.TemporaryDirectory() as folder:
            destination=Path(folder)/'roms'
            service=Service(destination,database=Database([]),pce_database=PceDatabase([record]))
            service.adapter=service.transport=Cartridge(raw)
            worker=ReaderWorker(service_factory=lambda **args:service)
            states=[];progress=[];failures=[]
            worker.succeeded.connect(lambda task,state:states.append(state))
            worker.progress.connect(lambda stage,percent:progress.append((stage,percent)))
            worker.failed.connect(lambda *args:failures.append(args))
            worker.execute('connect',{})
            worker.execute('poll',None);worker.execute('poll',None)
            self.assertFalse(failures);self.assertTrue(progress)
            self.assertEqual(states[-1].profile.title,record['title'])
            self.assertFalse(states[-1].saved);self.assertFalse(destination.exists())
            self.window._apply_state(states[-1])
            self.assertEqual(self.window.title.text(),record['title'])
            self.assertEqual(self.window.candidates.item(0,2).text(),'256 KiB')
            worker.execute('disconnect',None)

    def test_gb_unsupported_header_disables_capture_and_uses_configured_path(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))
        from test_cart_station_gb import fixture
        from cart_station_service import PollState
        profile, _, _ = fixture(1, 7)
        destination = self.destination / 'gb'
        self.window.connected = True
        self.window._apply_state(PollState('Connected', 'GB header', False, (profile,), profile,
                                          False, destination=destination))
        self.window._enabled()
        self.assertFalse(self.window.dump.isEnabled())
        self.assertIn('not implemented', self.window.details.text())
        self.assertEqual(self.window.destination.text(), str(destination))


if __name__ == '__main__':
    unittest.main()
