"""Mega Drive ROM readers; no save-data access."""
import hashlib
import codecs
import time
from dataclasses import dataclass, replace
from cart_station_protocol import CART_MD, ProtocolError, cleanup_after
from cart_station_rom_db import RomDatabase
from cart_station_storage import rom_filename

BLOCK = 32768
LINEAR_LIMIT = 4 * 1024 * 1024
MAX_ROM = 12 * 1024 * 1024
SSF2 = b'SUPER STREET FIGHTER2 The New Ch'
SONIC_K = b'SONIC & KNUCKLES'.ljust(32)
SONIC_TITLES = {b'SONIC THE' + b' '*15 + b'HEDGEHOG': ('Sonic1', 2621440),
                b'SONIC THE' + b' '*13 + b'HEDGEHOG 2': ('Sonic2', 3407872),
                b'SONIC THE' + b' '*13 + b'HEDGEHOG 3': ('Sonic3', 4194304)}


@dataclass(frozen=True)
class MdProfile:
    header: bytes
    title: str
    serial: str
    prg_size: int
    controller: str = 'None'
    attached: str = 'None'
    attached_header: bytes = b''
    raw_sha1: str = ''
    file_sha256: str = ''
    system: str = 'MD'
    chr_size: int = 0

    @property
    def id(self):
        return 'md-header-' + hashlib.sha1(self.header + self.attached_header + self.controller.encode('ascii')).hexdigest()

    def support(self):
        limits = {'None': LINEAR_LIMIT, 'SAndN': LINEAR_LIMIT, 'MortalK2': LINEAR_LIMIT, 'SuperSf2': MAX_ROM}
        if (self.controller not in limits or not 512 <= self.prg_size <= limits[self.controller]
                or self.prg_size % 2 or int.from_bytes(self.header[0xa0:0xa4], 'big') != 0):
            return 'MD controller/ROM range is not implemented'
        return None

    def filename(self):
        if not self.raw_sha1:
            raise ProtocolError('MD needs full DB verification before saving')
        title = self.title[:-3] if self.title.lower().endswith('.md') else self.title
        return rom_filename(title, '.md', 'MD_' + self.raw_sha1)


class MdDatabase(RomDatabase):
    system = 'MD'
    min_size = 512
    # Metadata can describe unsupported boards; support() guards real reads.
    max_size = 16 * 1024 * 1024
    filename = 'md_profiles.reference.json'


def text(raw, encoding='ascii'):
    raw = raw.split(b'\0', 1)[0]
    try:
        decoder = codecs.getdecoder(encoding)
    except (LookupError, ImportError):
        # MiSTer's minimal Python may omit the Japanese codec extension.
        # This affects the preview only; identity and DB hashes use raw bytes.
        # JIS X 0201 half-width katakana (0xA1-0xDF) is still recoverable by hand.
        value = ''.join(chr(0xff61 + b - 0xa1) if 0xa1 <= b <= 0xdf else
                        chr(b) if b < 0x80 else '?' for b in raw)
    else:
        value = decoder(raw, 'replace')[0]
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in value).split())


def parse_header(header):
    if not isinstance(header, bytes) or len(header) != 256:
        raise ProtocolError('MD header must be exactly 256 bytes')
    if header[:4] == b'ESAG':
        raise ProtocolError('MD header has swapped byte order; no automatic conversion')
    if header[:4] != b'SEGA':
        raise ProtocolError('MD SEGA header mismatch; check cartridge contacts')
    title = text(header[0x20:0x50], 'shift_jis') or text(header[0x50:0x80], 'shift_jis') or 'MD cartridge'
    serial = text(header[0x80:0x8e])
    start, end = int.from_bytes(header[0xa0:0xa4], 'big'), int.from_bytes(header[0xa4:0xa8], 'big')
    size = end-start+1
    controller = 'SuperSf2' if header[0x20:0x40] == SSF2 else 'SAndN' if header[0x20:0x40] == SONIC_K else 'None'
    if controller == 'SuperSf2':
        size = 5242880
    overrides = {('ULTRACORE', 'GM T-SNAKE-MOD'): 2097152, ('ZERO WING', 'GM T-40023 -00'): 1048576,
                 ('AGAME', 'GM T-XXXXX -XX'): 1048576}
    size = overrides.get((title, serial), size)
    return MdProfile(header, title, serial, size, controller)


def read_header(adapter):
    # Read the header as sixteen 16-byte reads at $100..$1ff.
    parts = [adapter.md_read(0x100 + offset, 16) for offset in range(0, 256, 16)]
    if any(not isinstance(p, bytes) or len(p) != 16 for p in parts):
        raise ProtocolError('Incomplete MD header')
    return b''.join(parts)


def inspect_header(adapter, controller=None):
    if adapter.status() != CART_MD:
        raise ProtocolError('Insert one Mega Drive cartridge')
    with cleanup_after(adapter.mode, 0):
        adapter.mode(1)
        time.sleep(0.2)
        first, second = read_header(adapter), read_header(adapter)
        if first != second:
            raise ProtocolError('Two MD header reads differ')
        profile = parse_header(first)
        if controller is not None:
            if controller not in ('None', 'SAndN', 'SuperSf2', 'MortalK2'):
                raise ProtocolError('Unknown MD controller')
            profile = replace(profile, controller=controller,
                              prg_size=5242880 if controller == 'SuperSf2' else profile.prg_size)
        if profile.controller == 'SAndN':
            first, second = adapter.md_read(0x200100, 64), adapter.md_read(0x200100, 64)
            if not isinstance(first, bytes) or len(first) != 64 or first != second:
                raise ProtocolError('Two MD attached cartridge headers differ or are incomplete')
            attached, size = SONIC_TITLES.get(first[32:64], ('None', profile.prg_size))
            profile = replace(profile, attached=attached, attached_header=first, prg_size=size)
        return profile


def read_mortal_chunk(adapter, address, size, checkpoint):
    parts = []
    for offset in range(0, size, 64):
        count = min(64, size-offset)
        all_ff = 0
        for attempt in range(11):
            checkpoint()
            chunk = adapter.md_read(address+offset, count)
            if not isinstance(chunk, bytes) or len(chunk) != count:
                raise ProtocolError('Incomplete MD MortalK2 read')
            if 255 not in chunk:
                break
            if chunk == b'\xff'*count:
                all_ff += 1
        else:
            if all_ff < 11:
                words = []
                for inner in range(0, count, 2):
                    for retry in range(11):
                        checkpoint()
                        word = adapter.md_read(address+offset+inner, 2)
                        if not isinstance(word, bytes) or len(word) != 2:
                            raise ProtocolError('Incomplete MD MortalK2 word read')
                        if 255 not in word:
                            break
                    words.append(word)
                chunk = b''.join(words)
        parts.append(chunk)
    return b''.join(parts)


def read_rom(adapter, profile, progress=None):
    parts = []
    if profile.controller in ('SuperSf2', 'SAndN'):
        adapter.md_register(0xa130f1, 0)
        if profile.controller == 'SuperSf2':
            adapter.md_register(0xa130ff, 7)
        elif profile.attached == 'Sonic2':
            adapter.md_register(0xa130f1, 1)
    done = 0
    def checkpoint():
        if progress:
            progress(done, profile.prg_size)
    while done < profile.prg_size:
        checkpoint()
        count = min(BLOCK, profile.prg_size-done)
        address = done
        if profile.controller == 'SuperSf2' and done >= LINEAR_LIMIT:
            group, inner = divmod(done, 524288)
            adapter.md_register(0xa130ff, group)
            address = 0x380000 + inner
        if profile.controller == 'MortalK2' and done >= 2097152:
            chunk = read_mortal_chunk(adapter, 0xc00000|done, count, checkpoint)
        else:
            chunk = adapter.md_read(address, count)
        if not isinstance(chunk, bytes) or len(chunk) != count:
            raise ProtocolError('Incomplete MD ROM read')
        parts.append(chunk)
        done += count
    checkpoint()
    return b''.join(parts)


def validate_payload(profile, payload, require_hash=True):
    if len(payload) != profile.prg_size or payload[0x100:0x200] != profile.header:
        raise ProtocolError('MD ROM size/header mismatch')
    parse_header(bytes(payload[0x100:0x200]))
    if require_hash:
        if not profile.raw_sha1 or hashlib.sha1(payload).hexdigest() != profile.raw_sha1:
            raise ProtocolError('MD full ROM hash mismatch')
        if profile.file_sha256 and hashlib.sha256(payload).hexdigest() != profile.file_sha256:
            raise ProtocolError('MD full ROM SHA-256 mismatch')


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


def dump_md(adapter, profile, database, progress=None):
    if profile.support():
        raise ProtocolError(profile.support())
    if adapter.status() != CART_MD:
        raise ProtocolError('Insert one Mega Drive cartridge')
    mapping_started = False

    def release():
        with cleanup_after(adapter.mode, 0):
            if mapping_started and profile.controller in ('SuperSf2', 'SAndN'):
                try:
                    adapter.md_register(0xa130f1, 0)
                finally:
                    if profile.controller == 'SuperSf2':
                        adapter.md_register(0xa130ff, 7)

    with cleanup_after(release):
        adapter.mode(1)
        time.sleep(0.2)
        if read_header(adapter) != profile.header:
            raise ProtocolError('MD cartridge changed; check again')
        if profile.attached_header and adapter.md_read(0x200100, 64) != profile.attached_header:
            raise ProtocolError('MD attached cartridge changed; check again')
        results = []
        for attempt in range(2):
            stage = 'Read %d/2' % (attempt+1)
            report = (lambda done, total: progress(stage, attempt*total+done, total*2)) if progress else None
            if report:
                report(0, profile.prg_size)
            mapping_started = True
            payload = read_rom(adapter, profile, report)
            validate_payload(profile, payload, require_hash=False)
            results.append(payload)
        if results[0] != results[1]:
            raise ProtocolError('Two MD ROM reads differ; no ROM saved')
        matched = database.match(profile, results[0])
        validate_payload(matched, results[0])
        return matched, results[0]
