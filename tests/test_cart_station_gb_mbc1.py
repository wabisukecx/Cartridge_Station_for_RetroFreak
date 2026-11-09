"""Large MBC1 layouts, using independently wired ROM models."""
from _paths import ROOT, data  # noqa: F401
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_gb import (GbDatabase, GbUnknownRom, parse_header, inspect_header, dump_gb,
                             select_mbc1_layout)
from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter
from cart_station_service import Service
from cart_station_nes_db import Database
from test_cart_station_gb import fixture, Cartridge
from test_cart_station import Wire


class Mbc1Cartridge(Cartridge):
    def __init__(self,payload,low_bits=5):
        super().__init__(payload)
        self.low_bits=low_bits
        self.bank_reads=[]

    def gb_read(self,address,size):
        self.events.append(('R',address,size))
        # Standard MBC1 uses five low ROM bits. MBC1M wiring drops bit 4
        # after the full five-bit zero-to-one translation.
        mask=(1 << self.low_bits)-1
        outer=(self.high & 3) << self.low_bits
        if address < 0x4000:
            bank=outer if self.mode_value else 0
        else:
            bank=outer | (((self.bank & 31) or 1) & mask)
        bank %= len(self.payload)//16384
        self.bank_reads.append((address,size,bank))
        offset=bank*16384+(address & 16383)
        return self.payload[offset:offset+size]


@patch('cart_station_gb.time.sleep')
class LargeMbc1Tests(unittest.TestCase):
    def test_profile_limits_include_source_layouts_and_reject_above_two_mib(self,sleep):
        profile,_,_=fixture(1,5)
        for name in ('MBC1','MBC1Ex2M','MBC1Ex4M'):
            for size in (1048576,2097152):
                self.assertIsNone(replace(profile,controller=name,prg_size=size).support())
            oversized=replace(profile,controller=name,prg_size=4194304)
            bus=Mock()
            with self.assertRaises(ProtocolError):dump_gb(bus,oversized,GbDatabase([]))
            self.assertEqual(bus.mock_calls,[])

    def test_standard_two_mib_reads_all_boundary_banks_from_lower_window(self,sleep):
        profile,payload,record=fixture(1,6)
        bus=Mbc1Cartridge(payload)
        matched,result=dump_gb(bus,profile,GbDatabase([record]))
        self.assertEqual(result,payload);self.assertEqual(matched.controller,'MBC1')
        reads=[(address,bank) for address,size,bank in bus.bank_reads if size==16384]
        self.assertEqual([bank for _,bank in reads],list(range(128))*2)
        for boundary in (32,64,96):self.assertEqual(reads[boundary],(0,boundary))
        self.assertEqual(bus.events[-3:],[('W',0x6000,0),('W',0x2100,0),('W',0x4000,0)])
        self.assertEqual(bus.modes,[1,0])

    def test_reference_one_mib_detection_selects_both_layouts(self,sleep):
        profile,payload,_=fixture(1,5)
        for bits,name in ((4,'MBC1Ex2M'),(5,'MBC1Ex4M')):
            bus=Mbc1Cartridge(payload,bits)
            selected=inspect_header(bus)
            self.assertEqual(selected.controller,name)
            self.assertEqual(selected.header,profile.header)
            self.assertEqual(bus.events[:2],[('R',0x100,80)]*2)
            self.assertEqual(bus.events[2:5],[('W',0x2100,0),('W',0x6000,0),('W',0x4000,0)])
            self.assertEqual(bus.events[-3:],[('W',0x6000,0),('W',0x2100,0),('W',0x4000,0)])
            self.assertEqual(bus.gb_read(0x100,80),profile.header)

    def test_reference_detector_can_keep_normal_mbc1_or_choose_zero_fill_branch(self,sleep):
        profile,payload,_=fixture(1,5)
        for responses,expected in (([b'A'*16384,b'B'*16384,b'C'*16384,b'D'*16384],'MBC1'),
                                   ([b'A'*16384,b'B'*16384,bytes(16384),b'D'*16384],'MBC1Ex4M')):
            bus=Mbc1Cartridge(payload);bus.gb_read=Mock(side_effect=responses)
            self.assertEqual(select_mbc1_layout(bus,profile).controller,expected)
            self.assertTrue(all(e[0]=='W' for e in bus.events))

    def test_detector_skips_other_sizes_and_controllers_without_touching_bus(self,sleep):
        profile,_,_=fixture(1,5)
        for other in (replace(profile,prg_size=524288),replace(profile,prg_size=2097152),replace(profile,controller='MBC3')):
            bus=Mock();self.assertEqual(select_mbc1_layout(bus,other),other)
            self.assertEqual(bus.mock_calls,[])

    def test_extended_layouts_reconstruct_complete_known_roms(self,sleep):
        for bits,name,size_code in ((4,'MBC1Ex2M',5),(5,'MBC1Ex4M',5),(5,'MBC1Ex4M',6)):
            profile,payload,record=fixture(1,size_code)
            profile=replace(profile,controller=name)
            bus=Mbc1Cartridge(payload,bits)
            matched,result=dump_gb(bus,profile,GbDatabase([record]))
            self.assertEqual(result,payload);self.assertEqual(matched.controller,name)
            banks=[bank for _,size,bank in bus.bank_reads if size==16384]
            self.assertEqual(banks,list(range(len(payload)//16384))*2)
            self.assertFalse(any(e[0]=='W' and (e[1]<0x2000 or e[1]>=0x8000) for e in bus.events))

    def test_misleading_detection_is_corrected_only_by_complete_db_hash(self,sleep):
        profile,payload,record=fixture(1,5)
        modified=bytearray(payload)
        modified[17*16384:18*16384]=modified[16384:32768]
        checksum=(sum(modified)-modified[0x14e]-modified[0x14f]) & 65535
        modified[0x14e:0x150]=checksum.to_bytes(2,'big');payload=bytes(modified)
        profile=parse_header(payload[0x100:0x150])
        record=dict(record,sha1=hashlib.sha1(payload).hexdigest(),sha256=hashlib.sha256(payload).hexdigest())
        bus=Mbc1Cartridge(payload)
        selected=inspect_header(bus)
        self.assertEqual(selected.controller,'MBC1Ex2M')
        bus.bank_reads.clear()
        matched,result=dump_gb(bus,selected,GbDatabase([record]))
        self.assertEqual(matched.controller,'MBC1');self.assertEqual(result,payload)
        self.assertEqual(len([x for x in bus.bank_reads if x[1]==16384]),256)

    def test_transport_error_does_not_retry_another_layout(self,sleep):
        profile,payload,record=fixture(1,5)
        bus=Mbc1Cartridge(payload);original=bus.gb_read
        def failed(address,size):
            if size==16384:raise ProtocolError('USB disconnected')
            return original(address,size)
        bus.gb_read=Mock(side_effect=failed)
        with self.assertRaisesRegex(ProtocolError,'USB disconnected'):
            dump_gb(bus,profile,GbDatabase([record]))
        self.assertEqual(bus.gb_read.call_count,2)
        self.assertEqual(bus.modes,[1,0])

    def test_two_read_instability_does_not_retry_another_layout(self,sleep):
        profile,payload,record=fixture(1,5)
        bus=Mbc1Cartridge(payload);starts=[]
        def progress(stage,done,total):
            if done==0:starts.append(stage)
            if stage=='Read 2/2':bus.payload=payload[:-1]+bytes((payload[-1]^1,))
        with self.assertRaisesRegex(ProtocolError,'Two GB ROM reads differ'):
            dump_gb(bus,profile,GbDatabase([record]),progress)
        self.assertEqual(starts,['Read 1/2']);self.assertEqual(bus.modes,[1,0])

    def test_db_sha256_failure_does_not_retry_another_layout(self,sleep):
        profile,payload,record=fixture(1,5)
        bus=Mbc1Cartridge(payload)
        with self.assertRaisesRegex(ProtocolError,'SHA-256 mismatch'):
            dump_gb(bus,profile,GbDatabase([dict(record,sha256='0'*64)]))
        self.assertEqual(len([x for x in bus.bank_reads if x[1]==16384]),128)

    def test_unknown_rom_tries_only_allowed_layouts_and_creates_no_file(self,sleep):
        profile,payload,_=fixture(1,5)
        with tempfile.TemporaryDirectory() as folder:
            dest=Path(folder)/'out';bus=Mbc1Cartridge(payload)
            service=Service(dest,database=Database([]),gb_database=GbDatabase([]))
            service.adapter,service.profile,service.selected_id=bus,profile,profile.id
            with self.assertRaises(GbUnknownRom):service.capture(lambda *args:None)
            self.assertFalse(dest.exists());self.assertEqual(bus.modes,[1,0])
            self.assertEqual(len([x for x in bus.bank_reads if x[1]==16384]),384)

    def test_header_can_be_read_again_after_large_capture(self,sleep):
        profile,payload,record=fixture(1,6)
        bus=Mbc1Cartridge(payload)
        dump_gb(bus,profile,GbDatabase([record]))
        self.assertEqual(inspect_header(bus).header,profile.header)
        self.assertEqual(bus.modes,[1,0,1,0])

    def test_detector_error_still_restores_bank_zero_and_releases_reader(self,sleep):
        profile,payload,_=fixture(1,5)
        bus=Mbc1Cartridge(payload);original=bus.gb_read
        def failed(address,size):
            if size==16384:raise ProtocolError('probe failed')
            return original(address,size)
        bus.gb_read=failed
        with self.assertRaisesRegex(ProtocolError,'probe failed'):inspect_header(bus)
        self.assertEqual(bus.events[-3:],[('W',0x6000,0),('W',0x2100,0),('W',0x4000,0)])
        self.assertEqual(bus.modes,[1,0])

    def test_source_extended_selector_values_are_scoped_rom_register_writes(self,sleep):
        wire=Wire(bytes(16));Adapter(wire).gb_register(0x4000,7)
        self.assertEqual(wire.writes,[packet(0x1c,8,1,0x4000,1),b'\x07'])
        for address,value in ((0x4000,8),(0,10),(0xa000,1)):
            with self.assertRaises(ValueError):Adapter(Wire(b'')).gb_register(address,value)


if __name__=='__main__':unittest.main()
