from _paths import ROOT, data  # noqa: F401
import cart_station_storage
import errno
import importlib.util
import os
import struct
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_transport import Adapter, SerialTransport, UsbTransport, find_linux_libusb, is_usb_timeout, open_transport, usb_backend
from cart_station_protocol import ProtocolError, packet
from cart_station_environment import default_output_directory, detect_environment
from cart_station_nes_core import dump
from cart_station_storage import conflict_path, rom_filename, save_rom, save_verified_rom


class Wire:
    def __init__(self, replies, fragment=7):
        self.replies = bytearray(replies)
        self.writes = []
        self.fragment = fragment

    def write(self, data):
        self.writes.append(data)

    def read(self, size):
        count = min(size, self.fragment, len(self.replies))
        data = bytes(self.replies[:count])
        del self.replies[:count]
        return data


def status(cart=3):
    return packet(0, 4) + bytes((cart, 0, 0, 0))


def memory(data):
    return packet(0, len(data)) + data + status()


class ProtocolTests(unittest.TestCase):
    def test_product_partial_reads(self):
        adapter = Adapter(Wire(packet(0, 4) + struct.pack('<HH', 12, 34), 1))
        self.assertEqual(adapter.product(), {'build': 12, 'product': 34})

    def test_no_cart(self):
        self.assertEqual(Adapter(Wire(packet(0, 0))).status(), 0)

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ProtocolError):
            Adapter(Wire(packet(0, 9))).status()

    def test_truncated_header(self):
        with self.assertRaises(ProtocolError):
            Adapter(Wire(b'\0' * 7)).product()

    def test_wrong_read_size(self):
        with self.assertRaises(ProtocolError):
            Adapter(Wire(packet(0, 10))).memory(0x11, 0x8000, 20)

    def test_pipeline(self):
        wire = Wire(memory(b'abcd'))
        self.assertEqual(Adapter(wire).memory(0x11, 0x8000, 4), b'abcd')
        self.assertEqual(wire.writes, [packet(0x11, 8, 4, 0x8000, 4), packet(6, 0, 4)])

    def test_removed_cart(self):
        wire = Wire(packet(0, 4) + b'abcd' + status(0))
        with self.assertRaises(ProtocolError):
            Adapter(wire).memory(0x11, 0x8000, 4)

    @patch('time.sleep')
    def test_dump_all_layouts_and_cleanup(self, sleep):
        for prg_kib in (16, 32):
            for chr_kib in (0, 8):
                blocks = [bytes([n + 1]) * 16384 for n in range(prg_kib // 16)]
                if chr_kib:
                    blocks.append(b'C' * 8192)
                read = b''.join(memory(block) for block in blocks)
                wire = Wire(status() + bytes(16) + read * 2 + bytes(16), 4093)
                rom = dump(Adapter(wire), prg_kib, chr_kib, 'vertical', True)
                self.assertEqual(rom[:8], b'NES\x1a' + bytes((prg_kib // 16, chr_kib // 8, 3, 0)))
                self.assertEqual(rom[16:], b''.join(blocks))
                self.assertEqual(wire.writes[-2:], [packet(4, 4, 0, 0), packet(5, 0, 0)])
                self.assertEqual(len(wire.replies), 0)

    @patch('time.sleep')
    def test_mismatch_fails_but_still_shuts_down(self, sleep):
        wire = Wire(status() + bytes(16) + memory(b'A' * 16384) + memory(b'B' * 16384) + bytes(16), 4096)
        with self.assertRaisesRegex(ProtocolError, 'differ'):
            dump(Adapter(wire), 16, 0, 'horizontal')
        self.assertEqual(wire.writes[-2:], [packet(4, 4, 0, 0), packet(5, 0, 0)])

    def test_wrong_cart_no_initialization(self):
        wire = Wire(status(5))
        with self.assertRaises(ProtocolError):
            dump(Adapter(wire), 16, 8, 'horizontal')
        self.assertEqual(wire.writes, [packet(6, 0, 4)])

    def test_invalid_layout_no_io(self):
        wire = Wire(b'')
        with self.assertRaises(ValueError):
            dump(Adapter(wire), 64, 8, 'horizontal')
        self.assertFalse(wire.writes)

    def test_usb_surplus_is_preserved(self):
        class Endpoint:
            wMaxPacketSize = 64
            calls = []

            def read(self, size, timeout):
                self.calls.append(size)
                return b'header12payload'

        usb = UsbTransport.__new__(UsbTransport)
        usb.input = Endpoint()
        usb.pending = bytearray()
        self.assertEqual(usb.read(8), b'header12')
        self.assertEqual(usb.read(7), b'payload')
        self.assertEqual(usb.input.calls, [64])


class StatusAndStorageTests(unittest.TestCase):
    def test_raw_ff_payload_is_preserved(self):
        self.assertEqual(Adapter(Wire(packet(0, 4) + b'\xff' * 4)).status(), 255)


    def test_save_never_overwrites_and_recognizes_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dump.nes'
            self.assertTrue(save_rom(path, b'first'))
            self.assertFalse(save_rom(path, b'first', allow_identical=True))
            with self.assertRaises(FileExistsError):
                save_rom(path, b'second', allow_identical=True)
            self.assertEqual(path.read_bytes(), b'first')


class HostAndConfirmationTests(unittest.TestCase):
    @patch('cart_station_transport.ctypes.util.find_library', return_value=None)
    @patch('cart_station_transport.ctypes.CDLL')
    def test_linux_finds_loadable_soname_without_library_lookup(self, cdll, lookup):
        self.assertEqual(find_linux_libusb('usb-1.0'), 'libusb-1.0.so.0')
        cdll.assert_called_once_with('libusb-1.0.so.0')

    @patch('cart_station_transport.ctypes.util.find_library', return_value=None)
    @patch('cart_station_transport.ctypes.CDLL', side_effect=OSError('not found'))
    def test_linux_missing_native_library_returns_none(self, cdll, lookup):
        self.assertIsNone(find_linux_libusb('usb-1.0'))

    @patch('cart_station_transport.ctypes.util.find_library', return_value='legacy.so')
    @patch('cart_station_transport.ctypes.CDLL')
    def test_linux_rejects_library_without_libusb1_symbols(self, cdll, lookup):
        cdll.side_effect = [object(), Mock()]
        self.assertEqual(find_linux_libusb('usb-1.0'), 'libusb-1.0.so.0')

    @patch('cart_station_transport.platform.system', return_value='Linux')
    @patch('usb.backend.libusb1.get_backend')
    def test_linux_backend_uses_custom_finder(self, backend, system):
        self.assertIs(usb_backend(), backend.return_value)
        backend.assert_called_once_with(find_library=find_linux_libusb)

    @patch('cart_station_transport.platform.system', return_value='Windows')
    @patch('usb.backend.libusb1.get_backend', return_value=None)
    def test_missing_backend_has_clear_error(self, backend, system):
        with self.assertRaisesRegex(ProtocolError, 'backend unavailable'):
            usb_backend()
        backend.assert_called_once_with()

    @patch('cart_station_environment.Path.is_dir', return_value=True)
    @patch('cart_station_environment.Path.is_file', return_value=True)
    @patch('cart_station_environment.platform.machine', return_value='armv7l')
    @patch('cart_station_environment.platform.system', return_value='Linux')
    def test_mister_detection_requires_sd_layout(self, system, machine, is_file, is_dir):
        self.assertEqual(detect_environment(), 'mister')
        is_file.return_value = False
        self.assertEqual(detect_environment(), 'pc')
        is_file.return_value = True
        machine.return_value = 'x86_64'
        self.assertEqual(detect_environment(), 'pc')

    @patch('cart_station_environment.platform.system', return_value='Windows')
    def test_windows_and_output_defaults(self, system):
        self.assertEqual(detect_environment(), 'pc')
        self.assertEqual(default_output_directory('pc'), Path.home() / 'RetroFreakDumps')
        self.assertEqual(default_output_directory('mister'), Path('/media/fat/games/NES'))

    def test_custom_per_system_output_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'paths.json'
            config.write_text(json.dumps({'version': 1, 'pc': {'NES': folder},
                'mister': {'NES': '/media/fat/games/NES', 'SNES': '/media/fat/games/SNES'}}), encoding='utf-8')
            self.assertEqual(default_output_directory('pc', 'NES', config), Path(folder))
            self.assertEqual(default_output_directory('mister', 'SNES', config), Path('/media/fat/games/SNES'))

    def test_unconfigured_system_does_not_guess_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'paths.json'
            config.write_text('{"version":1,"mister":{"NES":"/media/fat/games/NES"}}', encoding='utf-8')
            with self.assertRaisesRegex(ProtocolError, 'No output path configured'):
                default_output_directory('mister', 'UNIMPLEMENTED', config)

    def test_invalid_or_missing_settings_stop_instead_of_using_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'paths.json'
            for content in ('invalid json', '{"version":2}',
                            '{"version":1,"mister":{"NES":"relative/NES"}}',
                            '{"version":1,"mister":{"NES":null}}'):
                config.write_text(content, encoding='utf-8')
                with self.assertRaises(ProtocolError):
                    default_output_directory('mister', 'NES', config)
            with self.assertRaises(ProtocolError):
                default_output_directory('pc', 'NES', Path(folder) / 'missing.json')

    def ports(self, ports):
        return patch.dict('sys.modules', {
            'serial': Mock(),
            'serial.tools': Mock(list_ports=Mock(comports=Mock(return_value=ports)))})

    @patch('cart_station_transport.SerialTransport')
    @patch('cart_station_transport.UsbTransport')
    def test_auto_selects_only_matching_serial_reader(self, usb, serial):
        ports = [Mock(device='COM1', vid=123, pid=456),
                 Mock(device='COM7', vid=0xf00d, pid=0x1337)]
        with self.ports(ports):
            self.assertIs(open_transport(), serial.return_value)
        serial.assert_called_once_with('COM7')
        usb.assert_not_called()

    @patch('cart_station_transport.platform.system', return_value='Linux')
    @patch('cart_station_transport.UsbTransport')
    def test_linux_uses_usb_when_no_serial_reader(self, usb, system):
        with self.ports([]):
            self.assertIs(open_transport(), usb.return_value)

    @patch('cart_station_transport.SerialTransport')
    @patch('cart_station_transport.UsbTransport')
    def test_ambiguous_readers_are_not_opened(self, usb, serial):
        with self.ports([Mock(device='COM3', vid=0xf00d, pid=0x1337),
                         Mock(device='COM4', vid=0xf00d, pid=0x1337)]):
            with self.assertRaises(ProtocolError):
                open_transport()
        serial.assert_not_called()
        usb.assert_not_called()

    @patch('cart_station_transport.SerialTransport', side_effect=OSError('busy'))
    @patch('cart_station_transport.UsbTransport')
    def test_serial_open_error_does_not_fall_back(self, usb, serial):
        with self.ports([Mock(device='COM3', vid=0xf00d, pid=0x1337)]):
            with self.assertRaises(OSError):
                open_transport()
        usb.assert_not_called()


class AtomicSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'game.nes'

    def names(self):
        return sorted(item.name for item in self.folder.iterdir())

    def test_success_leaves_only_the_rom(self):
        self.assertTrue(save_rom(self.path, b'rom'))
        self.assertEqual(self.path.read_bytes(), b'rom')
        self.assertEqual(self.names(), ['game.nes'])

    def test_failed_flush_leaves_neither_rom_nor_temporary(self):
        with patch('cart_station_storage.os.fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                save_rom(self.path, b'rom')
        self.assertEqual(self.names(), [])

    def test_interruption_before_publish_never_exposes_a_partial_file(self):
        with patch('cart_station_storage._publish_no_replace', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                save_rom(self.path, b'rom')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.names(), [])

    def test_different_file_is_preserved_by_default(self):
        self.path.write_bytes(b'old')
        with self.assertRaises(FileExistsError):
            save_rom(self.path, b'new')
        self.assertEqual(self.path.read_bytes(), b'old')
        self.assertEqual(self.names(), ['game.nes'])

    def test_long_titles_leave_room_for_the_conflict_name(self):
        digest = 'f' * 64
        for title in ('A' * 400, 'ア' * 200, 'Game ' * 60 + '.'):
            name = rom_filename(title, '.sfc', 'SFC_fallback')
            alternate = conflict_path(self.folder / name, digest).name
            self.assertLessEqual(len(alternate.encode('utf-8')), 255)
            self.assertTrue(name.endswith('.sfc'))
            self.assertFalse(name[:-4].endswith((' ', '.')))
        self.assertEqual(rom_filename('Bad:/Name', '.nes', 'NES_x'), 'Bad__Name.nes')
        self.assertEqual(rom_filename('CON', '.md', 'MD_x'), 'MD_x.md')
        self.assertEqual(rom_filename(' . ', '.gb', 'GB_x'), 'GB_x.gb')

    def test_conflict_keeps_original_and_reuses_verified_copy(self):
        self.path.write_bytes(b'partial')
        alternate, created = save_verified_rom(self.path, b'good')
        self.assertTrue(created)
        self.assertEqual(self.path.read_bytes(), b'partial')
        self.assertEqual(alternate.read_bytes(), b'good')
        self.assertIn('.verified-', alternate.name)
        self.assertEqual(save_verified_rom(self.path, b'good'), (alternate, False))
        self.assertEqual(len(self.names()), 2)

    def test_identical_file_is_reused_without_backup(self):
        save_rom(self.path, b'good')
        self.assertEqual(save_verified_rom(self.path, b'good'), (self.path, False))
        self.assertEqual(self.names(), ['game.nes'])

    def test_writer_arriving_at_publish_is_never_overwritten(self):
        publisher = cart_station_storage._publish_no_replace

        def race(source, target):
            target.write_bytes(b'other writer')
            publisher(source, target)

        with patch('cart_station_storage._publish_no_replace', side_effect=race):
            with self.assertRaises(FileExistsError):
                save_rom(self.path, b'our ROM')
        self.assertEqual(self.path.read_bytes(), b'other writer')
        self.assertEqual(self.names(), ['game.nes'])

    def test_concurrent_saves_publish_one_complete_winner(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(2)
        publisher = cart_station_storage._publish_no_replace

        def synchronized(source, target):
            barrier.wait(timeout=10)
            publisher(source, target)

        def writer(data):
            try:
                return save_rom(self.path, data)
            except FileExistsError:
                return False

        with patch('cart_station_storage._publish_no_replace', side_effect=synchronized):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(writer, data) for data in (b'a' * 10000, b'b' * 10000)]
                results = [future.result() for future in futures]
        self.assertEqual(sorted(results), [False, True])
        self.assertIn(self.path.read_bytes(), (b'a' * 10000, b'b' * 10000))
        self.assertEqual(self.names(), ['game.nes'])

    def test_simultaneous_identical_save_reuses_winner(self):
        publisher = cart_station_storage._publish_no_replace
        def race(source, target):
            target.write_bytes(b'same')
            publisher(source, target)
        with patch('cart_station_storage._publish_no_replace', side_effect=race):
            self.assertFalse(save_rom(self.path, b'same', allow_identical=True))
        self.assertEqual(self.names(), ['game.nes'])

    def test_conflicting_alternate_is_not_overwritten(self):
        self.path.write_bytes(b'original')
        alternate, _ = save_verified_rom(self.path, b'good')
        alternate.write_bytes(b'user file')
        with self.assertRaises(FileExistsError):
            save_verified_rom(self.path, b'good')
        self.assertEqual(alternate.read_bytes(), b'user file')
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_linux_rename_noreplace_flags_and_collision(self):
        rename = Mock(return_value=-1)
        library = Mock(renameat2=rename)
        with patch('cart_station_storage.os.name', 'posix'), patch('cart_station_storage.platform.system', return_value='Linux'), \
                patch('cart_station_storage.ctypes.CDLL', return_value=library), \
                patch('cart_station_storage.ctypes.get_errno', return_value=errno.EEXIST), \
                patch('cart_station_storage.os.link') as link:
            with self.assertRaises(FileExistsError):
                cart_station_storage._publish_no_replace(self.path, self.path)
        self.assertEqual(rename.call_args.args[-1], 1)
        link.assert_not_called()

    def test_mister_arm_uses_syscall_when_libc_wrapper_is_missing(self):
        from types import SimpleNamespace
        syscall = Mock(return_value=0)
        library = SimpleNamespace(renameat2=None, syscall=syscall)
        with patch('cart_station_storage.os.name', 'posix'), patch('cart_station_storage.platform.system', return_value='Linux'), \
                patch('cart_station_storage.platform.machine', return_value='armv7l'), \
                patch('cart_station_storage.ctypes.sizeof', return_value=4), \
                patch('cart_station_storage.ctypes.CDLL', return_value=library), patch('cart_station_storage.os.link') as link:
            cart_station_storage._publish_no_replace(self.path, self.path)
        self.assertEqual(syscall.call_args.args[0], 382)
        self.assertEqual(syscall.call_args.args[-1], 1)
        link.assert_not_called()

    def test_interruption_after_publish_keeps_complete_rom(self):
        publisher = cart_station_storage._publish_no_replace
        def interrupted(source, target):
            publisher(source, target)
            raise KeyboardInterrupt
        with patch('cart_station_storage._publish_no_replace', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                save_rom(self.path, b'complete')
        self.assertEqual(self.path.read_bytes(), b'complete')
        self.assertEqual(self.names(), ['game.nes'])

    def test_unsupported_storage_fails_without_overwriting(self):
        from types import SimpleNamespace
        # A plain Mock invents a callable syscall attribute on ARM, so this
        # test used a stale native errno instead of simulating missing support.
        library = SimpleNamespace(renameat2=None, syscall=None)
        with patch('cart_station_storage.os.name', 'posix'), patch('cart_station_storage.platform.system', return_value='Linux'), \
                patch('cart_station_storage.platform.machine', return_value='armv7l'), \
                patch('cart_station_storage.ctypes.sizeof', return_value=4), \
                patch('cart_station_storage.ctypes.CDLL', return_value=library), \
                patch('cart_station_storage.ctypes.get_errno', side_effect=AssertionError('No native call was made')), \
                patch('cart_station_storage.os.link', side_effect=OSError(errno.EPERM, 'no hard links')):
            with self.assertRaises(ProtocolError):
                cart_station_storage._publish_no_replace(self.path, self.path)

    def test_only_old_temporaries_of_this_program_are_removed(self):
        old, recent, other = (self.folder / name for name in ('.rfdump-old.tmp', '.rfdump-new.tmp', 'user.tmp'))
        for item in (old, recent, other):
            item.write_bytes(b'x')
        past = time.time() - 7200
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        save_rom(self.path, b'rom')
        self.assertEqual(self.names(), ['.rfdump-new.tmp', 'game.nes', 'user.tmp'])


class OrderedWire(Wire):
    def __init__(self, replies, events):
        super().__init__(replies)
        self.events = events

    def drop_input(self):
        self.events.append('drop')

    def write(self, data):
        self.events.append('write')
        super().write(data)


class TransportRecoveryTests(unittest.TestCase):
    def test_stale_input_is_dropped_before_the_read_request(self):
        events = []
        Adapter(OrderedWire(memory(b'abcd'), events)).memory(0x11, 0x8000, 4)
        self.assertEqual(events[:2], ['drop', 'write'])
        self.assertEqual(events.count('drop'), 1)

    def test_serial_drop_clears_the_driver_buffer(self):
        transport = SerialTransport.__new__(SerialTransport)
        transport.device = Mock()
        transport.drop_input()
        transport.device.reset_input_buffer.assert_called_once_with()

    def test_usb_drop_discards_only_buffered_bytes(self):
        transport = UsbTransport.__new__(UsbTransport)
        transport.pending = bytearray(b'stale')
        transport.drop_input()
        self.assertEqual(transport.pending, bytearray())

    @unittest.skipUnless(importlib.util.find_spec('usb'), 'pyusb is not installed')
    def test_usb_timeout_reads_as_empty_like_serial(self):
        import usb.core

        class Endpoint:
            wMaxPacketSize = 64

            def read(self, size, timeout):
                raise usb.core.USBTimeoutError('Operation timed out', None, errno.ETIMEDOUT)

        transport = UsbTransport.__new__(UsbTransport)
        transport.input, transport.pending = Endpoint(), bytearray()
        self.assertEqual(transport.read(8), b'')
        with self.assertRaisesRegex(ProtocolError, 'Truncated response'):
            Adapter(transport).exact(8)

    @unittest.skipUnless(importlib.util.find_spec('usb'), 'pyusb is not installed')
    def test_other_usb_errors_are_not_hidden(self):
        import usb.core

        class Endpoint:
            wMaxPacketSize = 64

            def read(self, size, timeout):
                raise usb.core.USBError('Pipe error', None, errno.EPIPE)

        transport = UsbTransport.__new__(UsbTransport)
        transport.input, transport.pending = Endpoint(), bytearray()
        with self.assertRaises(usb.core.USBError):
            transport.read(8)

    @unittest.skipUnless(importlib.util.find_spec('usb'), 'pyusb is not installed')
    def test_timeout_classification(self):
        import usb.core
        self.assertTrue(is_usb_timeout(usb.core.USBError('timeout', None, errno.ETIMEDOUT)))
        self.assertFalse(is_usb_timeout(usb.core.USBError('pipe', None, errno.EPIPE)))
        self.assertFalse(is_usb_timeout(ValueError('not usb')))


if __name__ == '__main__':
    unittest.main()
