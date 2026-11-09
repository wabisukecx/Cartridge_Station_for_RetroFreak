"""Atomic, no-overwrite ROM publication shared by all systems."""
import ctypes
import errno
import hashlib
import os
import platform
import re
import time
from cart_station_protocol import ProtocolError


def _sync_directory(directory):
    """Best effort: make the rename itself durable (not supported on every OS/filesystem)."""
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _remove_stale_temporaries(directory, max_age=3600):
    """Remove leftovers of an interrupted save. Only this module's own temp names."""
    now = time.time()
    try:
        for item in directory.glob('.rfdump-*.tmp'):
            try:
                if item.is_file() and now - item.stat().st_mtime > max_age:
                    item.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _publish_no_replace(source, target):
    """Atomically publish a complete file, refusing an existing destination.

    Windows rename refuses replacement. Linux renameat2 works on FAT without
    hard links. ARM EABI also supports older libc through syscall(382).
    Otherwise try hard links; unsupported storage fails closed.
    """
    if os.name == 'nt':
        os.rename(str(source), str(target))
        return
    if platform.system() == 'Linux':
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, 'renameat2', None)
        arguments = (-100, os.fsencode(source), -100, os.fsencode(target), 1)
        types = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                 ctypes.c_char_p, ctypes.c_uint]
        if rename is not None:
            rename.argtypes, rename.restype = types, ctypes.c_int
        elif (platform.machine().lower() in ('armv6l', 'armv7l', 'armv8l')
              and ctypes.sizeof(ctypes.c_void_p) == 4):
            # MiSTer: 32-bit ARM EABI. Linux arch/arm/tools/syscall.tbl:
            # https://github.com/torvalds/linux/blob/master/arch/arm/tools/syscall.tbl
            rename = getattr(library, 'syscall', None)
            if rename is not None:
                arguments = (382,) + arguments
                rename.argtypes = [ctypes.c_long] + types
                rename.restype = ctypes.c_long
        if rename is not None:
            if rename(*arguments) == 0:
                return
            code = ctypes.get_errno()
            if code not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                raise OSError(code, os.strerror(code), str(target))
    try:
        os.link(str(source), str(target))
    except FileExistsError:
        raise
    except OSError as error:
        raise ProtocolError('Storage cannot publish ROMs without overwriting; '
                            'existing files were preserved: %s' % error) from error


def save_rom(path, data, allow_identical=False):
    """Publish flushed data without replacing any existing file.

    Directory sync is best effort; power-loss durability depends on storage.
    Incomplete writes stay under temporary names, never under the ROM name.
    """
    directory = path.parent
    if path.exists():
        if allow_identical and path.is_file() and path.read_bytes() == data:
            return False
        raise FileExistsError(errno.EEXIST, 'File exists', str(path))
    _remove_stale_temporaries(directory)
    temporary = directory / ('.rfdump-%s.tmp' % os.urandom(6).hex())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(str(temporary), flags, 0o666)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            if stream.write(data) != len(data):
                raise OSError('Incomplete ROM file write')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _publish_no_replace(temporary, path)
        except FileExistsError:
            # Another writer may have published identical verified data.
            if allow_identical and path.is_file() and path.read_bytes() == data:
                return False
            raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _sync_directory(directory)
    return True


_RESERVED_NAMES = frozenset(('CON', 'PRN', 'AUX', 'NUL', *('COM%d' % n for n in range(1, 10)),
                             *('LPT%d' % n for n in range(1, 10))))
# 255-byte name limit minus '.verified-' + SHA-256 (74) and the longest suffix.
MAX_STEM_BYTES = 170


def rom_filename(title, suffix, fallback):
    """A safe ROM filename from a DB title; titles are labels, never trusted paths."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', title).strip(' .')
    stem = stem.encode('utf-8')[:MAX_STEM_BYTES].decode('utf-8', 'ignore').strip(' .')
    if not stem or stem.split('.')[0].upper() in _RESERVED_NAMES:
        stem = fallback
    return stem + suffix


def conflict_path(path, digest):
    """A deterministic alternate filename; no assumptions about the old file."""
    return path.with_name(path.stem + '.verified-' + digest + path.suffix)


def save_verified_rom(path, data):
    """Save already verified bytes, preserving a conflicting file in place.

    Both CLI and manager use the same recovery: a partial old file or another
    user's ROM stays untouched; the new complete ROM gets a hash-qualified name.
    """
    try:
        return path, save_rom(path, data, allow_identical=True)
    except FileExistsError:
        alternate = conflict_path(path, hashlib.sha256(data).hexdigest())
        return alternate, save_rom(alternate, data, allow_identical=True)


