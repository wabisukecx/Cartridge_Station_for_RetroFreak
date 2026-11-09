"""Shared reader service, verified storage lookup and MiSTer launch metadata.

No Qt, terminal or gamepad dependency. Both frontends use this service.
"""
import errno
import hashlib
import json
import logging
import os
import stat
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from cart_station_transport import Adapter, open_transport
from cart_station_storage import save_verified_rom
from cart_station_protocol import ProtocolError, CART_SFC, CART_NES, CART_GB, CART_GBA, CART_MD, CART_PCE, CART_EMPTY, CART_MULTIPLE
from cart_station_environment import default_output_directory
from cart_station_paths import ROOT, config_path, mister_runtime_directory
from cart_station_metrics import CaptureMetrics, write_capture_report
from cart_station_gb import GbProfile, GbDatabase, inspect_header, dump_gb, verified_file as verified_gb_file
from cart_station_gba import (GbaProfile, GbaDatabase, inspect_header as inspect_gba_header,
                              dump_gba, verified_file as verified_gba_file)
from cart_station_md import (MdProfile, MdDatabase, inspect_header as inspect_md_header,
                             dump_md, verified_file as verified_md_file)
from cart_station_sfc import (SfcProfile, SfcDatabase, inspect_header as inspect_sfc_header,
                              dump_sfc, verified_file as verified_sfc_file)
from cart_station_pce import (PceProfile, PceDatabase, inspect_header as inspect_pce_header,
                              identify_pce, dump_pce, verified_file as verified_pce_file)
from cart_station_nes_db import Profile, Database, auto_read_group, dump_candidates, dump_profile, identify, verified_rom_file


class UserCancelled(Exception):
    pass


# Used when mister_cores.json has no entry; NES must be configured explicitly.
DEFAULT_CORES = {
    'GB': dict(rbf='_Console/Gameboy', type='f', index=1, delay=2),
    'GBC': dict(rbf='_Console/Gameboy', type='f', index=1, delay=2),
    'GBA': dict(rbf='_Console/GBA', type='f', index=1, delay=2),
    'SFC': dict(rbf='_Console/SNES', type='f', index=1, delay=2),
    'MD': dict(rbf='_Console/MegaDrive', type='f', index=1, delay=2),
    'PCE': dict(rbf='_Console/TurboGrafx16', type='f', index=0, delay=2),
    'SGX': dict(rbf='_Console/TurboGrafx16', type='f', index=1, delay=2),
}


@dataclass(frozen=True)
class HeaderSystem:
    """One header-based ROM system: everything the service needs to identify, read and verify it.

    Add a system here (plus its module) instead of branching on profile types.
    """
    cart: int
    profile_type: type
    label: str
    database_attribute: str
    database_type: type
    inspect: object      # (adapter, progress) -> profile from the cartridge header
    dump: object         # (adapter, profile, database, progress) -> (matched, payload)
    verify: object       # (path, profile) -> bool
    total: object        # profile -> progress total for the first stage
    identify: object = None  # optional (adapter, profile, database, progress) -> profile

    def poll(self, service):
        if not service.identification_done:
            profile = self.identify_profile(service)
            service.profile, service.selected_id = profile, profile.id
            service.candidates = (profile,)
            service.identification_done = True
            service.file_cache.clear()
            if service.follow_system_paths:
                service.destination = default_output_directory(service.environment, profile.system, service.paths_config)
        if isinstance(service.profile, PceProfile):
            return service.snapshot(service.profile.system + ': identified by DB; choose Read / DUMP to save')
        return service.snapshot(service.profile.system + ': header detected; full DB hash checked after reading')

    def identify_profile(self, service):
        """Identify a linear-ROM cartridge from its header (PCE: by one full DB-matched read)."""
        profile = self.inspect(service.adapter, service.identification_progress)
        if self.identify is not None:
            database = getattr(service, self.database_attribute)
            if database is None:
                database = self.database_type.load()
                setattr(service, self.database_attribute, database)
            return self.identify(service.adapter, profile, database, service.identification_progress)
        previous = service.recent_profile
        # Keep the existing object (and its DB-verified fields) for the same cartridge.
        return previous if isinstance(previous, type(profile)) and previous.id == profile.id else profile

    def check_selection(self, service):
        profile = service.profile
        if (service.adapter is None or service.selected_id != profile.id
                or profile.support() is not None):
            raise ProtocolError('Select a supported %s cartridge before dumping' % self.label)

    def capture(self, service, progress):
        profile = service.profile
        progress('Checking %s cartridge...' % self.label, 0, self.total(profile))
        database = getattr(service, self.database_attribute)
        if database is None:
            database = service._measure_capture('load_' + self.database_attribute,
                                             self.database_type.load)
            setattr(service, self.database_attribute, database)
        matched, payload = self.dump(service.adapter, profile, database, progress)
        if isinstance(matched, PceProfile) and service.follow_system_paths:
            service.destination = default_output_directory(
                service.environment, matched.system, service.paths_config)
        return service._save_capture(matched, payload, progress)


@dataclass(frozen=True)
class NesSystem:
    """NES identification retains partial-hash candidates until full ROM verification."""
    cart: int = CART_NES
    profile_type: type = Profile
    label: str = 'FC/NES'
    verify: object = verified_rom_file

    def poll(self, service):
        if service.follow_system_paths:
            service.destination = default_output_directory(service.environment, 'NES', service.paths_config)
        if not service.identification_done:
            service.candidates = identify(service.adapter, service.database)
            service.identification_done = True
            if len(service.candidates) == 1:
                service.profile = service.candidates[0]
                service.selected_id = service.profile.id
                service.file_cache.clear()
            else:
                # Several same-geometry readable titles: full hashes after
                # reading decide, so no manual choice is needed.
                service.auto_group = auto_read_group(service.candidates)
                if service.auto_group:
                    service.profile = None
                    service.file_cache.clear()
        if not service.candidates:
            return service.snapshot('FC/NES: no DB candidates')
        if service.selected_id is None and service.auto_group:
            return service.snapshot('FC/NES: %d candidates, auto-detect' % len(service.auto_group))
        if service.selected_id is None:
            return service.snapshot('FC/NES: choose from %d DB candidates' % len(service.candidates))
        return service.snapshot('FC/NES: DB candidate selected')

    def check_selection(self, service):
        profile = service.profile
        group = service.auto_group if service.selected_id is None else ()
        if group:
            if service.adapter is None:
                raise ProtocolError('Select a supported cartridge before dumping')
        elif (service.adapter is None or profile is None or service.selected_id != profile.id
                or profile.support() is not None):
            raise ProtocolError('Select a supported cartridge before dumping')

    def capture(self, service, progress):
        profile = service.profile
        group = service.auto_group if service.selected_id is None else ()
        total = 2 * ((group[0] if group else profile).prg_size
                     + (group[0] if group else profile).chr_size)
        progress('Checking cartridge...', 0, total)
        # Recheck immediately before any mapper writes: a selection is not
        # permission to use a different cartridge inserted in the meantime.
        candidates = service._measure_capture('identify_nes', identify, service.adapter, service.database)
        if group:
            if {p.id for p in candidates} != {p.id for p in group}:
                raise ProtocolError('Cartridge changed; CHECK / RETRY')
            profile, data = dump_candidates(service.adapter, group, progress=progress)
            # The hashes of the data actually read chose the title.
        else:
            if profile.id not in {p.id for p in candidates}:
                raise ProtocolError('Cartridge changed; CHECK / RETRY')
            data = dump_profile(service.adapter, profile, progress=progress)
        return service._save_capture(profile, data, progress)


SYSTEMS = (
    NesSystem(),
    HeaderSystem(CART_SFC, SfcProfile, 'SFC', 'sfc_database', SfcDatabase,
                 inspect_sfc_header, dump_sfc, verified_sfc_file, lambda p: p.prg_size * 2),
    HeaderSystem(CART_PCE, PceProfile, 'PCE HuCard', 'pce_database', PceDatabase,
                 lambda adapter, progress: inspect_pce_header(adapter), dump_pce, verified_pce_file,
                 lambda p: 17825792, identify=identify_pce),
    HeaderSystem(CART_MD, MdProfile, 'MD', 'md_database', MdDatabase,
                 lambda adapter, progress: inspect_md_header(adapter), dump_md, verified_md_file,
                 lambda p: p.prg_size * 2),
    HeaderSystem(CART_GBA, GbaProfile, 'GBA', 'gba_database', GbaDatabase,
                 lambda adapter, progress: inspect_gba_header(adapter), dump_gba, verified_gba_file,
                 lambda p: 64 * 1024 * 1024),
    HeaderSystem(CART_GB, GbProfile, 'GB/GBC', 'gb_database', GbDatabase,
                 lambda adapter, progress: inspect_header(adapter), dump_gb, verified_gb_file,
                 lambda p: p.prg_size * 2),
)
_SYSTEM_BY_CART = {system.cart: system for system in SYSTEMS}
_SYSTEM_BY_PROFILE = {system.profile_type: system for system in SYSTEMS}
READABLE_CARTS = tuple(_SYSTEM_BY_CART)


def verified_file(path, profile):
    system = _SYSTEM_BY_PROFILE.get(type(profile))
    return (system.verify if system else verified_rom_file)(path, profile)


def save_and_reverify(path, payload, profile, measure=None):
    """Save verified bytes, then reread the file on disk against the same profile.

    Shared by the service and the CLI so "Saved and verified" always means the
    published file itself matched, not only the bytes held in memory.
    """
    measure = measure or (lambda name, function, *args: function(*args))
    target, created = measure('save_verified_rom', save_verified_rom, path, payload)
    if not measure('verify_saved_file', verified_file, target, profile):
        raise ProtocolError('Saved %s ROM failed verification' % getattr(profile, 'system', 'NES'))
    return target, created


def read_core_config(path=None, system='NES'):
    if path is None:
        path = config_path('mister_cores.json')
    try:
        settings = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(settings, dict) or type(settings.get('version')) is not int or settings['version'] != 1:
            raise ValueError('unsupported version')
        core = settings.get(system)
        if core is None and system in DEFAULT_CORES:
            core = dict(DEFAULT_CORES[system])
        if core is None:
            raise ValueError('No core configured for ' + system)
        rbf = core['rbf']
        if (not isinstance(rbf, str) or not rbf or PurePosixPath(rbf).is_absolute()
                or '..' in PurePosixPath(rbf).parts or '\\' in rbf
                or any(ord(char) < 32 or ord(char) == 127 for char in rbf)
                or rbf.lower().endswith('.rbf')):
            raise ValueError('rbf must be a relative core prefix without .rbf')
        required_index = 0 if system == 'PCE' else 1
        if core['type'] != 'f' or type(core['index']) is not int or core['index'] != required_index:
            raise ValueError(system + ' requires type=f and index=%d' % required_index)
        if type(core['delay']) is not int or not 1 <= core['delay'] <= 30:
            raise ValueError('delay must be 1..30 seconds')
        return dict(core)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProtocolError('Invalid %s core settings: %s: %s' % (system, path, error)) from error


def core_available(core, sd_root):
    prefix = sd_root / core['rbf']
    return any(path.is_file() and path.suffix.lower() == '.rbf'
               and (path.name.lower() == (prefix.name + '.rbf').lower()
                    or path.name.lower().startswith(prefix.name.lower() + '_'))
               for path in prefix.parent.glob('*.rbf'))


def create_mgl(rom, core, directory, sd_root=Path('/media/fat'), profile=None):
    if not verified_file(rom, profile):
        raise ProtocolError('Saved ROM is missing or does not match the database')
    if not core_available(core, sd_root):
        raise ProtocolError('Core not found: ' + core['rbf'])
    root = ET.Element('mistergamedescription')
    ET.SubElement(root, 'rbf').text = core['rbf']
    ET.SubElement(root, 'file', delay=str(core['delay']), type=core['type'],
                  index=str(core['index']), path=str(rom.resolve()))
    directory.mkdir(parents=True, exist_ok=True)
    name = (getattr(profile, 'system', 'NES') + '_' + profile.raw_sha1 + '_' + hashlib.sha1(profile.header).hexdigest()[:8]
            if profile is not None else 'NES_cartridge')
    path = directory / (name + '.mgl')
    fd, temporary = tempfile.mkstemp(prefix='.launch-', suffix='.mgl', dir=str(directory))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(ET.tostring(root, encoding='utf-8', xml_declaration=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def send_launch(path, fifo=Path('/dev/MiSTer_cmd')):
    text = str(path.resolve())
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ProtocolError('Invalid launch path')
    command = ('load_core ' + text + '\n').encode('utf-8')
    if len(command) > 1000:
        raise ProtocolError('Launch command is too long')
    if not stat.S_ISFIFO(fifo.stat().st_mode):
        raise ProtocolError('MiSTer command interface is not a FIFO')
    # Never block if Main_MiSTer is not listening. One short atomic FIFO write.
    try:
        fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        if error.errno == errno.ENXIO:
            raise ProtocolError('MiSTer is not listening for launch commands') from error
        raise
    try:
        if os.write(fd, command) != len(command):
            raise ProtocolError('Incomplete launch command')
    finally:
        os.close(fd)


@dataclass(frozen=True)
class PollState:
    reader: str
    cartridge: str
    supported: bool
    candidates: tuple
    profile: object
    saved: bool
    selection_pending: bool = False
    auto: int = 0
    destination: object = None


class Service:
    """One worker owns USB, candidate selection and file verification."""
    CONFLICT_LISTING_SECONDS = 5.0

    def __init__(self, destination, port=None, usb=False, core_path=None,
                 sd_root=Path('/media/fat'), db_path=None, database=None, gb_database=None,
                 environment=None, paths_config=None, follow_system_paths=False, gba_database=None, md_database=None,
                 pce_database=None, sfc_database=None, metrics_path=None):
        self.destination = destination
        self.environment, self.paths_config = environment, paths_config
        self.follow_system_paths = follow_system_paths
        self.gb_database = gb_database
        self.gba_database = gba_database
        self.md_database = md_database
        self.sfc_database = sfc_database
        self.pce_database = pce_database
        self.identification_progress = None
        self.metrics_path = metrics_path
        self.last_capture_metrics = self._capture_metrics = None
        self.reader_info = None
        self.port, self.usb = port, usb
        self.core_path, self.sd_root = core_path, sd_root
        self.database = database if database is not None else Database.load(db_path)
        self.transport = self.adapter = None
        self.product = ''
        self.candidate, self.samples = None, 0
        self.candidates = ()
        self.identification_done = False
        self.selected_id = None
        self.auto_group = ()
        self.profile = None
        # The last selected profile, kept only so that reinserting the same
        # header cartridge keeps its DB-verified fields. Never launched or shown.
        self.recent_profile = None
        self.file_cache = {}

    def close(self):
        transport, self.transport = self.transport, None
        self.adapter = None
        self.product = 'Not connected'
        self.reader_info = None
        self.candidate, self.samples = None, 0
        self.candidates, self.selected_id, self.auto_group = (), None, ()
        self.identification_done = False
        if transport is not None:
            transport.close()

    def snapshot(self, cartridge):
        supported = (self.profile is not None and self.selected_id == self.profile.id
                     and self.profile.support() is None)
        auto = len(self.auto_group) if self.auto_group and self.selected_id is None else 0
        return PollState(self.product, cartridge, supported, self.candidates,
                         self.profile, bool(self.profile is not None and not auto
                                            and self.existing(cached=True) is not None),
                         len(self.candidates) > 1 and self.selected_id is None and not auto, auto, self.destination)

    def poll(self):
        try:
            if self.adapter is None:
                self.transport = open_transport(self.port, self.usb)
                self.adapter = Adapter(self.transport)
                self.reader_info = self.adapter.product()
                self.product = 'Connected'
            cart = self.adapter.status()
            if cart == self.candidate:
                self.samples = min(3, self.samples + 1)
            else:
                self.candidate, self.samples = cart, 1
                self.candidates, self.selected_id, self.auto_group = (), None, ()
                self.identification_done = False
                # After removal the saved game stays available for GAME START.
                # A newly inserted cartridge must not launch the previous ROM.
                if cart in READABLE_CARTS and self.profile is not None:
                    self.recent_profile, self.profile = self.profile, None
            if self.samples < 3:
                return self.snapshot('Waiting for stable cartridge...')
            if cart not in READABLE_CARTS:
                # Preserve the raw code; both legacy 0 and observed FF are empty.
                if cart in CART_EMPTY:
                    message = 'No cartridge detected'
                elif cart == CART_MULTIPLE:
                    message = 'Multiple cartridges detected; leave only one inserted'
                else:
                    message = 'Different console / type %d' % cart
                return self.snapshot(message)
            return _SYSTEM_BY_CART[cart].poll(self)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def select_profile(self, identifier):
        profile = next((p for p in self.candidates if p.id == identifier), None)
        if self.adapter is None or profile is None or self.candidate not in READABLE_CARTS:
            raise ProtocolError('Candidate is no longer available; CHECK / RETRY')
        self.profile, self.selected_id = profile, profile.id
        self.file_cache.clear()
        return self.snapshot('FC/NES: DB candidate selected')

    def retry(self):
        try:
            self.close()
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Reader cleanup failed during retry: %s", error, exc_info=True
            )
        self.file_cache.clear()
        return self.poll()

    def existing(self, cached=False):
        if self.profile is not None and not self.profile.raw_sha1:
            return None
        profile = self.profile
        if profile is None:
            return None

        def valid(path):
            try:
                status = path.stat()
                key = (profile.id, str(path), status.st_size, status.st_mtime_ns, status.st_ctime_ns)
            except OSError:
                return False
            if not cached or key not in self.file_cache:
                self.file_cache[key] = verified_file(path, profile)
            return self.file_cache[key]

        target = self.destination / profile.filename()
        if valid(target):
            return target
        # A conflict copy must still match the selected DB profile in full;
        # the hash in its filename is never trusted as verification evidence.
        for copy in self._conflict_copies(target, cached):
            if valid(copy):
                return copy
        return None

    def _conflict_copies(self, target, cached):
        """Conflict-copy names; polling reuses a listing for a few seconds.

        A large games folder is not listed on every poll. Saving clears the
        cache, and launch/capture paths always list the folder again.
        """
        key = ('conflict_copies', str(target))
        now = time.monotonic()
        if cached and key in self.file_cache and now < self.file_cache[key][0]:
            return self.file_cache[key][1]
        copies = (tuple(sorted(item for item in self.destination.iterdir()
                               if item.name.startswith(target.stem + '.verified-')
                               and item.name.endswith(target.suffix)))
                  if self.destination.is_dir() else ())
        self.file_cache[key] = (now + self.CONFLICT_LISTING_SECONDS, copies)
        return copies

    def capture(self, progress):
        metrics = CaptureMetrics()
        adapter = self.adapter
        previous_metrics = getattr(adapter, 'io_metrics', None) if adapter is not None else None
        if adapter is not None:
            adapter.io_metrics = metrics.io
        self._capture_metrics = metrics
        reader = self.reader_info if isinstance(self.reader_info, dict) else {}
        transport = type(self.transport).__name__ if self.transport is not None else None
        outcome, error_type = 'error', None

        def measured_progress(stage, done, total):
            metrics.progress(stage)
            return progress(stage, done, total)

        try:
            result = self._perform_capture(measured_progress)
            outcome = 'saved' if result[1] else 'identical'
            return result
        except BaseException as error:
            outcome = 'cancelled' if isinstance(error, UserCancelled) else 'error'
            error_type = type(error).__name__
            raise
        finally:
            if adapter is not None:
                adapter.io_metrics = previous_metrics
            self._capture_metrics = None
            report = metrics.finish(outcome, error_type)
            profile = self.profile
            report.update(system=getattr(profile, 'system', 'NES') if profile is not None else None,
                          transport=transport,
                          reader={key: value for key, value in reader.items()
                                  if key in ('build', 'product') and type(value) is int})
            size = getattr(profile, 'prg_size', None)
            chr_size = getattr(profile, 'chr_size', 0)
            report['rom_bytes'] = size + chr_size if outcome in ('saved', 'identical') and type(size) is int and type(chr_size) is int else None
            self.last_capture_metrics = report
            if self.metrics_path is not None:
                write_capture_report(self.metrics_path, report, ROOT)

    def _measure_capture(self, name, function, *args):
        if self._capture_metrics is None:
            return function(*args)
        return self._capture_metrics.call(name, function, *args)

    def _perform_capture(self, progress):
        profile = self.profile or (self.auto_group[0] if self.auto_group else None)
        system = _SYSTEM_BY_PROFILE.get(type(profile))
        if system is None:
            raise ProtocolError('Select a supported cartridge before dumping')
        system.check_selection(self)
        try:
            return system.capture(self, progress)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _save_capture(self, profile, payload, progress):
        """Publish verified bytes and update the selection only after rechecking disk."""
        total = len(payload) * 2
        progress('Verifying and saving...', total, total)
        self.destination.mkdir(parents=True, exist_ok=True)
        try:
            target, created = save_and_reverify(self.destination / profile.filename(), payload,
                                                profile, self._measure_capture)
        finally:
            self.file_cache.clear()
        self.profile, self.selected_id = profile, profile.id
        self.candidates = (profile,)
        return target, created

    def prepare_game(self):
        profile = self.profile
        self.close()
        if profile is None:
            raise ProtocolError('No game selected')
        source = self.existing()  # Always rehash before launch, never trust UI cache.
        if source is None:
            raise ProtocolError('No verified ROM is available for the selected game')
        target = self.destination / profile.filename()
        if source.parent == self.destination:
            target = source
        else:
            self.destination.mkdir(parents=True, exist_ok=True)
            target, _ = save_verified_rom(target, source.read_bytes())
        return create_mgl(target, read_core_config(self.core_path, getattr(profile, 'system', 'NES')),
                          mister_runtime_directory(self.sd_root), self.sd_root,
                          profile=profile)


