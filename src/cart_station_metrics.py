"""Capture timing only; never retain transport payloads or change reader calls."""
import hashlib
import json
import os
import platform
import tempfile
import time
from pathlib import Path


class IoMetrics:
    def __init__(self, clock):
        self.clock = clock
        self.values = {name: 0 for name in ('read_calls', 'write_calls', 'read_bytes', 'write_bytes',
                                           'read_errors', 'write_errors', 'read_seconds', 'write_seconds')}

    def call(self, transport, operation, value):
        started = self.clock()
        self.values[operation + '_calls'] += 1
        try:
            result = getattr(transport, operation)(value)
        except BaseException:
            self.values[operation + '_errors'] += 1
            raise
        else:
            self.values[operation + '_bytes'] += (len(result) if result is not None else 0) if operation == 'read' else len(value)
            return result
        finally:
            self.values[operation + '_seconds'] += self.clock() - started

    def snapshot(self):
        return dict(self.values)


class CaptureMetrics:
    def __init__(self, clock=None):
        self.clock = clock or time.perf_counter
        self.io = IoMetrics(self.clock)
        self.started = self.phase_started = self.clock()
        self.phase = 'Preparing capture'
        self.phase_io = self.io.snapshot()
        self.phases = []
        self.operations = {}

    def _end_phase(self, now):
        current = self.io.snapshot()
        self.phases.append({'stage': self.phase, 'seconds': now - self.phase_started,
                            'io': {key: current[key] - self.phase_io[key] for key in current}})
        self.phase_started, self.phase_io = now, current

    def progress(self, stage):
        if stage != self.phase:
            self._end_phase(self.clock())
            self.phase = stage

    def call(self, name, function, *args):
        started = self.clock()
        try:
            return function(*args)
        finally:
            self.operations[name] = self.operations.get(name, 0) + self.clock() - started

    def finish(self, outcome, error_type=None):
        ended = self.clock()
        self._end_phase(ended)
        return {'version': 1, 'scope': 'capture_only', 'outcome': outcome, 'error_type': error_type,
                'elapsed_seconds': ended - self.started, 'phases': self.phases,
                'io': self.io.snapshot(), 'operation_seconds': dict(self.operations),
                'nested_timings': 'io and operations are included in phase/elapsed times; do not add them'}


def write_capture_report(path, report, manifest_root):
    """Best-effort latest-only metadata. Timing/log failures never fail a ROM save."""
    temporary = None
    try:
        path = Path(path)
        document = dict(report, recorded_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        python=platform.python_version(), platform=platform.system(), machine=platform.machine())
        document['bundle_manifest_sha256'] = None
        names = (('WINDOWS_MANIFEST.json', 'BUNDLE_MANIFEST.json') if document['platform'] == 'Windows'
                 else ('BUNDLE_MANIFEST.json', 'WINDOWS_MANIFEST.json'))
        for name in names:
            manifest = Path(manifest_root) / name
            if manifest.is_file():
                document['bundle_manifest_sha256'] = hashlib.sha256(manifest.read_bytes()).hexdigest()
                break
        payload = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.capture-metrics-', dir=str(path.parent))
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
        os.replace(temporary, path)
        temporary = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
