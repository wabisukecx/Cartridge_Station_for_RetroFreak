"""Mapper 206.1 transactions exercised with fragmented synthetic device replies.

Also provides the fixed Mapper 206.1 entry of the shipped FC/NES DB to other
tests: MAPPER206_PROFILE is a mutable dict so tests can substitute synthetic
hashes with patch.dict; fixed_mapper206_profile() builds a Profile from its
current contents.
"""
from _paths import ROOT, data  # noqa: F401
import hashlib
import json
import re
import unittest
from unittest.mock import Mock, patch

from cart_station_transport import Adapter
from cart_station_protocol import ProtocolError, packet
from cart_station_nes_db import Profile, dump_profile
from cart_station_nes_mappers import MapperReader
from test_cart_station import Wire, memory, status

PROFILE_ID = '610a40eb37b7a0c27d2fa02a19feeafeccfcdc18-m206-s1'


def _shipped_metadata():
    path = ROOT / 'data' / 'nes_profiles.reference.json'
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    matches = [row for row in document['profiles'] if row['id'] == PROFILE_ID]
    if len(matches) != 1:
        raise ValueError('Expected one shipped Mapper 206.1 profile')
    record = dict(matches[0])
    for key in ('prg_sha1', 'chr_sha1', 'raw_sha1', 'file_sha1'):
        if isinstance(record.get(key), str):
            record[key] = record[key].lower()
    record['header'] = bytes.fromhex(record['header'])
    title = record['title']
    record['rom_name'] = title if title.lower().endswith('.nes') else title + '.nes'
    return record


MAPPER206_PROFILE = _shipped_metadata()


def fixed_mapper206_profile():
    record = dict(MAPPER206_PROFILE, header=MAPPER206_PROFILE['header'].hex())
    record.pop('rom_name', None)
    return Profile.parse(record)


def dump_super_chinese(adapter, progress=None):
    profile = fixed_mapper206_profile()
    result = dump_profile(adapter, profile, progress=progress)
    if progress:
        total = 2 * (profile.prg_size + profile.chr_size)
        progress('Verified', total, total)
    return result


def read_fixed(adapter):
    return MapperReader(adapter, fixed_mapper206_profile()).read()


PRG = bytes(range(256)) * 128
CHR = b''.join(bytes((bank,)) * 1024 for bank in range(16))
RAW = PRG + CHR


def hashes():
    return {
        'prg_sha1': hashlib.sha1(PRG).hexdigest(),
        'chr_sha1': hashlib.sha1(CHR).hexdigest(),
        'raw_sha1': hashlib.sha1(RAW).hexdigest(),
        'file_sha1': hashlib.sha1(MAPPER206_PROFILE['header'] + RAW).hexdigest(),
    }


def read_reply(prg=PRG, chr_data=CHR):
    replies = memory(prg[:16384]) + memory(prg[16384:])
    for bank in range(16):
        # Each bank has two writes, each with two ACKs and one CPU dummy read.
        replies += (bytes(16) + memory(prg[:8])) * 2
        replies += memory(chr_data[bank * 1024:(bank + 1) * 1024])
    return replies


class Mapper206Tests(unittest.TestCase):
    @patch('time.sleep')
    def test_full_dump_fragmented_wire_and_bank_order(self, sleep):
        wire = Wire(status() + bytes(16) + read_reply() * 2 + bytes(16), fragment=19)
        progress = Mock()
        with patch.dict(MAPPER206_PROFILE, hashes()):
            result = dump_super_chinese(Adapter(wire), progress=progress)
        self.assertEqual(result, bytes.fromhex('4e45531a0202e0c81000000000000001') + RAW)
        self.assertEqual(len(result), 49168)
        self.assertEqual(wire.replies, b'')
        requests = [data for data in wire.writes if len(data) == 20]
        writes = [data for data in requests if data[0] == 0x12]
        self.assertEqual(writes, [request for _ in range(32) for request in
                                 (packet(0x12, 8, 1, 0x8000, 1), packet(0x12, 8, 1, 0x8001, 1))])
        payloads = [data for data in wire.writes if len(data) == 1]
        self.assertEqual(payloads, [value for _ in range(2) for bank in range(16)
                                    for value in (b'\x02', bytes((bank,)))])
        chr_requests = [data for data in requests if data[0] == 0x13]
        self.assertEqual(chr_requests, [packet(0x13, 8, 1024, 0x1000, 1024)] * 32)
        self.assertEqual(wire.writes[-2:], [packet(4, 4, 0, 0), packet(5, 0, 0)])
        self.assertEqual(progress.call_args_list[0].args, ('Read 1/2', 0, 98304))
        self.assertEqual(progress.call_args.args, ('Verified', 98304, 98304))
        values = [item.args[1] for item in progress.call_args_list]
        self.assertEqual(values, sorted(values))

    @patch('time.sleep')
    def test_cancel_callback_still_runs_device_shutdown(self, sleep):
        adapter = Mock()
        adapter.status.return_value = 3
        with self.assertRaisesRegex(RuntimeError, 'cancel'):
            dump_super_chinese(adapter, progress=Mock(side_effect=RuntimeError('cancel')))
        self.assertEqual(adapter.mode.call_args_list[-1].args, (0,))

    def test_wrong_prg_is_rejected_before_register_write(self):
        wire = Wire(memory(b'X' * 16384) * 2)
        with self.assertRaisesRegex(ProtocolError, 'PRG hash mismatch'):
            read_fixed(Adapter(wire))
        self.assertFalse(any(len(data) == 20 and data[0] == 0x12 for data in wire.writes))

    def test_wrong_chr_hash_is_rejected(self):
        wire = Wire(read_reply(), fragment=97)
        with patch.dict(MAPPER206_PROFILE, {'prg_sha1': hashes()['prg_sha1'], 'chr_sha1': '0' * 40}):
            with self.assertRaisesRegex(ProtocolError, 'CHR hash mismatch'):
                read_fixed(Adapter(wire))

    def test_register_write_range_is_restricted(self):
        wire = Wire(b'')
        for address, value in ((0x6000, 0), (0x8000, 6), (0x8001, 16), (0x8001, -1)):
            with self.assertRaises(ValueError):
                Adapter(wire).nes.mapper206_register(address, value)
        self.assertEqual(wire.writes, [])

    def test_first_write_ack_failure_does_not_send_payload(self):
        wire = Wire(packet(1, 0))
        with self.assertRaises(ProtocolError):
            Adapter(wire).nes.mapper206_register(0x8000, 2)
        self.assertEqual(wire.writes, [packet(0x12, 8, 1, 0x8000, 1)])

    def test_second_write_ack_failure_does_not_dummy_read(self):
        wire = Wire(packet(0, 0) + packet(1, 0))
        with self.assertRaises(ProtocolError):
            Adapter(wire).nes.mapper206_register(0x8000, 2)
        self.assertEqual(wire.writes, [packet(0x12, 8, 1, 0x8000, 1), b'\x02'])

    @patch('time.sleep')
    def test_verify_failure_still_shuts_down(self, sleep):
        for first, second, message, metadata in (
                (read_reply(), read_reply(chr_data=CHR[:-1] + b'Z'), 'Two mapper ROM reads differ', hashes()),
                (read_reply(), b'', 'PRG+CHR hash mismatch', dict(hashes(), raw_sha1='0' * 40)),
                (read_reply(), read_reply(), 'DB completed file hash mismatch', dict(hashes(), file_sha1='0' * 40))):
            with self.subTest(message=message):
                wire = Wire(status() + bytes(16) + first + second + bytes(16), fragment=19)
                with patch.dict(MAPPER206_PROFILE, metadata):
                    with self.assertRaisesRegex(ProtocolError, re.escape(message)):
                        dump_super_chinese(Adapter(wire))
                self.assertEqual(wire.replies, b'')
                self.assertEqual(wire.writes[-2:], [packet(4, 4, 0, 0), packet(5, 0, 0)])

    def test_wrong_console_does_not_initialize(self):
        adapter = Mock()
        adapter.status.return_value = 5
        with self.assertRaises(ProtocolError):
            dump_super_chinese(adapter)
        adapter.mode.assert_not_called()


if __name__ == '__main__':
    unittest.main()
