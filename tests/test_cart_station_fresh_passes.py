"""Pass boundaries must reject changed cartridges before reading or saving."""
from _paths import ROOT  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cart_station_gba import GbaDatabase, dump_gba
from cart_station_nes_db import Database
from cart_station_protocol import ProtocolError
from cart_station_service import Service, UserCancelled
from cart_station_sfc import SfcDatabase, dump_sfc
from test_cart_station_gba import Cartridge as GbaCartridge, fixture as gba_fixture
from test_cart_station_sfc import Cartridge as SfcCartridge, fixture as sfc_fixture


class FreshPassTests(unittest.TestCase):
    @patch('cart_station_gba.time.sleep')
    def test_gba_rechecks_complete_header_after_mode_setup(self, sleep):
        profile, payload, record = gba_fixture(65536)
        for failure in ('changed', 'short', 'removed', 'wire'):
            with self.subTest(failure=failure):
                bus = GbaCartridge(payload)
                original = bus.gba_read

                def read(address, size):
                    if len(bus.modes) == 2:
                        if failure == 'wire':
                            raise ProtocolError('wire failure')
                        if failure == 'short':
                            return payload[:192]
                        if failure == 'changed':
                            return bytes(size)
                    return original(address, size)

                bus.gba_read = read
                bus.status = lambda: 255 if failure == 'removed' and len(bus.modes) == 2 else 6
                with self.assertRaises(ProtocolError):
                    dump_gba(bus, profile, GbaDatabase([record]))
                self.assertEqual(bus.modes, [0, 0, 0])
                self.assertEqual(bus.events.count((0, 32768)), 1)

    @patch('cart_station_gba.time.sleep')
    def test_cancel_at_pass_boundary_never_saves_or_reinitializes(self, sleep):
        profile, payload, record = gba_fixture(65536)
        with tempfile.TemporaryDirectory() as folder:
            service = Service(Path(folder) / 'roms', database=Database([]),
                              gba_database=GbaDatabase([record]))
            bus = service.adapter = service.transport = GbaCartridge(payload)
            service.profile, service.selected_id = profile, profile.id

            def progress(stage, *args):
                if stage == 'Preparing GBA read 2/2':
                    raise UserCancelled('cancel')

            with self.assertRaises(UserCancelled):
                service.capture(progress)
            self.assertFalse(service.destination.exists())
            self.assertTrue(bus.closed)
            self.assertEqual(bus.modes, [0, 0])

    def test_sfc_changed_or_invalid_second_header_never_retries_or_reads_rom(self):
        profile, payload, record = sfc_fixture()
        other, _, _ = sfc_fixture(title=b'CHANGED CART')
        for header in (other.header, bytes(48), b''):
            with self.subTest(header=header):
                bus = SfcCartridge(profile, payload)
                bus.override = lambda a, n: header if a == 0xffb0 and len(bus.modes) >= 3 else None
                with self.assertRaises(ProtocolError):
                    dump_sfc(bus, profile, SfcDatabase([record]))
                self.assertEqual(bus.modes, [1, 0, 1, 0])
                self.assertEqual(bus.events.count('R,32768,32768'), 1)

    def test_sfc_release_failure_never_starts_second_pass(self):
        profile, payload, record = sfc_fixture()
        bus = SfcCartridge(profile, payload)
        original = bus.snes_mode

        def mode(value):
            original(value)
            if value == 0:
                raise ProtocolError('release failure')

        bus.snes_mode = mode
        with self.assertRaisesRegex(ProtocolError, 'release failure'):
            dump_sfc(bus, profile, SfcDatabase([record]))
        self.assertEqual(bus.modes.count(1), 1)
        self.assertEqual(bus.events.count('R,32768,32768'), 1)


if __name__ == '__main__':
    unittest.main()
