"""Synthetic GBA bus tests; no real cartridge, ROM or device access."""
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
from cart_station_gba import (GbaDatabase, MAX_ROM, dump_gba, inspect_header,
                              parse_header, validate_payload, verified_file)
from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter
from cart_station_environment import default_output_directory
from cart_station_service import Service, UserCancelled, read_core_config, create_mgl
from cart_station_nes_db import Database
from cart_station_presentation import profile_columns, profile_details
from test_cart_station import Wire


def fixture(size=1048576):
    payload = bytearray((bytes(range(256)) * ((size+255)//256))[:size])
    payload[0xa0:0xac] = b'SYNTHETICGBA'
    payload[0xac:0xb0] = b'TSTJ'
    payload[0xb2] = 0x96
    payload[0xbc] = 1
    payload[0xbd] = (-sum(payload[0xa0:0xbd])-0x19) & 255
    payload = bytes(payload)
    profile = parse_header(payload[:192])
    record = dict(system='GBA', title='Synthetic GBA (Test).gba', size=size,
                  sha1=hashlib.sha1(payload).hexdigest(), sha256=hashlib.sha256(payload).hexdigest())
    return profile, payload, record


class Cartridge:
    def __init__(self, payload):
        self.payload, self.events, self.modes = payload, [], []
        self.closed = False

    def status(self):return 6
    def product(self):return dict(build=10, product=4919)
    def mode(self, value):self.modes.append(value)
    def close(self):self.closed=True

    def gba_read(self, address, size):
        self.events.append((address,size))
        # Synthetic mirrored ROM beyond its useful size.
        return bytes(self.payload[(address+i)%len(self.payload)] for i in range(size))


class GbaHeaderAndWireTests(unittest.TestCase):
    def test_header_does_not_invent_size_and_preserves_code_revision(self):
        profile,_,_=fixture()
        self.assertEqual((profile.game_code,profile.revision,profile.prg_size),('TSTJ',1,0))
        self.assertIsNone(profile.support())
        with self.assertRaises(ProtocolError):profile.filename()

    def test_invalid_fixed_byte_checksum_and_length_stop(self):
        p,_,_=fixture()
        for address in (0xb2,0xa0,0xbd):
            header=bytearray(p.header);header[address]^=1
            with self.assertRaises(ProtocolError):parse_header(bytes(header))
        for header in (bytes(191),bytes(193),bytearray(p.header)):
            with self.assertRaises(ProtocolError):parse_header(header)

    def test_read_command_is_byte_addressed_with_status_before_payload(self):
        payload=bytes(range(80))
        wire=Wire(packet(0,80)+payload+packet(0,4)+packet(6),fragment=3)
        self.assertEqual(Adapter(wire).gba_read(0x123400,80),payload)
        self.assertEqual(wire.writes,[packet(0x1f,8,80,0x123400,80),packet(6,0,4)])
        self.assertFalse(wire.replies)

    def test_read_range_alignment_and_wrong_cartridge(self):
        for address,size in ((-2,2),(1,80),(0,1),(0,32770),(MAX_ROM-2,4),(True,80)):
            wire=Wire(b'')
            with self.assertRaises(ValueError):Adapter(wire).gba_read(address,size)
            self.assertFalse(wire.writes)
        wire=Wire(packet(0,2)+b'ab'+packet(0,4)+packet(5))
        with self.assertRaisesRegex(ProtocolError,'changed'):Adapter(wire).gba_read(0,2)

    def test_last_word_and_truncated_response(self):
        wire=Wire(packet(0,2)+b'ab'+packet(0,4)+packet(6))
        self.assertEqual(Adapter(wire).gba_read(MAX_ROM-2,2),b'ab')
        with self.assertRaisesRegex(ProtocolError,'Truncated'):
            Adapter(Wire(packet(0,80)+b'ab')).gba_read(0,80)


@patch('cart_station_gba.time.sleep')
class GbaReadingTests(unittest.TestCase):
    def test_inspection_reads_two_2k_blocks_and_releases(self,sleep):
        profile,payload,_=fixture();bus=Cartridge(payload)
        self.assertEqual(inspect_header(bus),profile)
        self.assertEqual(bus.events,[(0,2048)]*2);self.assertEqual(bus.modes,[0,0])
        sleep.assert_called_once_with(0.2)

    def test_header_area_mismatch_or_wrong_type_stops(self,sleep):
        _,payload,_=fixture();bus=Cartridge(payload)
        original=bus.gba_read;count=[0]
        def read(a,n):
            count[0]+=1;out=original(a,n)
            return out if count[0]==1 else bytes(n)
        bus.gba_read=read
        with self.assertRaises(ProtocolError):inspect_header(bus)
        self.assertEqual(bus.modes,[0,0])
        bus=Cartridge(payload);bus.status=lambda:5
        with self.assertRaises(ProtocolError):inspect_header(bus)
        self.assertEqual(bus.events,[])

    def test_full_hash_discovers_size_and_reads_independent_second_pass(self,sleep):
        profile,payload,record=fixture(262144);bus=Cartridge(payload)
        matched,out=dump_gba(bus,profile,GbaDatabase([record]))
        self.assertEqual(out,payload);self.assertEqual(matched.prg_size,len(payload))
        self.assertEqual(matched.title,record['title']);self.assertEqual(matched.id,profile.id)
        self.assertEqual(bus.events[0],(0,2048))
        self.assertEqual(bus.events[9], (0, 2048))
        self.assertEqual(bus.events[1:9],bus.events[10:18]);self.assertEqual(len(bus.events),18)
        self.assertEqual(bus.modes,[0,0,0]);validate_payload(matched,out)

    def test_ff_region_is_not_an_end_marker_and_small_nonmatch_continues(self,sleep):
        profile,payload,record=fixture(2097152)
        modified=bytearray(payload);modified[1048576:1081344]=b'\xff'*32768
        payload=bytes(modified);record.update(sha1=hashlib.sha1(payload).hexdigest(),sha256=hashlib.sha256(payload).hexdigest())
        decoy=dict(record,size=1048576,sha1='0'*40)
        matched,out=dump_gba(Cartridge(payload),profile,GbaDatabase([decoy,record]))
        self.assertEqual(out,payload);self.assertEqual(matched.prg_size,2097152)

    def test_non_power_of_two_database_length_is_verified_exactly(self,sleep):
        p,payload,r=fixture(65538);bus=Cartridge(payload)
        matched,out=dump_gba(bus,p,GbaDatabase([r]))
        self.assertEqual(len(out),65538);self.assertEqual(bus.events[-1],(65536,2))
        self.assertEqual(matched.prg_size,65538)

    def test_second_pass_mismatch_stops(self,sleep):
        p,payload,r=fixture(65536);bus=Cartridge(payload);original=bus.gba_read
        def read(a,n):
            out=original(a,n)
            return b'\xff'*n if len(bus.events)==6 else out
        bus.gba_read=read
        with self.assertRaisesRegex(ProtocolError,'Two GBA ROM reads differ'):dump_gba(bus,p,GbaDatabase([r]))
        self.assertEqual(bus.modes[-1],0)

    def test_unknown_rom_stops_at_32m_without_trimming_mirrors(self,sleep):
        p,payload,r=fixture(65536);bus=Cartridge(payload)
        r=dict(r,sha1='0'*40)
        with patch('cart_station_gba.MAX_ROM',131072):
            with self.assertRaisesRegex(ProtocolError,'not in the database'):dump_gba(bus,p,GbaDatabase([r]))
        self.assertEqual(max(a+n for a,n in bus.events),131072)
        self.assertEqual(bus.modes[-1],0)

    def test_cancel_before_first_rom_block_releases_mode(self,sleep):
        p,payload,r=fixture();bus=Cartridge(payload)
        def cancel(*args):raise UserCancelled('cancel')
        with self.assertRaises(UserCancelled):dump_gba(bus,p,GbaDatabase([r]),cancel)
        self.assertEqual(bus.events,[(0,2048)]);self.assertEqual(bus.modes[-1],0)

    def test_changed_header_and_transfer_error_release_mode(self,sleep):
        p,payload,r=fixture();bus=Cartridge(bytes(len(payload)))
        with self.assertRaisesRegex(ProtocolError,'changed'):dump_gba(bus,p,GbaDatabase([r]))
        self.assertEqual(bus.modes[-1],0)
        bus=Cartridge(payload)
        def fail(*args):raise ProtocolError('disconnected')
        bus.gba_read=fail
        with self.assertRaisesRegex(ProtocolError,'disconnected'):dump_gba(bus,p,GbaDatabase([r]))
        self.assertEqual(bus.modes[-1],0)

    def test_database_sha256_is_enforced(self,sleep):
        p,payload,r=fixture();r['sha256']='0'*64
        with self.assertRaisesRegex(ProtocolError,'SHA-256'):dump_gba(Cartridge(payload),p,GbaDatabase([r]))


@patch('cart_station_gba.time.sleep')
class GbaServiceTests(unittest.TestCase):
    def service(self,folder,payload,record):
        config=Path(folder)/'paths.json'
        config.write_text(json.dumps(dict(version=1,pc=dict(NES=folder,GBA=str(Path(folder)/'gba')))))
        service=Service(Path(folder),database=Database([]),gba_database=GbaDatabase([record]),
                        environment='pc',paths_config=config,follow_system_paths=True)
        service.adapter=service.transport=Cartridge(payload)
        return service

    def test_poll_capture_reuse_and_launch_mgl(self,sleep):
        p,payload,r=fixture(65536)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=self.service(folder,payload,r)
            for _ in range(3):state=service.poll()
            self.assertTrue(state.supported);self.assertFalse(state.saved)
            self.assertEqual(service.destination,root/'gba');self.assertEqual(service.profile.prg_size,0)
            path,created=service.capture(lambda *args:None)
            self.assertTrue(created);self.assertEqual(path.read_bytes(),payload)
            self.assertEqual(service.existing(),path)
            self.assertFalse(service.capture(lambda *args:None)[1])
            (root/'_Console').mkdir();(root/'_Console/GBA_test.rbf').write_bytes(b'core')
            core=dict(rbf='_Console/GBA',type='f',index=1,delay=2)
            mgl=create_mgl(path,core,root/'runtime',root,service.profile)
            self.assertEqual(ET.parse(mgl).find('rbf').text,'_Console/GBA')
            self.assertEqual(ET.parse(mgl).find('file').get('index'),'1')
            self.assertTrue(verified_file(path,service.profile))
            path.write_bytes(bytes(len(payload)));self.assertFalse(verified_file(path,service.profile))

    def test_cancel_never_creates_output_and_closes_reader(self,sleep):
        p,payload,r=fixture()
        with tempfile.TemporaryDirectory() as folder:
            service=self.service(folder,payload,r)
            for _ in range(3):service.poll()
            bus=service.adapter
            def cancel(stage,*args):
                if stage.startswith('Read 2/2'):raise UserCancelled('cancel')
            with self.assertRaises(UserCancelled):service.capture(cancel)
            self.assertFalse((Path(folder)/'gba').exists());self.assertTrue(bus.closed)

    def test_legacy_paths_and_core_fallback_preserve_config(self,sleep):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'paths.json';text='{"version":1,"mister":{},"pc":{}}';path.write_text(text)
            self.assertEqual(default_output_directory('mister','GBA',path),Path('/media/fat/games/GBA'))
            self.assertEqual(default_output_directory('pc','GBA',path),Path.home()/'RetroFreakDumps')
            self.assertEqual(read_core_config(path,'GBA')['rbf'],'_Console/GBA')
            self.assertEqual(path.read_text(),text)

    def test_presentation_and_embedded_database(self,sleep):
        p,payload,r=fixture()
        self.assertEqual(profile_columns(p),('Linear ROM','Auto (DB)','GBA'))
        self.assertIn('TSTJ',profile_details(p))
        matched=GbaDatabase([r]).match(p,payload)
        self.assertEqual(profile_columns(matched)[1],'1024 KiB')
        db=GbaDatabase.load(data('gba_profiles.reference.json'))
        self.assertEqual(len(db.index),3154)
        self.assertTrue(any(x['title']=='Battle Network - Rockman EXE 3 (Japan).gba' for x in db.index.values()))

    def test_cli_identify_does_not_dump_or_save(self,sleep):
        import cart_station_cli
        p,payload,r=fixture();bus=Cartridge(payload)
        with patch('cart_station_cli.open_transport',return_value=bus),patch('cart_station_cli.Adapter',return_value=bus), \
             patch.object(sys,'argv',['cart_station.py','--port','COM5','identify-gba']), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            cart_station_cli.main()
        result=json.loads(output.getvalue())
        self.assertEqual(result['game_code'],'TSTJ');self.assertIsNone(result['rom_size'])
        self.assertEqual(bus.events,[(0,2048)]*2);self.assertTrue(bus.closed)


class GbaDatabaseTests(unittest.TestCase):
    def test_invalid_record_sizes_hashes_conflicts_and_format(self):
        p,payload,r=fixture()
        for change in (dict(size=33554434),dict(size=65537),dict(size=True),dict(system='GB'),
                       dict(sha1='bad'),dict(sha256='bad'),dict(title='')):
            with self.assertRaises(ProtocolError):GbaDatabase([dict(r,**change)])
        with self.assertRaises(ProtocolError):GbaDatabase([r,dict(r,title='conflict')])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.json';path.write_text('{"version":true,"records":[]}')
            with self.assertRaises(ProtocolError):GbaDatabase.load(path)

    def test_filename_sanitization_and_validation(self):
        p,payload,r=fixture();p=GbaDatabase([r]).match(p,payload)
        self.assertEqual(replace(p,title='Bad:/Name.gba').filename(),'Bad__Name.gba')
        self.assertTrue(replace(p,title='CON.gba').filename().startswith('GBA_'))
        with self.assertRaises(ProtocolError):validate_payload(p,payload[:-2])
        changed=bytearray(payload);changed[-1]^=1
        with self.assertRaises(ProtocolError):validate_payload(p,changed)
