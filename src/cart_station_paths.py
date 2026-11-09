"""Installation paths shared by the application, tools and tests."""
from pathlib import Path
from cart_station_branding import INSTALL_DIRECTORY

ROOT = Path(__file__).resolve().parent.parent


def data_path(name):
    return ROOT / 'data' / name


def config_path(name):
    # Personal settings first, then shipped defaults.
    # An update never lets an example override an existing personal setting.
    example = name[:-5] + '.example.json' if name.endswith('.json') else name
    candidates = (ROOT / 'config' / name, ROOT / 'config' / example)
    return next((p for p in candidates if p.is_file()), candidates[0])


def user_nes_database():
    path = ROOT / 'config/nes_profiles.json'
    return path if path.is_file() else None


def mister_runtime_directory(sd_root, installation_root=None):
    """Use the active Scripts installation."""
    scripts = Path(sd_root).resolve() / 'Scripts'
    installed = Path(installation_root if installation_root is not None else ROOT).resolve()
    directory = installed if installed.parent == scripts else scripts / INSTALL_DIRECTORY
    return directory / 'runtime'
