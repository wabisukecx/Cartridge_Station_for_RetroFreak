"""Build the open Python source package with its standard DB; no executable bundling."""
from _bootstrap import ROOT
from _bundle import project_sources
from cart_station_branding import INSTALL_DIRECTORY, WINDOWS_ARCHIVE, PRODUCT_NAME, WINDOWS_EDITION
import argparse
import ast
import hashlib
import json
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validation', action='store_true',
                        help='Include common/GUI tests, fixtures and developer tools in a separate ZIP')
    args = parser.parse_args()
    root = ROOT
    sources = project_sources(root, windows=True, validation=args.validation)
    manifest = {}
    for name, source in sources.items():
        payload = source.read_bytes()
        if name.endswith('.py'):
            ast.parse(payload, filename=name, feature_version=(3, 9))
        manifest[name] = hashlib.sha256(payload).hexdigest()
    filename = WINDOWS_ARCHIVE + ('_Validation.zip' if args.validation else '.zip')
    target = root / 'dist' / filename
    target.parent.mkdir(exist_ok=True)
    with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
        for name, source in sorted(sources.items()):
            archive.write(source, INSTALL_DIRECTORY + '/' + name)
        archive.writestr(INSTALL_DIRECTORY + '/WINDOWS_MANIFEST.json',
                         json.dumps({'product': PRODUCT_NAME, 'edition': WINDOWS_EDITION, 'package_kind': 'validation' if args.validation else 'runtime',
                                     'files': manifest, 'dependencies': 'requirements-windows.txt'}, indent=2) + '\n')
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    # Write bytes so Windows newline translation cannot introduce CRLF.
    target.with_suffix('.zip.sha256').write_bytes((checksum + '  ' + target.name + '\n').encode('ascii'))
    print(str(target))
    print('Files:', len(manifest), 'Bytes:', target.stat().st_size)


if __name__ == '__main__':
    main()
