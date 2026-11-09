"""Versioned NES metadata and read-only partial-hash identification.

No mapper is executed by loading or searching a database. Runtime: Python 3.9+.
"""
from cart_station_paths import data_path, user_nes_database
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from cart_station_protocol import ProtocolError, verify_sha1, cleanup_after
from cart_station_storage import rom_filename
from cart_station_nes_core import dump
from cart_station_nes_mappers import limitation, dump_mapped, dump_mapped_group


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def digest(value, optional=False):
    if optional and value in (None, ''):
        return None
    if not isinstance(value, str) or re.fullmatch('[0-9a-fA-F]{40}', value) is None:
        raise ValueError('Expected a 40-digit SHA-1')
    return value.lower()


def crc32_digest(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str) or re.fullmatch('[0-9a-fA-F]{8}', value) is None:
        raise ValueError('Expected an 8-digit PRG CRC32')
    return value.upper()


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Integer outside %d..%d' % (low, high))
    return value


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    mapper: int
    submapper: int
    prg_size: int
    chr_size: int
    prg_sha1: str
    chr_sha1: object
    raw_sha1: str
    file_sha1: object
    header: bytes
    probes: tuple
    prg_crc32: object = None

    @classmethod
    def parse(cls, value):
        identifier, title = value['id'], value['title']
        if (not isinstance(identifier, str) or not identifier or not isinstance(title, str)
                or not title.strip() or len(title) > 240
                or any(ord(c) < 32 or ord(c) == 127 for c in identifier + title)):
            raise ValueError('Invalid profile identifier or title')
        mapper = integer(value['mapper'], 0, 4095)
        submapper = integer(value['submapper'], 0, 15)
        prg = integer(value['prg_size'], 16384, 64 * 1024 * 1024)
        chr_size = integer(value['chr_size'], 0, 64 * 1024 * 1024)
        header = bytes.fromhex(value['header'])
        if len(header) != 16 or header[:4] != b'NES\x1a' or header[6] & 4:
            raise ValueError('A 16-byte NES header without trainer is required')
        nes20 = header[7] & 12 == 8
        if not nes20 and (header[7] & 12 or any(header[8:])):
            raise ValueError('Unsupported iNES header extensions')
        upper = header[9] if nes20 else 0
        if upper & 15 == 15 or upper >> 4 == 15:
            raise ValueError('Exponent ROM sizes are not implemented')
        actual_mapper = (header[6] >> 4) | (header[7] & 240) | ((header[8] & 15) << 8 if nes20 else 0)
        if (actual_mapper != mapper or (header[8] >> 4 if nes20 else 0) != submapper
                or (header[4] | ((upper & 15) << 8)) * 16384 != prg
                or (header[5] | ((upper >> 4) << 8)) * 8192 != chr_size):
            raise ValueError('Header disagrees with mapper or ROM sizes')
        probes = value.get('probes', {})
        if not isinstance(probes, dict) or set(probes) - {
                'first1k_sha1', 'last1k_sha1', 'halflast1k_sha1', 'lastprev16k_1k_sha1'}:
            raise ValueError('Invalid partial-hash fields')
        return cls(identifier, title, mapper, submapper, prg, chr_size,
                   digest(value['prg_sha1']), digest(value.get('chr_sha1'), optional=chr_size == 0),
                   digest(value['raw_sha1']), digest(value.get('file_sha1'), optional=True),
                   header, tuple(sorted((key, digest(item)) for key, item in probes.items())),
                   crc32_digest(value.get("prg_crc32")))

    def support(self):
        if self.header[7] & 3 or self.header[14]:
            return 'Console variant is not implemented'
        if (self.mapper == 0 and self.submapper == 0
                and self.prg_size in (16384, 32768) and self.chr_size in (0, 8192)):
            return None
        if self.mapper in (1, 2, 3, 4, 5, 7, 9, 10, 11, 13, 16, 18, 19, 21, 22, 23, 24, 25, 26,
                           30, 32, 33, 34, 37, 47, 48, 64, 65, 66, 67, 68, 69, 70, 71, 72,
                           73, 75, 76, 77, 78, 80, 82, 85, 86, 87, 88, 89, 92, 93, 94, 95, 96, 97,
                           118, 119, 140, 148, 152, 153, 154, 155, 157, 158, 159, 180, 184, 185, 186, 188, 206, 207, 210, 406, 552):
            return limitation(self)
        return 'Mapper/layout has not been ported'

    def filename(self):
        title = self.title[:-4] if self.title.lower().endswith('.nes') else self.title
        return rom_filename(title, '.nes', 'NES_' + self.raw_sha1)



class Database:
    def __init__(self, profiles):
        self.profiles = tuple(profiles)
        self.by_id = {}
        self.index = {'first': {}, 'last': {}}
        for profile in self.profiles:
            if profile.id in self.by_id:
                raise ProtocolError('Duplicate NES profile ID: ' + profile.id)
            self.by_id[profile.id] = profile
            for field, value in profile.probes:
                side = 'first' if field in ('first1k_sha1', 'lastprev16k_1k_sha1') else 'last'
                self.index[side].setdefault(value, set()).add(profile.id)

    @classmethod
    def load(cls, path=None):
        if path is None:
            personal = user_nes_database()
            path = personal or data_path('nes_profiles.reference.json')
        else:
            path = Path(path)
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
                raise ValueError('Unsupported database version')
            if not isinstance(data.get('profiles'), list) or len(data['profiles']) > 100000:
                raise ValueError('Invalid profile list')
            records = list(data['profiles'])
            # Unclassified supplemental catalogs are never auto-merged. Old
            # installations may still contain one after a ZIP update.
            database = cls(Profile.parse(item) for item in records)
            # Preserve the loaded metadata when exporting a local partial-hash index.
            database.document = dict(data, profiles=records)
            return database
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ProtocolError('Invalid NES database %s: %s' % (path, error)) from error

    def candidates(self, first, last):
        if len(first) != 1024 or len(last) != 1024:
            raise ProtocolError('Identification requires two 1 KiB samples')
        # Union of the four partial-hash indexes. A candidate is not a confirmed title.
        ids = self.index['first'].get(sha1(first), set()) | self.index['last'].get(sha1(last), set())
        return tuple(profile for profile in self.profiles if profile.id in ids)

    def summary(self):
        """Profile counts, not game counts or claims of hardware verification."""
        return {'profiles': len(self.profiles),
                'with_partial_hashes': sum(bool(p.probes) for p in self.profiles),
                'full_hash_only': sum(not p.probes for p in self.profiles),
                'reader_supported': sum(p.support() is None for p in self.profiles),
                'hardware_verified': 'not inferred from database membership'}

    def search(self, query='', mapper=None):
        query = query.casefold().strip()
        return tuple(p for p in self.profiles
                     if (mapper is None or p.mapper == mapper)
                     and (not query or query in p.title.casefold() or query in p.id.casefold()))

    def select_catalog_profile(self, identifier):
        """An explicit read plan, never a claim that the inserted title matches."""
        profile = self.by_id.get(identifier)
        if profile is None:
            raise ProtocolError('Unknown NES catalog profile: ' + identifier)
        return select_candidate((profile,), identifier)


def identify(adapter, database):
    if adapter.status() != 3:
        raise ProtocolError('Insert one FC/NES cartridge')
    with cleanup_after(adapter.mode, 0):
        adapter.mode(1)
        time.sleep(0.2)
        # One dummy read, then the first and last 1 KiB probes of the fixed PRG window.
        adapter.memory(0x11, 0x8000, 1024)
        first = adapter.memory(0x11, 0x8000, 1024)
        last = adapter.memory(0x11, 0xfc00, 1024)
        return database.candidates(first, last)


def select_candidate(candidates, identifier=None):
    if identifier is not None:
        candidates = tuple(profile for profile in candidates if profile.id == identifier)
    if len(candidates) != 1:
        raise ProtocolError('No unique DB candidate; identify first and select a candidate with --profile')
    profile = candidates[0]
    reason = profile.support()
    if reason:
        raise ProtocolError('%s: %s' % (profile.title, reason))
    return profile


def dump_profile(adapter, profile, progress=None):
    reason = profile.support()
    if reason:
        raise ProtocolError(reason)
    if profile.mapper == 0:
        options = {'progress': progress} if progress else {}
        data = dump(adapter, profile.prg_size // 1024, profile.chr_size // 1024,
                    'vertical' if profile.header[6] & 1 else 'horizontal', bool(profile.header[6] & 2), **options)
        # All DB output uses the validated DB header, not a guessed default.
        data = profile.header + data[16:]
    else:
        data = dump_mapped(adapter, profile, progress=progress) if progress else dump_mapped(adapter, profile)
    return verify_profile_data(profile, data)


def verify_profile_data(profile, data):
    raw = data[16:]
    if len(raw) != profile.prg_size + profile.chr_size:
        raise ProtocolError('DB ROM length mismatch')
    verify_sha1(raw[:profile.prg_size], profile.prg_sha1, 'DB PRG')
    if profile.chr_size:
        verify_sha1(raw[profile.prg_size:], profile.chr_sha1, 'DB CHR')
    verify_sha1(raw, profile.raw_sha1, 'DB PRG+CHR')
    if profile.file_sha1:
        verify_sha1(data, profile.file_sha1, 'DB completed file')
    return data


def geometry(profile):
    return profile.mapper, profile.submapper, profile.prg_size, profile.chr_size


def auto_read_group(candidates):
    """Candidates that may be read together without asking the user.

    Only when there are several, all are readable and share one ROM geometry.
    The full hashes then decide which one it is. Any
    other list (unported member, mixed geometry) stays a manual choice.
    """
    candidates = tuple(candidates)
    if (len(candidates) < 2 or any(item.support() is not None for item in candidates)
            or len({geometry(item) for item in candidates}) != 1):
        return ()
    return candidates


def dump_candidates(adapter, candidates, progress=None):
    """Read a same-geometry group; return (profile, data) only if one profile's
    PRG, CHR, PRG+CHR and file hashes all match. Never saves anything."""
    group = auto_read_group(candidates)
    if not group:
        raise ProtocolError('These candidates cannot be read without selecting one')
    if group[0].mapper == 0:
        options = {'progress': progress} if progress else {}
        first = group[0]
        data = dump(adapter, first.prg_size // 1024, first.chr_size // 1024,
                    'vertical' if first.header[6] & 1 else 'horizontal', bool(first.header[6] & 2), **options)
        raw = data[16:]
        matches = [item for item in group if len(raw) == item.prg_size + item.chr_size
                   and sha1(raw) == item.raw_sha1]
        if len(matches) != 1:
            raise ProtocolError('The cartridge matches %d of %d DB candidates; no ROM will be saved'
                                % (len(matches), len(group)))
        profile = matches[0]
        return profile, verify_profile_data(profile, profile.header + raw)
    profile, data = dump_mapped_group(adapter, group, progress=progress) if progress \
        else dump_mapped_group(adapter, group)
    return profile, verify_profile_data(profile, data)


def verified_rom_file(path, profile):
    """Check actual header, PRG, CHR, raw and optional file hashes in one pass."""
    try:
        if not path.is_file() or path.stat().st_size != 16 + profile.prg_size + profile.chr_size:
            return False
        raw_hash, file_hash = hashlib.sha1(), hashlib.sha1()
        with path.open('rb') as stream:
            header = stream.read(16)
            if header != profile.header:
                return False
            file_hash.update(header)
            for size, expected in ((profile.prg_size, profile.prg_sha1), (profile.chr_size, profile.chr_sha1)):
                section = hashlib.sha1()
                remaining = size
                while remaining:
                    data = stream.read(min(remaining, 1024 * 1024))
                    if not data:
                        return False
                    remaining -= len(data)
                    section.update(data)
                    raw_hash.update(data)
                    file_hash.update(data)
                if size and section.hexdigest() != expected:
                    return False
            return (not stream.read(1) and raw_hash.hexdigest() == profile.raw_sha1
                    and (not profile.file_sha1 or file_hash.hexdigest() == profile.file_sha1))
    except OSError:
        return False


def candidate_info(profile):
    reason = profile.support()
    return {'id': profile.id, 'title': profile.title, 'mapper': profile.mapper,
            'submapper': profile.submapper, 'prg_size': profile.prg_size,
            'chr_size': profile.chr_size, 'readable': reason is None,
            'limitation': reason}
