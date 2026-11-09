"""Package local pure-Python dependencies; no device or system changes."""
from _bootstrap import ROOT
from _bundle import project_sources
from cart_station_branding import (INSTALL_DIRECTORY, MISTER_ARCHIVE, MISTER_LAUNCHER,
                                   PRODUCT_NAME, MISTER_EDITION)
import argparse
import ast
import hashlib
import json
import sysconfig
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validation', action='store_true',
                        help='Include tests, fixtures and developer tools in a separate ZIP')
    args = parser.parse_args()
    root = ROOT
    packages = Path(sysconfig.get_path('purelib'))
    sources = project_sources(root, validation=args.validation)
    for name in ('serial', 'usb', 'pyserial-3.5.dist-info', 'pyusb-1.3.1.dist-info'):
        directory = packages / name
        if not directory.is_dir():
            raise RuntimeError('Install requirements.txt into the build environment first: ' + name)
        for source in sorted(directory.rglob('*')):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix not in ('.pyc', '.pyo'):
                sources['vendor/' + source.relative_to(packages).as_posix()] = source
    manifest = {}
    for name, source in sources.items():
        content = source.read_bytes()
        if source.suffix == '.py':
            ast.parse(content, filename=name, feature_version=(3, 9))
        if source.suffix.lower() in ('.dll', '.pyd', '.so', '.exe'):
            raise RuntimeError('Native binary cannot be included: ' + name)
        manifest[name] = hashlib.sha256(content).hexdigest()
    (root / 'dist').mkdir(exist_ok=True)
    filename = MISTER_ARCHIVE + ('_Validation.zip' if args.validation else '.zip')
    target = root / 'dist' / filename
    with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
        for name, source in sorted(sources.items()):
            archive.write(source, INSTALL_DIRECTORY + '/' + name)
        external = {}
        for name in (MISTER_LAUNCHER,):
            launcher = root / name
            info = ZipInfo(name)
            info.external_attr = 0o100755 << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, launcher.read_bytes())
            external[name] = hashlib.sha256(launcher.read_bytes()).hexdigest()
        archive.writestr(INSTALL_DIRECTORY + '/BUNDLE_MANIFEST.json',
                         json.dumps({'product': PRODUCT_NAME, 'edition': MISTER_EDITION,
                                     'package_kind': 'validation' if args.validation else 'runtime',
                                     'python_syntax': '3.9', 'files': manifest,
                                     'external_files': external}, indent=2) + '\n')
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    # Write bytes so Windows newline translation cannot introduce CRLF.
    target.with_suffix('.zip.sha256').write_bytes((checksum + '  ' + target.name + '\n').encode('ascii'))
    print(str(target))
    print('Files:', len(manifest), 'Bytes:', target.stat().st_size)


if __name__ == '__main__':
    main()
