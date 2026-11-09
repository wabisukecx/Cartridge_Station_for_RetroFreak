"""Host detection and per-system output paths, independent of UI and CLI."""
import json
import platform
from pathlib import Path, PurePosixPath
from cart_station_protocol import ProtocolError
from cart_station_paths import config_path as find_config


# Used when output_paths.json has no entry for a system (NES must be configured).
PC_DEFAULT_OUTPUT = '~/RetroFreakDumps'
MISTER_DEFAULT_OUTPUTS = {
    'SFC': '/media/fat/games/SNES', 'GB': '/media/fat/games/Gameboy', 'GBC': '/media/fat/games/GBC',
    'GBA': '/media/fat/games/GBA', 'MD': '/media/fat/games/MegaDrive',
    'PCE': '/media/fat/games/TGFX16', 'SGX': '/media/fat/games/TGFX16',
}


def detect_environment():
    """Recognize MiSTer only with both an ARM Linux host and its SD layout."""
    if (platform.system() == 'Linux'
            and platform.machine().lower().startswith(('arm', 'aarch64'))
            and Path('/media/fat/MiSTer').is_file()
            and Path('/media/fat/linux').is_dir()):
        return 'mister'
    return 'pc'


def default_output_directory(environment, system='NES', config_path=None):
    if environment not in ('pc', 'mister'):
        raise ValueError('Unknown environment')
    if config_path is not None:
        source = Path(config_path)
    else:
        source = find_config('output_paths.json')
    try:
        settings = json.loads(source.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise ProtocolError('Cannot read output path settings: %s: %s' % (source, error)) from error
    if not isinstance(settings, dict) or type(settings.get('version')) is not int or settings['version'] != 1:
        raise ProtocolError('Unsupported output path settings version: ' + str(source))
    systems = settings.get(environment)
    value = systems.get(system) if isinstance(systems, dict) else None
    if value is None and system in MISTER_DEFAULT_OUTPUTS:
        value = MISTER_DEFAULT_OUTPUTS[system] if environment == 'mister' else PC_DEFAULT_OUTPUT
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError('No output path configured for %s / %s in %s' % (environment, system, source))
    if environment == 'mister':
        if not PurePosixPath(value).is_absolute() or '\\' in value:
            raise ProtocolError('MiSTer output path must be an absolute Linux path')
    elif not Path(value).expanduser().is_absolute():
        raise ProtocolError('PC output path must be absolute or begin with ~/')
    return Path(value).expanduser()


