from _paths import ROOT  # noqa: F401
import errno
import io
import json
import struct
import unittest
from unittest.mock import Mock, call, patch

import usb.core

from cart_station_protocol import CommunicationError, ProtocolError, packet
from cart_station_transport import Adapter, TraceTransport, UsbTransport
from test_cart_station_usb_cdc import Interface


def quiet():
    return usb.core.USBTimeoutError('quiet', None, errno.ETIMEDOUT)


class UsbRecoveryTests(unittest.TestCase):
    def transport(self, parts=(), pending=b'', recover=True):
        transport = UsbTransport.__new__(UsbTransport)
        transport.pending = bytearray(pending)
        transport._needs_recovery = recover
        transport.last_recovery = None
        transport.input = Mock(wMaxPacketSize=64)
        transport.input.read.side_effect = list(parts) + [quiet(), quiet()]
        transport.output = Mock()
        transport.output.write.side_effect = lambda data, timeout: len(data)
        return transport

    def test_drains_full_old_payload_and_pending_before_new_request(self):
        transport = self.transport([b'x' * 16384, b'y' * 16384, b'tail'], b'buffered')
        events = Mock()
        events.attach_mock(transport.input.read, 'read')
        events.attach_mock(transport.output.write, 'write')
        with self.assertLogs('cart_station_transport', level='WARNING'):
            transport.write(b'new request')
        self.assertEqual(transport.last_recovery['discarded_bytes'], 32768 + 4 + 8)
        self.assertEqual(transport.last_recovery['outcome'], 'quiet')
        self.assertEqual(events.mock_calls[-1], call.write(b'new request', timeout=5000))
        self.assertEqual(transport.input.read.call_count, 5)
        self.assertFalse(transport.pending)
        self.assertFalse(transport._needs_recovery)

    def test_normal_drop_and_write_never_poll_usb_or_lose_new_reply(self):
        transport = self.transport([b'reply'], b'old local bytes', recover=False)
        transport.drop_input()
        transport.write(b'request')
        transport.input.read.assert_not_called()
        self.assertEqual(transport.read(5), b'reply')
        self.assertEqual(transport.input.read.call_args, call(64, timeout=3000))

    def test_partial_response_timeout_then_next_request_recovers_without_resending(self):
        reply = packet(0, 4) + struct.pack('<HH', 10, 4919)
        transport = self.transport([b'\0\0', quiet(), b'x' * 16384,
                                    b'y' * 16384, quiet(), quiet(), reply], recover=False)
        adapter = Adapter(transport)
        with self.assertRaises(CommunicationError) as caught:
            adapter.product()
        self.assertEqual(caught.exception.received_bytes, 2)
        with self.assertLogs('cart_station_transport', level='WARNING'):
            self.assertEqual(adapter.product(), {'build': 10, 'product': 4919})
        self.assertEqual(transport.output.write.call_args_list,
                         [call(packet(1, 0, 4), timeout=5000)] * 2)
        self.assertEqual(transport.last_recovery['discarded_bytes'], 32768)
        self.assertFalse(transport.pending)

    def test_byte_limit_blocks_all_outgoing_requests(self):
        transport = self.transport()
        transport.input.read.side_effect = lambda size, timeout: b'x' * size
        with self.assertLogs('cart_station_transport', level='WARNING'):
            with self.assertRaisesRegex(ProtocolError, 'recovery limit'):
                transport.write(b'must not send')
        self.assertEqual(transport.last_recovery['discarded_bytes'], 256 * 1024)
        self.assertEqual(transport.input.read.call_count, 16)
        transport.output.write.assert_not_called()
        self.assertTrue(transport._needs_recovery)

    def test_time_limit_blocks_request_without_an_extra_read(self):
        transport = self.transport([b'old'])
        with patch('cart_station_transport.time.monotonic', side_effect=[0, 0, 0.6, 0.6]):
            with self.assertLogs('cart_station_transport', level='WARNING'):
                with self.assertRaisesRegex(ProtocolError, 'recovery limit'):
                    transport.write(b'must not send')
        transport.input.read.assert_called_once()
        transport.output.write.assert_not_called()

    def test_read_limit_bounds_fast_small_transfers(self):
        transport = self.transport()
        transport.input.read.side_effect = lambda size, timeout: b'x'
        with self.assertLogs('cart_station_transport', level='WARNING'):
            with self.assertRaisesRegex(ProtocolError, 'recovery limit'):
                transport.drop_input()
        self.assertEqual(transport.input.read.call_count, 64)
        self.assertEqual(transport.last_recovery['discarded_bytes'], 64)

    def test_pipe_disconnect_and_cancel_are_not_treated_as_quiet(self):
        for error in (usb.core.USBError('pipe', None, errno.EPIPE),
                      usb.core.USBError('disconnected', None, errno.ENODEV),
                      KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                transport = self.transport([error])
                with self.assertLogs('cart_station_transport', level='WARNING'):
                    with self.assertRaises(type(error)) as caught:
                        transport.write(b'must not send')
                self.assertIs(caught.exception, error)
                transport.output.write.assert_not_called()
                self.assertEqual(transport.last_recovery['outcome'], 'failed')
                self.assertTrue(transport._needs_recovery)

    def test_data_between_timeouts_restarts_quiet_confirmation(self):
        transport = self.transport([quiet(), b'late stale data', quiet(), quiet()])
        with self.assertLogs('cart_station_transport', level='WARNING'):
            transport.drop_input()
        self.assertEqual(transport.input.read.call_count, 4)
        self.assertEqual(transport.last_recovery['discarded_bytes'], 15)
        self.assertTrue(all(c.kwargs['timeout'] <= 20 for c in transport.input.read.call_args_list))

    def test_empty_read_and_short_write_mark_recovery_needed(self):
        transport = self.transport([b''], recover=False)
        self.assertEqual(transport.read(8), b'')
        self.assertTrue(transport._needs_recovery)
        transport = self.transport(recover=False)
        transport.output.write.side_effect = None
        transport.output.write.return_value = 1
        with self.assertRaisesRegex(ProtocolError, 'Incomplete USB write'):
            transport.write(b'longer request')
        self.assertTrue(transport._needs_recovery)

    def test_trace_reports_open_and_subsequent_recovery_without_old_rom_bytes(self):
        transport = self.transport([b'private ROM'])
        stream = io.StringIO()
        with self.assertLogs('cart_station_transport', level='WARNING'):
            transport.drop_input()
        traced = TraceTransport(transport, stream)
        transport._needs_recovery = True
        transport.input.read.side_effect = [b'other ROM', quiet(), quiet()]
        with self.assertLogs('cart_station_transport', level='WARNING'):
            traced.write(b'request')
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(events[0]['usb_recovery']['discarded_bytes'], 11)
        self.assertEqual(events[1]['usb_recovery']['discarded_bytes'], 9)
        self.assertNotIn(b'private ROM'.hex(), stream.getvalue())
        self.assertNotIn(b'other ROM'.hex(), stream.getvalue())

    def constructor(self, endpoint):
        output = Mock(bmAttributes=2, bEndpointAddress=0x02)
        device = Mock()
        device.get_active_configuration.return_value = [Interface(1, 255, 0, [endpoint, output])]
        device.is_kernel_driver_active.return_value = True
        return device, output

    def test_reconnect_drains_previous_connection_before_product(self):
        reply = packet(0, 4) + struct.pack('<HH', 10, 4919)
        endpoint = Mock(bmAttributes=2, bEndpointAddress=0x81, wMaxPacketSize=64)
        endpoint.read.side_effect = [b'x' * 16384, b'y' * 16384, quiet(), quiet(), reply]
        device, output = self.constructor(endpoint)
        output.write.return_value = 12
        with patch('usb.core.find', return_value=[device]), patch('cart_station_transport.usb_backend'), \
                patch('usb.util.claim_interface'), patch('usb.util.release_interface'), \
                patch('usb.util.dispose_resources'):
            with self.assertLogs('cart_station_transport', level='WARNING'):
                transport = UsbTransport()
            output.write.assert_not_called()
            self.assertEqual(Adapter(transport).product(), {'build': 10, 'product': 4919})
            transport.close()
        self.assertEqual(transport.last_recovery['discarded_bytes'], 32768)
        device.attach_kernel_driver.assert_called_once_with(1)

    def test_failed_connection_recovery_restores_driver_and_disposes_resources(self):
        endpoint = Mock(bmAttributes=2, bEndpointAddress=0x81, wMaxPacketSize=64)
        endpoint.read.side_effect = lambda size, timeout: b'x' * size
        device, output = self.constructor(endpoint)
        with patch('usb.core.find', return_value=[device]), patch('cart_station_transport.usb_backend'), \
                patch('usb.util.claim_interface'), patch('usb.util.release_interface') as release, \
                patch('usb.util.dispose_resources') as dispose:
            with self.assertLogs('cart_station_transport', level='WARNING'):
                with self.assertRaisesRegex(ProtocolError, 'recovery limit'):
                    UsbTransport()
        output.write.assert_not_called()
        release.assert_called_once_with(device, 1)
        device.attach_kernel_driver.assert_called_once_with(1)
        dispose.assert_called_once_with(device)


if __name__ == '__main__':
    unittest.main()
