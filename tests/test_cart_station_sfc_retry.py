"""Bounded retries for stable invalid header content, never wire or ROM errors."""
from _paths import ROOT  # noqa: F401
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database
from cart_station_service import Service, UserCancelled
from cart_station_sfc import (HEADER_ATTEMPTS, SfcDatabase, SfcInvalidHeader,
                              dump_sfc, inspect_header, parse_header)
from test_cart_station_sfc import Cartridge, fixture


def invalid(header, offset=44, value=0):
    result = bytearray(header)
    result[offset] = value
    return bytes(result)


class RetryCartridge(Cartridge):
    def __init__(self, profile, raw, headers):
        super().__init__(profile, raw)
        self.headers = headers
        self.attempts = self.status_calls = self.header_reads = 0
        self.override = self.header_response

    def status(self):
        self.status_calls += 1
        return 1

    def snes_mode(self, value):
        super().snes_mode(value)
        if value:
            self.attempts += 1

    def header_response(self, address, size):
        if address == 0xffb0 and size == 48:
            self.header_reads += 1
            value = self.headers[min(self.attempts - 1, len(self.headers) - 1)]
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, tuple):
                value = value[(self.header_reads - 1) % 2]
            return value


@patch('cart_station_sfc.time.sleep')
class SfcRetryTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.raw, self.record = fixture()
        self.bad = invalid(self.profile.header)

    def test_only_complete_header_content_errors_are_retryable(self, sleep):
        for header in (self.bad, invalid(self.profile.header, 37, 255),
                       invalid(self.profile.header, 39, 255)):
            with self.assertRaises(SfcInvalidHeader):
                parse_header(header)
        with self.assertRaises(ProtocolError) as raised:
            parse_header(bytes(47))
        self.assertNotIsInstance(raised.exception, SfcInvalidHeader)

    def test_invalid_header_then_valid_releases_and_reinitializes(self, sleep):
        bus = RetryCartridge(self.profile, self.raw, [self.bad, self.profile.header])
        progress = Mock()
        self.assertEqual(inspect_header(bus, progress), self.profile)
        self.assertEqual(bus.modes, [1, 0, 1, 0])
        self.assertEqual((bus.status_calls, bus.header_reads), (2, 4))
        self.assertEqual(bus.events.count('R,49152,16384'), 2)
        self.assertEqual({call.args[0] for call in progress.call_args_list},
                         {'Checking SFC header (1/5)', 'Checking SFC header (2/5)'})
        sleep.assert_called_once_with(0.1)

    def test_five_total_attempts_preserve_the_last_reason_and_stop(self, sleep):
        last = invalid(self.profile.header, 39, 255)
        bus = RetryCartridge(self.profile, self.raw, [self.bad] * 4 + [last])
        with self.assertRaises(SfcInvalidHeader) as raised:
            inspect_header(bus)
        self.assertEqual(HEADER_ATTEMPTS, 5)
        self.assertEqual(bus.modes, [1, 0] * 5)
        self.assertEqual((bus.status_calls, bus.header_reads), (5, 10))
        self.assertIn('after 5 attempts', str(raised.exception))
        self.assertIn('size code', str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, SfcInvalidHeader)
        self.assertEqual(sleep.call_count, 4)

    def test_disagreeing_header_pair_is_not_retried(self, sleep):
        bus = RetryCartridge(self.profile, self.raw, [(self.bad, self.profile.header)])
        with self.assertRaisesRegex(ProtocolError, 'probe reads differ'):
            inspect_header(bus)
        self.assertEqual(bus.modes, [1, 0])
        self.assertEqual(bus.header_reads, 2)
        sleep.assert_not_called()

    def test_truncated_header_or_initialization_read_is_not_retried(self, sleep):
        bus = RetryCartridge(self.profile, self.raw, [b'short'])
        with self.assertRaisesRegex(ProtocolError, 'incomplete'):
            inspect_header(bus)
        self.assertEqual(bus.modes, [1, 0])
        bus = RetryCartridge(self.profile, self.raw, [self.profile.header])
        bus.override = lambda address, size: b'short' if address == 0xc000 else None
        with self.assertRaisesRegex(ProtocolError, 'initialization read'):
            inspect_header(bus)
        self.assertEqual(bus.modes, [1, 0])
        self.assertEqual(bus.header_reads, 0)
        sleep.assert_not_called()

    def test_transport_failure_is_not_retried(self, sleep):
        failure = OSError(32, 'Pipe error')
        bus = RetryCartridge(self.profile, self.raw, [failure])
        with self.assertRaises(OSError) as raised:
            inspect_header(bus)
        self.assertIs(raised.exception, failure)
        self.assertEqual(bus.modes, [1, 0])
        self.assertEqual(bus.header_reads, 1)
        sleep.assert_not_called()

    def test_changed_slot_is_checked_before_initialization_and_each_retry(self, sleep):
        for statuses, modes in (([5], []), ([1, 255], [1, 0])):
            with self.subTest(statuses=statuses):
                bus = RetryCartridge(self.profile, self.raw, [self.bad])
                bus.status = Mock(side_effect=statuses)
                with self.assertRaisesRegex(ProtocolError, 'Insert one Super Famicom'):
                    inspect_header(bus)
                self.assertEqual(bus.modes, modes)

    def test_cancellation_before_io_after_init_and_between_attempts_stops(self, sleep):
        for phase, modes in (('before', []), ('initialized', [1, 0]), ('retry', [1, 0])):
            with self.subTest(phase=phase):
                bus = RetryCartridge(self.profile, self.raw, [self.bad])
                calls = []
                def cancel(stage, done, total):
                    calls.append(stage)
                    if (phase == 'before' or phase == 'initialized' and len(calls) == 2
                            or phase == 'retry' and '(2/5)' in stage):
                        raise UserCancelled('cancel')
                with self.assertRaises(UserCancelled):
                    inspect_header(bus, cancel)
                self.assertEqual(bus.modes, modes)

    def test_release_failure_aborts_with_original_header_error_as_context(self, sleep):
        bus = RetryCartridge(self.profile, self.raw, [self.bad])
        original = bus.snes_mode
        failure = OSError(5, 'release failed')
        def release(value):
            original(value)
            if value == 0:
                raise failure
        bus.snes_mode = release
        with self.assertRaises(OSError) as raised:
            inspect_header(bus)
        self.assertIs(raised.exception, failure)
        self.assertIsInstance(raised.exception.__context__, SfcInvalidHeader)
        self.assertEqual(bus.modes, [1, 0])
        sleep.assert_not_called()

    def test_valid_but_unported_controller_is_not_reinitialized(self, sleep):
        profile, raw, _ = fixture(makeup=0x3a, chip=0xf5)
        bus = RetryCartridge(profile, raw, [profile.header])
        result = inspect_header(bus)
        self.assertIn('not yet ported', result.support())
        self.assertEqual(bus.modes, [1, 0])
        sleep.assert_not_called()

    def test_capture_recheck_retries_then_keeps_two_independent_rom_reads(self, sleep):
        bus = RetryCartridge(self.profile, self.raw, [self.bad, self.profile.header])
        matched, payload = dump_sfc(bus, self.profile, SfcDatabase([self.record]))
        self.assertEqual(payload, self.raw)
        self.assertEqual(matched.raw_sha1, self.record['sha1'])
        self.assertEqual(bus.modes, [1, 0, 1, 0, 1, 0])
        self.assertEqual(bus.events.count('R,32768,32768'), 2)

    def test_changed_cartridge_and_full_rom_mismatch_do_not_retry(self, sleep):
        other, _, _ = fixture(title=b'OTHER CART')
        bus = RetryCartridge(self.profile, self.raw, [self.bad, other.header])
        with self.assertRaisesRegex(ProtocolError, 'cartridge changed'):
            dump_sfc(bus, self.profile, SfcDatabase([self.record]))
        self.assertEqual(bus.modes, [1, 0, 1, 0])
        self.assertNotIn('R,32768,32768', bus.events)
        bus = RetryCartridge(self.profile, self.raw, [self.profile.header])
        with self.assertRaisesRegex(ProtocolError, 'not in the database'):
            dump_sfc(bus, self.profile, SfcDatabase([dict(self.record, sha1='0' * 40)]))
        self.assertEqual(bus.modes, [1, 0])

    def test_service_identification_forwards_progress_without_saving_and_exhaustion_preserves_files(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'sfc'
            bus = RetryCartridge(self.profile, self.raw, [self.bad, self.profile.header])
            service = Service(destination, database=Database([]), sfc_database=SfcDatabase([self.record]))
            service.adapter = service.transport = bus
            service.identification_progress = Mock()
            for _ in range(3):
                state = service.poll()
            self.assertTrue(state.supported)
            self.assertTrue(any('(2/5)' in call.args[0] for call in service.identification_progress.call_args_list))
            self.assertFalse(destination.exists())
            destination.mkdir()
            original = destination / 'existing.sfc'
            original.write_bytes(b'keep')
            bus.headers = [self.bad]
            with self.assertRaises(SfcInvalidHeader):
                service.capture(lambda *args: None)
            self.assertTrue(bus.closed)
            self.assertIsNone(service.adapter)
            self.assertEqual(original.read_bytes(), b'keep')
            self.assertEqual(list(destination.iterdir()), [original])

    def test_cli_reports_retry_once_on_stderr_and_keeps_stdout_json(self, sleep):
        import cart_station_cli
        bus = RetryCartridge(self.profile, self.raw, [self.bad, self.profile.header])
        with patch('cart_station_cli.open_transport', return_value=bus), \
                patch('cart_station_cli.Adapter', return_value=bus), \
                patch.object(sys, 'argv', ['cart_station.py', '--port', 'TEST', 'identify-sfc']), \
                patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch('sys.stderr', new_callable=io.StringIO) as errors:
            cart_station_cli.main()
        self.assertEqual(json.loads(output.getvalue())['system'], 'SFC')
        self.assertEqual(errors.getvalue().strip(), 'Checking SFC header (2/5)')
        self.assertTrue(bus.closed)


if __name__ == '__main__':
    unittest.main()
