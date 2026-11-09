"""Full-ROM metadata shared by linear cartridge systems. Contains no ROM data."""
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from cart_station_paths import data_path
from cart_station_protocol import ProtocolError


class RomDatabase:
    system = ''
    max_size = 0
    min_size = 192
    filename = ''

    def __init__(self, records):
        self.index = {}
        for record in records:
            size, sha = record.get('size'), record.get('sha1')
            if (record.get('system') != self.system or type(size) is not int
                    or not self.min_size <= size <= self.max_size or size % 2
                    or not isinstance(sha, str) or re.fullmatch('[0-9a-f]{40}', sha) is None
                    or not isinstance(record.get('title'), str) or not record['title']):
                raise ProtocolError('Invalid %s database record' % self.system)
            sha256 = record.get('sha256', '')
            if not isinstance(sha256, str) or (sha256 and re.fullmatch('[0-9a-f]{64}', sha256) is None):
                raise ProtocolError('Invalid %s DB SHA-256' % self.system)
            key = size, sha
            if key in self.index and self.index[key] != record:
                raise ProtocolError('Conflicting %s database identity' % self.system)
            self.index[key] = dict(record)
        self.sizes = tuple(sorted({key[0] for key in self.index}))

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path is not None else data_path(cls.filename)
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if type(data.get('version')) is not int or data['version'] != 1 or not isinstance(data.get('records'), list):
                raise ValueError('unsupported database format')
            return cls(data['records'])
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise ProtocolError('Cannot load %s database: %s: %s' % (cls.system, path, error)) from error

    def match(self, profile, payload):
        sha = hashlib.sha1(payload).hexdigest()
        record = self.index.get((len(payload), sha))
        if record is None:
            raise ProtocolError('%s full ROM hash is not in the database; no ROM saved. SHA-1: %s' % (self.system, sha))
        sha256 = record.get('sha256', '')
        if sha256 and hashlib.sha256(payload).hexdigest() != sha256:
            raise ProtocolError(self.system + ' DB SHA-256 mismatch')
        return replace(profile, title=record['title'], prg_size=len(payload), raw_sha1=sha, file_sha256=sha256)
