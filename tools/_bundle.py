"""Explicit source selection shared by source-only distribution builders."""
from cart_station_branding import (MISTER_LAUNCHER, WINDOWS_LAUNCHER)

def project_sources(root, windows=False, validation=False):
    sources = {}
    patterns = ['src/*.py', 'data/*.json', 'licenses/*.txt', 'docs/*.md', 'docs/ja/*.md', 'docs/*.json',
                'docs/images/windows_gui_*.png']
    if validation:
        # Validation packages carry the complete source tree so tests can
        # inspect either platform's packaging without requiring another ZIP.
        patterns.extend(('tools/*.py', 'tests/*.py', 'tests/fixtures/*.json', 'windows_tests/*.py'))
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.name == 'nes_catalog.reference.json':
                continue  # Unfiltered research metadata is not a standard catalog.
            if not windows and not validation and path.name == 'cart_station_windows.py':
                continue
            sources[path.relative_to(root).as_posix()] = path
    for name in ('LICENSE', 'CHANGELOG.md', 'cart_station.py', 'README.md', 'USER_GUIDE.md', 'README.ja.md',
                 'requirements.txt', 'cart_station_check.sh', MISTER_LAUNCHER):
        sources[name] = root / name
    if windows or validation:
        for name in ('requirements-windows.txt', WINDOWS_LAUNCHER):
            sources[name] = root / name
    # Only examples ship; personal configuration and diagnostic logs stay local.
    for name in ('output_paths.example.json', 'mister_cores.example.json'):
        sources['config/' + name] = root / 'config' / name
    return sources
