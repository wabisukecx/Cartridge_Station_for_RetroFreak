"""Timing diagnostics preserve transactions, independent reads and saved results."""
from _paths import ROOT  # noqa: F401
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_metrics import CaptureMetrics, IoMetrics, write_capture_report
from cart_station_protocol import packet
from cart_station_transport import Adapter
from cart_station_service import Service, UserCancelled
from cart_station_nes_db import Database
from cart_station_gb import GbDatabase, GbClassificationMismatch
from test_cart_station import Wire
from test_cart_station_gb import fixture, Cartridge


class Clock:
    now = 0.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class CaptureMetricsTests(unittest.TestCase):
    def test_io_calls_return_the_original_data_and_count_without_retaining_payloads(self):
        clock = Clock()
        io = IoMetrics(clock)
        data = bytearray(b'sensitive-data')
        class Transport:
            def read(self, size):
                clock.advance(0.25)
                return data
            def write(self, value):
                self.last = value
                clock.advance(0.5)
        transport = Transport()
        self.assertIs(io.call(transport, 'read', 100), data)
        self.assertIsNone(io.call(transport, 'write', b'req'))
        self.assertEqual(transport.last, b'req')
        result = io.snapshot()
        self.assertEqual((result['read_calls'], result['write_calls']), (1, 1))
        self.assertEqual((result['read_bytes'], result['write_bytes']), (len(data), 3))
        self.assertEqual((result['read_seconds'], result['write_seconds']), (0.25, 0.5))
        self.assertNotIn('sensitive-data', json.dumps(result))

    def test_io_exception_identity_and_wait_time_are_preserved(self):
        clock = Clock()
        io = IoMetrics(clock)
        failure = OSError(32, 'Pipe error')
        def read(size):
            clock.advance(0.75)
            raise failure
        with self.assertRaises(OSError) as raised:
            io.call(Mock(read=read), 'read', 8)
        self.assertIs(raised.exception, failure)
        self.assertEqual(io.values['read_errors'], 1)
        self.assertEqual(io.values['read_bytes'], 0)
        self.assertEqual(io.values['read_seconds'], 0.75)

    def test_adapter_packet_sequence_and_fragmented_responses_are_unchanged(self):
        replies = packet(0, 4) + struct.pack('<HH', 10, 4919)
        plain, measured = Wire(replies, fragment=3), Wire(replies, fragment=3)
        expected = Adapter(plain).product()
        adapter = Adapter(measured)
        io = adapter.io_metrics = IoMetrics(Clock())
        self.assertEqual(adapter.product(), expected)
        self.assertEqual(measured.writes, plain.writes)
        self.assertEqual(measured.replies, b'')
        self.assertEqual((io.values['read_calls'], io.values['write_calls']), (5, 1))
        self.assertEqual(io.values['read_bytes'], len(replies))

    def test_phases_partition_the_total_and_repeated_progress_does_not_split_them(self):
        clock = Clock()
        metrics = CaptureMetrics(clock)
        clock.advance(1)
        metrics.progress('Read 1/2')
        clock.advance(2)
        metrics.progress('Read 1/2')
        clock.advance(2)
        metrics.progress('Read 2/2')
        clock.advance(3)
        report = metrics.finish('saved')
        self.assertEqual([p['seconds'] for p in report['phases']], [1, 4, 3])
        self.assertEqual(sum(p['seconds'] for p in report['phases']), report['elapsed_seconds'])

    def test_nested_operation_times_are_not_added_to_total_and_keep_failures(self):
        clock = Clock()
        metrics = CaptureMetrics(clock)
        metrics.progress('Verifying and saving...')
        result = object()
        def save():
            clock.advance(2)
            return result
        self.assertIs(metrics.call('save_verified_rom', save), result)
        failure = ValueError('verification failure')
        def verify():
            clock.advance(1)
            raise failure
        with self.assertRaises(ValueError) as raised:
            metrics.call('verify_saved_file', verify)
        self.assertIs(raised.exception, failure)
        report = metrics.finish('error', 'ValueError')
        self.assertEqual(report['elapsed_seconds'], 3)
        self.assertEqual(report['operation_seconds'], {'save_verified_rom': 2, 'verify_saved_file': 1})
        self.assertEqual(sum(p['seconds'] for p in report['phases']), 3)

    def test_latest_file_replacement_failure_or_invalid_json_preserves_the_previous_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'runtime/last_capture_metrics.json'
            report = CaptureMetrics(Clock()).finish('identical')
            self.assertTrue(write_capture_report(path, report, root))
            previous = path.read_bytes()
            with patch('cart_station_metrics.os.replace', side_effect=OSError('read-only')):
                self.assertFalse(write_capture_report(path, dict(report, outcome='error'), root))
            self.assertFalse(write_capture_report(path, dict(report, elapsed_seconds=float('nan')), root))
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def make_service(self, root, metrics=True, wrong_class=False):
        profile, payload, record = fixture(cart=0, size_code=0)
        service = Service(root / 'roms', database=Database([]),
                          gb_database=GbDatabase([dict(record, system='GBC') if wrong_class else record]),
                          metrics_path=root / 'runtime/last_capture_metrics.json' if metrics else None)
        service.profile, service.selected_id = profile, profile.id
        service.candidates = (profile,)
        service.adapter = Cartridge(payload)
        service.transport = Mock()
        service.reader_info = {'build': 10, 'product': 4919, 'private': 'not recorded'}
        return service, payload

    @patch('cart_station_gb.time.sleep')
    def test_fresh_and_identical_captures_still_read_twice_and_replace_one_small_report(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service, payload = self.make_service(root)
            bus = service.adapter
            target, created = service.capture(lambda *args: None)
            self.assertTrue(created)
            first = json.loads(service.metrics_path.read_text(encoding='utf-8'))
            self.assertEqual(first['outcome'], 'saved')
            self.assertEqual(first['reader'], {'build': 10, 'product': 4919})
            self.assertEqual(first['rom_bytes'], len(payload))
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(service.capture(lambda *args: None)[1])
            second = json.loads(service.metrics_path.read_text(encoding='utf-8'))
            self.assertEqual(second['outcome'], 'identical')
            self.assertEqual(bus.modes, [1, 0, 1, 0])
            self.assertEqual(sum(e[0] == 'R' and e[2] == 16384 for e in bus.events), 8)
            self.assertEqual(list(service.metrics_path.parent.iterdir()), [service.metrics_path])
            self.assertLess(service.metrics_path.stat().st_size, 16384)
            self.assertIn('save_verified_rom', second['operation_seconds'])
            self.assertIn('verify_saved_file', second['operation_seconds'])
            self.assertIsNone(bus.io_metrics)

    @patch('cart_station_gb.time.sleep')
    def test_failure_and_cancellation_keep_original_exceptions_and_do_not_publish_roms(self, sleep):
        for cancelled in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                service, _ = self.make_service(root, wrong_class=not cancelled)
                failure = UserCancelled('cancel')
                def progress(stage, done, total):
                    if cancelled and stage == 'Read 1/2': raise failure
                error = UserCancelled if cancelled else GbClassificationMismatch
                with self.assertRaises(error) as raised:
                    service.capture(progress)
                if cancelled: self.assertIs(raised.exception, failure)
                report = json.loads(service.metrics_path.read_text(encoding='utf-8'))
                self.assertEqual(report['outcome'], 'cancelled' if cancelled else 'error')
                self.assertEqual(report['error_type'], error.__name__)
                self.assertFalse((root / 'roms').exists())
                self.assertIsNone(service._capture_metrics)
                self.assertIsNone(service.adapter)

    @patch('cart_station_gb.time.sleep')
    def test_report_write_failure_cannot_turn_a_saved_rom_into_a_capture_failure(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            service, payload = self.make_service(Path(folder))
            with patch('cart_station_service.write_capture_report', return_value=False):
                target, created = service.capture(lambda *args: None)
            self.assertTrue(created)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(service.last_capture_metrics['outcome'], 'saved')

    @patch('cart_station_sfc.time.sleep')
    def test_sfc_database_load_is_measured_once_without_changing_two_fresh_reads(self, sleep):
        from test_cart_station_sfc import fixture as sfc_fixture, Cartridge as SfcCartridge
        from cart_station_sfc import SfcDatabase
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile, payload, record = sfc_fixture()
            service = Service(root / 'roms', database=Database([]))
            service.profile, service.selected_id = profile, profile.id
            bus = service.adapter = SfcCartridge(profile, payload)
            service.transport = Mock()
            clock = Clock()
            metrics = CaptureMetrics(clock)
            database = SfcDatabase([record])
            def load():
                clock.advance(0.5)
                return database
            with patch('cart_station_service.SfcDatabase.load', side_effect=load) as loader:
                with patch('cart_station_service.CaptureMetrics', return_value=metrics):
                    target, created = service.capture(lambda *args: None)
                first = service.last_capture_metrics
                self.assertTrue(created)
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(first['operation_seconds']['load_sfc_database'], 0.5)
                checking = next(p for p in first['phases'] if p['stage'] == 'Checking SFC cartridge...')
                self.assertEqual(checking['seconds'], 0.5)
                self.assertFalse(service.capture(lambda *args: None)[1])
                loader.assert_called_once_with()
            self.assertNotIn('load_sfc_database', service.last_capture_metrics['operation_seconds'])
            self.assertIs(service.sfc_database, database)
            self.assertEqual(bus.events.count('R,32768,32768'), 4)
            self.assertEqual(bus.modes, [1, 0, 1, 0] * 2)

    def test_failed_database_load_records_time_and_preserves_exception_without_rom_io(self):
        from test_cart_station_sfc import fixture as sfc_fixture, Cartridge as SfcCartridge
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile, payload, _ = sfc_fixture()
            service = Service(root / 'roms', database=Database([]))
            service.profile, service.selected_id = profile, profile.id
            bus = service.adapter = SfcCartridge(profile, payload)
            service.transport = Mock()
            clock = Clock()
            metrics = CaptureMetrics(clock)
            failure = ValueError('invalid database')
            def load():
                clock.advance(0.25)
                raise failure
            with patch('cart_station_service.CaptureMetrics', return_value=metrics), \
                    patch('cart_station_service.SfcDatabase.load', side_effect=load):
                with self.assertRaises(ValueError) as caught:
                    service.capture(lambda *args: None)
            self.assertIs(caught.exception, failure)
            self.assertEqual(service.last_capture_metrics['operation_seconds']['load_sfc_database'], 0.25)
            self.assertEqual(service.last_capture_metrics['outcome'], 'error')
            self.assertIsNone(service.sfc_database)
            self.assertEqual(bus.events, [])
            self.assertEqual(bus.modes, [])
            self.assertFalse((root / 'roms').exists())

    @patch('cart_station_gb.time.sleep')
    def test_library_service_can_collect_in_memory_without_creating_a_log(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service, payload = self.make_service(root, metrics=False)
            progress = Mock()
            with patch('cart_station_service.write_capture_report') as writer:
                target, _ = service.capture(progress)
            writer.assert_not_called()
            self.assertTrue(progress.called)
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse((root / 'runtime').exists())


if __name__ == '__main__':
    unittest.main()
