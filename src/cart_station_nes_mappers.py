"""ROM-only mapper readers; no save/flash writes.

Unknown submappers and geometries remain disabled. No save or flash writes.
"""
import hashlib
import time
import zlib

from cart_station_protocol import ProtocolError, verify_sha1, cleanup_after
from cart_station_banks import read_banks


# ROM-only address wiring from the eight VRC2/VRC4 reference classes.
# VRC2a (mapper 22) has its separate shifted CHR numbering below.
VRC_CHR_HIGH = {
    (21, 1): 0xb002, (21, 2): 0xb040,
    (23, 1): 0xb001, (23, 2): 0xb004, (23, 3): 0xb001,
    (25, 1): 0xb002, (25, 2): 0xb008, (25, 3): 0xb002,
}


# CHR unlock writes per PRG CRC32, from publicly documented Mapper 185 values
# (gamebank.jp dumper mapper information), plus the PRG1 revision 49FF2E0B
# (NESdev CNROM: Mighty Bomb Jack PRG1 uses 00 -> 11).
# The second pair for 9CF17FAB is intentionally kept as is: it overwrites value2.
CNROM_CP_UNLOCK = {
    '2A629F7D': ((240, 15),), '725D5CC4': ((0, 51),),
    'AA755715': ((0, 17),), '87A3F91E': ((32, 34),),
    'DAADEBFA': ((32, 34),), '81A743F5': ((32, 34),),
    '73DE64AD': ((0, 42),), 'B36457C7': ((19, 33),),
    'A03C98A7': ((33, 32),), '9CF17FAB': ((16, 18), (0, 19)),
    '49FF2E0B': ((0, 17),),
}


class RomMismatch(ProtocolError):
    pass


# ROM geometry readable per mapper, or per (mapper, submapper) when it differs:
# (PRG step, PRG min, PRG max, CHR step, CHR min, CHR max) in bytes. Ranges follow
# the bank registers of each board, so they are hardware limits, not policy.
_K = 1024
GEOMETRY = {
    1: (16 * _K, 32 * _K, 512 * _K, 4 * _K, 0, 128 * _K),
    155: (16 * _K, 32 * _K, 512 * _K, 4 * _K, 0, 128 * _K),
    2: (16 * _K, 16 * _K, 4096 * _K, 1, 0, 0),
    3: (16 * _K, 16 * _K, 32 * _K, 8 * _K, 8 * _K, 2048 * _K),
    4: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 0, 256 * _K),
    5: (8 * _K, 32 * _K, 1024 * _K, 2 * _K, 8 * _K, 512 * _K),
    7: (32 * _K, 32 * _K, 8192 * _K, 1, 0, 0),
    9: (8 * _K, 32 * _K, 2048 * _K, 4 * _K, 8 * _K, 1024 * _K),
    10: (16 * _K, 32 * _K, 4096 * _K, 4 * _K, 8 * _K, 1024 * _K),
    11: (32 * _K, 32 * _K, 128 * _K, 8 * _K, 8 * _K, 128 * _K),
    13: (32 * _K, 32 * _K, 32 * _K, 1, 0, 0),
    16: (16 * _K, 16 * _K, 256 * _K, 1 * _K, 8 * _K, 256 * _K),
    159: (16 * _K, 16 * _K, 256 * _K, 1 * _K, 8 * _K, 256 * _K),
    18: (8 * _K, 32 * _K, 512 * _K, 1 * _K, 8 * _K, 256 * _K),
    19: (8 * _K, 32 * _K, 512 * _K, 1 * _K, 8 * _K, 256 * _K),
    210: (8 * _K, 32 * _K, 512 * _K, 1 * _K, 8 * _K, 256 * _K),
    21: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 512 * _K),
    (23, 3): (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    23: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 512 * _K),
    (25, 3): (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    25: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 512 * _K),
    22: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    64: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    158: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 128 * _K),
    24: (16 * _K, 32 * _K, 4096 * _K, 1 * _K, 8 * _K, 256 * _K),
    26: (16 * _K, 32 * _K, 4096 * _K, 1 * _K, 8 * _K, 256 * _K),
    30: (16 * _K, 16 * _K, 512 * _K, 1, 0, 0),
    # G-101 submapper 1 fixes the PRG layout as 8+8+16F with five bank bits.
    (32, 1): (8 * _K, 32 * _K, 256 * _K, 1 * _K, 8 * _K, 256 * _K),
    32: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    33: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    48: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    65: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    # NINA-001 (submapper 1); BNROM follows.
    (34, 1): (32 * _K, 32 * _K, 64 * _K, 4 * _K, 8 * _K, 64 * _K),
    34: (32 * _K, 32 * _K, 8192 * _K, 1, 0, 0),
    66: (32 * _K, 32 * _K, 512 * _K, 8 * _K, 8 * _K, 128 * _K),
    67: (16 * _K, 16 * _K, 4096 * _K, 2 * _K, 8 * _K, 512 * _K),
    # Sunsoft-4 with the external ROM: PRG is exactly 128 or 144 KiB (checked above).
    (68, 1): (16 * _K, 16 * _K, 256 * _K, 2 * _K, 8 * _K, 512 * _K),
    68: (16 * _K, 16 * _K, 256 * _K, 2 * _K, 8 * _K, 512 * _K),
    69: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    70: (16 * _K, 16 * _K, 256 * _K, 8 * _K, 8 * _K, 128 * _K),
    71: (16 * _K, 16 * _K, 4096 * _K, 1, 0, 0),
    97: (16 * _K, 16 * _K, 4096 * _K, 1, 0, 0),
    72: (16 * _K, 16 * _K, 2048 * _K, 8 * _K, 8 * _K, 512 * _K),
    92: (16 * _K, 16 * _K, 2048 * _K, 8 * _K, 8 * _K, 512 * _K),
    73: (16 * _K, 16 * _K, 4096 * _K, 1, 0, 0),
    75: (8 * _K, 32 * _K, 2048 * _K, 4 * _K, 8 * _K, 128 * _K),
    76: (8 * _K, 32 * _K, 2048 * _K, 2 * _K, 8 * _K, 512 * _K),
    77: (32 * _K, 32 * _K, 512 * _K, 2 * _K, 8 * _K, 32 * _K),
    78: (16 * _K, 16 * _K, 128 * _K, 8 * _K, 8 * _K, 128 * _K),
    89: (16 * _K, 16 * _K, 128 * _K, 8 * _K, 8 * _K, 128 * _K),
    152: (16 * _K, 16 * _K, 128 * _K, 8 * _K, 8 * _K, 128 * _K),
    80: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    207: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    # Taito X1-017 mappers 82 and 552 decode a six-bit PRG register (64 x 8 KiB).
    82: (8 * _K, 32 * _K, 512 * _K, 1 * _K, 8 * _K, 256 * _K),
    552: (8 * _K, 32 * _K, 512 * _K, 1 * _K, 8 * _K, 256 * _K),
    85: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    86: (32 * _K, 32 * _K, 128 * _K, 8 * _K, 8 * _K, 64 * _K),
    87: (16 * _K, 16 * _K, 32 * _K, 16 * _K, 16 * _K, 32 * _K),
    88: (8 * _K, 32 * _K, 2048 * _K, 2 * _K, 8 * _K, 128 * _K),
    154: (8 * _K, 32 * _K, 2048 * _K, 2 * _K, 8 * _K, 128 * _K),
    93: (16 * _K, 16 * _K, 128 * _K, 1, 0, 0),
    94: (16 * _K, 16 * _K, 1024 * _K, 1, 0, 0),
    95: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    96: (32 * _K, 32 * _K, 128 * _K, 1, 0, 0),
    118: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 128 * _K),
    119: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 64 * _K),
    140: (32 * _K, 32 * _K, 512 * _K, 8 * _K, 8 * _K, 128 * _K),
    148: (32 * _K, 32 * _K, 64 * _K, 8 * _K, 8 * _K, 64 * _K),
    153: (16 * _K, 32 * _K, 512 * _K, 1, 0, 0),
    157: (16 * _K, 16 * _K, 4096 * _K, 1, 0, 0),
    180: (16 * _K, 16 * _K, 4096 * _K, 1, 0, 0),
    184: (16 * _K, 16 * _K, 32 * _K, 4 * _K, 8 * _K, 32 * _K),
    186: (16 * _K, 16 * _K, 4096 * _K, 1, 0, 0),
    188: (16 * _K, 16 * _K, 256 * _K, 1, 0, 0),
    # Mapper 206 submapper 1: fixed 32 KiB PRG.
    (206, 1): (8 * _K, 32 * _K, 32 * _K, 1 * _K, 8 * _K, 256 * _K),
    206: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
    406: (8 * _K, 32 * _K, 2048 * _K, 1 * _K, 8 * _K, 256 * _K),
}


def limitation(profile):
    allowed = ((4, 5, 6, 7) if profile.mapper == 185 else
               (4, 5) if profile.mapper == 16 else
               (1, 2) if profile.mapper == 21 else
               (1, 2, 3) if profile.mapper in (23, 25) else
               (0, 5) if profile.mapper == 1 else
               (0, 1, 3) if profile.mapper == 4 else
               (0, 1, 2) if profile.mapper in (2, 3, 7) else
               (0, 1, 2) if profile.mapper == 34 else
               (0, 1, 3) if profile.mapper == 78 else
               (0, 2, 3, 4, 5) if profile.mapper == 19 else
               (0, 1, 2) if profile.mapper == 85 else
               (0, 1) if profile.mapper in (32, 48, 68, 206) else
               (0, 1, 2) if profile.mapper == 210 else (0,))
    if profile.submapper not in allowed:
        return 'This mapper subvariant has not been ported'
    prg, chr_size = profile.prg_size, profile.chr_size
    mapper, submapper = profile.mapper, profile.submapper
    if mapper == 1 and submapper == 5:
        # SEROM/SHROM/SH1ROM have a physically fixed 32 KiB PRG window.
        # Keep CHR-ROM geometries explicit; this is not a CHR-RAM board.
        valid = prg == 32768 and chr_size in (8192, 16384, 32768, 65536, 131072)
    elif mapper in (37, 47):
        # Only the known ROM-only 256+256 KiB boards. Do not route a RAM-
        # bearing profile through the outer latch at $6000.
        valid = (prg == 262144 and chr_size == 262144
                 and not profile.header[6] & 2 and profile.header[10] == 0)
    elif mapper == 185:
        pairs = CNROM_CP_UNLOCK.get(profile.prg_crc32)
        if pairs is None:
            return 'CNROM-CP PRG CRC32 is missing or its unlock sequence has not been ported'
        if profile.prg_crc32 == '49FF2E0B':
            # Additional revision. Enable only its
            # reviewed NES 2.0 CS=1 layout, not an unknown-CRC fallback.
            valid = submapper == 5 and prg == 32768 and chr_size == 8192
        else:
            valid = prg in (16384, 32768) and chr_size == 8192 * len(pairs)
    else:
        geometry = GEOMETRY.get((mapper, submapper), GEOMETRY.get(mapper))
        if geometry is None:
            valid = False
        else:
            prg_step, prg_low, prg_high, chr_step, chr_low, chr_high = geometry
            if mapper == 68 and submapper == 1:
                valid_prg = prg in (131072, 147456)
            else:
                valid_prg = prg % prg_step == 0 and prg_low <= prg <= prg_high
            valid_chr = chr_size % chr_step == 0 and chr_low <= chr_size <= chr_high
            # VRC7 boards may have CHR RAM only, which is never read.
            valid = valid_prg and (valid_chr or mapper == 85 and chr_size == 0)
    return None if valid else 'Mapper ROM geometry has not been ported'


class MapperReader:
    def __init__(self, adapter, profile, chr_bank=4096, prg_offset=0, candidates=None):
        self.adapter, self.profile = adapter, profile
        self.chr_bank, self.prg_offset = chr_bank, prg_offset
        # Optional group of same-geometry profiles; the hashes of the data
        # actually read decide which one it is. `matched` is set by read().
        self.candidates = tuple(candidates) if candidates else None
        self.matched = None
        self._mapper87_prg_verified = False
        self._mapper184_prg_verified = False
        self._cnrom_cp_crc = None
        self.register_variant = 'AXR'
        # Enabled only inside one complete Mapper 2/3 read; never shared by passes.
        self._fixed_search_blocks = self._fixed_search_addresses = None

    @property
    def registers(self):
        return self.adapter.nes

    def cpu(self, address, size):
        return self.adapter.memory(0x11, address, size)

    def ppu(self, address, size):
        return self.adapter.memory(0x13, address, size)

    def write(self, address, value):
        self.registers.write(self.profile.mapper, address, value)

    def serial(self, address, value):
        # Write the shifted low five bits, not only a 0/1 byte.
        for bit in range(5):
            self.write(address, (value & 31) >> bit)

    def initialize(self):
        self._fixed_search_blocks = self._fixed_search_addresses = None
        if self.profile.mapper == 5:
            # SRAM write protection before any ROM read.
            self.write(0x5102, 0)
            self.write(0x5103, 0)
            # Use PRG mode 3: mode 0 (32 KiB) would not suit selecting
            # $5114 and reading 8 KiB; that register works only in mode 3.
            for address, value in ((0x5100, 3), (0x5101, 2), (0x5114, 0),
                                   (0x5115, 0), (0x5116, 0), (0x5117, 255)):
                self.write(address, value)
        if self.profile.mapper in (82, 552):
            for address in (0x7ef7, 0x7ef8, 0x7ef9):
                self.write(address, 255)
            self.write(0x7ef6, 0)
        if self.profile.mapper in (1, 155):
            for value in range(8):
                self.write(0x8000, 0x80 | value)
            self.serial(0x8000, 28)
            self.serial(0xa000, 0)
            self.serial(0xc000, 0)
            if self.profile.submapper != 5:
                self.serial(0xe000, 16)

    def search_write(self, start, end, value):
        # Bus-conflict write: find the value in ROM (inclusive 1 KiB blocks,
        # fallback to start), write there without a dummy read, then wait 2 ms.
        if self.profile.mapper == 87 and (
                not self._mapper87_prg_verified or not 0x6000 <= start <= end <= 0x7fff):
            raise ProtocolError("Mapper 87 needs verified fixed PRG before CHR selection")
        value &= 255
        # Only these windows are invariant under the mapper's bank writes.
        # Other mappers can switch PRG in the search window and must reread it.
        cached = (self._fixed_search_blocks is not None and
                  ((self.profile.mapper == 2 and (start, end) == (0xc000, 0xffff)) or
                   (self.profile.mapper == 3 and (start, end) == (0x8000, 0xffff))))
        key = (start, end, value)
        if cached and key in self._fixed_search_addresses:
            address = self._fixed_search_addresses[key]
        else:
            address = start
            for offset in range(start, end + 1, 1024):
                size = min(1024, end + 1 - offset)
                block = (offset, size)
                if cached and block in self._fixed_search_blocks:
                    data = self._fixed_search_blocks[block]
                else:
                    data = self.cpu(offset, size)
                    if cached and len(data) != size:
                        raise ProtocolError('Incomplete bus-conflict search block')
                    if cached:
                        self._fixed_search_blocks[block] = data
                found = data.find(bytes((value,)))
                if found >= 0:
                    address = offset + found
                    break
            # Fall back to the default start address when none is cached.
            if cached:
                self._fixed_search_addresses[key] = address
        if self.profile.mapper == 87:
            self.registers.mapper87_register(address, value)
        else:
            self.registers.cpu_register_write(address, value, dummy=False)
        time.sleep(0.002)

    def mmc3_flash_registers(self):
        if self.register_variant == 'AXR':
            return 0x8000, 0x8002
        if self.register_variant == 'INL':
            return 0xe000, 0xe001
        raise ProtocolError('Unknown Mapper 406 register variant')

    def prg_bank(self, bank, size):
        mapper = self.profile.mapper
        if mapper == 206 and self.profile.submapper == 1:
            # Fixed 32 KiB PRG: never write PRG registers.
            return self.cpu(0x8000 if bank == 0 else 0xc000, size)
        if mapper in (1, 155):
            if self.profile.submapper == 5:
                # PRG address lines are wired directly on these boards. Read
                # both halves in place; do not try to switch a PRG bank.
                return self.cpu(0x8000 + bank * 16384, size)
            if bank == self.profile.prg_size // 16384 - 1:
                return self.cpu(0xc000, size)
            number = bank + self.prg_offset
            self.serial(0xa000, number & 16)
            self.serial(0xc000, number & 16)
            self.serial(0xe000, (number & 15) | 16)
        elif mapper in (2, 30):
            self.search_write(0xc000, 0xffff, bank)
        elif mapper in (3, 13, 87, 184, 185):
            return self.cpu(0x8000 if bank == 0 else 0xc000, size)
        elif mapper == 5:
            self.write(0x5114, bank | 0x80)
        elif mapper in (4, 118, 119):
            self.write(0x8000, 6)
            self.write(0x8001, bank)
        elif mapper in (37, 47):
            if mapper == 37:
                block, number = ((8, bank) if bank < 8 else
                                 (11, bank - 8) if bank < 16 else (12, bank - 16))
            else:
                # Outer PRG block size: each outer PRG block
                # contains sixteen 8 KiB banks (128 KiB).
                block, number = int(bank >= 16), bank
            self.registers.mmc3_multicart_select(mapper, block)
            self.write(0x8000, 6)
            self.write(0x8001, number)
        elif mapper == 34 and self.profile.submapper == 1:
            self.registers.nina001_rom_register(0x7ffd, bank)
        elif mapper in (7, 11, 34, 66):
            value = bank if mapper in (7, 34) else bank & 3 if mapper == 11 else bank << 4
            self.search_write(0x8000, 0xffff, value)
        elif mapper == 77:
            self.search_write(0x8000, 0xffff, bank & 15)
        elif mapper in (9, 10):
            self.write(0xa000, bank)
        elif mapper == 18:
            self.write(0x8000, bank & 15)
            self.write(0x8001, (bank >> 4) & 3)
        elif mapper == 19:
            self.write(0xe800, 0xc0 | bank)
            self.write(0xe000, bank)
            return self.cpu(0xa000, size)
        elif mapper in (21, 22, 23, 25):
            self.write(0xa000, bank)
            return self.cpu(0xa000, size)
        elif mapper in (24, 26):
            self.write(0x8000, bank)
        elif mapper in (64, 158):
            self.write(0x8000, 6)
            self.write(0x8001, bank)
        elif mapper == 32:
            self.write(0x9000, 0)
            self.write(0x8000, bank)
        elif mapper in (33, 48, 75, 85):
            self.write(0x8000, bank)
        elif mapper == 65:
            self.write(0xa000, bank)
            return self.cpu(0xa000, size)
        elif mapper == 69:
            self.write(0x8000, 9)
            self.write(0xa000, bank)
        elif mapper == 68 and self.profile.submapper == 1:
            if bank < 8:
                self.write(0xf000, 8 | bank)
            else:
                self.registers.sunsoft4_addon_select()
        elif mapper in (67, 68):
            self.write(0xf800 if mapper == 67 else 0xf000, bank)
        elif mapper in (76, 88, 95, 154, 206):
            if mapper in (88, 95, 154, 206):
                self.write(0xa001, 0)
            self.write(0x8000, 6)
            self.write(0x8001, bank)
        elif mapper == 210:
            self.write(0xe000, bank & 63)
        elif mapper == 73:
            self.write(0xf000, bank)
        elif mapper in (72, 92):
            self.search_write(0x8000, 0xffff, bank & 127)
            self.search_write(0x8000, 0xffff, (bank & 127) | 128)
            return self.cpu(0x8000 if mapper == 72 else 0xc000, size)
        elif mapper == 70:
            self.search_write(0x8000, 0xffff, (bank & 15) << 4)
        elif mapper == 71:
            self.write(0xc000, bank)
        elif mapper == 78:
            self.search_write(0x8000, 0xffff, bank & 7)
        elif mapper in (89, 152):
            self.search_write(0x8000, 0xffff, (bank & 7) << 4)
        elif mapper == 94:
            self.write(0x8000, bank << 2)
        elif mapper == 97:
            self.write(0x8000, bank)
            return self.cpu(0xc000, size)
        elif mapper == 552:
            # The six PRG register bits are wired in reverse order
            # (Taito X1-017 as mapper 552; see NESdev Wiki, mappers 82/552).
            if not 0 <= bank < 64:
                raise ProtocolError('Mapper 552 PRG bank outside the 6-bit register')
            self.write(0x7efa, int('{:06b}'.format(bank)[::-1], 2))
        elif mapper in (80, 82, 207):
            self.write(0x7efa, (bank << 2) if mapper == 82 else bank)
        elif mapper in (86, 140):
            self.write(0x6000, ((bank & 3) if mapper == 86 else bank) << 4)
        elif mapper == 93:
            self.write(0x6000, bank)
            self.search_write(0xd000, 0xffff, (bank & 7) << 4)
        elif mapper == 96:
            self.search_write(0x8000, 0xffff, bank & 3)
        elif mapper == 148:
            self.write(0x8000, (bank & 1) << 3)
        elif mapper in (16, 159):
            for address in range(0x8000, 0x8004):
                self.write(address, bank // 16)
            self.write(0x6008, bank)
            self.write(0x8008, bank)
        elif mapper == 153:
            self.registers.mapper153_select_prg(bank)
        elif mapper == 157:
            for address in range(0x8000, 0x8004):
                self.write(address, bank // 16)
            self.write(0x8008, bank)
        elif mapper == 186:
            if bank == 0:
                return self.cpu(0xc000, size)
            self.registers.studybox_select_prg(bank)
        elif mapper == 406:
            selector, register = self.mmc3_flash_registers()
            self.write(selector, 6)
            self.write(register, bank)
        elif mapper == 188:
            number = bank & 15
            self.search_write(0x8000, 0xffff, number - 8 if number >= 8 else number | 16)
        elif mapper == 180:
            self.search_write(0x8000, 0xffff, bank)
            return self.cpu(0xc000, size)
        return self.cpu(0x8000, size)

    def chr_rom_bank(self, bank, size):
        mapper = self.profile.mapper
        if mapper == 5:
            self.write(0x5130, 0)
            self.write(0x5121, bank)
        elif mapper in (1, 155):
            self.serial(0xa000, bank & 31)
        elif mapper == 3:
            self.search_write(0x8000, 0xffff, (bank | 0xfc) if self.profile.chr_size <= 32768 else bank)
        elif mapper in (4, 118, 119):
            self.write(0x8000, 2)
            self.write(0x8001, bank)
            return self.ppu(0x1000, size)
        elif mapper in (37, 47):
            if mapper == 37:
                block, number = (8, bank) if bank < 128 else (12, bank - 128)
            else:
                # Each outer CHR block contains 128 one-KiB banks.
                block, number = int(bank >= 128), bank
            self.registers.mmc3_multicart_select(mapper, block)
            self.write(0x8000, 2)
            self.write(0x8001, number)
            return self.ppu(0x1000, size)
        elif mapper in (80, 82, 207, 552):
            self.write(0x7ef2, bank)
            if mapper == 82:
                self.write(0x7ef3, (bank + 1) & 255)
            return self.ppu(0x1000, size)
        elif mapper == 34 and self.profile.submapper == 1:
            self.registers.nina001_rom_register(0x7ffe, bank)
        elif mapper == 406:
            selector, register = self.mmc3_flash_registers()
            self.write(selector, 2)
            self.write(register, bank)
            return self.ppu(0x1000, size)
        elif mapper == 77:
            self.search_write(0x8000, 0xffff, (bank << 4) & 0xf0)
        elif mapper == 86:
            self.write(0x6000, ((bank & 4) << 4) | (bank & 3))
        elif mapper == 140:
            self.write(0x6000, bank & 15)
        elif mapper in (16, 159):
            self.write(0x6000, bank)
            self.write(0x8000, bank)
        elif mapper == 185:
            pairs = CNROM_CP_UNLOCK.get(self._cnrom_cp_crc)
            if pairs is None or not 0 <= bank < len(pairs):
                raise ProtocolError('CNROM-CP needs verified fixed PRG and a known CRC32')
            for value in pairs[bank]:
                self.search_write(0x8000, 0xffff, value)
        elif mapper == 184:
            if not self._mapper184_prg_verified:
                raise ProtocolError("Mapper 184 needs verified fixed PRG before CHR selection")
            self.write(0x6000, bank)
        elif mapper == 148:
            self.write(0x8000, bank & 7)
        elif mapper in (11, 66):
            value = (bank & 15) << 4 if mapper == 11 else bank & 15
            self.search_write(0x8000, 0xffff, value)
        elif mapper in (72, 92):
            self.search_write(0x8000, 0xffff, bank & 63)
            self.search_write(0x8000, 0xffff, (bank & 63) | 64)
        elif mapper == 210:
            self.write(0x8000, bank)
        elif mapper == 70:
            self.search_write(0x8000, 0xffff, bank & 15)
        elif mapper == 78:
            self.search_write(0x8000, 0xffff, (bank & 15) << 4)
        elif mapper == 89:
            # CHR bank bit 3 moves to register bit 7.
            self.search_write(0x8000, 0xffff, ((bank & 8) << 4) | (bank & 7))
        elif mapper == 152:
            self.search_write(0x8000, 0xffff, bank & 15)
        elif mapper in (9, 10):
            # Reading 0x0000..0x0FFF can switch the PPU latch. Program both
            # FD and FE registers to the same bank before every 4 KiB read.
            self.write(0xb000, bank)
            self.write(0xc000, bank)
        elif mapper in (32, 65):
            self.write(0xb000, bank)
        elif mapper == 18:
            self.write(0xa000, bank & 15)
            self.write(0xa001, (bank >> 4) & 15)
        elif mapper in (21, 23, 25):
            self.write(0xb000, bank & 15)
            self.write(VRC_CHR_HIGH[(mapper, self.profile.submapper)], (bank >> 4) & 31)
        elif mapper == 22:
            number = bank << 1
            self.write(0xb000, number & 15)
            self.write(0xb002, (number >> 4) & 31)
        elif mapper in (24, 26):
            self.write(0xb003, 0)
            self.write(0xd000, bank)
        elif mapper in (64, 158):
            self.write(0x8000, 32)
            self.write(0x8001, bank)
        elif mapper == 19:
            self.write(0xe800, 0xc0)
            self.write(0x8000, bank)
        elif mapper == 69:
            self.write(0x8000, 0)
            self.write(0xa000, bank)
        elif mapper == 85:
            self.write(0xa000, bank)
        elif mapper in (67, 68):
            self.write(0x8800 if mapper == 67 else 0x8000, bank)
        elif mapper == 76:
            self.write(0x8000, 2)
            self.write(0x8001, bank)
        elif mapper in (88, 154):
            if bank < 32:
                self.write(0x8000, 0)
                self.write(0x8001, bank * 2)
            else:
                self.write(0x8000, 2)
                self.write(0x8001, bank * 2)
                self.write(0x8000, 3)
                self.write(0x8001, bank * 2 + 1)
                return self.ppu(0x1000, size)
        elif mapper in (95, 206):
            self.write(0x8000, 2)
            self.write(0x8001, bank)
            return self.ppu(0x1000, size)
        elif mapper in (33, 48):
            self.write(0xa000, bank)
            return self.ppu(0x1000, size)
        elif mapper == 75:
            self.write(0x9000, (bank & 16) >> 3)
            self.write(0xe000, bank & 15)
        elif mapper == 87:
            if not self._mapper87_prg_verified:
                raise ProtocolError('Mapper 87 needs verified fixed PRG before CHR selection')
            value = ((bank >> 1) & 1) | ((bank << 1) & 2)
            self.search_write(0x6000, 0x7fff, value)
        return self.ppu(0, size)

    def read(self, progress=None):
        try:
            return self._read(progress)
        finally:
            # Also discard on cancellation, hash mismatch or a transfer failure.
            self._fixed_search_blocks = self._fixed_search_addresses = None

    def _read(self, progress=None):
        self._mapper87_prg_verified = False
        self._mapper184_prg_verified = False
        self._cnrom_cp_crc = None
        if progress:
            progress(0, self.profile.prg_size + self.profile.chr_size)
        self.initialize()
        if self.profile.mapper in (2, 3):
            self._fixed_search_blocks, self._fixed_search_addresses = {}, {}
        prg_bank = (16384 if self.profile.mapper == 206 and self.profile.submapper == 1 else
                    8192 if self.profile.mapper in (4, 5, 9, 18, 19, 21, 22, 23, 25, 32, 33, 37, 47,
                                                   48, 64, 65, 69, 75, 76, 80, 82, 85, 88, 95,
                                                   118, 119, 154, 158, 206, 207, 210, 406, 552) else
                    32768 if self.profile.mapper in (7, 11, 34, 66, 77, 86, 96, 140, 148) else 16384)
        chr_bank = {1: self.chr_bank, 2: 8192, 3: 8192, 4: 1024, 5: 2048, 37: 1024, 47: 1024,
                    7: 2048, 9: 4096, 10: 4096,
                    11: 8192, 18: 1024, 19: 1024, 32: 1024, 33: 1024, 34: 4096 if self.profile.submapper == 1 else 8192,
                    65: 1024, 66: 8192, 67: 2048, 68: 2048, 69: 1024, 70: 8192,
                    71: 8192, 75: 4096, 76: 2048, 78: 8192, 85: 1024,
                    87: 8192, 88: 2048, 89: 8192, 94: 8192, 95: 1024,
                    97: 8192, 118: 1024, 119: 1024, 152: 8192,
                    154: 2048, 180: 8192, 206: 1024,
                    48: 1024, 72: 8192, 73: 8192, 92: 8192, 210: 1024,
                    21: 1024, 22: 1024, 23: 1024, 24: 1024, 25: 1024,
                    26: 1024, 64: 1024, 158: 1024,
                    96: 4096, 148: 8192, 155: 4096, 157: 1024, 188: 8192,
                    80: 1024, 82: 1024, 207: 1024, 552: 1024,
                    86: 8192, 93: 8192, 140: 8192, 184: 4096,
                    16: 1024, 159: 1024, 13: 4096, 30: 8192, 77: 2048, 185: 8192, 153: 1024, 186: 8192, 406: 1024}[self.profile.mapper]
        total = self.profile.prg_size + self.profile.chr_size
        prg = read_banks(self.prg_bank, self.profile.prg_size, prg_bank,
                         (lambda done, size: progress(done, total)) if progress else None)
        alive = self.candidates or (self.profile,)
        alive = self.narrow(alive, prg, 'prg_sha1', 'Mapper PRG')
        if self.profile.mapper == 87:
            self._mapper87_prg_verified = True
        if self.profile.mapper == 184:
            self._mapper184_prg_verified = True
        if self.profile.mapper == 185:
            actual_crc = '%08X' % (zlib.crc32(prg) & 0xffffffff)
            alive = tuple(item for item in alive if item.prg_crc32 == actual_crc)
            if not alive or actual_crc not in CNROM_CP_UNLOCK:
                raise RomMismatch('CNROM-CP PRG CRC32 mismatch or unknown unlock sequence')
            self._cnrom_cp_crc = actual_crc
        chr_data = read_banks(self.chr_rom_bank, self.profile.chr_size, chr_bank,
                              (lambda done, size: progress(self.profile.prg_size + done, total)) if progress else None)
        if self.profile.chr_size:
            alive = self.narrow(alive, chr_data, 'chr_sha1', 'Mapper CHR')
        alive = self.narrow(alive, prg + chr_data, 'raw_sha1', 'Mapper PRG+CHR')
        if len(alive) != 1:
            raise ProtocolError('Hashes match several DB profiles; no ROM will be saved')
        self.matched = alive[0]
        return prg + chr_data

    @classmethod
    def narrow(cls, profiles, data, field, label):
        value = hashlib.sha1(data).hexdigest()
        alive = tuple(item for item in profiles if getattr(item, field) == value)
        if not alive:
            raise RomMismatch(label + ' hash mismatch')
        return alive

    @staticmethod
    def verify(data, expected, label):
        if hashlib.sha1(data).hexdigest() != expected:
            raise RomMismatch(label + ' hash mismatch')


def dump_mapped(adapter, profile, progress=None):
    return _dump_variants(adapter, (profile,), progress)[1]


def dump_mapped_group(adapter, profiles, progress=None):
    """Read once and let full hashes pick the profile among same-geometry ones.

    Returns (profile, data). Nothing is returned unless PRG, CHR, PRG+CHR and
    the optional file hash of exactly one profile match two identical reads.
    """
    profiles = tuple(profiles)
    if len(profiles) < 2 or len({(p.mapper, p.submapper, p.prg_size, p.chr_size) for p in profiles}) != 1:
        raise ProtocolError('Auto-detection needs several candidates with one ROM geometry')
    return _dump_variants(adapter, profiles, progress)


def _dump_variants(adapter, profiles, progress):
    profile = profiles[0]
    for item in profiles:
        reason = limitation(item)
        if reason:
            raise ProtocolError(reason)
    # MMC1 and Mapper 406 variants share mapper numbers. Use full hashes to choose;
    # Only a first-read hash mismatch can reject a variant. Once it matches,
    # any second-read failure is fatal; never retry transport failures either.
    variants = [(4096, 0)]
    if profile.mapper == 1 and profile.submapper == 0:
        if profile.chr_size == 8192:
            variants.append((8192, 0))
        if profile.prg_size == 65536:
            variants.append((4096, 4))
    if profile.mapper == 406:
        variants = [(4096, 0), (4096, 0)]
    last_error = None
    for variant_index, (chr_bank, prg_offset) in enumerate(variants):
        if adapter.status() != 3:
            raise ProtocolError('Insert one FC/NES cartridge')
        with cleanup_after(adapter.mode, 0):
            adapter.mode(1)
            time.sleep(0.2)
            if len(profiles) == 1:
                reader = MapperReader(adapter, profile, chr_bank, prg_offset)
            else:
                reader = MapperReader(adapter, profile, chr_bank, prg_offset, candidates=profiles)
            if profile.mapper == 406:
                reader.register_variant = ('AXR', 'INL')[variant_index]
            try:
                if progress:
                    total = profile.prg_size + profile.chr_size
                    first = reader.read(lambda done, size: progress('Read 1/2', done, total * 2))
                else:
                    first = reader.read()
            except RomMismatch as error:
                last_error = error
                continue
            matched = getattr(reader, 'matched', None) or profile
            try:
                if progress:
                    second = reader.read(lambda done, size: progress('Read 2/2', total + done, total * 2))
                else:
                    second = reader.read()
            except RomMismatch as error:
                raise ProtocolError('Two mapper ROM reads differ; no ROM will be saved: %s' % error) from error
            if first != second:
                raise ProtocolError('Two mapper ROM reads differ; no ROM will be saved')
            result = matched.header + first
            if matched.file_sha1:
                verify_sha1(result, matched.file_sha1, 'DB completed file')
            return matched, result
    raise ProtocolError('No mapper variant matched the complete ROM hashes: %s' % last_error)
