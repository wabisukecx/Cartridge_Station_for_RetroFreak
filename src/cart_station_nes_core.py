"""NES fixed-layout readers."""
import time
from cart_station_protocol import ProtocolError, cleanup_after


def dump(adapter, prg_kib, chr_kib, mirroring, battery=False, progress=None):
    if prg_kib not in (16, 32) or chr_kib not in (0, 8) or mirroring not in ('horizontal', 'vertical'):
        raise ValueError('Unsupported NROM layout')
    if adapter.status() != 3:
        raise ProtocolError('Insert exactly one NES/FC cartridge')
    with cleanup_after(adapter.mode, 0):
        adapter.mode(1)
        time.sleep(0.2)
        if progress:
            total = (prg_kib + chr_kib) * 1024
            progress('Read 1/2', 0, total * 2)
            first = adapter.nrom(prg_kib, chr_kib,
                                 lambda done, size: progress('Read 1/2', done, total * 2))
            second = adapter.nrom(prg_kib, chr_kib,
                                  lambda done, size: progress('Read 2/2', total + done, total * 2))
        else:
            first = adapter.nrom(prg_kib, chr_kib)
            second = adapter.nrom(prg_kib, chr_kib)
        if first != second:
            raise ProtocolError('Two ROM reads differ; no ROM will be saved')
    flags = (mirroring == 'vertical') | (int(battery) << 1)
    return b'NES\x1a' + bytes((prg_kib // 16, chr_kib // 8, flags, 0)) + bytes(8) + first

