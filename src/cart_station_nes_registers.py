"""NES ROM register policies and complete protected bank-selection transactions.

The adapter owns wire I/O; this module owns mapper-specific address/value guards.
"""
from cart_station_protocol import write_byte


class NesRegisters:
    def __init__(self, adapter):
        self.adapter = adapter

    def write(self, mapper, address, value):
        if mapper == 5:
            self.mmc5_rom_register(address, value)
        elif mapper in (16, 159) and address < 0x8000:
            self.bandai_rom_register(mapper, address, value)
        elif mapper in (86, 93, 140, 184) and address == 0x6000:
            self.discrete_rom_register(mapper, value)
        elif mapper in (80, 82, 207, 552) and address < 0x8000:
            self.taito_rom_register(mapper, address, value)
        else:
            self.cpu_register_write(address, value)

    def mapper206_register(self, address, value):
        # Only the two register values required by Mapper 206.1 (fixed PRG) are exposed.
        if not ((address == 0x8000 and value == 2)
                or (address == 0x8001 and 0 <= value < 16)):
            raise ValueError('Register write outside the Mapper 206.1 profile')
        self.cpu_register_write(address, value)

    def cpu_register_write(self, address, value, dummy=True):
        # ROM bank registers only; no save RAM or flash programming API.
        if (type(address) is not int or not 0x8000 <= address <= 0xffff
                or type(value) is not int or not 0 <= value <= 255):
            raise ValueError('Invalid NES bank register write')
        self._nes_bank_write(address, value, dummy)

    def mapper87_register(self, address, value):
        # Mapper 87 alone decodes this lower window. Keep the general ROM
        # register guard intact; expose only its two CHR selection bits.
        if (type(address) is not int or not 0x6000 <= address <= 0x7fff
                or type(value) is not int or not 0 <= value <= 3):
            raise ValueError('Invalid Mapper 87 CHR register write')
        self._nes_bank_write(address, value, dummy=False)

    def taito_rom_register(self, mapper, address, value):
        # Exact ROM controls only. Do not expose the save-data unlock keys or
        # general lower-address writes through cpu_register_write.
        valid = (type(mapper) is int and type(address) is int
                 and type(value) is int and 0 <= value <= 255)
        if valid and mapper in (80, 207):
            valid = address in (0x7ef2, 0x7efa)
        elif valid and mapper == 82:
            valid = (address in (0x7ef2, 0x7ef3)
                     or address == 0x7efa and value % 4 == 0
                     or address == 0x7ef6 and value == 0
                     or address in (0x7ef7, 0x7ef8, 0x7ef9) and value == 255)
        elif valid and mapper == 552:
            # X1-017 "newest": same control registers as 82, but the six PRG
            # bits are not shifted, so any value 0..63 is a legal ROM bank.
            valid = (address in (0x7ef2, 0x7ef3)
                     or address == 0x7efa and value < 64
                     or address == 0x7ef6 and value == 0
                     or address in (0x7ef7, 0x7ef8, 0x7ef9) and value == 255)
        else:
            valid = False
        if not valid:
            raise ValueError('Invalid Taito ROM register write')
        # Mapper 80 uses write command 21; the others use command 18.
        write_byte(self.adapter, 0x15 if mapper == 80 else 0x12, address, value)
        self.memory(0x11, 0x8000, 8)

    def discrete_rom_register(self, mapper, value):
        # Only the 0x6000 bank values used by these four ROM scripts.
        # The generic lower-address guard and save/flash APIs stay closed.
        valid = type(mapper) is int and type(value) is int and 0 <= value <= 255
        if valid and mapper == 86:
            valid = value in (0, 16, 32, 48) or value & ~0x43 == 0
        elif valid and mapper == 140:
            valid = value < 16 or value % 16 == 0
        elif valid and mapper in (93, 184):
            valid = value <= 7
        else:
            valid = False
        if not valid:
            raise ValueError('Invalid discrete ROM bank register write')
        self._nes_bank_write(0x6000, value, dummy=True)

    def bandai_rom_register(self, mapper, address, value):
        # Only the lower ROM-bank aliases for FCG/EEPROM boards. Mapper 153
        # SRAM handling is deliberately separate and is not enabled here.
        if (type(mapper) is not int or mapper not in (16, 159)
                or type(address) is not int or type(value) is not int
                or not (address == 0x6000 and 0 <= value <= 255
                        or address == 0x6008 and 0 <= value <= 15)):
            raise ValueError('Invalid Bandai ROM register write')
        self._nes_bank_write(address, value, dummy=True)

    def sunsoft4_addon_select(self):
        # Sunsoft-4 with the external ROM: switch out the SRAM window before
        # selecting the external ROM. Keep these writes together; expose no arbitrary
        # lower-address write or save-enable value.
        self.cpu_register_write(0xf000, 0)
        self._nes_bank_write(0x6000, 1, dummy=True)

    def mapper153_select_prg(self, bank):
        # The lower alias overlaps SRAM. Save routines would finish
        # with 0x800D=0; establish that protection before EVERY ROM bank.
        if type(bank) is not int or not 0 <= bank <= 31:
            raise ValueError('Invalid Mapper 153 PRG bank')
        self.cpu_register_write(0x800d, 0)
        for address in range(0x8000, 0x8004):
            self.cpu_register_write(address, bank // 16)
        self._nes_bank_write(0x6008, bank, dummy=True)
        self.cpu_register_write(0x8008, bank)

    def nina001_rom_register(self, address, value):
        if (type(address) is not int or address not in (0x7ffd, 0x7ffe)
                or type(value) is not int
                or not (0 <= value <= (1 if address == 0x7ffd else 15))):
            raise ValueError('Invalid NINA-001 ROM register write')
        self._nes_bank_write(address, value, dummy=True)

    def mmc3_multicart_select(self, mapper, block):
        # These ROM-only multicarts repurpose $6000 as the outer-bank latch.
        # Explicitly enable its MMC3 write gate, select, then disable it.
        # No arbitrary lower address, SRAM data, or MMC3 enable API is exposed.
        if (type(mapper) is not int or type(block) is not int
                or not (mapper == 37 and block in (8, 11, 12)
                        or mapper == 47 and block in (0, 1))):
            raise ValueError('Invalid MMC3 multicart ROM block')
        self.cpu_register_write(0xa001, 128)
        self._nes_bank_write(0x6000, block, dummy=True)
        self.cpu_register_write(0xa001, 0)

    def mmc5_rom_register(self, address, value):
        # ROM controls only; never expose SRAM unlock values, ExRAM, audio,
        # or arbitrary lower-address writes. Only the <=512 KiB CHR path.
        valid = type(address) is int and type(value) is int and 0 <= value <= 255
        if valid:
            valid = (address == 0x5100 and value == 3
                     or address == 0x5101 and value == 2
                     or address in (0x5102, 0x5103, 0x5115, 0x5116, 0x5130) and value == 0
                     or address == 0x5114 and (value == 0 or value >= 128)
                     or address == 0x5117 and value == 255
                     or address == 0x5121)
        if not valid:
            raise ValueError('Invalid MMC5 ROM register write')
        self._nes_bank_write(address, value, dummy=True)

    def studybox_select_prg(self, bank):
        if type(bank) is not int or not 1 <= bank <= 255:
            raise ValueError('Invalid StudyBox PRG bank')
        self._nes_bank_write(0x4201, bank, dummy=True)

    def _nes_bank_write(self, address, value, dummy):
        self.adapter._nes_bank_write(address, value, dummy=dummy)

    def memory(self, command, address, size):
        return self.adapter.memory(command, address, size)
