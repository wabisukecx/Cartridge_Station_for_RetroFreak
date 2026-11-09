"""Use the exact two-interface descriptor layout reported by the MiSTer reader."""
from _paths import ROOT, data  # noqa: F401
import struct
import unittest
import errno
import usb.core
from unittest.mock import Mock, call, patch

from cart_station_protocol import ProtocolError
from cart_station_transport import UsbTransport


class Interface(list):
    bAlternateSetting = 0

    def __init__(self, number, cls, subclass, endpoints):
        super().__init__(endpoints)
        self.bInterfaceNumber = number
        self.bInterfaceClass = cls
        self.bInterfaceSubClass = subclass


class CdcTests(unittest.TestCase):
    def setUp(self):
        self.input = Mock(bmAttributes=2, bEndpointAddress=0x81, wMaxPacketSize=64)
        self.output = Mock(bmAttributes=2, bEndpointAddress=0x02, wMaxPacketSize=64)
        self.input.read.side_effect = usb.core.USBTimeoutError('quiet', None, errno.ETIMEDOUT)
        self.control = Interface(0, 2, 2, [Mock(bmAttributes=3, bEndpointAddress=0x83)])
        self.data = Interface(1, 10, 0, [self.input, self.output])
        self.device = Mock()
        self.device.get_active_configuration.return_value = [self.control, self.data]
        self.device.is_kernel_driver_active.return_value = False
        self.device.ctrl_transfer.side_effect = [7, 0, 0]
        self.find = patch('usb.core.find', return_value=[self.device]).start()
        self.claim = patch('usb.util.claim_interface').start()
        self.release = patch('usb.util.release_interface').start()
        self.dispose = patch('usb.util.dispose_resources').start()
        patch('cart_station_transport.usb_backend', return_value=Mock()).start()
        self.addCleanup(patch.stopall)

    def test_cdc_setup_precedes_first_bulk_write(self):
        timeline = Mock()
        timeline.attach_mock(self.device.ctrl_transfer, 'control')
        timeline.attach_mock(self.output.write, 'bulk')
        self.output.write.return_value = 12
        transport = UsbTransport()
        transport.write(bytes(12))
        self.assertEqual(timeline.mock_calls[:3], [
            call.control(0x21, 0x20, 0, 0, struct.pack('<IBBB', 115200, 0, 0, 8), timeout=3000),
            call.control(0x21, 0x22, 3, 0, None, timeout=3000),
            call.bulk(bytes(12), timeout=5000)])
        self.assertEqual(self.claim.call_args_list, [call(self.device, 0), call(self.device, 1)])
        transport.close()
        self.device.ctrl_transfer.assert_called_with(0x21, 0x22, 0, 0, None, timeout=3000)
        self.assertEqual(self.release.call_args_list, [call(self.device, 1), call(self.device, 0)])

    def test_short_line_coding_stops_before_bulk_and_releases(self):
        self.device.ctrl_transfer.side_effect = [6]
        with self.assertRaisesRegex(ProtocolError, 'SET_LINE_CODING'):
            UsbTransport()
        self.output.write.assert_not_called()
        self.assertEqual(self.release.call_count, 2)
        self.dispose.assert_called_once_with(self.device)

    def test_failed_control_lines_still_restores_both_drivers(self):
        self.device.is_kernel_driver_active.return_value = True
        self.device.ctrl_transfer.side_effect = [7, OSError('control failed')]
        with self.assertRaisesRegex(OSError, 'control failed'):
            UsbTransport()
        self.assertEqual(self.device.detach_kernel_driver.call_args_list, [call(0), call(1)])
        self.assertEqual(self.device.attach_kernel_driver.call_args_list, [call(1), call(0)])
        self.assertEqual(self.release.call_count, 2)
        self.output.write.assert_not_called()

    def test_ambiguous_control_interface_is_rejected(self):
        self.device.get_active_configuration.return_value = [
            self.control, Interface(2, 2, 2, []), self.data]
        with self.assertRaisesRegex(ProtocolError, 'ambiguous'):
            UsbTransport()
        self.claim.assert_not_called()
        self.device.ctrl_transfer.assert_not_called()

    def test_vendor_bulk_interface_does_not_receive_cdc_requests(self):
        self.data.bInterfaceClass = 255
        transport = UsbTransport()
        self.device.ctrl_transfer.assert_not_called()
        self.claim.assert_called_once_with(self.device, 1)
        transport.close()
        self.device.ctrl_transfer.assert_not_called()

    def test_close_failure_still_releases_interfaces_and_restores_drivers(self):
        self.device.is_kernel_driver_active.return_value = True
        self.device.ctrl_transfer.side_effect = [7, 0, OSError('disconnected')]
        transport = UsbTransport()
        with self.assertRaisesRegex(OSError, 'disconnected'):
            transport.close()
        self.assertEqual(self.release.call_count, 2)
        self.assertEqual(self.device.attach_kernel_driver.call_args_list, [call(1), call(0)])
        self.dispose.assert_called_once_with(self.device)


if __name__ == '__main__':
    unittest.main()
