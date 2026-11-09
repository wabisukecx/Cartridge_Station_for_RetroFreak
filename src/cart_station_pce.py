"""HuCard readers: mapping, full DB size/order verification, no saves."""
import hashlib
import time
from dataclasses import dataclass, replace
from cart_station_protocol import CART_PCE, ProtocolError, cleanup_after
from cart_station_rom_db import RomDatabase
from cart_station_storage import rom_filename

BLOCK = 32768
SAMPLE = 8192
LIMITS = {'Linear': 1048576, 'Interval': 524288, 'Sf2Dash': 8912896}
BIT_REVERSE = bytes(int(format(n, '08b')[::-1], 2) for n in range(256))


@dataclass(frozen=True)
class PceProfile:
    header: bytes  # Raw first 8 KiB fingerprint, not a structured ROM header.
    controller: str
    title: str = 'PC Engine HuCard'
    prg_size: int = 0
    raw_sha1: str = ''  # Hash of the normalized saved ROM, as in the DB.
    file_sha256: str = ''
    system: str = 'PCE'
    data_order: str = 'unverified'
    chr_size: int = 0

    @property
    def id(self):
        return 'pce-sample-' + hashlib.sha1(
            self.header + self.controller.encode('ascii')
        ).hexdigest()

    def support(self):
        if (self.controller not in LIMITS or len(self.header) != SAMPLE
                or (self.prg_size and (
                    not SAMPLE <= self.prg_size <= LIMITS[self.controller]
                    or self.prg_size % 2))):
            return 'PCE controller/ROM size is not implemented'
        return None

    def filename(self):
        if not self.raw_sha1:
            raise ProtocolError('PCE needs full DB verification before saving')
        suffix = '.sgx' if self.system == 'SGX' else '.pce'
        title = self.title[:-len(suffix)] if self.title.lower().endswith(suffix) else self.title
        return rom_filename(title, suffix, self.system + '_' + self.raw_sha1)


class PceDatabase(RomDatabase):
    system = 'PCE'
    min_size = SAMPLE
    max_size = 8912896
    filename = 'pce_profiles.reference.json'

    def __init__(self, records):
        records = list(records)
        if any(r.get('platform', 'PCE') not in ('PCE', 'SGX') for r in records):
            raise ProtocolError('Invalid PCE platform metadata')
        super().__init__(records)

    def match(self, profile, payload):
        matched = super().match(profile, payload)
        record = self.index[(len(payload), matched.raw_sha1)]
        return replace(matched, system=record.get('platform', 'PCE'))


def stable_read(adapter, address, size):
    first, second = adapter.pce_read(address, size), adapter.pce_read(address, size)
    if (not isinstance(first, bytes) or not isinstance(second, bytes)
            or len(first) != size or first != second):
        raise ProtocolError('Two PCE probe reads differ or are incomplete')
    return first


def inspect_header(adapter, controller=None):
    if adapter.status() != CART_PCE:
        raise ProtocolError('Insert one PC Engine HuCard')
    with cleanup_after(adapter.mode, 0):
        adapter.mode(1)  # 5 V VDC (default).
        time.sleep(0.2)
        first = stable_read(adapter, 0, SAMPLE)
        if controller is not None:
            if controller not in LIMITS:
                raise ProtocolError('Unknown PCE controller')
            return PceProfile(first, controller)
        middle = stable_read(adapter, 0x40000, SAMPLE)
        upper = stable_read(adapter, 0x80000, SAMPLE)
        if first == middle:
            kind = 'Linear' if first == upper else 'Interval'
        else:
            with cleanup_after(adapter.pce_register, 0x1ff0, 255):
                adapter.pce_register(0x1ff1, 255)
                alternate = stable_read(adapter, 0x80000, SAMPLE)
            kind = 'Sf2Dash' if upper != alternate else 'Linear'
        return PceProfile(first, kind)


def read_block(adapter, controller, offset, size):
    address = offset
    if controller == 'Interval' and offset >= 0x40000:
        address += 0x40000
    elif controller == 'Sf2Dash':
        if offset < 0x100000:
            bank = 0
        else:
            bank = (offset - 0x80000) // 0x80000
            address = 0x80000 + offset % 0x80000
        adapter.pce_register(0x1ff0 + bank, 255)
    data = adapter.pce_read(address, size)
    if not isinstance(data, bytes) or len(data) != size:
        raise ProtocolError('Incomplete PCE ROM read')
    return data


def read_rom(adapter, profile, progress=None):
    parts = []
    done = 0
    while done < profile.prg_size:
        if progress:
            progress(done, profile.prg_size)
        size = min(BLOCK - done % BLOCK, profile.prg_size - done)
        parts.append(read_block(adapter, profile.controller, done, size))
        done += size
    if progress:
        progress(done, profile.prg_size)
    return b''.join(parts)


def validate_payload(profile, payload, require_hash=True):
    expected = profile.header.translate(BIT_REVERSE) if profile.data_order == 'bit_reversed' else profile.header
    if (not SAMPLE <= len(payload) <= LIMITS.get(profile.controller, 0)
            or payload[:SAMPLE] != expected
            or (profile.prg_size and len(payload) != profile.prg_size)):
        raise ProtocolError('PCE ROM size/fingerprint mismatch')
    if require_hash:
        if not profile.raw_sha1 or hashlib.sha1(payload).hexdigest() != profile.raw_sha1:
            raise ProtocolError('PCE full ROM hash mismatch')
        if profile.file_sha256 and hashlib.sha256(payload).hexdigest() != profile.file_sha256:
            raise ProtocolError('PCE full ROM SHA-256 mismatch')


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


def _read_checked(adapter, profile, database, progress=None, identify_only=False):
    if profile.support():
        raise ProtocolError(profile.support())
    if adapter.status() != CART_PCE:
        raise ProtocolError('Insert one PC Engine HuCard')
    sizes = [size for size in database.sizes if size <= LIMITS[profile.controller]]
    if not sizes:
        raise ProtocolError('No PCE DB sizes for this controller')
    mapping_started = False

    def release():
        with cleanup_after(adapter.mode, 0):
            if mapping_started and profile.controller == 'Sf2Dash':
                adapter.pce_register(0x1ff0, 255)

    with cleanup_after(release):
        adapter.mode(1)
        time.sleep(0.2)
        if stable_read(adapter, 0, SAMPLE) != profile.header:
            raise ProtocolError('PCE cartridge changed; check again')
        first = bytearray()
        native_hash, reversed_hash = hashlib.sha1(), hashlib.sha1()
        done = 0
        matched = None
        order = None
        for boundary in sorted(set(sizes) | {LIMITS[profile.controller]}):
            while done < boundary:
                if progress:
                    progress(
                        'Identifying HuCard (no save)' if identify_only else 'Read 1/2: determining size by DB',
                        done, LIMITS[profile.controller] * (1 if identify_only else 2)
                    )
                size = min(BLOCK - done % BLOCK, boundary - done)
                mapping_started = True
                block = read_block(adapter, profile.controller, done, size)
                first.extend(block)
                native_hash.update(block)
                reversed_hash.update(block.translate(BIT_REVERSE))
                done += size
            hashes = {'native': native_hash.hexdigest(), 'bit_reversed': reversed_hash.hexdigest()}
            hits = {sha for sha in hashes.values() if (done, sha) in database.index}
            if len(hits) > 1:
                raise ProtocolError('Ambiguous PCE data order; no ROM saved')
            if hits:
                order = next(name for name, sha in hashes.items() if sha in hits)
                normalized = bytes(first) if order == 'native' else first.translate(BIT_REVERSE)
                matched = replace(database.match(profile, normalized), data_order=order)
                break
        if matched is None:
            raise ProtocolError(
                'PCE full ROM hash is not in the database; no ROM saved. Native SHA-1: '
                + native_hash.hexdigest()
            )
        validate_payload(matched, normalized)
        if identify_only:
            if progress:
                progress('Identified HuCard (not saved)', done, done)
            return matched, bytes(normalized)
        if progress:
            progress('Read 2/2', matched.prg_size, matched.prg_size * 2)
        report = (lambda done, total: progress('Read 2/2', total + done, total * 2)) if progress else None
        second = read_rom(adapter, matched, report)
        if first != second:
            raise ProtocolError('Two PCE ROM reads differ; no ROM saved')
        second = second if order == 'native' else second.translate(BIT_REVERSE)
        validate_payload(matched, second)
        return matched, bytes(normalized)


def identify_pce(adapter, profile, database, progress=None):
    """One full DB-matched read for display only. Never cache data for capture."""
    matched, _ = _read_checked(adapter, profile, database, progress, identify_only=True)
    return matched


def dump_pce(adapter, profile, database, progress=None):
    return _read_checked(adapter, profile, database, progress)
