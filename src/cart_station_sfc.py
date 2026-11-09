"""SFC ROM mapping (LoROM/HiROM/ExHiROM and special chips); only reviewed ROM-register writes."""
import hashlib
import re
import time
from dataclasses import dataclass, replace
from cart_station_protocol import CART_SFC, ProtocolError, cleanup_after
from cart_station_rom_db import RomDatabase
from cart_station_storage import rom_filename
from cart_station_md import text

BLOCK = 32768
LIMITS = {'LoRom': 4194304, 'HiRom': 4194304, 'BigLoRom': 4194304,
          'LoRomAndHiRom': 4194304, 'ExHiRom': 8388608}
SPECIAL_LIMITS = {'Sa1': 8388608, 'Sdd1': 8388608}
READ_LIMITS = dict(LIMITS, **SPECIAL_LIMITS)
MAKEUPS = {0x20: 'LoRom', 0x30: 'LoRom', 0x21: 'HiRom', 0x31: 'HiRom',
           0x23: 'Sa1', 0x32: 'Sdd1', 0x35: 'ExHiRom', 0x3a: 'Spc7110'}
HEADER_ATTEMPTS = 5  # Total attempts, including the first one.


# The only CX4 title whose initialization sequence this reader implements.
CX4_TITLE = 'ROCKMAN X2'


class SfcInvalidHeader(ProtocolError):
    """A complete, stable header has invalid content, rather than a wire error."""


@dataclass(frozen=True)
class SfcProfile:
    header: bytes
    title: str
    controller: str
    prg_size: int
    raw_sha1: str = ''
    file_sha256: str = ''
    system: str = 'SFC'
    chr_size: int = 0

    @property
    def id(self):
        return 'sfc-header-' + hashlib.sha1(self.header + self.controller.encode('ascii')).hexdigest()

    @property
    def chip(self):
        chip_type = self.header[38]
        if chip_type in (0x13, 0x14, 0x15, 0x1a):
            return 'SuperFX'
        return {0x34: 'SA-1', 0x35: 'SA-1', 0x43: 'S-DD1', 0x45: 'S-DD1',
                0xf3: 'CX4', 0xf5: 'SPC7110', 0xf9: 'SPC7110 + RTC'}.get(
                    chip_type, 'Type %02X' % chip_type)

    @property
    def cartridge_title(self):
        """The hardware header title stays fixed when the DB supplies a filename."""
        return text(self.header[16:37], 'shift_jis')

    def support(self):
        if self.controller not in READ_LIMITS:
            return ('SFC %s needs a dedicated reader (not yet ported); '
                    'adapter initialization and ROM mapping are unverified') % self.controller
        if not BLOCK <= self.prg_size <= READ_LIMITS[self.controller] or self.prg_size % BLOCK:
            return 'SFC ROM size exceeds this reader'
        if self.controller in SPECIAL_LIMITS:
            expected = {'Sa1': (0x34, 0x35), 'Sdd1': (0x43, 0x45)}
            if self.header[38] not in expected[self.controller]:
                return 'SFC map mode and chip type disagree'
            return None
        if self.header[38] == 0xf3:
            if (self.cartridge_title == CX4_TITLE and self.controller == 'LoRom'
                    and self.prg_size <= 2097152):
                return None
            return 'SFC CX4 currently supports only %s LoROM up to 2 MiB' % CX4_TITLE
        if self.header[38] not in (0, 1, 2, 3, 4, 5, 0x13, 0x14, 0x15, 0x1a):
            return 'SFC special chip %s (type %02X) is not yet supported' % (self.chip, self.header[38])
        return None

    def filename(self):
        if not self.raw_sha1:
            raise ProtocolError('SFC needs full DB verification before saving')
        title = re.sub(r'\.(sfc|smc)$', '', self.title, flags=re.I)
        return rom_filename(title, '.sfc', 'SFC_' + self.raw_sha1)


class SfcDatabase(RomDatabase):
    system = 'SFC'
    min_size = BLOCK
    max_size = 16 * 1024 * 1024
    filename = 'sfc_profiles.reference.json'


def parse_header(header):
    if not isinstance(header, bytes) or len(header) != 48:
        raise ProtocolError('SFC header must be exactly 48 bytes')
    complement = int.from_bytes(header[44:46], 'little')
    checksum = int.from_bytes(header[46:48], 'little')
    if complement ^ checksum != 0xffff or header[37] not in MAKEUPS:
        raise SfcInvalidHeader('SFC header checksum pair or map mode is invalid; check cartridge contacts')
    # Bound the exponent before shifting. Full hashes, not this pair, verify ROM data.
    if not 5 <= header[39] <= 14:
        raise SfcInvalidHeader('SFC ROM size code is outside supported metadata range')
    title = text(header[16:37], 'shift_jis') or 'SFC cartridge'
    controller = MAKEUPS[header[37]]
    if controller == 'LoRom':
        if title == 'MENU PROGRAM':
            controller = 'SfMemory'
        elif title == 'ADD-ON BASE CASSETE':
            controller = 'SufamiTurbo'
    if controller == 'Sdd1' and title == 'SP MOMOTAROU DENTETSU':
        controller = 'LoRom'
    return SfcProfile(header, title, controller, 1024 << header[39])


def stable_read(adapter, address, size):
    first, second = adapter.snes_read(address, size), adapter.snes_read(address, size)
    if not isinstance(first, bytes) or len(first) != size or first != second:
        raise ProtocolError('Two SFC probe reads differ or are incomplete')
    return first


def choose_controller(adapter, profile):
    if profile.controller != 'LoRom' or profile.prg_size <= 2097152:
        return profile
    h = profile.header
    if h[42] == 51 and h[2] == 90 and h[5] != 32:
        return replace(profile, controller='BigLoRom')
    # Header.CheckExtraMemoryController, without accepting unstable probe data.
    first = stable_read(adapter, 0x400000, BLOCK)
    second = stable_read(adapter, 0x410000, BLOCK)
    if first == second:
        return profile
    if adapter.snes_read(0x408000, BLOCK) == first or adapter.snes_read(0x418000, BLOCK) == second:
        return profile
    return replace(profile, controller='LoRomAndHiRom')


def initialize(adapter):
    adapter.snes_mode(1)
    # Warm-up read, then header at $00:ffb0 for every map type.
    data = adapter.snes_read(0xc000, 16384)
    if not isinstance(data, bytes) or len(data) != 16384:
        raise ProtocolError('Incomplete SFC initialization read')


def _initialize_header(adapter, progress=None):
    """Return with SFC mode active; release every failed attempt before retrying."""
    for attempt in range(1, HEADER_ATTEMPTS + 1):
        stage = 'Checking SFC header (%d/%d)' % (attempt, HEADER_ATTEMPTS)
        if progress:
            progress(stage, attempt - 1, HEADER_ATTEMPTS)
        if adapter.status() != CART_SFC:
            raise ProtocolError('Insert one Super Famicom cartridge')
        try:
            initialize(adapter)
            if progress:
                progress(stage, attempt - 1, HEADER_ATTEMPTS)
            header = stable_read(adapter, 0xffb0, 48)
            if progress:
                progress(stage, attempt - 1, HEADER_ATTEMPTS)
            profile = parse_header(header)
        except SfcInvalidHeader as error:
            adapter.snes_mode(0)
            if attempt == HEADER_ATTEMPTS:
                raise SfcInvalidHeader('SFC header invalid after %d attempts: %s'
                                       % (HEADER_ATTEMPTS, error)) from error
            # Wait 100 ms after releasing a failed attempt.
            # Wire errors, cancellation and release failures never reach this.
            time.sleep(0.1)
        except BaseException:
            # Release the bus, but keep this error primary if the release fails too.
            with cleanup_after(adapter.snes_mode, 0):
                raise
        else:
            return profile


def inspect_header(adapter, progress=None):
    profile = _initialize_header(adapter, progress)
    with cleanup_after(adapter.snes_mode, 0):
        return choose_controller(adapter, profile)


def rom_address(controller, bank):
    if controller not in READ_LIMITS or type(bank) is not int or not 0 <= bank < READ_LIMITS[controller] // BLOCK:
        raise ValueError('Invalid SFC ROM bank')
    if controller == 'Sa1':
        bank %= 128
        return 0x8000 + bank * 0x10000 if bank < 64 else 0x808000 + (bank - 64) * 0x10000
    if controller == 'Sdd1':
        return 0xc00000 + bank % 128 * BLOCK
    if controller == 'LoRom':
        return 0x8000 + bank * 0x10000 if bank < 126 else 0xfe8000 + (bank - 126) * 0x10000
    if controller == 'HiRom':
        return 0x400000 + bank * BLOCK if bank < 124 else 0xfe0000 + (bank - 124) * BLOCK
    if controller == 'BigLoRom':
        return 0x8000 + bank * 0x10000 if bank < 64 else 0x808000 + (bank - 64) * 0x10000
    if controller == 'LoRomAndHiRom':
        return 0x8000 + bank * 0x10000 if bank < 64 else 0x400000 + (bank - 64) * 0x10000
    return 0xc00000 + bank * BLOCK if bank < 128 else 0x400000 + (bank - 128) * BLOCK


def prepare_rom(adapter, profile):
    """Set only ROM mappings, after the current header has been verified."""
    if profile.chip == 'CX4' and profile.cartridge_title == CX4_TITLE:
        adapter.snes_rom_register('CX4', 0x7f52, 0)
        data = adapter.snes_read(0xc000, 16384)
        if not isinstance(data, bytes) or len(data) != 16384:
            raise ProtocolError('Incomplete CX4 initialization read')
        if stable_read(adapter, 0xffb0, 48) != profile.header:
            raise ProtocolError('SFC cartridge changed after CX4 initialization')
    if profile.controller in SPECIAL_LIMITS:
        select_rom_bank(adapter, profile, 0)


def select_rom_bank(adapter, profile, bank):
    base = {'Sa1': 0x2220, 'Sdd1': 0x4804}.get(profile.controller)
    if base is not None:
        for index in range(4):
            adapter.snes_rom_register(profile.controller, base + index,
                                      index + (4 if bank >= 128 else 0))


def read_rom(adapter, profile, size, progress=None):
    reason = profile.support()
    if reason:
        raise ProtocolError(reason)
    if profile.controller not in READ_LIMITS or not BLOCK <= size <= min(profile.prg_size, READ_LIMITS[profile.controller]):
        raise ProtocolError('Invalid SFC read range')
    prepare_rom(adapter, profile)
    result = bytearray()
    for offset in range(0, size, BLOCK):
        if progress:
            progress(offset, size)
        count = min(BLOCK, size - offset)
        select_rom_bank(adapter, profile, offset // BLOCK)
        data = adapter.snes_read(rom_address(profile.controller, offset // BLOCK), count)
        if not isinstance(data, bytes) or len(data) != count:
            raise ProtocolError('Incomplete SFC ROM read')
        result.extend(data)
    if progress:
        progress(size, size)
    return bytes(result)


def validate_payload(profile, payload, require_hash=True):
    if len(payload) != profile.prg_size:
        raise ProtocolError('SFC ROM length mismatch')
    offsets = (0xffb0, 0x40ffb0) if profile.controller == 'ExHiRom' else (0xffb0,) if profile.controller == 'HiRom' else (0x7fb0,)
    if not any(payload[offset:offset + 48] == profile.header for offset in offsets):
        raise ProtocolError('SFC ROM header changed during reading')
    if require_hash:
        if not profile.raw_sha1 or hashlib.sha1(payload).hexdigest() != profile.raw_sha1:
            raise ProtocolError('SFC full ROM hash mismatch')
        if profile.file_sha256 and hashlib.sha256(payload).hexdigest() != profile.file_sha256:
            raise ProtocolError('SFC full ROM SHA-256 mismatch')


def dump_sfc(adapter, profile, database, progress=None):
    if profile.support():
        raise ProtocolError(profile.support())
    current = _initialize_header(adapter, progress)
    with cleanup_after(adapter.snes_mode, 0):
        current = choose_controller(adapter, current)
        if current.id != profile.id:
            raise ProtocolError('SFC cartridge changed; check again')
        prepare_rom(adapter, current)
        # Header sizes are powers of two; some real ROMs are smaller. Accept
        # only a full DB hash at a recorded boundary, never FF/mirror guesses.
        sizes = {n for n in database.sizes if BLOCK <= n <= profile.prg_size and n % BLOCK == 0}
        if not sizes:
            raise ProtocolError('No SFC DB sizes fit this header')
        payload = bytearray()
        digest = hashlib.sha1()
        matched = None
        for offset in range(0, max(sizes), BLOCK):
            if progress:
                progress('Read 1/2', offset, profile.prg_size * 2)
            select_rom_bank(adapter, profile, offset // BLOCK)
            data = adapter.snes_read(rom_address(profile.controller, offset // BLOCK), BLOCK)
            if not isinstance(data, bytes) or len(data) != BLOCK:
                raise ProtocolError('Incomplete SFC ROM read')
            payload.extend(data)
            digest.update(data)
            if len(payload) in sizes and (len(payload), digest.hexdigest()) in database.index:
                matched = database.match(profile, bytes(payload))
                validate_payload(matched, bytes(payload))
                # A previously verified selection cannot silently change identity.
                if profile.raw_sha1 and matched.raw_sha1 != profile.raw_sha1:
                    raise ProtocolError('SFC cartridge identity changed')
                break
        if matched is None:
            raise ProtocolError('SFC full ROM hash is not in the database; no ROM saved. SHA-1: ' + digest.hexdigest())
        size = len(payload)
        if progress:
            progress('Preparing SFC read 2/2', size, size * 2)
        # A second pass starts from a fresh mode and rechecks the same mapping.
        # Do not retry invalid headers here: identity must remain stable.
        adapter.snes_mode(0)
        if adapter.status() != CART_SFC:
            raise ProtocolError('SFC cartridge disappeared before second read')
        initialize(adapter)
        current = choose_controller(adapter, parse_header(stable_read(adapter, 0xffb0, 48)))
        if current.id != profile.id:
            raise ProtocolError('SFC cartridge changed before second read')
        second = read_rom(adapter, matched, size,
            (lambda done, total: progress('Read 2/2', size + done, size * 2)) if progress else None)
        if bytes(payload) != second:
            raise ProtocolError('Two SFC ROM reads differ; no ROM saved')
        validate_payload(matched, second)
        return matched, second


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
