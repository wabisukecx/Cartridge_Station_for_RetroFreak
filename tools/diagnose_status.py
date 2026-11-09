"""Interactive status-only evidence collection; no ROM or bank operations."""
import _bootstrap  # noqa: F401

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

from cart_station_paths import ROOT
from cart_station_transport import Adapter, TraceTransport, open_transport


STAGES = (
    ('empty', 'Remove all cartridges from the reader'),
    ('inserted', 'Insert a GBA cartridge; leave USB connected'),
    ('removed', 'Remove the cartridge; leave USB connected'),
    ('reinserted', 'Insert the same cartridge again; leave USB connected'),
)

# GB, GBC and GBA share a physical connector but are checked separately.
SINGLE_CARTRIDGES = (
    ('fc', 'an FC cartridge'),
    ('sfc', 'an SFC cartridge'),
    ('gb', 'a GB cartridge'),
    ('gbc', 'a GBC cartridge'),
    ('gba', 'a GBA cartridge'),
    ('md', 'an MD cartridge'),
    ('pce', 'a PCE cartridge'),
)
SLOT_STAGES = (('empty_start', 'Remove all cartridges'),) + tuple(
    stage
    for key, title in SINGLE_CARTRIDGES
    for stage in (
        (key + '_only', 'Insert ONLY ' + title + '; all other slots must be empty'),
        (key + '_removed', 'Remove ' + title + '; all slots must now be empty'),
    )
)
MULTI_STAGES = (
    ('empty_start', 'Remove all cartridges'),
    ('fc_first', 'Insert ONLY an FC cartridge'),
    ('fc_then_gba', 'Keep FC inserted; add a GBA cartridge'),
    ('gba_remaining', 'Remove ONLY FC; keep GBA inserted'),
    ('empty_middle', 'Remove GBA; all slots must now be empty'),
    ('gba_first', 'Insert ONLY a GBA cartridge'),
    ('gba_then_fc', 'Keep GBA inserted; add an FC cartridge'),
    ('fc_remaining', 'Remove ONLY GBA; keep FC inserted'),
    ('empty_end', 'Remove FC; all slots must now be empty'),
)
PLANS = {'gba': STAGES, 'slots': SLOT_STAGES, 'multi': MULTI_STAGES}


def collect(adapter, stage, emit, pause=time.sleep):
    """Keep raw status bytes in TraceTransport, without inferring unknown values."""
    codes = []
    for sample in range(1, 6):
        code = adapter.status()
        codes.append(code)
        emit({'event': 'status', 'stage': stage, 'sample': sample,
              'code': code, 'hex': '0x%02X' % code})
        if sample < 5:
            pause(0.2)
    return codes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    link = parser.add_mutually_exclusive_group()
    link.add_argument('--port')
    link.add_argument('--usb', action='store_true')
    parser.add_argument('--plan', choices=tuple(PLANS), default='gba',
                        help='gba: original check; slots: single cartridges; '
                             'multi: FC/GBA insertion order and remaining cartridge')
    parser.add_argument('--log', type=Path,
                        help='New JSONL file; an existing file is never overwritten')
    args = parser.parse_args()
    path = args.log or ROOT / 'logs' / (
        'status-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    print('Log:', path.resolve(), flush=True)
    with path.open('x', encoding='utf-8') as stream:
        def emit(event):
            stream.write(json.dumps(event) + '\n')
            stream.flush()
            print(json.dumps(event), flush=True)

        transport = None
        try:
            transport = TraceTransport(open_transport(args.port, args.usb), stream)
            adapter = Adapter(transport)
            emit({'event': 'product', **adapter.product()})
            emit({'event': 'plan', 'plan': args.plan,
                  'note': 'Keep USB connected. Status queries only; no ROM reads.'})
            summaries = []
            for stage, instruction in PLANS[args.plan]:
                input(instruction + '. Wait 2 seconds, then press Enter: ')
                emit({'event': 'stage', 'stage': stage})
                codes = collect(adapter, stage, emit)
                summaries.append({'event': 'summary', 'stage': stage,
                                  'codes': codes, 'stable': len(set(codes)) == 1})
            for summary in summaries:
                emit(summary)
            emit({'event': 'complete'})
        except BaseException as error:
            emit({'event': 'stopped', 'type': type(error).__name__,
                  'error': str(error)})
            raise
        finally:
            if transport is not None:
                transport.close()


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
