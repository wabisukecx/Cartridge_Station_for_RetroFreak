"""Shared wire framing and ROM bank operations. No CLI or UI imports."""
import contextlib
import hashlib
import struct

class ProtocolError(Exception):
    pass


class CommunicationError(ProtocolError):
    """Transfer context without claiming that a timeout proves disconnection."""
    def __init__(self, reason, operation, expected_bytes, received_bytes=None):
        self.operation = operation
        self.expected_bytes = expected_bytes
        self.received_bytes = received_bytes
        count = ('%d/%d bytes received' % (received_bytes, expected_bytes)
                 if received_bytes is not None else
                 '%d bytes requested; transmitted count unknown' % expected_bytes)
        super().__init__('%s [%s; %s]' % (reason, operation, count))


CART_NONE = 0
CART_SFC = 1
CART_MD = 2
CART_NES = 3
CART_GB = 5
CART_GBA = 6
CART_PCE = 8
# Observed on the reader: empty slot replies
# header 0000000004000000 + payload ffffffff; GBA replies 06000000.
CART_MULTIPLE = 254  # Observed with FC + GBA in either insertion order (build 10).
CART_EMPTY_FF = 255
CART_EMPTY = frozenset((CART_NONE, CART_EMPTY_FF))


def verify_sha1(data, expected, label):
    actual = hashlib.sha1(data).hexdigest()
    if actual != expected:
        raise ProtocolError('%s hash mismatch: expected %s, received %s' % (label, expected, actual))


def packet(*words):
    return struct.pack('<' + 'I' * len(words), *words)



def read_memory(adapter, command, address, size, cartridge, label):
    context = '%s read command 0x%02X address 0x%06X' % (label, command, address)
    adapter.drop_input()
    adapter.send(packet(command, 8, size, address, size), context + ' request')
    adapter.header(size, context + ' header')
    # The status request is appended before collecting the ROM bytes.
    adapter.send(packet(6, 0, 4), context + ' appended status request')
    data = adapter.exact(size, context + ' payload')
    observed = adapter.status(send=False, context=context + ' trailing status')
    if observed != cartridge:
        raise ProtocolError('%s cartridge disappeared or changed '
                            '(expected %d, received %d) [%s]' %
                            (label, cartridge, observed, context))
    return data


def write_byte(adapter, command, address, value):
    context = 'Bank write command 0x%02X address 0x%06X' % (command, address)
    adapter.send(packet(command, 8, 1, address, 1), context + ' request')
    adapter.header(0, context + ' ready header')
    adapter.send(bytes((value,)), context + ' value')
    adapter.header(0, context + ' completion header')


@contextlib.contextmanager
def cleanup_after(release, *args):
    """Run release(*args) after the block; a failing release never hides the block's own error.

    On success a release failure propagates. When the block already failed, the
    original error stays primary and the release failure is appended to it.
    """
    try:
        yield
    except BaseException as error:
        try:
            release(*args)
        except Exception as cleanup_error:
            _append_cleanup_failure(error, cleanup_error)
        raise
    else:
        release(*args)


def _append_cleanup_failure(error, cleanup_error):
    text = 'cleanup also failed: %s' % (cleanup_error,)
    # CLI and GUI show str(error), so the text goes into the message when possible.
    if (type(error).__str__ is Exception.__str__ and not isinstance(error, OSError)
            and len(error.args) == 1 and isinstance(error.args[0], str)):
        error.args = ('%s (%s)' % (error.args[0], text),)
    elif hasattr(error, 'add_note'):
        error.add_note(text)

