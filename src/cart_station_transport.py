"""RetroFreak serial/USB transport and adapter. No CLI or frontend imports."""
import ctypes
import ctypes.util
import errno
import json
import logging
import platform
import struct
import sys
import time
from collections import deque
from cart_station_protocol import (ProtocolError, CommunicationError, CART_NONE, CART_SFC, CART_NES, CART_GB, CART_GBA, CART_MD, CART_PCE,
    packet, read_memory, write_byte)

from cart_station_paths import ROOT
from cart_station_banks import read_banks
from cart_station_nes_registers import NesRegisters

_vendor = ROOT / 'vendor'
if _vendor.is_dir() and str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))


def open_transport(port=None, usb=False):
    if port:
        return SerialTransport(port)
    if usb:
        return UsbTransport()
    # Enumerating ports never opens unrelated serial devices. If opening the
    # identified reader fails, propagate the error instead of silently switching.
    try:
        from serial.tools import list_ports
    except ModuleNotFoundError as error:
        if error.name not in ('serial', 'serial.tools', 'serial.tools.list_ports'):
            raise
        if platform.system() == 'Windows':
            raise ProtocolError('Install requirements.txt to discover the reader serial port') from error
        ports = []
    else:
        ports = [p.device for p in list_ports.comports()
                 if p.vid == 0xf00d and p.pid == 0x1337]
    if len(ports) > 1:
        raise ProtocolError('Multiple readers found; select one using --port')
    if ports:
        return SerialTransport(ports[0])
    if platform.system() == 'Linux':
        return UsbTransport()
    raise ProtocolError('No matching serial reader found. Check the driver or specify --port')


def find_linux_libusb(name):
    """MiSTer can load libusb even when ctypes.util.find_library returns None."""
    located = ctypes.util.find_library(name)
    candidates = [located, 'libusb-1.0.so.0', '/lib/libusb-1.0.so.0',
                  '/usr/lib/libusb-1.0.so.0']
    for candidate in dict.fromkeys(candidates):
        if candidate is None:
            continue
        try:
            library = ctypes.CDLL(candidate)
            getattr(library, 'libusb_init')
            return candidate
        except (OSError, AttributeError):
            continue
    return None


def usb_backend():
    import usb.backend.libusb1
    options = {'find_library': find_linux_libusb} if platform.system() == 'Linux' else {}
    backend = usb.backend.libusb1.get_backend(**options)
    if backend is None:
        raise ProtocolError('libusb 1.0 backend unavailable; check the native library and Python dependencies')
    return backend


def is_usb_timeout(error):
    """pyusb raises USBTimeoutError (errno ETIMEDOUT); the serial transport returns b'' instead."""
    try:
        import usb.core
    except ModuleNotFoundError:
        return False
    timeout_type = getattr(usb.core, 'USBTimeoutError', None)
    if timeout_type is not None and isinstance(error, timeout_type):
        return True
    return isinstance(error, usb.core.USBError) and error.errno in (errno.ETIMEDOUT, 110, 10060)


class TraceTransport:
    """Record application-level I/O; this is not a USB packet capture."""
    def __init__(self, transport, stream):
        self.transport = transport
        self.stream = stream
        self.started = time.monotonic()
        event = {'operation': 'open', 'transport': type(transport).__name__,
                 'platform': platform.platform(), 'python': platform.python_version()}
        recovery = getattr(transport, 'last_recovery', None)
        if isinstance(recovery, dict):
            event['usb_recovery'] = recovery
        self._record(event)

    def _record(self, event):
        self.stream.write(json.dumps(event) + '\n')
        self.stream.flush()

    def _call(self, operation, *args):
        start = time.monotonic()
        recovery = getattr(self.transport, 'last_recovery', None)
        event = {'operation': operation, 'start_ms': round((start-self.started)*1000, 3)}
        if operation == 'write':
            event['hex'] = bytes(args[0]).hex()
        elif operation == 'read':
            event['requested_bytes'] = args[0]
        try:
            result = getattr(self.transport, operation)(*args)
            if operation == 'read':
                event['hex'] = bytes(result).hex()
                event['received_bytes'] = len(result)
            return result
        except Exception as error:
            event['error'] = '%s: %s' % (type(error).__name__, error)
            raise
        finally:
            latest = getattr(self.transport, 'last_recovery', None)
            if isinstance(latest, dict) and latest is not recovery:
                event['usb_recovery'] = latest
            event['duration_ms'] = round((time.monotonic()-start)*1000, 3)
            self._record(event)

    def write(self, data):
        return self._call('write', data)

    def read(self, size):
        return self._call('read', size)

    def drop_input(self):
        return self._call('drop_input')

    def close(self):
        return self._call('close')


class SerialTransport:
    def __init__(self, port):
        import serial
        self.device = serial.Serial(port, 115200, timeout=3, write_timeout=5,
                                    rtscts=False, dsrdtr=False)
        try:
            self.device.dtr = True
            self.device.rts = True
            self.device.reset_input_buffer()
        except BaseException:
            self.device.close()
            raise

    def write(self, data):
        if self.device.write(data) != len(data):
            raise ProtocolError('Incomplete serial write')
        self.device.flush()

    def read(self, size):
        return self.device.read(size)

    def drop_input(self):
        self.device.reset_input_buffer()

    def close(self):
        self.device.close()


class UsbTransport:
    """PyUSB bulk transport; endpoints are chosen from the device descriptors."""
    RECOVERY_SECONDS = 0.5
    RECOVERY_BYTES = 256 * 1024
    RECOVERY_READS = 64
    RECOVERY_TIMEOUT_MS = 20
    RECOVERY_QUIET_READS = 2

    def __init__(self):
        import usb.core
        import usb.util
        self.util = usb.util
        devices = list(usb.core.find(find_all=True, idVendor=0xf00d, idProduct=0x1337,
                                     backend=usb_backend()))
        if len(devices) != 1:
            raise ProtocolError('Expected exactly one USB F00D:1337 device; found %d' % len(devices))
        self.device = devices[0]
        self.detached = []
        self.claimed = []
        self.control_interface = None
        self.control_lines_enabled = False
        self.pending = bytearray()
        self._needs_recovery = True
        self.last_recovery = None
        try:
            config = self.device.get_active_configuration()
            candidates = []
            for interface in config:
                # Do not change alternate settings or device configuration implicitly.
                if interface.bAlternateSetting != 0:
                    continue
                ins = [ep for ep in interface if ep.bmAttributes & 3 == 2 and ep.bEndpointAddress & 0x80]
                outs = [ep for ep in interface if ep.bmAttributes & 3 == 2 and not ep.bEndpointAddress & 0x80]
                if len(ins) == len(outs) == 1:
                    candidates.append((interface, ins[0], outs[0]))
            if len(candidates) != 1:
                raise ProtocolError('USB bulk interface is ambiguous or absent; capture descriptors first')
            interface, self.input, self.output = candidates[0]
            self.interface = interface.bInterfaceNumber
            interfaces = [self.interface]
            if interface.bInterfaceClass == 10:  # CDC Data
                controls = [item.bInterfaceNumber for item in config
                            if item.bAlternateSetting == 0 and item.bInterfaceClass == 2
                            and item.bInterfaceSubClass == 2]
                # This reader omits CDC Union descriptors. A single ACM control
                # and a single bulk data interface are unambiguous; reject others.
                if len(controls) != 1:
                    raise ProtocolError('CDC control interface is ambiguous or absent')
                self.control_interface = controls[0]
                interfaces.insert(0, self.control_interface)
            for number in interfaces:
                try:
                    if self.device.is_kernel_driver_active(number):
                        self.device.detach_kernel_driver(number)
                        self.detached.append(number)
                except NotImplementedError:
                    pass
                usb.util.claim_interface(self.device, number)
                self.claimed.append(number)
            if self.control_interface is not None:
                # Mirror the PC SerialPort settings: 115200 baud, 8N1, DTR/RTS.
                coding = struct.pack('<IBBB', 115200, 0, 0, 8)
                sent = self.device.ctrl_transfer(0x21, 0x20, 0, self.control_interface,
                                                 coding, timeout=3000)
                if sent != len(coding):
                    raise ProtocolError('Incomplete CDC SET_LINE_CODING')
                result = self.device.ctrl_transfer(0x21, 0x22, 3, self.control_interface,
                                                   None, timeout=3000)
                if result != 0:
                    raise ProtocolError('Unexpected CDC SET_CONTROL_LINE_STATE result')
                self.control_lines_enabled = True
            # No protocol request has been sent on this connection. Discard
            # leftovers from a previous failed session before its first request.
            self._recover_input()
        except BaseException:
            try:
                self.close()
            except Exception:
                pass  # Preserve the initialization failure, not a cleanup error.
            raise

    def write(self, data):
        if getattr(self, '_needs_recovery', False):
            self._recover_input()
        try:
            if self.output.write(data, timeout=5000) != len(data):
                raise ProtocolError('Incomplete USB write')
        except BaseException:
            self._needs_recovery = True
            raise

    def read(self, size):
        if not self.pending:
            # USB reads must accommodate a complete endpoint packet even when the
            # protocol caller only needs an 8-byte header. Retain surplus bytes.
            packet_size = self.input.wMaxPacketSize
            count = ((size + packet_size - 1) // packet_size) * packet_size
            try:
                self.pending.extend(self.input.read(count, timeout=3000))
                if not self.pending:
                    self._needs_recovery = True
            except BaseException as error:
                self._needs_recovery = True
                if not is_usb_timeout(error):
                    raise
                # Same contract as SerialTransport: empty bytes mean a timeout, so
                # Adapter.exact reports "Truncated response" on both transports.
                return b''
        data = bytes(self.pending[:size])
        del self.pending[:size]
        return data

    def drop_input(self):
        # Normal bank reads retain the existing local-buffer cleanup without
        # polling the endpoint. Drain it only after a failed transfer.
        if getattr(self, '_needs_recovery', False):
            self._recover_input()
        else:
            self.pending.clear()

    def _recover_input(self):
        """Bounded drain at a request boundary; never resend a failed command.

        The service owns one synchronous transport, so no other request may be
        in flight here. Quiet reads are evidence of an empty endpoint at this
        moment, not proof that the firmware or a USB stall has been reset.
        """
        self._needs_recovery = True
        started = time.monotonic()
        drained = len(self.pending)
        self.pending.clear()
        reads = quiet = 0
        outcome = 'failed'
        try:
            while quiet < self.RECOVERY_QUIET_READS:
                remaining = self.RECOVERY_SECONDS - (time.monotonic() - started)
                if remaining <= 0 or drained >= self.RECOVERY_BYTES or reads >= self.RECOVERY_READS:
                    raise ProtocolError('USB input recovery limit reached; CHECK / RETRY or reconnect USB')
                packet_size = self.input.wMaxPacketSize
                count = min(16384, self.RECOVERY_BYTES - drained)
                count = (count // packet_size) * packet_size
                if not count:
                    raise ProtocolError('USB input recovery byte limit reached; CHECK / RETRY or reconnect USB')
                timeout = min(self.RECOVERY_TIMEOUT_MS, max(1, int(remaining * 1000)))
                reads += 1
                try:
                    chunk = self.input.read(count, timeout=timeout)
                except Exception as error:
                    if not is_usb_timeout(error):
                        raise
                    chunk = b''
                if chunk:
                    drained += len(chunk)
                    quiet = 0
                else:
                    quiet += 1
            outcome = 'quiet'
            self._needs_recovery = False
        finally:
            self.last_recovery = {'outcome': outcome, 'discarded_bytes': drained,
                                  'reads': reads, 'duration_ms': round((time.monotonic() - started) * 1000, 3)}
            logger = logging.getLogger(__name__)
            if drained or outcome != 'quiet':
                logger.warning('USB input recovery: %s', self.last_recovery)
            else:
                logger.debug('USB input recovery: %s', self.last_recovery)

    def close(self):
        errors = []
        try:
            if self.control_lines_enabled:
                self.control_lines_enabled = False
                try:
                    self.device.ctrl_transfer(0x21, 0x22, 0, self.control_interface,
                                              None, timeout=3000)
                except Exception as error:
                    errors.append(error)
            for number in reversed(self.claimed):
                try:
                    self.util.release_interface(self.device, number)
                except Exception as error:
                    errors.append(error)
            self.claimed.clear()
            for number in reversed(self.detached):
                try:
                    self.device.attach_kernel_driver(number)
                except Exception as error:
                    errors.append(error)
            self.detached.clear()
        finally:
            self.util.dispose_resources(self.device)
        if errors:
            raise errors[0]


class Adapter:
    def __init__(self, transport):
        self.transport = transport
        self.nes = NesRegisters(self)
        self.control_replies = deque(maxlen=32)
        self.io_metrics = None

    def send(self, data, context='request'):
        try:
            if self.io_metrics is None:
                self.transport.write(data)
            else:
                self.io_metrics.call(self.transport, 'write', data)
        except Exception as error:
            raise CommunicationError('%s: %s' % (type(error).__name__, error),
                                     context, len(data)) from error

    def exact(self, size, context='response'):
        result = bytearray()
        deadline = time.monotonic() + 10
        while len(result) < size:
            if time.monotonic() >= deadline:
                raise CommunicationError('Response deadline exceeded (10 seconds)',
                                         context, size, len(result))
            try:
                part = (self.transport.read(size - len(result)) if self.io_metrics is None else
                        self.io_metrics.call(self.transport, 'read', size - len(result)))
            except Exception as error:
                raise CommunicationError('%s: %s' % (type(error).__name__, error),
                                         context, size, len(result)) from error
            if not part:
                raise CommunicationError('Truncated response: no bytes returned '
                                         '(timeout or end of stream)',
                                         context, size, len(result))
            result.extend(part)
        return bytes(result)

    def header(self, expected, context='response header'):
        status, length = struct.unpack('<II', self.exact(8, context))
        if status != 0 or length != expected:
            raise ProtocolError('Unexpected reply status=%d length=%d (expected %d) [%s]' %
                                (status, length, expected, context))

    def status(self, send=True, context='Cartridge status command 0x06'):
        if send:
            self.send(packet(6, 0, 4), context + ' request')
        status, length = struct.unpack('<II', self.exact(8, context + ' header'))
        # A length==0 status header is still accepted as empty, but it has not
        # been observed on this hardware: build 10 traces show the empty slot
        # as length 4 + FFFFFFFF. Any other header is rejected.
        if status != 0 or length not in (0, 4):
            raise ProtocolError('Unconfirmed cartridge status header: %d, %d [%s]' %
                                (status, length, context))
        if length == 0:
            return CART_NONE
        return self.exact(4, context + ' payload')[0]

    def product(self):
        self.send(packet(1, 0, 4), 'Product command 0x01 request')
        self.header(4, 'Product command 0x01 header')
        build, product = struct.unpack('<HH', self.exact(4, 'Product command 0x01 payload'))
        return {'build': build, 'product': product}

    def initialize_connection(self):
        """Connection initialization: command 04(0), without command 05."""
        self.send(packet(4, 4, 0, 0), 'Initialize command 0x04 request')
        self.control_replies.append(self.exact(8, 'Initialize command 0x04 reply').hex())

    def mode(self, value):
        for request in (packet(4, 4, 0, value), packet(5, 0, 0)):
            context = 'Mode command 0x%02X' % struct.unpack_from('<I', request)[0]
            self.send(request, context + ' request')
            # The reply contents carry no documented meaning. Preserve raw evidence rather
            # than inventing acknowledgement semantics for commands 04 and 05.
            self.control_replies.append(self.exact(8, context + ' reply').hex())

    def drop_input(self):
        """Discard stale bytes before each read."""
        drop = getattr(self.transport, 'drop_input', None)
        if drop is not None:
            drop()

    def memory(self, command, address, size):
        return read_memory(self, command, address, size, CART_NES, 'NES')

    def snes_mode(self, value):
        if type(value) is not int or value not in (0, 1):
            raise ValueError('Invalid SFC mode')
        # SFC enter/leave sequences, as observed on the reader: order differs from mode().
        requests = ((packet(4, 4, 0, 1), packet(0x2f, 8, 0, 0, 1),
                     packet(5, 0, 0), packet(5, 0, 0)) if value else
                    (packet(0x2f, 8, 0, 0, 0), packet(4, 4, 0, 0), packet(5, 0, 0)))
        for request in requests:
            context = 'SFC mode command 0x%02X' % struct.unpack_from('<I', request)[0]
            self.send(request, context + ' request')
            self.control_replies.append(self.exact(8, context + ' reply').hex())

    def snes_read(self, address, size):
        if (type(address) is not int or type(size) is not int
                or not 0 <= address < 0x1000000 or not 1 <= size <= 32768
                or address + size > 0x1000000):
            raise ValueError('Invalid SFC ROM read')
        return read_memory(self, 7, address, size, CART_SFC, 'SFC')

    def snes_rom_register(self, controller, address, value):
        """Only the SA-1, S-DD1 and CX4 ROM mapping writes; never save or chip RAM."""
        bases = {'Sa1': 0x2220, 'Sdd1': 0x4804}
        if not isinstance(controller, str):
            raise ValueError('Invalid SFC ROM mapping register')
        base = bases.get(controller)
        if type(address) is not int or type(value) is not int:
            raise ValueError('Invalid SFC ROM mapping register')
        if controller == 'CX4':
            allowed = address == 0x7f52 and value == 0
        else:
            allowed = (base is not None and base <= address < base + 4
                       and value in (address - base, address - base + 4))
        if not allowed:
            raise ValueError('Invalid SFC ROM mapping register')
        write_byte(self, 8, address, value)

    def gb_read(self, address, size):
        if (type(address) is not int or type(size) is not int
                or not 0 <= address < 0x8000 or not 1 <= size <= 16384
                or address + size > 0x8000):
            raise ValueError('Invalid GB ROM read')
        return read_memory(self, 0x1b, address, size, CART_GB, 'GB')

    def gb_register(self, address, value):
        # ROM bank controls only: never enable external RAM or touch save/RTC.
        limits = {0x2100: 255, 0x3000: 1, 0x4000: 7, 0x6000: 1}
        if (type(address) is not int or type(value) is not int
                or address not in limits or not 0 <= value <= limits[address]):
            raise ValueError('Invalid GB ROM bank register')
        write_byte(self, 0x1c, address, value)

    def gb_tama5_bank(self, bank):
        # One complete ROM selection transaction, not arbitrary $A000 data
        # writes with a potentially stale RAM/RTC command selector.
        if type(bank) is not int or not 0 <= bank <= 31:
            raise ValueError('Invalid TAMA5 ROM bank')
        write_byte(self, 0x1c, 0xa001, 1)
        write_byte(self, 0x1c, 0xa000, bank >> 4)
        write_byte(self, 0x1c, 0xa001, 0)
        # Bank zero is read from the fixed window.
        if bank:
            write_byte(self, 0x1c, 0xa000, bank & 15)

    def gb_mbc6_register(self, address, value):
        # MBC6 ROM-only controls. Never select flash ($08), enable it,
        # or expose RAM/flash data writes through the generic GB method.
        limits = {0x2000: 127, 0x3000: 127, 0x2800: 0, 0x3800: 0}
        if (type(address) is not int or type(value) is not int
                or address not in limits or not 0 <= value <= limits[address]):
            raise ValueError('Invalid MBC6 ROM bank register')
        write_byte(self, 0x1c, address, value)

    def gba_read(self, address, size):
        if (type(address) is not int or type(size) is not int
                or not 0 <= address < 33554432 or not 2 <= size <= 32768
                or address % 2 or size % 2 or address + size > 33554432):
            raise ValueError('Invalid GBA ROM read')
        return read_memory(self, 0x1f, address, size, CART_GBA, 'GBA')

    def md_read(self, address, size):
        if (type(address) is not int or type(size) is not int
                or not 0 <= address < 0x1000000 or not 2 <= size <= 32768
                or address % 2 or size % 2 or address + size > 0x1000000):
            raise ValueError('Invalid MD ROM read')
        return read_memory(self, 0x0d, address, size, CART_MD, 'MD')

    def md_register(self, address, value):
        # Expose only ROM mapping registers, never save/flash writes.
        if (type(address) is not int or type(value) is not int
                or not ((address == 0xa130f1 and value in (0, 1))
                        or (address == 0xa130ff and 0 <= value <= 23))):
            raise ValueError('Invalid MD ROM mapping register')
        write_byte(self, 0x0e, address, value)

    def pce_read(self, address, size):
        if (type(address) is not int or type(size) is not int or not 0 <= address < 1048576
                or not 1 <= size <= 32768 or address+size > 1048576):
            raise ValueError('Invalid PCE ROM read')
        self.drop_input()
        context = 'PCE read command 0x2D address 0x%06X' % address
        self.send(packet(0x2d, 8, size, address, size), context + ' request')
        self.header(size, context + ' header')
        # PCE reads consume all data first (no appended status request).
        data = self.exact(size, context + ' payload')
        # An additional independent status query follows the completed read.
        observed = self.status(context=context + ' status')
        if observed != CART_PCE:
            raise ProtocolError('%s cartridge disappeared or changed (expected %d, received %d)' %
                                (context, CART_PCE, observed))
        return data

    def pce_register(self, address, value):
        if (type(address) is not int or type(value) is not int
                or not 0x1ff0 <= address <= 0x1fff or value != 255):
            raise ValueError('Invalid PCE ROM mapping register')
        write_byte(self, 0x2e, address, value)

    def nrom(self, prg_kib, chr_kib, progress=None):
        if prg_kib not in (16, 32) or chr_kib not in (0, 8):
            raise ValueError('Unsupported NROM layout')
        prg = read_banks(lambda bank, size: self.memory(0x11, 0x8000 + bank * 0x4000, size),
                         prg_kib * 1024, 0x4000,
                         (lambda done, total: progress(done, (prg_kib + chr_kib) * 1024)) if progress else None)
        chr_data = read_banks(lambda bank, size: self.memory(0x13, 0, size), chr_kib * 1024, 0x2000,
                              (lambda done, total: progress(prg_kib * 1024 + done, (prg_kib + chr_kib) * 1024))
                              if progress else None)
        return prg + chr_data

    def _nes_bank_write(self, address, value, dummy):
        write_byte(self, 0x12, address, value)
        # A short CPU read after every register write completes the bus cycle.
        if dummy:
            self.memory(0x11, 0x8000, 8)

