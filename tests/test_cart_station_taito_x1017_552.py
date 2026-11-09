from _paths import ROOT, data  # noqa: F401
from cart_station_nes_registers import NesRegisters
import unittest
from unittest.mock import patch

from cart_station_protocol import packet
from cart_station_transport import Adapter
from cart_station_nes_db import Database, dump_profile
from test_cart_station import Wire, memory
from test_cart_station_nes_mappers import profile_for
from test_cart_station_simple_mappers import banks

PAGES = 16  # 128 KiB PRG, the size of the real X1-017 carts


def rev6(value):
    return int('{:06b}'.format(value)[::-1], 2)


class X1017Mapper552Cartridge:
    """Mesen TaitoX1017 model for the mapper-552 wiring, written independently of the reader.

    The $7efa register holds six bits; the physical 8 KiB page is the bit-reversed
    value (mapper 82 would use value >> 2). Only $7ef2 selects CHR in this wiring.
    """
    def __init__(self, prg, chr_data):
        self.prg, self.chr = prg, chr_data
        self.reg = self.chr_bank = 0
        self.locks, self.chr_mode = [0, 0, 0], -1
        self.prg_reads = self.chr_reads = 0
        self.modes = []

    def status(self):
        return 3

    def mode(self, value):
        self.modes.append(value)

    def taito_rom_register(self, mapper, address, value):
        if address in (0x7ef7, 0x7ef8, 0x7ef9):
            assert value == 255, 'save unlock must not be used'
            self.locks[address - 0x7ef7] = value
        elif address == 0x7ef6:
            self.chr_mode = value
        elif address == 0x7efa:
            assert value < 64
            self.reg = value
        elif address == 0x7ef2:
            self.chr_bank = value
        elif address == 0x7ef3:
            pass  # 82-style pair write; ignored by this wiring
        else:
            raise AssertionError('Unexpected lower write %#x' % address)

    def memory(self, command, address, size):
        if command == 0x11:
            if address == 0x8000 and size == 8:
                return self.prg[:8]
            assert address == 0x8000 and size == 8192
            self.prg_reads += 1
            page = rev6(self.reg) % PAGES
            return self.prg[page * 8192:(page + 1) * 8192]
        assert command == 0x13 and address == 0x1000 and size == 1024
        self.chr_reads += 1
        return self.chr[self.chr_bank * 1024:(self.chr_bank + 1) * 1024]

    nes = property(lambda self: self)
    write = NesRegisters.write


def make():
    prg = banks(PAGES, 8192)
    chr_data = banks(128, 1024, 43)
    return prg, chr_data


class Mapper552Tests(unittest.TestCase):
    @patch('cart_station_nes_mappers.time.sleep')
    def test_552_route_dumps_the_real_page_order(self, sleep):
        prg, chr_data = make()
        profile = profile_for(552, prg, chr_data)
        self.assertIsNone(profile.support())
        bus = X1017Mapper552Cartridge(prg, chr_data)
        self.assertEqual(dump_profile(bus, profile), profile.header + prg + chr_data)
        self.assertEqual(bus.prg_reads, 2 * PAGES)
        self.assertEqual(bus.modes, [1, 0])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_82_route_on_same_cart_gives_physical_order_not_the_real_rom(self, sleep):
        prg, chr_data = make()
        profile = profile_for(82, prg, chr_data)
        bus = X1017Mapper552Cartridge(prg, chr_data)
        # The model proves the old hack image differs; the reader's full-hash check refuses it.
        with self.assertRaises(Exception):
            dump_profile(bus, profile)
        swapped = b''.join(prg[(int('{:04b}'.format(k)[::-1], 2)) * 8192:][:8192] for k in range(PAGES))
        self.assertNotEqual(swapped, prg)
        self.assertEqual(swapped[:8192], prg[:8192])
        self.assertEqual(swapped[-8192:], prg[-8192:])

    @patch('cart_station_nes_mappers.time.sleep')
    def test_corrupted_second_read_never_returns_rom(self, sleep):
        prg, chr_data = make()
        profile = profile_for(552, prg, chr_data)
        bus = X1017Mapper552Cartridge(prg, chr_data)
        original = bus.memory

        def flaky(command, address, size):
            result = original(command, address, size)
            if command == 0x11 and size == 8192 and bus.prg_reads > PAGES:
                return bytes((result[0] ^ 1,)) + result[1:]
            return result
        bus.memory = flaky
        with self.assertRaises(Exception):
            dump_profile(bus, profile)
        self.assertEqual(bus.modes, [1, 0])

    def test_adapter_guard_for_552(self):
        for address, value in ((0x7efa, 63), (0x7ef2, 255), (0x7ef3, 0), (0x7ef6, 0),
                               (0x7ef7, 255), (0x7ef8, 255), (0x7ef9, 255)):
            wire = Wire(bytes(16) + memory(b'P' * 8), fragment=3)
            Adapter(wire).nes.taito_rom_register(552, address, value)
            self.assertEqual(wire.writes[0], packet(0x12, 8, 1, address, 1))
        for address, value in ((0x7efa, 64), (0x7ef6, 1), (0x7ef7, 202), (0x7ef8, 105),
                               (0x7ef9, 132), (0x7ef7, 0), (0x6000, 0), (0x7f00, 0), (0x8000, 0)):
            wire = Wire(b'')
            with self.subTest(address=address, value=value), self.assertRaises(ValueError):
                Adapter(wire).nes.taito_rom_register(552, address, value)
            self.assertFalse(wire.writes)

    def test_bundled_database_has_four_real_552_roms_and_no_hack_images(self):
        db = Database.load(data('nes_profiles.reference.json'))
        real = [p for p in db.profiles if p.mapper == 552]
        self.assertEqual(sorted(p.title for p in real), sorted([
            'Kyuukyoku Harikiri Koushien (Japan).nes', 'Kyuukyoku Harikiri Stadium - Heisei Gannen Ban (Japan).nes',
            'Kyuukyoku Harikiri Stadium III (Japan).nes', 'SD Keiji - Blader (Japan).nes']))
        self.assertTrue(all(p.support() is None and p.submapper == 0 and p.prg_size == 131072 for p in real))
        self.assertFalse([p for p in db.profiles if p.mapper == 82])
        self.assertEqual(len({p.raw_sha1 for p in real}), 4)


if __name__ == '__main__':
    unittest.main()
