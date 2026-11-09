"""GBA linear ROM reads; full DB hashes determine size and title."""
import hashlib
import time
from dataclasses import dataclass
from cart_station_protocol import CART_GBA, ProtocolError, cleanup_after
from cart_station_rom_db import RomDatabase
from cart_station_storage import rom_filename

MAX_ROM = 32 * 1024 * 1024
BLOCK = 32768


@dataclass(frozen=True)
class GbaProfile:
    header: bytes
    title: str
    game_code: str
    revision: int
    prg_size: int = 0
    raw_sha1: str = ''
    file_sha256: str = ''
    system: str = 'GBA'
    controller: str = 'Linear ROM'
    chr_size: int = 0

    @property
    def id(self):
        return 'gba-header-' + hashlib.sha1(self.header).hexdigest()

    def support(self):
        if self.prg_size and not 192 <= self.prg_size <= MAX_ROM:
            return 'GBA ROM exceeds the 32 MiB linear address space'
        return None

    def filename(self):
        if not self.raw_sha1:
            raise ProtocolError('GBA needs full DB verification before saving')
        title = self.title[:-4] if self.title.lower().endswith('.gba') else self.title
        return rom_filename(title, '.gba', 'GBA_' + self.raw_sha1)


def parse_header(header):
    if not isinstance(header, bytes) or len(header) != 192:
        raise ProtocolError('GBA header must be exactly 192 bytes')
    if header[0xb2] != 0x96:
        raise ProtocolError('GBA fixed header byte mismatch; check cartridge contacts')
    if (-sum(header[0xa0:0xbd]) - 0x19) & 255 != header[0xbd]:
        raise ProtocolError('GBA header checksum mismatch')
    title = header[0xa0:0xac].split(b'\0', 1)[0].decode('ascii', errors='replace')
    title = ''.join(c if 32 <= ord(c) <= 126 else '?' for c in title).strip() or 'GBA cartridge'
    code = header[0xac:0xb0].decode('ascii', errors='replace')
    return GbaProfile(header, title, code, header[0xbc])


class GbaDatabase(RomDatabase):
    system = 'GBA'
    max_size = MAX_ROM
    filename = 'gba_profiles.reference.json'


def inspect_header(adapter):
    if adapter.status() != CART_GBA:
        raise ProtocolError('Insert one GBA cartridge')
    with cleanup_after(adapter.mode, 0):
        adapter.mode(0)
        time.sleep(0.2)
        # Read 2 KiB at address zero; the header occupies its first 192 bytes.
        first, second = adapter.gba_read(0, 2048), adapter.gba_read(0, 2048)
        if len(first) != 2048 or len(second) != 2048 or first != second:
            raise ProtocolError('Two GBA header-area reads differ or are incomplete')
        return parse_header(first[:192])


def validate_payload(profile, payload, require_hash=True):
    if (len(payload) < 192 or len(payload) > MAX_ROM or payload[:192] != profile.header
            or (profile.prg_size and len(payload) != profile.prg_size)):
        raise ProtocolError('GBA ROM size/header mismatch')
    parse_header(bytes(payload[:192]))
    if require_hash:
        if not profile.raw_sha1 or hashlib.sha1(payload).hexdigest() != profile.raw_sha1:
            raise ProtocolError('GBA full ROM hash mismatch')
        if profile.file_sha256 and hashlib.sha256(payload).hexdigest() != profile.file_sha256:
            raise ProtocolError('GBA full ROM SHA-256 mismatch')


def verified_file(path, profile):
    if not profile.raw_sha1:
        return False
    try:
        if not path.is_file() or path.stat().st_size != profile.prg_size:
            return False
        validate_payload(profile, path.read_bytes())
        return True
    except (OSError, ProtocolError):
        return False


def dump_gba(adapter, profile, database, progress=None):
    if profile.support():
        raise ProtocolError(profile.support())
    if adapter.status() != CART_GBA:
        raise ProtocolError('Insert one GBA cartridge')
    if not database.sizes:
        raise ProtocolError('GBA database is empty')
    with cleanup_after(adapter.mode, 0):
        adapter.mode(0)
        time.sleep(0.2)
        header_area = adapter.gba_read(0, 2048)
        if not isinstance(header_area, bytes) or len(header_area) != 2048:
            raise ProtocolError('Incomplete GBA header-area read')
        if header_area[:192] != profile.header:
            raise ProtocolError('GBA cartridge changed; check again')
        # Stop only at a complete known hash. Neither FF regions nor mirrors
        # are sufficient evidence of the physical ROM's useful length.
        first = bytearray()
        digest = hashlib.sha1()
        done = 0
        matched = None
        boundaries = sorted(set(database.sizes) | {MAX_ROM})
        for boundary in boundaries:
            while done < boundary:
                if progress:
                    progress('Read 1/2: determining size by DB', done, MAX_ROM * 2)
                size = min(BLOCK, boundary - done)
                data = adapter.gba_read(done, size)
                if not isinstance(data, bytes) or len(data) != size:
                    raise ProtocolError('Incomplete GBA ROM read')
                first.extend(data)
                digest.update(data)
                done += size
            if (done, digest.hexdigest()) in database.index:
                matched = database.match(profile, first)
                break
        if matched is None:
            raise ProtocolError('GBA full ROM hash is not in the database; no ROM saved. SHA-1: ' + digest.hexdigest())
        validate_payload(matched, first)
        if progress:
            progress('Preparing GBA read 2/2', matched.prg_size, matched.prg_size * 2)
        # Reapply the documented GBA mode sequence; this is not a power cycle.
        adapter.mode(0)
        time.sleep(0.2)
        if adapter.status() != CART_GBA:
            raise ProtocolError('GBA cartridge disappeared before second read')
        header_area = adapter.gba_read(0, 2048)
        if not isinstance(header_area, bytes) or len(header_area) != 2048:
            raise ProtocolError('Incomplete GBA header-area read')
        if header_area[:192] != profile.header:
            raise ProtocolError('GBA cartridge changed before second read')
        second = bytearray()
        done = 0
        while done < matched.prg_size:
            if progress:
                progress('Read 2/2', matched.prg_size + done, matched.prg_size * 2)
            size = min(BLOCK, matched.prg_size - done)
            data = adapter.gba_read(done, size)
            if not isinstance(data, bytes) or len(data) != size:
                raise ProtocolError('Incomplete GBA ROM read')
            second.extend(data)
            done += size
        if first != second:
            raise ProtocolError('Two GBA ROM reads differ; no ROM saved')
        validate_payload(matched, second)
        if progress:
            progress('Verifying GBA', done * 2, done * 2)
        return matched, bytes(first)
