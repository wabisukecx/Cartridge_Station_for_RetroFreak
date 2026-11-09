"""MD synthetic-bus verification; never access a real cartridge or USB device."""
from _paths import ROOT, data  # noqa: F401
import hashlib
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from cart_station_md import (MdDatabase, SSF2, SONIC_K, SONIC_TITLES, dump_md,
                             inspect_header, parse_header, read_rom, read_mortal_chunk,
                             validate_payload, verified_file)
from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter
from cart_station_environment import default_output_directory
from cart_station_service import Service, UserCancelled, create_mgl, read_core_config
from cart_station_nes_db import Database
from cart_station_presentation import profile_details, profile_columns
from test_cart_station import Wire


def fixture(size=1048576,title=b'SYNTHETIC MD',serial=b'GM TEST-00'):
    payload=bytearray((bytes(range(256))*((size+255)//256))[:size])
    h=bytearray(b' '*256)
    h[:16]=b'SEGA MEGA DRIVE '
    h[0x20:0x50]=title.ljust(48,b' ')
    h[0x50:0x80]=title.ljust(48,b' ')
    h[0x80:0x8e]=serial.ljust(14,b' ')
    h[0x8e:0x90]=b'\x12\x34'  # Advisory checksum, not an identity gate.
    h[0xa0:0xa4]=(0).to_bytes(4,'big')
    h[0xa4:0xa8]=(size-1).to_bytes(4,'big')
    payload[0x100:0x200]=h
    payload=bytes(payload)
    record=dict(system='MD',title='Synthetic MD (Test).md',size=size,
                sha1=hashlib.sha1(payload).hexdigest(),sha256=hashlib.sha256(payload).hexdigest())
    return parse_header(bytes(h)),payload,record


class Cartridge:
    def __init__(self,payload):
        self.payload=payload;self.events=[];self.modes=[];self.bank=7;self.closed=False
        self.attached_header=None
    def status(self):return 2
    def product(self):return dict(build=10,product=4919)
    def mode(self,value):self.modes.append(value)
    def close(self):self.closed=True
    def md_register(self,address,value):
        self.events.append('W,%d,%d'%(address,value))
        if address==0xa130ff:self.bank=value
    def md_read(self,address,size):
        self.events.append('R,%d,%d'%(address,size))
        if address==0x200100 and size==64 and self.attached_header is not None:
            return self.attached_header
        if address>=0xe00000:address &= 0x3fffff
        elif 0x380000<=address<0x400000 and self.bank>=8:
            address=self.bank*524288+address-0x380000
        return self.payload[address:address+size]


class MdHeaderWireTests(unittest.TestCase):
    def test_missing_shift_jis_preserves_ascii_header_and_controller_detection(self):
        p,payload,r=fixture()
        with patch('cart_station_md.codecs.getdecoder',side_effect=LookupError('unknown encoding: shift_jis')):
            self.assertEqual(parse_header(p.header),p)
            self.assertEqual(fixture(65536,SSF2)[0].controller,'SuperSf2')
            self.assertEqual(fixture(65536,SONIC_K)[0].controller,'SAndN')

    def test_missing_japanese_codec_changes_preview_only_not_rom_identity(self):
        # Fixed Shift-JIS bytes for the test title: creation must also work
        # when the runtime has no Japanese encoder or decoder installed.
        p,payload,r=fixture(title=bytes.fromhex('836583588367'))
        with patch('cart_station_md.codecs.getdecoder',side_effect=LookupError('codec unavailable')):
            fallback=parse_header(p.header)
        self.assertEqual(fallback.header,p.header);self.assertEqual(fallback.id,p.id)
        self.assertEqual(fallback.prg_size,p.prg_size)
        self.assertTrue(all(ord(c)<128 for c in fallback.title))
        matched=MdDatabase([r]).match(fallback,payload)
        self.assertEqual(matched.filename(),r['title']);validate_payload(matched,payload)

    def test_big_endian_range_and_unchanged_raw_order(self):
        p,payload,r=fixture()
        self.assertEqual(p.prg_size,1048576);self.assertEqual(p.header[:4],b'SEGA')
        self.assertEqual(p.serial,'GM TEST-00');self.assertIsNone(p.support())
        with self.assertRaises(ProtocolError):p.filename()

    def test_invalid_signature_length_and_swapped_header(self):
        p,_,_=fixture()
        for h in (bytes(256),p.header[:-1],bytearray(p.header)):
            with self.assertRaises(ProtocolError):parse_header(h)
        h=bytearray(p.header);h[:4]=b'ESAG'
        with self.assertRaisesRegex(ProtocolError,'swapped'):parse_header(bytes(h))

    def test_unsupported_ranges_are_visible_without_reading(self):
        p,_,_=fixture()
        for size in (0,511,1048577,4194306):self.assertIsNotNone(replace(p,prg_size=size).support())
        h=bytearray(p.header);h[0xa0:0xa4]=(2).to_bytes(4,'big')
        self.assertIsNotNone(parse_header(bytes(h)).support())
        self.assertIsNotNone(replace(p,controller='Unknown').support())

    def test_source_title_overrides_and_special_detection(self):
        for title,serial,size in ((b'ULTRACORE',b'GM T-SNAKE-MOD',2097152),
                                 (b'ZERO WING',b'GM T-40023 -00',1048576),
                                 (b'AGAME',b'GM T-XXXXX -XX',1048576)):
            self.assertEqual(fixture(65536,title,serial)[0].prg_size,size)
        self.assertEqual(fixture(65536,SSF2)[0].controller,'SuperSf2')
        self.assertEqual(fixture(65536,SSF2)[0].prg_size,5242880)
        self.assertEqual(fixture(65536,SONIC_K)[0].controller,'SAndN')

    def test_wire_command_and_status_before_payload(self):
        payload=bytes(range(16));wire=Wire(packet(0,16)+payload+packet(0,4)+packet(2),fragment=3)
        self.assertEqual(Adapter(wire).md_read(0x100,16),payload)
        self.assertEqual(wire.writes,[packet(0x0d,8,16,0x100,16),packet(6,0,4)])
        self.assertFalse(wire.replies)

    def test_wire_mapping_register_whitelist_and_ack(self):
        wire=Wire(packet(0,0)*2);Adapter(wire).md_register(0xa130ff,8)
        self.assertEqual(wire.writes,[packet(0x0e,8,1,0xa130ff,1),b'\x08'])
        for address,value in ((0x200001,1),(0,0),(0xa130f1,2),(0xa130ff,24),(True,1)):
            wire=Wire(b'')
            with self.assertRaises(ValueError):Adapter(wire).md_register(address,value)
            self.assertFalse(wire.writes)

    def test_read_bounds_alignment_and_wrong_cart(self):
        for address,size in ((-2,2),(0,1),(1,2),(0xffffff,2),(0,32770),(0,True)):
            wire=Wire(b'')
            with self.assertRaises(ValueError):Adapter(wire).md_read(address,size)
            self.assertFalse(wire.writes)
        with self.assertRaisesRegex(ProtocolError,'changed'):
            Adapter(Wire(packet(0,2)+b'ab'+packet(0,4)+packet(3))).md_read(0,2)
        with self.assertRaisesRegex(ProtocolError,'Truncated'):
            Adapter(Wire(packet(0,16)+b'ab')).md_read(0,16)


@patch('cart_station_md.time.sleep')
class MdReadingTests(unittest.TestCase):
    def test_header_sequence_matches_reference_16_byte_chunks_twice(self,sleep):
        p,payload,_=fixture();bus=Cartridge(payload)
        self.assertEqual(inspect_header(bus),p)
        expected=['R,%d,16'%a for a in range(0x100,0x200,16)]
        self.assertEqual(bus.events,expected*2);self.assertEqual(bus.modes,[1,0])
        sleep.assert_called_once_with(0.2)

    def test_header_mismatch_or_wrong_cart_stops(self,sleep):
        _,payload,_=fixture();bus=Cartridge(payload);read=bus.md_read
        def unstable(a,n):
            result=read(a,n)
            return bytes(n) if len(bus.events)==17 else result
        bus.md_read=unstable
        with self.assertRaisesRegex(ProtocolError,'differ'):inspect_header(bus)
        self.assertEqual(bus.modes[-1],0)
        bus=Cartridge(payload);bus.status=lambda:5
        with self.assertRaises(ProtocolError):inspect_header(bus)
        self.assertFalse(bus.events)

    def test_sonic_attached_header_identification_and_identity(self,sleep):
        _,payload,_=fixture(65536,SONIC_K);ids=[]
        for title,(name,size) in SONIC_TITLES.items():
            bus=Cartridge(payload);bus.attached_header=bytes(32)+title
            p=inspect_header(bus)
            self.assertEqual((p.attached,p.prg_size),(name,size));ids.append(p.id)
            self.assertEqual(bus.events[-2:],['R,2097408,64']*2)
        self.assertEqual(len(set(ids)),3)

    def test_all_four_bank_readers_match_reference_traces(self,sleep):
        cases=json.loads(data('md_reference_traces.json').read_text(encoding='utf-8'))['cases']
        for case in cases:
            p,payload,_=fixture(case['size']);p=replace(p,controller=case['controller'])
            bus=Cartridge(bytes(len(payload)));out=read_rom(bus,p)
            prefix=[]
            if p.controller in ('SAndN','SuperSf2'):prefix=['W,10563825,0']
            if p.controller=='SuperSf2':prefix+=['W,10563839,7']
            self.assertEqual(bus.events,prefix+case['trace'],p.controller)
            self.assertEqual(out,bytes(len(payload)),p.controller)

    def test_two_complete_independent_reads_and_full_db_match(self,sleep):
        p,payload,r=fixture(65538);bus=Cartridge(payload)
        matched,out=dump_md(bus,p,MdDatabase([r]))
        self.assertEqual(out,payload);self.assertEqual(matched.title,r['title'])
        self.assertEqual(bus.events[16:19],bus.events[19:22])
        self.assertEqual(bus.events[-1],'R,65536,2');self.assertEqual(bus.modes,[1,0])

    def test_sonic2_mapping_reinitialized_per_read_and_reset(self,sleep):
        p,payload,r=fixture(3407872,SONIC_K);bus=Cartridge(payload)
        title=next(t for t,v in SONIC_TITLES.items() if v[0]=='Sonic2')
        bus.attached_header=bytes(32)+title;p=inspect_header(bus);bus.events=[]
        dump_md(bus,p,MdDatabase([r]))
        writes=[x for x in bus.events if x.startswith('W')]
        self.assertEqual(writes,['W,10563825,0','W,10563825,1']*2+['W,10563825,0'])

    def test_mortal_all_ff_is_preserved_with_bounded_retries(self,sleep):
        _,payload,_=fixture();bus=Cartridge(payload)
        bus.md_read=lambda a,n:(bus.events.append((a,n)) or b'\xff'*n)
        out=read_mortal_chunk(bus,0xe00000,64,lambda:None)
        self.assertEqual(out,b'\xff'*64);self.assertEqual(len(bus.events),11)

    def test_mortal_mixed_ff_falls_back_to_words(self,sleep):
        _,payload,_=fixture();bus=Cartridge(payload)
        def read(a,n):
            bus.events.append((a,n))
            return b'\xff'+bytes(n-1) if n==64 else b'ab'
        bus.md_read=read
        self.assertEqual(read_mortal_chunk(bus,0xe00000,64,lambda:None),b'ab'*32)
        self.assertEqual(len(bus.events),11+32)

    def test_unknown_rom_sha256_and_second_pass_mismatch_stop(self,sleep):
        p,payload,r=fixture(65536)
        for db,message in ((MdDatabase([]),'not in the database'),
                           (MdDatabase([dict(r,sha256='0'*64)]),'SHA-256')):
            with self.assertRaisesRegex(ProtocolError,message):dump_md(Cartridge(payload),p,db)
        bus=Cartridge(payload);read=bus.md_read
        def corrupt(a,n):
            value=read(a,n)
            return b'\xff'*n if len(bus.events)==20 else value
        bus.md_read=corrupt
        with self.assertRaisesRegex(ProtocolError,'Two MD ROM reads differ'):dump_md(bus,p,MdDatabase([r]))

    def test_changed_cart_does_not_issue_mapping_writes(self,sleep):
        p,_,r=fixture(65536,SSF2);_,payload,_=fixture(65536)
        bus=Cartridge(payload)
        with self.assertRaisesRegex(ProtocolError,'changed'):dump_md(bus,p,MdDatabase([r]))
        self.assertFalse(any(x.startswith('W') for x in bus.events));self.assertEqual(bus.modes[-1],0)

    def test_cancel_after_mapping_reset_is_attempted(self,sleep):
        p,payload,r=fixture(5242880,SSF2);bus=Cartridge(payload)
        count=[0]
        def progress(stage,done,total):
            count[0]+=1
            if count[0]==3:raise UserCancelled('cancel')
        with self.assertRaises(UserCancelled):dump_md(bus,p,MdDatabase([r]),progress)
        self.assertEqual(bus.events[-2:],['W,10563825,0','W,10563839,7'])
        self.assertEqual(bus.modes[-1],0)

    def test_incomplete_rom_and_attached_cart_change_stop(self,sleep):
        p,payload,r=fixture(65536);bus=Cartridge(payload);read=bus.md_read
        bus.md_read=lambda a,n:read(a,n) if n==16 else b'bad'
        with self.assertRaisesRegex(ProtocolError,'Incomplete'):dump_md(bus,p,MdDatabase([r]))
        p,payload,r=fixture(65536,SONIC_K);bus=Cartridge(payload)
        p=replace(p,attached_header=bytes(64))
        with self.assertRaisesRegex(ProtocolError,'attached cartridge changed'):dump_md(bus,p,MdDatabase([r]))


@patch('cart_station_md.time.sleep')
class MdServiceTests(unittest.TestCase):
    def service(self,folder,payload,record):
        config=Path(folder)/'paths.json'
        config.write_text(json.dumps(dict(version=1,pc=dict(NES=folder,MD=str(Path(folder)/'md')))))
        service=Service(Path(folder),database=Database([]),md_database=MdDatabase([record]),
                        environment='pc',paths_config=config,follow_system_paths=True)
        service.adapter=service.transport=Cartridge(payload)
        return service

    def test_capture_reuse_conflict_preservation_and_mgl(self,sleep):
        p,payload,r=fixture(65536)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=self.service(folder,payload,r)
            for _ in range(3):state=service.poll()
            self.assertTrue(state.supported);self.assertFalse(state.saved)
            dest=root/'md';dest.mkdir();original=dest/r['title'];original.write_bytes(b'preserve')
            path,created=service.capture(lambda *args:None)
            self.assertTrue(created);self.assertIn('.verified-',path.name)
            self.assertEqual(original.read_bytes(),b'preserve');self.assertEqual(path.read_bytes(),payload)
            self.assertFalse(service.capture(lambda *args:None)[1]);self.assertEqual(service.existing(),path)
            (root/'_Console').mkdir();(root/'_Console/MegaDrive_test.rbf').write_bytes(b'core')
            core=dict(rbf='_Console/MegaDrive',type='f',index=1,delay=2)
            mgl=create_mgl(path,core,root/'runtime',root,service.profile)
            self.assertEqual(ET.parse(mgl).find('rbf').text,'_Console/MegaDrive')
            self.assertTrue(verified_file(path,service.profile))
            path.write_bytes(bytes(len(payload)));self.assertFalse(verified_file(path,service.profile))

    def test_cancel_does_not_save_and_closes_reader(self,sleep):
        _,payload,r=fixture(65536)
        with tempfile.TemporaryDirectory() as folder:
            service=self.service(folder,payload,r)
            for _ in range(3):service.poll()
            bus=service.adapter
            def cancel(stage,*args):
                if stage=='Read 2/2':raise UserCancelled('cancel')
            with self.assertRaises(UserCancelled):service.capture(cancel)
            self.assertFalse((Path(folder)/'md').exists());self.assertTrue(bus.closed)

    def test_cli_identify_reads_headers_only(self,sleep):
        import cart_station_cli
        _,payload,_=fixture(65536);bus=Cartridge(payload)
        with patch('cart_station_cli.open_transport',return_value=bus),patch('cart_station_cli.Adapter',return_value=bus), \
             patch.object(sys,'argv',['cart_station.py','--port','COM5','identify-md']), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            cart_station_cli.main()
        result=json.loads(output.getvalue());self.assertEqual(result['system'],'MD')
        self.assertEqual(result['rom_size'],65536);self.assertTrue(bus.closed)
        self.assertEqual(len(bus.events),32)

    def test_legacy_defaults_and_explicit_paths_are_preserved(self,sleep):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'paths.json';text='{"version":1,"mister":{},"pc":{}}';config.write_text(text)
            self.assertEqual(default_output_directory('mister','MD',config),Path('/media/fat/games/MegaDrive'))
            self.assertEqual(default_output_directory('pc','MD',config),Path.home()/'RetroFreakDumps')
            self.assertEqual(read_core_config(config,'MD')['rbf'],'_Console/MegaDrive')
            self.assertEqual(config.read_text(),text)
            config.write_text('{"version":1,"mister":{"MD":"/media/fat/games/Genesis"}}')
            self.assertEqual(default_output_directory('mister','MD',config),Path('/media/fat/games/Genesis'))

    def test_database_and_presentation(self,sleep):
        p,payload,r=fixture();db=MdDatabase.load(data('md_profiles.reference.json'))
        self.assertEqual(len(db.index),1969)
        record=next(x for x in db.index.values() if x['title']=='Shining and the Darkness (Japan).md')
        self.assertEqual(record['size'],1048576)
        self.assertEqual(profile_columns(p),('None','1024 KiB','MD'))
        self.assertIn('GM TEST-00',profile_details(p))
        matched=MdDatabase([r]).match(p,payload);self.assertEqual(matched.filename(),r['title'])
        validate_payload(matched,payload)
        with self.assertRaises(ProtocolError):validate_payload(matched,payload[:-2])
        self.assertTrue(replace(matched,title='CON.md').filename().startswith('MD_'))
