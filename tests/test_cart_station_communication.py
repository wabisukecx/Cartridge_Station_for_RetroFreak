from _paths import ROOT  # noqa: F401
import errno
import unittest
from unittest.mock import patch

from cart_station_protocol import CommunicationError, ProtocolError, packet
from cart_station_transport import Adapter


class ScriptedWire:
    def __init__(self, parts, write_error=None):
        self.parts = list(parts)
        self.writes = []
        self.write_error = write_error

    def write(self, data):
        self.writes.append(data)
        if self.write_error is not None:
            raise self.write_error

    def read(self, size):
        part = self.parts.pop(0)
        if isinstance(part, Exception):
            raise part
        if len(part) > size:
            self.parts.insert(0, part[size:])
        return part[:size]


class CommunicationDiagnosticTests(unittest.TestCase):
    def test_partial_product_header_reports_phase_and_byte_count(self):
        wire = ScriptedWire([b'\0\0', b''])
        with self.assertRaises(CommunicationError) as caught:
            Adapter(wire).product()
        error = caught.exception
        self.assertEqual((error.expected_bytes, error.received_bytes), (8, 2))
        self.assertIn('Product command 0x01 header', str(error))
        self.assertIn('timeout or end of stream', str(error))
        self.assertEqual(wire.writes, [packet(1, 0, 4)])
        self.assertFalse(wire.parts)

    def test_payload_timeout_keeps_address_and_does_not_read_trailing_status(self):
        wire = ScriptedWire([packet(0, 8), b'ab', b'', b'unread'])
        with self.assertRaises(CommunicationError) as caught:
            Adapter(wire).gba_read(0x8000, 8)
        error = caught.exception
        self.assertEqual((error.expected_bytes, error.received_bytes), (8, 2))
        self.assertIn('GBA read command 0x1F address 0x008000 payload', str(error))
        self.assertEqual(wire.parts, [b'unread'])
        self.assertEqual(wire.writes, [packet(0x1f, 8, 8, 0x8000, 8), packet(6, 0, 4)])

    def test_trailing_status_failure_is_distinct_from_complete_payload(self):
        wire = ScriptedWire([packet(0, 4), b'ROM!', b'\0' * 3, b''])
        with self.assertRaises(CommunicationError) as caught:
            Adapter(wire).gba_read(0, 4)
        self.assertIn('trailing status header', str(caught.exception))
        self.assertEqual(caught.exception.received_bytes, 3)
        self.assertEqual(caught.exception.expected_bytes, 8)

    def test_read_error_preserves_original_errno_and_partial_count(self):
        source = OSError(errno.EPIPE, 'Pipe error')
        wire = ScriptedWire([b'ab', source])
        with self.assertRaises(CommunicationError) as caught:
            Adapter(wire).exact(8, 'test payload')
        self.assertIs(caught.exception.__cause__, source)
        self.assertEqual(caught.exception.__cause__.errno, errno.EPIPE)
        self.assertEqual(caught.exception.received_bytes, 2)
        self.assertIn('Pipe error', str(caught.exception))

    def test_failed_send_does_not_invent_transmitted_count_or_continue(self):
        source = OSError(errno.EIO, 'Write failed')
        wire = ScriptedWire([b'unread'], write_error=source)
        with self.assertRaises(CommunicationError) as caught:
            Adapter(wire).gba_read(0, 4)
        self.assertIs(caught.exception.__cause__, source)
        self.assertIsNone(caught.exception.received_bytes)
        self.assertEqual(caught.exception.expected_bytes, 20)
        self.assertIn('transmitted count unknown', str(caught.exception))
        self.assertEqual(len(wire.writes), 1)
        self.assertEqual(wire.parts, [b'unread'])

    def test_deadline_has_partial_count_and_no_extra_read(self):
        wire = ScriptedWire([b'a', b'unread'])
        with patch('cart_station_transport.time.monotonic', side_effect=[0, 0, 11]):
            with self.assertRaises(CommunicationError) as caught:
                Adapter(wire).exact(8, 'test header')
        self.assertIn('deadline exceeded', str(caught.exception))
        self.assertEqual(caught.exception.received_bytes, 1)
        self.assertEqual(wire.parts, [b'unread'])

    def test_malformed_header_keeps_context_and_does_not_consume_payload(self):
        wire = ScriptedWire([packet(1, 4), b'unread'])
        with self.assertRaises(ProtocolError) as caught:
            Adapter(wire).product()
        self.assertIn('status=1 length=4', str(caught.exception))
        self.assertIn('Product command 0x01 header', str(caught.exception))
        self.assertEqual(wire.parts, [b'unread'])

    def test_fragmented_success_keeps_wire_sequence_and_payload(self):
        reply = packet(0, 4) + b'ROM!' + packet(0, 4) + b'\x06\0\0\0'
        wire = ScriptedWire([bytes((value,)) for value in reply])
        self.assertEqual(Adapter(wire).gba_read(0, 4), b'ROM!')
        self.assertFalse(wire.parts)
        self.assertEqual(wire.writes, [packet(0x1f, 8, 4, 0, 4), packet(6, 0, 4)])


if __name__ == '__main__':
    unittest.main()
