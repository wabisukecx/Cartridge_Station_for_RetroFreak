"""Cartridge Station for RetroFreak CLI.

FC/NES, GB/GBC, GBA, MD, PCE and SFC share transport, storage and application service.
No save-data or flash programming operations are exposed.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from cart_station_branding import PRODUCT_NAME


# The offline bundle keeps its pure-Python dependencies beside this file.
from cart_station_paths import ROOT

_vendor = ROOT / 'vendor'
if _vendor.is_dir():
    sys.path.insert(0, str(_vendor))


from cart_station_protocol import ProtocolError, CART_NES, cleanup_after
from cart_station_environment import detect_environment, default_output_directory
from cart_station_transport import Adapter, open_transport
from cart_station_service import save_and_reverify


def main():
    parser = argparse.ArgumentParser(description=PRODUCT_NAME + ' / Windows Edition and MiSTer FPGA Edition. FC/NES, GB/GBC, GBA, MD, PCE and SFC reader with verified database hashes')
    link = parser.add_mutually_exclusive_group()
    link.add_argument('--port', help='Serial port, e.g. COM3 or /dev/ttyACM0')
    link.add_argument('--usb', action='store_true', help='Experimental direct USB bulk transport')
    parser.add_argument('--environment', choices=('auto', 'pc', 'mister'), default='auto',
                        help='Host detection override; default: auto')
    parser.add_argument('--paths-config', type=Path,
                        help='Use a custom output_paths.json file')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('list', help='List serial ports without opening a device')
    sub.add_parser('probe', help='Query product and cartridge status')
    command = sub.add_parser('gui', help='PyQt6 desktop interface for Windows')
    command.add_argument('--output-dir', type=Path)
    command.add_argument('--db', type=Path, help='NES profiles JSON; default uses the shared database')
    command = sub.add_parser('manager', help='TV and gamepad menu for MiSTer')
    command.add_argument('--output-dir', type=Path)
    command.add_argument('--core-config', type=Path)
    command.add_argument('--db', type=Path, help='NES profiles JSON; default uses the same DB as identify-db')
    command.add_argument('--preview', action='store_true', help='Show the menu without opening any hardware')
    command = sub.add_parser('diagnose-gb', help='Compare three raw GB/GBC headers without bank writes or saving')
    command.add_argument('--reset', action='store_true',
                         help='Send connection initialization 04(0) before probing')
    command.add_argument('--trace', type=Path,
                         help='Write application I/O hex and timings to a new JSONL file')
    for action in ('identify-sfc', 'dump-sfc'):
        command = sub.add_parser(action, help='Inspect SFC header or confirm a verified ROM dump')
        command.add_argument('--sfc-db', type=Path, help='Optional SFC full-hash metadata JSON')
        if action == 'dump-sfc':
            command.add_argument('--output-dir', type=Path)
    for action in ('probe-pce', 'identify-pce', 'dump-pce'):
        command = sub.add_parser(action, help='Inspect HuCard mapping or confirm a verified PCE ROM dump')
        command.add_argument('--pce-db', type=Path, help='Optional PCE/SuperGrafx full-hash metadata JSON')
        command.add_argument('--controller', choices=('Linear', 'Interval', 'Sf2Dash'),
                             help='Manual reader selection; default uses mapping probes')
        if action == 'dump-pce':
            command.add_argument('--output-dir', type=Path)
    for action in ('identify-md', 'dump-md'):
        command = sub.add_parser(action, help='Inspect MD header or confirm a verified ROM dump')
        command.add_argument('--md-db', type=Path, help='Optional MD full-hash metadata JSON')
        command.add_argument('--controller', choices=('None', 'SAndN', 'SuperSf2', 'MortalK2'),
                             help='Manual reader selection; default follows header detection')
        if action == 'dump-md':
            command.add_argument('--output-dir', type=Path)
    for action in ('identify-gba', 'dump-gba'):
        command = sub.add_parser(action, help='Inspect GBA header or confirm a verified ROM dump')
        command.add_argument('--gba-db', type=Path, help='Optional GBA full-hash metadata JSON')
        if action == 'dump-gba':
            command.add_argument('--output-dir', type=Path)
    for action in ('identify-gb', 'dump-gb'):
        command = sub.add_parser(action, help='Inspect GB/GBC header or confirm a verified ROM dump')
        command.add_argument('--gb-db', type=Path, help='Optional GB/GBC full-hash metadata JSON')
        if action == 'dump-gb':
            command.add_argument('--output-dir', type=Path)
    command = sub.add_parser('list-db', help='Search NES metadata without connecting to the reader')
    command.add_argument('--db', type=Path)
    command.add_argument('--query', default='', help='Part of a title or profile ID')
    command.add_argument('--mapper', type=int, choices=range(4096), metavar='NUMBER')
    command.add_argument('--limit', type=int, choices=range(1, 1001), default=20, metavar='1..1000')
    for action in ('identify-db', 'dump-db'):
        command = sub.add_parser(action, help='Identify FC/NES candidates using partial-hash metadata')
        command.add_argument('--db', type=Path, help='Version 1 NES profiles JSON; default: user DB if present, otherwise reference DB')
        if action == 'dump-db':
            selection = command.add_mutually_exclusive_group()
            selection.add_argument('--profile', help='Choose an ID from the identified candidates')
            selection.add_argument('--catalog-profile', help='Explicit DB read plan even without partial matches; full verification required')
            command.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    if args.action == 'list':
        from serial.tools import list_ports
        for port in list_ports.comports():
            print('%s: %s [VID=%s PID=%s]' % (port.device, port.description,
                  '%04X' % port.vid if port.vid is not None else '?',
                  '%04X' % port.pid if port.pid is not None else '?'))
        return
    environment = detect_environment() if args.environment == 'auto' else args.environment
    if args.action == 'list-db':
        from cart_station_nes_db import Database, candidate_info
        database = Database.load(args.db)
        matches = database.search(args.query, args.mapper)
        print(json.dumps({'state': 'catalog_only', 'database': database.summary(),
                          'matches': len(matches), 'shown': min(len(matches), args.limit),
                          'profiles': [dict(candidate_info(p), partial_hashes=bool(p.probes))
                                       for p in matches[:args.limit]]}, indent=2))
        return
    if args.action == 'gui':
        if environment == 'mister':
            parser.error('Use manager on MiSTer; the PyQt6 GUI is for Windows')
        if args.usb:
            parser.error('The Windows GUI uses auto-detect or COM ports. Use --usb with CLI commands instead.')
        try:
            from cart_station_windows import run_windows
        except ModuleNotFoundError as error:
            if error.name and error.name.startswith('PyQt6'):
                raise ProtocolError('Install Windows GUI dependencies: python -m pip install -r requirements-windows.txt') from error
            raise
        return run_windows(args)
    if args.action == 'diagnose-gb':
        from cart_station_gb import diagnose_header
        from cart_station_transport import TraceTransport
        trace = args.trace.open('x', encoding='utf-8') if args.trace else None
        transport = None

        def release():
            try:
                if transport is not None:
                    transport.close()
            finally:
                if trace is not None:
                    trace.close()

        with cleanup_after(release):
            transport = open_transport(args.port, args.usb)
            if trace is not None:
                transport = TraceTransport(transport, trace)
            adapter = Adapter(transport)
            if args.reset:
                adapter.initialize_connection()
            product = adapter.product()
            result = diagnose_header(adapter)
            result['product'] = product
            result['connection_initialization'] = '04(0)' if args.reset else 'none'
            result['control_replies'] = list(adapter.control_replies)
            print(json.dumps(result, indent=2))
        return
    if args.action in ('identify-sfc', 'dump-sfc'):
        from cart_station_sfc import SfcDatabase, inspect_header, dump_sfc
        if args.action == 'dump-sfc' and not sys.stdin.isatty():
            parser.error('SFC dumping requires an interactive confirmation terminal')
        transport = open_transport(args.port, args.usb)
        with cleanup_after(transport.close):
            adapter = Adapter(transport)
            last_header_stage = ''

            def report_sfc_header(stage, done, total):
                nonlocal last_header_stage
                if stage.startswith('Checking SFC header ') and stage != last_header_stage:
                    last_header_stage = stage
                    if done:
                        print(stage, file=sys.stderr)

            profile = inspect_header(adapter, report_sfc_header)
            print(json.dumps(dict(state='header_only', title=profile.title, system='SFC',
                controller=profile.controller, chip=profile.chip, rom_size=profile.prg_size,
                size_source='header ceiling; full DB hash decides saved size',
                readable=profile.support() is None, limitation=profile.support()), indent=2))
            if args.action == 'dump-sfc':
                if profile.support():
                    raise ProtocolError(profile.support())
                directory = args.output_dir or default_output_directory(environment, 'SFC', args.paths_config)
                if input('Read twice and verify against the SFC database? [y/N] ').strip().lower() != 'y':
                    return
                matched, payload = dump_sfc(adapter, profile, SfcDatabase.load(args.sfc_db), report_sfc_header)
                directory.mkdir(parents=True, exist_ok=True)
                path, created = save_and_reverify(directory / matched.filename(), payload, matched)
                print(('Saved and verified: ' if created else 'Already saved (identical): ') + str(path))
                print('SHA-1: ' + matched.raw_sha1)
        return
    if args.action in ('probe-pce', 'identify-pce', 'dump-pce'):
        from cart_station_pce import PceDatabase, inspect_header, identify_pce, dump_pce
        if args.action == 'dump-pce' and not sys.stdin.isatty():
            parser.error('PCE dumping requires an interactive confirmation terminal')
        transport = open_transport(args.port, args.usb)
        with cleanup_after(transport.close):
            adapter = Adapter(transport)
            profile = inspect_header(adapter, args.controller)
            if args.action == 'identify-pce':
                profile = identify_pce(adapter, profile, PceDatabase.load(args.pce_db))
            print(json.dumps(dict(state='identified' if profile.raw_sha1 else 'sample_only',
                title=profile.title, system=profile.system,
                controller=profile.controller, rom_size=profile.prg_size or None, data_order=profile.data_order,
                sample_sha1=hashlib.sha1(profile.header).hexdigest(),
                readable=profile.support() is None, limitation=profile.support()), indent=2))
            if args.action == 'dump-pce':
                if input('Read twice and verify against the PCE database? [y/N] ').strip().lower() != 'y':
                    return
                matched, payload = dump_pce(adapter, profile, PceDatabase.load(args.pce_db))
                # PCE or SGX is known only from the DB match, so the folder is chosen after reading.
                directory = args.output_dir or default_output_directory(environment, matched.system, args.paths_config)
                directory.mkdir(parents=True, exist_ok=True)
                path, created = save_and_reverify(directory / matched.filename(), payload, matched)
                print(('Saved and verified: ' if created else 'Already saved (identical): ') + str(path))
                print('SHA-1: ' + matched.raw_sha1 + '; data order: ' + matched.data_order)
        return
    if args.action in ('identify-md', 'dump-md'):
        from cart_station_md import MdDatabase, inspect_header, dump_md
        if args.action == 'dump-md' and not sys.stdin.isatty():
            parser.error('MD dumping requires an interactive confirmation terminal')
        transport = open_transport(args.port, args.usb)
        with cleanup_after(transport.close):
            adapter = Adapter(transport)
            profile = inspect_header(adapter, args.controller)
            print(json.dumps(dict(state='header_only', title=profile.title, system='MD',
                serial=profile.serial, controller=profile.controller, attached=profile.attached,
                rom_size=profile.prg_size, size_source='header/reference overrides',
                readable=profile.support() is None, limitation=profile.support()), indent=2))
            if args.action == 'dump-md':
                if profile.support():
                    raise ProtocolError(profile.support())
                directory = args.output_dir or default_output_directory(environment, 'MD', args.paths_config)
                if input('Read twice and verify against the MD database? [y/N] ').strip().lower() != 'y':
                    return
                matched, payload = dump_md(adapter, profile, MdDatabase.load(args.md_db))
                directory.mkdir(parents=True, exist_ok=True)
                path, created = save_and_reverify(directory / matched.filename(), payload, matched)
                print(('Saved and verified: ' if created else 'Already saved (identical): ') + str(path))
                print('SHA-1: ' + matched.raw_sha1)
        return
    if args.action in ('identify-gba', 'dump-gba'):
        from cart_station_gba import GbaDatabase, inspect_header, dump_gba
        if args.action == 'dump-gba' and not sys.stdin.isatty():
            parser.error('GBA dumping requires an interactive confirmation terminal')
        transport = open_transport(args.port, args.usb)
        with cleanup_after(transport.close):
            adapter = Adapter(transport)
            profile = inspect_header(adapter)
            print(json.dumps(dict(state='header_only', title=profile.title, system='GBA',
                game_code=profile.game_code, revision=profile.revision,
                rom_size=None, size_detection='full DB hash after reading'), indent=2))
            if args.action == 'dump-gba':
                directory = args.output_dir or default_output_directory(environment, 'GBA', args.paths_config)
                if input('Read twice and verify against the GBA database? [y/N] ').strip().lower() != 'y':
                    return
                matched, payload = dump_gba(adapter, profile, GbaDatabase.load(args.gba_db))
                directory.mkdir(parents=True, exist_ok=True)
                path, created = save_and_reverify(directory / matched.filename(), payload, matched)
                print(('Saved and verified: ' if created else 'Already saved (identical): ') + str(path))
                print('SHA-1: ' + matched.raw_sha1)
        return
    if args.action in ('identify-gb', 'dump-gb'):
        from cart_station_gb import GbDatabase, inspect_header, dump_gb
        if args.action == 'dump-gb' and not sys.stdin.isatty():
            parser.error('GB dumping requires an interactive confirmation terminal')
        transport = open_transport(args.port, args.usb)
        with cleanup_after(transport.close):
            adapter = Adapter(transport)
            profile = inspect_header(adapter)
            print(json.dumps(dict(state='header_only', title=profile.title, system=profile.system,
                controller=profile.controller, rom_size=profile.prg_size,
                cartridge_type=profile.header[0x47], cgb_flag=profile.header[0x43],
                readable=profile.support() is None, limitation=profile.support()), indent=2))
            if args.action == 'dump-gb':
                reason = profile.support()
                if reason:
                    raise ProtocolError(reason)
                directory = args.output_dir or default_output_directory(environment, profile.system, args.paths_config)
                if input('Read twice and verify against the GB/GBC database? [y/N] ').strip().lower() != 'y':
                    return
                database = GbDatabase.load(args.gb_db)
                matched, payload = dump_gb(adapter, profile, database)
                directory.mkdir(parents=True, exist_ok=True)
                path, created = save_and_reverify(directory / matched.filename(), payload, matched)
                print(('Saved and verified: ' if created else 'Already saved (identical): ') + str(path))
                print('SHA-1: ' + matched.raw_sha1)
        return
    if args.action in ('identify-db', 'dump-db'):
        from cart_station_nes_db import (Database, auto_read_group, candidate_info, dump_candidates,
                               dump_profile, identify, select_candidate)
        database = Database.load(args.db)
        directory = None
        manual = None
        if args.action == 'dump-db':
            directory = args.output_dir or default_output_directory(environment, 'NES', args.paths_config)
            if not sys.stdin.isatty():
                parser.error('DB dumping requires an interactive confirmation terminal')
            if args.catalog_profile:
                manual = database.select_catalog_profile(args.catalog_profile)
        transport = open_transport(args.port, args.usb)
        with cleanup_after(transport.close):
            adapter = Adapter(transport)
            if manual is not None:
                if adapter.status() != CART_NES:
                    raise ProtocolError('Insert one FC/NES cartridge')
                candidates = (manual,)
                print('Manual DB read plan: title/revision is unconfirmed. Saved only after two reads and full DB hash verification.')
            else:
                candidates = identify(adapter, database)
            print(json.dumps({'state': 'manual_unverified' if manual is not None else 'candidates_only',
                              'candidates': [candidate_info(p) for p in candidates]}, indent=2))
            if args.action == 'dump-db':
                group = () if args.profile or manual is not None else auto_read_group(candidates)
                if group:
                    if input('Read and auto-identify among %d candidates? [y/N] ' % len(group)).strip().lower() != 'y':
                        return
                    profile, data = dump_candidates(adapter, group)
                    print('Identified by full hashes: ' + profile.title)
                else:
                    profile = manual or select_candidate(candidates, args.profile)
                    if input('Read %s? [y/N] ' % profile.title).strip().lower() != 'y':
                        return
                    data = dump_profile(adapter, profile)
                target = directory / profile.filename()
                directory.mkdir(parents=True, exist_ok=True)
                target, created = save_and_reverify(target, data, profile)
                print(('Saved and verified: ' if created else 'Already saved (identical): ') + str(target))
        return
    if args.action == 'manager':
        from cart_station_manager import run_manager
        return run_manager(args, environment)
    transport = open_transport(args.port, args.usb)
    adapter = Adapter(transport)

    def release():
        if adapter.control_replies:
            print('Control replies (04/05): ' + ', '.join(adapter.control_replies), file=sys.stderr)
        transport.close()

    with cleanup_after(release):
        print(json.dumps(dict(adapter.product(), cartridge=adapter.status()), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Stopped: %s' % error, file=sys.stderr)
        sys.exit(1)
