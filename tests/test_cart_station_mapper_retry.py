from _paths import ROOT  # noqa: F401
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from cart_station_protocol import ProtocolError
from cart_station_nes_db import Database, dump_profile
from cart_station_nes_mappers import (MapperReader, RomMismatch, dump_mapped,
                                      dump_mapped_group)
from cart_station_service import Service, UserCancelled
from test_cart_station_nes_mappers import Cartridge, profile_for


@patch('cart_station_nes_mappers.time.sleep')
class MapperRetryTests(unittest.TestCase):
    def fixture(self, mapper=1):
        raw = b'P' * 65536 + b'C' * 8192
        profile = profile_for(mapper, raw[:65536], raw[65536:])
        bus = Mock()
        bus.status.return_value = 3
        return profile, raw, bus

    def test_real_reader_second_hash_failure_stops_after_two_reads(self, sleep):
        profile, raw, _ = self.fixture()
        bus = Cartridge(1, raw[:65536], raw[65536:])
        reader = MapperReader(bus, profile)
        original_read = reader.read
        reads = []

        def corrupt_after_first(*args):
            reads.append(1)
            result = original_read(*args)
            bus.prg = b'X' + bus.prg[1:]
            return result

        with patch('cart_station_nes_mappers.MapperReader', return_value=reader) as factory:
            with patch.object(reader, 'read', side_effect=corrupt_after_first):
                with self.assertRaisesRegex(ProtocolError, 'Two mapper ROM reads differ') as caught:
                    dump_profile(bus, profile)
        self.assertIsInstance(caught.exception.__cause__, RomMismatch)
        self.assertEqual(len(reads), 2)
        factory.assert_called_once()
        self.assertEqual(bus.modes, [1, 0])

    def test_mmc1_second_mismatch_stops_in_each_variant_with_or_without_progress(self, sleep):
        for rejected in range(3):
            for with_progress in (False, True):
                with self.subTest(rejected=rejected, progress=with_progress):
                    profile, raw, bus = self.fixture()
                    failures = [SimpleNamespace(read=Mock(side_effect=RomMismatch('wrong wiring')))
                                for _ in range(rejected)]
                    error = RomMismatch('DB CHR hash mismatch')
                    chosen = SimpleNamespace(read=Mock(side_effect=[raw, error]))
                    with patch('cart_station_nes_mappers.MapperReader',
                               side_effect=failures + [chosen]) as factory:
                        with self.assertRaisesRegex(ProtocolError, 'Two mapper ROM reads differ') as caught:
                            dump_mapped(bus, profile, Mock() if with_progress else None)
                    self.assertIs(caught.exception.__cause__, error)
                    self.assertEqual(factory.call_count, rejected + 1)
                    self.assertEqual(chosen.read.call_count, 2)
                    self.assertEqual(bus.mode.call_args_list, [call(1), call(0)] * (rejected + 1))

    def test_mapper406_first_mismatch_can_fallback_but_second_cannot(self, sleep):
        for rejected in (0, 1):
            with self.subTest(rejected=rejected):
                profile, raw, bus = self.fixture(406)
                readers = [SimpleNamespace(read=Mock(side_effect=RomMismatch('wrong board')))
                           for _ in range(rejected)]
                chosen = SimpleNamespace(read=Mock(side_effect=[raw, RomMismatch('PRG mismatch')]))
                with patch('cart_station_nes_mappers.MapperReader',
                           side_effect=readers + [chosen]) as factory:
                    with self.assertRaisesRegex(ProtocolError, 'Two mapper ROM reads differ'):
                        dump_mapped(bus, profile)
                self.assertEqual(chosen.register_variant, ('AXR', 'INL')[rejected])
                self.assertEqual(factory.call_count, rejected + 1)
                self.assertEqual(bus.mode.call_args_list, [call(1), call(0)] * (rejected + 1))

    def test_group_second_hash_failure_or_different_matching_title_stops(self, sleep):
        for hash_failure in (False, True):
            with self.subTest(hash_failure=hash_failure):
                profile, raw, bus = self.fixture()
                other_raw = b'Q' * 65536 + raw[65536:]
                other = replace(profile_for(1, other_raw[:65536], other_raw[65536:]), id='other')
                second = RomMismatch('no candidate matches') if hash_failure else other_raw
                chosen = SimpleNamespace(matched=profile, read=Mock(side_effect=[raw, second]))
                with patch('cart_station_nes_mappers.MapperReader', return_value=chosen) as factory:
                    with self.assertRaisesRegex(ProtocolError, 'Two mapper ROM reads differ'):
                        dump_mapped_group(bus, (profile, other), Mock())
                factory.assert_called_once()
                self.assertEqual(chosen.read.call_count, 2)
                self.assertEqual(bus.mode.call_args_list, [call(1), call(0)])

    def test_transport_and_cancel_errors_keep_original_cause_without_retry(self, sleep):
        for failure_read in (1, 2):
            for error in (ProtocolError('USB unplugged'), OSError('pipe error'),
                          UserCancelled('cancelled'), KeyboardInterrupt()):
                with self.subTest(read=failure_read, error=type(error).__name__):
                    profile, raw, bus = self.fixture()
                    reader = SimpleNamespace(read=Mock(side_effect=[raw] * (failure_read - 1) + [error]))
                    with patch('cart_station_nes_mappers.MapperReader', return_value=reader) as factory:
                        with self.assertRaises(type(error)) as caught:
                            dump_mapped(bus, profile, Mock())
                    self.assertIs(caught.exception, error)
                    factory.assert_called_once()
                    self.assertEqual(reader.read.call_count, failure_read)
                    self.assertEqual(bus.mode.call_args_list, [call(1), call(0)])

    def test_first_mismatch_still_exhausts_only_available_variants(self, sleep):
        profile, _, bus = self.fixture()
        with patch('cart_station_nes_mappers.MapperReader') as factory:
            factory.return_value.read.side_effect = RomMismatch('first-read mismatch')
            with self.assertRaisesRegex(ProtocolError, 'No mapper variant matched'):
                dump_mapped(bus, profile)
        self.assertEqual([c.args[2:] for c in factory.call_args_list],
                         [(4096, 0), (8192, 0), (4096, 4)])
        self.assertEqual(factory.return_value.read.call_count, 3)
        self.assertEqual(bus.mode.call_args_list, [call(1), call(0)] * 3)

    def test_capture_second_mismatch_preserves_existing_file_and_never_publishes(self, sleep):
        for grouped in (False, True):
            with self.subTest(grouped=grouped), tempfile.TemporaryDirectory() as directory:
                profile, raw, bus = self.fixture()
                other = replace(profile, id='other', raw_sha1='0' * 40)
                candidates = (profile, other) if grouped else (profile,)
                destination = Path(directory)
                target = destination / profile.filename()
                target.write_bytes(b'original file')
                service = Service(destination, database=Database(candidates))
                service.adapter, service.profile = bus, profile
                service.candidates = candidates
                service.auto_group = candidates if grouped else ()
                service.selected_id = None if grouped else profile.id
                reader = SimpleNamespace(matched=profile,
                                         read=Mock(side_effect=[raw, RomMismatch('second read damaged')]))
                with patch('cart_station_service.identify', return_value=candidates):
                    with patch('cart_station_service.save_verified_rom') as publish:
                        with patch('cart_station_nes_mappers.MapperReader', return_value=reader) as factory:
                            with self.assertRaisesRegex(ProtocolError, 'Two mapper ROM reads differ'):
                                service.capture(Mock())
                publish.assert_not_called()
                factory.assert_called_once()
                self.assertIsNone(service.adapter)
                self.assertEqual(target.read_bytes(), b'original file')
                self.assertEqual(list(destination.iterdir()), [target])


if __name__ == '__main__':
    unittest.main()
