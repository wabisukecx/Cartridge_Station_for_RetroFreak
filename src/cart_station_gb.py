"""GB/GBC ROM-only reader. Header geometry first, full embedded DB hash last."""
import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from cart_station_paths import data_path

from cart_station_protocol import ProtocolError, CART_GB, cleanup_after
from cart_station_banks import read_banks
from cart_station_storage import rom_filename

LOGO = bytes.fromhex('ceed6666cc0d000b03730083000c000d0008111f8889000edccc6ee6dddd d999bbbb67636e0eecccdddc999fbbb9333e'.replace(' ', ''))


class GbUnknownRom(ProtocolError):
    pass


class GbClassificationMismatch(ProtocolError):
    """Matching full-ROM metadata exists only under the other GB/GBC class."""


@dataclass(frozen=True)
class GbProfile:
    header: bytes
    title: str
    controller: str
    prg_size: int
    system: str
    raw_sha1: str = ''
    file_sha256: str = ''
    chr_size: int = 0

    @property
    def id(self):
        return 'gb-header-' + hashlib.sha1(self.header).hexdigest()

    def support(self):
        if self.controller == 'TAMA5':
            if (self.header[0x47] != 0xfd
                    or self.prg_size not in tuple(32768 << n for n in range(5))):
                return 'TAMA5 needs reviewed ROM geometry (up to 512 KiB)'
            return None
        if self.controller == 'MBC6':
            # Two independent 8 KiB windows, each with a seven-bit bank.
            # A 16 KiB-bank size check would admit wraparound, so sizes are limited.
            if (self.header[0x47] != 0x20
                    or self.prg_size not in tuple(32768 << n for n in range(6))):
                return 'MBC6 needs reviewed ROM geometry (up to 1 MiB; no flash)'
            return None
        if self.controller in ('HuC3', 'MBC7'):
            expected = 0xfe if self.controller == 'HuC3' else 0x22
            if (self.header[0x47] != expected
                    or self.prg_size not in tuple(32768 << n for n in range(7))):
                return 'GB special controller needs reviewed ROM geometry (up to 2 MiB)'
            return None
        if self.controller in ('HuC1', 'PocketCamera'):
            expected = 0xff if self.controller == 'HuC1' else 0xfc
            # Reviewed six-bit ROM banking only. HuC1's $4000 register
            # selects RAM, so there is no high-bank path beyond 1 MiB.
            if (self.header[0x47] != expected
                    or self.prg_size not in (32768, 65536, 131072, 262144, 524288, 1048576)):
                return 'GB special controller needs reviewed ROM geometry (up to 1 MiB)'
            return None
        if self.controller == 'ROM' and self.prg_size != 32768:
            return 'Unbanked GB needs 32 KiB ROM'
        limits = {'ROM': 32768, 'MBC1': 2097152, 'MBC1Ex2M': 2097152, 'MBC1Ex4M': 2097152, 'MBC2': 262144,
                  'MBC3': 2097152, 'MBC5': 8388608}
        if self.controller not in limits or self.prg_size > limits[self.controller]:
            return 'GB controller/geometry is not implemented'
        return None

    def filename(self):
        if not self.raw_sha1:
            raise ProtocolError('GB title needs complete DB verification before saving')
        suffix = '.gbc' if self.system == 'GBC' else '.gb'
        title = self.title[:-len(suffix)] if self.title.lower().endswith(suffix) else self.title
        return rom_filename(title, suffix, self.system + '_' + self.raw_sha1)


def parse_header(header):
    if not isinstance(header, bytes) or len(header) != 80:
        raise ProtocolError('GB header must be exactly 80 bytes')
    if header[4:52] != LOGO:
        raise ProtocolError('GB logo mismatch; check cartridge contacts')
    checksum = 0
    for value in header[0x34:0x4d]:
        checksum = (checksum - value - 1) & 255
    if checksum != header[0x4d]:
        raise ProtocolError('GB header checksum mismatch')
    flag, cart, size = header[0x43], header[0x47], header[0x48]
    if size > 8:
        raise ProtocolError('GB ROM size code is not implemented: %02x' % size)
    system = 'GBC' if flag in (0x80, 0xc0) else 'GB'
    end = 0x3f if system == 'GBC' else 0x44
    title = header[0x34:end].split(b'\0', 1)[0].decode('ascii', errors='replace')
    title = ''.join(c if 32 <= ord(c) <= 126 else '?' for c in title).strip() or 'GB cartridge'
    types = {0: 'ROM', 8: 'ROM', 9: 'ROM', 1: 'MBC1', 2: 'MBC1', 3: 'MBC1',
             5: 'MBC2', 6: 'MBC2', **{v: 'MBC3' for v in range(0x0f, 0x14)},
             **{v: 'MBC5' for v in range(0x19, 0x1f)},
             0x20: 'MBC6', 0x22: 'MBC7', 0xfc: 'PocketCamera',
             0xfd: 'TAMA5', 0xfe: 'HuC3', 0xff: 'HuC1'}
    controller = types.get(cart, 'Type %02X' % cart)
    if title.startswith('NP M-MENU'):
        controller = 'GB Memory multicart'
    return GbProfile(header, title, controller, 32768 << size, system)


class GbDatabase:
    def __init__(self, records):
        self.index = {}
        for record in records:
            system, size, sha = record.get('system'), record.get('size'), record.get('sha1')
            if (system not in ('GB', 'GBC') or type(size) is not int
                    or not 32768 <= size <= 8388608 or not isinstance(sha, str)
                    or re.fullmatch('[0-9a-f]{40}', sha) is None
                    or not isinstance(record.get('title'), str) or not record['title']):
                raise ProtocolError('Invalid GB DB record')
            sha256 = record.get('sha256', '')
            if not isinstance(sha256, str) or (sha256 and re.fullmatch('[0-9a-f]{64}', sha256) is None):
                raise ProtocolError('Invalid GB DB SHA-256')
            key = system, size, sha
            if key in self.index and self.index[key] != record:
                raise ProtocolError('Conflicting GB database identity')
            self.index[key] = dict(record)

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path is not None else data_path('gb_profiles.reference.json')
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if type(data.get('version')) is not int or data['version'] != 1 or not isinstance(data.get('records'), list):
                raise ValueError('unsupported GB database format')
            return cls(data['records'])
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise ProtocolError('Cannot load GB database: %s: %s' % (path, error)) from error

    def match(self, profile, payload):
        sha = hashlib.sha1(payload).hexdigest()
        record = self.index.get((profile.system, len(payload), sha))
        if record is None:
            other_system = 'GB' if profile.system == 'GBC' else 'GBC'
            other = self.index.get((other_system, len(payload), sha))
            if other is not None:
                # Diagnostic only: never return a cross-system profile, change
                # its suffix/destination, or try more bank layouts for this.
                expected = other.get('sha256', '')
                if expected and hashlib.sha256(payload).hexdigest() != expected:
                    raise ProtocolError('GB DB SHA-256 mismatch in alternate classification; no ROM saved')
                raise GbClassificationMismatch(
                    'GB/GBC classification differs: header %s, DB %s; no ROM saved. SHA-1: %s'
                    % (profile.system, other_system, sha))
            raise GbUnknownRom('GB full ROM hash is not in the database; no ROM saved. SHA-1: ' + sha)
        sha256 = record.get('sha256', '')
        if sha256 and hashlib.sha256(payload).hexdigest() != sha256:
            raise ProtocolError('GB DB SHA-256 mismatch')
        return replace(profile, title=record['title'], raw_sha1=sha, file_sha256=sha256)


def inspect_header(adapter):
    if adapter.status() != CART_GB:
        raise ProtocolError('Insert one GB/GBC cartridge')
    with cleanup_after(adapter.mode, 0):
        adapter.mode(1)
        time.sleep(0.2)
        first, second = adapter.gb_read(0x100, 80), adapter.gb_read(0x100, 80)
        if first != second:
            raise ProtocolError('Two GB header reads differ')
        profile = parse_header(first)
        return select_mbc1_layout(adapter, profile)


def select_mbc1_layout(adapter, profile):
    """1 MiB content comparison to choose a layout; it is a hint, not identity."""
    if profile.controller != 'MBC1' or profile.prg_size != 1048576:
        return profile
    write, read = adapter.gb_register, adapter.gb_read
    try:
        write(0x2100, 0)
        write(0x6000, 0)
        write(0x4000, 0)
        write(0x2100, 1)
        write(0x6000, 1)
        write(0x4000, 0)
        first = read(0x4000, 16384)
        write(0x2100, 17)
        write(0x6000, 1)
        write(0x4000, 0)
        second = read(0x4000, 16384)
        if first == second:
            return replace(profile, controller='MBC1Ex2M')
        write(0x6000, 0)
        write(0x2100, 0)
        write(0x4000, 1)
        first = read(0x4000, 16384)
        write(0x6000, 0)
        write(0x2100, 1)
        write(0x4000, 1)
        second = read(0x4000, 16384)
        if first == second or first == bytes(16384):
            return replace(profile, controller='MBC1Ex4M')
        return profile
    finally:
        # Leave bank zero visible for capture's header recheck.
        write(0x6000, 0)
        write(0x2100, 0)
        write(0x4000, 0)


def mbc1_variants(profile):
    if (profile.header[0x47] in (1, 2, 3) and profile.prg_size >= 1048576
            and profile.controller in ('MBC1', 'MBC1Ex2M', 'MBC1Ex4M')):
        names = (profile.controller, 'MBC1', 'MBC1Ex2M', 'MBC1Ex4M')
        return tuple(replace(profile, controller=name) for name in dict.fromkeys(names))
    return (profile,)


def read_rom(adapter, profile, progress=None):
    def read_bank(bank, size):
        write = adapter.gb_register
        if profile.controller == 'TAMA5':
            adapter.gb_tama5_bank(bank)
            return adapter.gb_read(0 if bank == 0 else 0x4000, size)
        if profile.controller == 'MBC6':
            # Read through the two 8 KiB ROM windows. Dedicated controls
            # cannot select flash or write RAM/flash data.
            select = adapter.gb_mbc6_register
            select(0x2800, 0)
            select(0x3800, 0)
            select(0x2000, bank * 2)
            select(0x3000, bank * 2 + 1)
            return adapter.gb_read(0x4000, size)
        if profile.controller in ('MBC1Ex2M', 'MBC1Ex4M'):
            group_size = 16 if profile.controller == 'MBC1Ex2M' else 32
            group, inner = divmod(bank, group_size)
            write(0x2100, inner)
            write(0x6000, 1 if inner == 0 else 0)
            write(0x4000, group)
            return adapter.gb_read(0 if inner == 0 else 0x4000, size)
        if profile.controller == 'MBC1':
            if bank and bank % 32 == 0:
                # $4000 remaps low bank zero to one. Select the fixed window
                # in mode 1 for banks $20/$40/$60 (Ex4M layout).
                write(0x2100, 0)
                write(0x6000, 1)
                write(0x4000, bank >> 5)
                return adapter.gb_read(0, size)
            write(0x6000, 1 if bank > 32 else 0)
            write(0x2100, bank & 31)
            write(0x4000, (bank >> 5) & 3)
        elif profile.controller == 'HuC1':
            write(0x2100, bank & 63)
            # Within the reviewed range this is always
            # RAM bank zero, not an additional ROM address bit.
            write(0x4000, 0)
        elif profile.controller == 'PocketCamera':
            write(0x2100, bank & 127)
        elif profile.controller == 'HuC3':
            # HuC3 needs a seven-bit ROM number (HuC1's six bits lose bit 6). $4000 selects RAM,
            # not high ROM bits; retain RAM bank zero without accessing it.
            write(0x2100, bank & 127)
            write(0x4000, 0)
        elif profile.controller == 'MBC7':
            # Unlike MBC5, $3000 is another ROM-bank write, not bit 8.
            # Do not overwrite the selected bank or enable peripherals.
            write(0x2100, bank & 127)
        elif profile.controller == 'MBC2':
            write(0x2100, bank & 15)
        elif profile.controller == 'MBC3':
            write(0x2100, bank & 127)
        elif profile.controller == 'MBC5':
            write(0x2100, bank & 255)
            write(0x3000, (bank >> 8) & 1)
        address = bank * 16384 if profile.controller == 'ROM' else (0 if bank == 0 else 0x4000)
        return adapter.gb_read(address, size)
    return read_banks(read_bank, profile.prg_size, 16384, progress)


def validate_payload(profile, payload, require_hash=True):
    if len(payload) != profile.prg_size or payload[0x100:0x150] != profile.header:
        raise ProtocolError('GB ROM size/header mismatch')
    parse_header(payload[0x100:0x150])
    if require_hash:
        if not profile.raw_sha1 or hashlib.sha1(payload).hexdigest() != profile.raw_sha1:
            raise ProtocolError('GB full ROM hash mismatch')
        if profile.file_sha256 and hashlib.sha256(payload).hexdigest() != profile.file_sha256:
            raise ProtocolError('GB full ROM SHA-256 mismatch')
    return payload


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


def dump_gb(adapter, profile, database, progress=None):
    reason = profile.support()
    if reason:
        raise ProtocolError(reason)
    if adapter.status() != CART_GB:
        raise ProtocolError('Insert one GB/GBC cartridge')
    restore_mapper = False

    def release():
        with cleanup_after(adapter.mode, 0):
            if restore_mapper:
                adapter.gb_register(0x6000, 0)
                adapter.gb_register(0x2100, 0)
                adapter.gb_register(0x4000, 0)

    with cleanup_after(release):
        adapter.mode(1)
        time.sleep(0.2)
        if adapter.gb_read(0x100, 80) != profile.header:
            raise ProtocolError('GB cartridge changed; check again')
        restore_mapper = profile.header[0x47] in (1, 2, 3)
        last_error = None
        for variant in mbc1_variants(profile):
            results = []
            for attempt in range(2):
                total = variant.prg_size * 2
                stage = 'Read %d/2' % (attempt + 1)
                if progress:
                    progress(stage, attempt * variant.prg_size, total)
                report = (lambda done, size: progress(stage,
                    attempt * variant.prg_size + done, total)) if progress else None
                payload = read_rom(adapter, variant, report)
                validate_payload(variant, payload, require_hash=False)
                results.append(payload)
            if results[0] != results[1]:
                raise ProtocolError('Two GB ROM reads differ; no ROM saved')
            try:
                matched = database.match(variant, results[0])
            except GbUnknownRom as error:
                last_error = error
                continue
            validate_payload(matched, results[0])
            return matched, results[0]
        raise last_error


def diagnose_header(adapter, samples=3):
    """Read raw headers only: no mapper probes, bank writes, DB lookup or save."""
    if type(samples) is not int or not 1 <= samples <= 5:
        raise ValueError('GB diagnostic samples must be 1..5')
    cartridge = adapter.status()
    result = dict(cartridge=cartridge, requested_samples=samples, reads=[])
    if cartridge != CART_GB:
        result['error'] = 'Insert one GB/GBC cartridge (expected type 5)'
        return result
    headers = []
    try:
        adapter.mode(1)
        time.sleep(0.2)
        for number in range(1, samples + 1):
            try:
                header = adapter.gb_read(0x100, 80)
            except (OSError, ProtocolError) as error:
                result['reads'].append(dict(sample=number, error=str(error), error_type=type(error).__name__))
                break  # A partial response can leave the transport out of sync.
            headers.append(header)
            record = dict(sample=number, bytes=len(header), header_hex=header.hex(),
                logo_matching_bytes=sum(a == b for a, b in zip(header[4:52], LOGO)),
                logo_bytes=48, all_zero=header == bytes(80), all_ff=header == b'\xff' * 80)
            if len(header) == 80:
                checksum = (-sum(header[0x34:0x4d]) - 25) & 255
                record.update(header_checksum_ok=checksum == header[0x4d],
                    cgb_flag=header[0x43], cartridge_type=header[0x47], rom_size_code=header[0x48])
            try:
                profile = parse_header(header)
                record.update(valid=True, title=profile.title, system=profile.system,
                              controller=profile.controller, rom_size=profile.prg_size)
            except ProtocolError as error:
                record.update(valid=False, error=str(error))
            result['reads'].append(record)
    except (OSError, ProtocolError) as error:
        result['error'] = str(error)
    finally:
        try:
            adapter.mode(0)
        except (OSError, ProtocolError) as error:
            result['release_error'] = str(error)
    result['all_reads_equal'] = len(headers) >= 2 and all(raw == headers[0] for raw in headers)
    result['all_headers_valid'] = (len(headers) == samples
        and all(item.get('valid', False) for item in result['reads']))
    return result
