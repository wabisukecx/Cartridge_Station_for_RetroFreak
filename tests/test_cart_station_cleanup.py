"""Release failures keep the original error primary; CLI saves are re-read from disk."""
from _paths import ROOT, data  # noqa: F401
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_protocol import ProtocolError
from cart_station_cli import main
from cart_station_protocol import cleanup_after
from cart_station_service import save_and_reverify


class CleanupAfterTests(unittest.TestCase):
    def test_original_error_stays_primary_with_cleanup_failure_appended(self):
        release = Mock(side_effect=ProtocolError('cleanup mode failure'))
        with self.assertRaises(ProtocolError) as caught:
            with cleanup_after(release, 0):
                raise ProtocolError('original read failure')
        self.assertIn('original read failure', str(caught.exception))
        self.assertIn('cleanup mode failure', str(caught.exception))
        self.assertTrue(str(caught.exception).startswith('original read failure'))
        release.assert_called_once_with(0)

    def test_cleanup_failure_alone_propagates(self):
        with self.assertRaisesRegex(ProtocolError, 'cleanup mode failure'):
            with cleanup_after(Mock(side_effect=ProtocolError('cleanup mode failure'))):
                pass

    def test_cleanup_runs_after_return_and_success(self):
        release = Mock()

        def read():
            with cleanup_after(release):
                return 'data'
        self.assertEqual(read(), 'data')
        release.assert_called_once_with()

    def test_keyboard_interrupt_is_preserved(self):
        with self.assertRaises(KeyboardInterrupt):
            with cleanup_after(Mock(side_effect=OSError('gone'))):
                raise KeyboardInterrupt()


class SaveAndReverifyTests(unittest.TestCase):
    def test_saved_file_is_reread_against_the_profile(self):
        profile = Mock(system='GBA')
        with tempfile.TemporaryDirectory() as folder, \
                patch('cart_station_service.verified_file', return_value=False) as check:
            with self.assertRaisesRegex(ProtocolError, 'Saved GBA ROM failed verification'):
                save_and_reverify(Path(folder) / 'game.gba', b'rom', profile)
            check.assert_called_once_with(Path(folder) / 'game.gba', profile)

    def test_success_reports_created(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('cart_station_service.verified_file', return_value=True):
            path = Path(folder) / 'game.gba'
            self.assertEqual(save_and_reverify(path, b'rom', Mock()), (path, True))
            self.assertEqual(save_and_reverify(path, b'rom', Mock()), (path, False))


class RestoreFailureTests(unittest.TestCase):
    @patch('cart_station_pce.time.sleep')
    def test_pce_bank_restore_failure_keeps_the_read_error(self, sleep):
        import cart_station_pce
        adapter = Mock()
        adapter.status.return_value = cart_station_pce.CART_PCE
        adapter.pce_register.side_effect = [None, ProtocolError('restore failure')]
        reads = [b'A' * 8, b'B' * 8, b'C' * 8, ProtocolError('original read failure')]

        def stable(adapter, address, size):
            value = reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        with patch('cart_station_pce.stable_read', side_effect=stable):
            with self.assertRaises(ProtocolError) as caught:
                cart_station_pce.inspect_header(adapter)
        self.assertTrue(str(caught.exception).startswith('original read failure'))
        self.assertIn('restore failure', str(caught.exception))

    def test_cli_close_failure_keeps_the_read_error(self):
        transport = Mock()
        transport.close.side_effect = ProtocolError('transport close failure')
        with patch('sys.argv', ['cart_station.py', '--port', 'COM5', 'probe']),                 patch('cart_station_cli.open_transport', return_value=transport),                 patch('cart_station_cli.Adapter', return_value=Mock(
                    product=Mock(side_effect=ProtocolError('original read failure')), control_replies=[])),                 contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ProtocolError) as caught:
                main()
        self.assertTrue(str(caught.exception).startswith('original read failure'))
        self.assertIn('transport close failure', str(caught.exception))


class RemovedCommandTests(unittest.TestCase):
    def test_unverified_nrom_commands_are_gone(self):
        for command in ('dump-nrom', 'watch-nrom'):
            with patch('sys.argv', ['cart_station.py', command]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main()


if __name__ == '__main__':
    unittest.main()
