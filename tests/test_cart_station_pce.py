"""HuCard synthetic bus and wire tests. No real ROM, reader or USB access."""
from _paths import ROOT,data  # noqa: F401
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
from cart_station_protocol import ProtocolError,packet
from cart_station_transport import Adapter
from cart_station_pce import (PceProfile,PceDatabase,SAMPLE,BIT_REVERSE,
                              inspect_header,identify_pce,dump_pce,read_rom,validate_payload,verified_file)
from cart_station_service import Service,UserCancelled,create_mgl,read_core_config
from cart_station_nes_db import Database
from cart_station_environment import default_output_directory
from cart_station_presentation import profile_columns,profile_details
from test_cart_station import Wire


def fixture(size=262144,controller='Linear',reversed_bits=False,sgx=False):
    # Distinct 8 KiB regions prevent accidental mirror matches in probe tests.
    payload=b''.join(bytes((i%251,))*8192 for i in range((size+8191)//8192))[:size]
    raw=payload.translate(BIT_REVERSE) if reversed_bits else payload
    profile=PceProfile(raw[:SAMPLE],controller)
    suffix='.sgx' if sgx else '.pce'
    record=dict(system='PCE',platform='SGX' if sgx else 'PCE',title='Synthetic HuCard (Test)'+suffix,
                size=size,sha1=hashlib.sha1(payload).hexdigest(),sha256=hashlib.sha256(payload).hexdigest())
    return profile,raw,record,payload


class Cartridge:
    def __init__(self,payload,controller='Linear'):
        self.payload=payload;self.controller=controller;self.bank=0
        self.events=[];self.modes=[];self.closed=False
    def status(self):return 8
    def product(self):return dict(build=10,product=4919)
    def mode(self,value):self.modes.append(value)
    def close(self):self.closed=True
    def pce_register(self,address,value):
        self.events.append('W,%d,%d'%(address,value))
        if self.controller=='Sf2Dash':self.bank=address-0x1ff0
    def pce_read(self,address,size):
        self.events.append('R,%d,%d'%(address,size))
        if self.controller=='Interval':
            if 0x40000<=address<0x80000:address-=0x40000
            elif address>=0x80000:address-=0x40000
        elif self.controller=='Sf2Dash' and address>=0x80000:
            address+=self.bank*0x80000
        return (self.payload*((address+size)//len(self.payload)+1))[address:address+size]


class PceWireTests(unittest.TestCase):
    def test_status_is_sent_only_after_complete_payload_not_pipelined(self):
        events=[]
        class ObservedWire(Wire):
            def write(self,value):events.append(('write',value));super().write(value)
            def read(self,size):
                value=super().read(size);events.append(('read',value));return value
        payload=bytes(range(20))
        wire=ObservedWire(packet(0,20)+payload+packet(0,4)+packet(8),fragment=3)
        self.assertEqual(Adapter(wire).pce_read(123,20),payload)
        self.assertEqual(wire.writes,[packet(0x2d,8,20,123,20),packet(6,0,4)])
        status_index=events.index(('write',packet(6,0,4)))
        before=b''.join(value for op,value in events[:status_index] if op=='read')
        self.assertEqual(before,packet(0,20)+payload);self.assertFalse(wire.replies)

    def test_bank_write_ack_and_whitelist(self):
        wire=Wire(packet(0,0)*2);Adapter(wire).pce_register(0x1ff1,255)
        self.assertEqual(wire.writes,[packet(0x2e,8,1,0x1ff1,1),b'\xff'])
        for address,value in ((0x80000,0),(0xf0000,115),(0x1fef,255),(0x2000,255),(0x1ff0,0)):
            wire=Wire(b'')
            with self.assertRaises(ValueError):Adapter(wire).pce_register(address,value)
            self.assertFalse(wire.writes)

    def test_read_bounds_truncation_and_changed_cart(self):
        for address,size in ((-1,1),(1048575,2),(0,32769),(0,0),(True,8)):
            with self.assertRaises(ValueError):Adapter(Wire(b'')).pce_read(address,size)
        with self.assertRaisesRegex(ProtocolError,'Truncated'):
            Adapter(Wire(packet(0,8)+b'ab')).pce_read(0,8)
        with self.assertRaisesRegex(ProtocolError,'changed'):
            Adapter(Wire(packet(0,2)+b'ab'+packet(0,4)+packet(5))).pce_read(0,2)


@patch('cart_station_pce.time.sleep')
class PceReadingTests(unittest.TestCase):
    def test_identification_reads_once_and_capture_reads_two_fresh_passes(self,sleep):
        p,raw,r,payload=fixture();bus=Cartridge(raw)
        matched=identify_pce(bus,p,PceDatabase([r]))
        self.assertEqual(matched.title,r['title']);self.assertEqual(matched.prg_size,len(raw))
        self.assertEqual(len(bus.events),2+8)
        bus.events=[]
        again,out=dump_pce(bus,matched,PceDatabase([r]))
        self.assertEqual(out,payload);self.assertEqual(len(bus.events),2+8*2)

    def test_probe_detects_three_reference_mappings(self,sleep):
        for controller,size in (('Linear',262144),('Interval',393216),('Sf2Dash',2621440)):
            p,raw,record,payload=fixture(size,controller);bus=Cartridge(raw,controller)
            actual=inspect_header(bus)
            self.assertEqual(actual.controller,controller);self.assertEqual(actual.header,p.header)
            self.assertEqual(actual.prg_size,0);self.assertIsNone(actual.support())
            self.assertEqual(bus.modes,[1,0]);self.assertEqual(bus.bank,0)

    def test_unstable_probe_and_wrong_cart_stop(self,sleep):
        p,raw,_,_=fixture();bus=Cartridge(raw);read=bus.pce_read
        def unstable(a,n):
            out=read(a,n);return b'\xff'*n if len(bus.events)==2 else out
        bus.pce_read=unstable
        with self.assertRaisesRegex(ProtocolError,'differ'):inspect_header(bus)
        self.assertEqual(bus.modes[-1],0)
        bus=Cartridge(raw);bus.status=lambda:2
        with self.assertRaises(ProtocolError):inspect_header(bus)
        self.assertFalse(bus.events)

    def test_manual_probe_does_not_write_mapping_registers(self,sleep):
        _,raw,_,_=fixture();bus=Cartridge(raw)
        self.assertEqual(inspect_header(bus,'Interval').controller,'Interval')
        self.assertEqual(len(bus.events),2)
        self.assertFalse(any(x.startswith('W') for x in bus.events))

    def test_three_bank_readers_match_reference_traces(self,sleep):
        cases=json.loads(data('pce_reference_traces.json').read_text())['cases']
        for case in cases:
            p,raw,_,_=fixture(case['size'],case['controller']);p=replace(p,prg_size=case['size'])
            bus=Cartridge(raw,case['controller'])
            self.assertEqual(read_rom(bus,p),raw)
            self.assertEqual(bus.events,case['trace'],case['controller'])

    def test_full_hash_size_detection_and_independent_second_pass(self,sleep):
        p,raw,r,payload=fixture();bus=Cartridge(raw)
        matched,out=dump_pce(bus,p,PceDatabase([r]))
        self.assertEqual(out,payload);self.assertEqual(matched.prg_size,len(payload))
        self.assertEqual(matched.data_order,'native');self.assertEqual(matched.title,r['title'])
        self.assertEqual(bus.events[2:10],bus.events[10:18]);self.assertEqual(bus.modes,[1,0])
        validate_payload(matched,out)

    def test_reversed_bits_are_selected_by_db_not_reset_vector(self,sleep):
        p,raw,r,payload=fixture(reversed_bits=True);bus=Cartridge(raw)
        matched,out=dump_pce(bus,p,PceDatabase([r]))
        self.assertEqual(out,payload);self.assertEqual(matched.data_order,'bit_reversed')
        self.assertEqual(matched.header,p.header);self.assertEqual(matched.id,p.id)
        validate_payload(matched,out)

    def test_interval_mapping_preserves_384k(self,sleep):
        p,raw,r,payload=fixture(393216,'Interval');bus=Cartridge(raw,'Interval')
        matched,out=dump_pce(bus,p,PceDatabase([r]))
        self.assertEqual(out,payload);self.assertEqual(matched.prg_size,393216)
        self.assertIn('R,524288,32768',bus.events)

    def test_sf2_size_window_and_cleanup(self,sleep):
        p,raw,r,payload=fixture(2621440,'Sf2Dash');bus=Cartridge(raw,'Sf2Dash')
        matched,out=dump_pce(bus,p,PceDatabase([r]))
        self.assertEqual(out,payload);self.assertEqual(matched.prg_size,2621440)
        self.assertIn('W,8179,255',bus.events);self.assertEqual(bus.events[-1],'W,8176,255')
        self.assertEqual(bus.bank,0)

    def test_unknown_rom_is_not_trimmed_by_mirror(self,sleep):
        p,raw,r,_=fixture(131072);bus=Cartridge(raw);r=dict(r,sha1='0'*40)
        with self.assertRaisesRegex(ProtocolError,'not in the database'):
            dump_pce(bus,p,PceDatabase([r]))
        self.assertIn('R,1015808,32768',bus.events);self.assertEqual(bus.modes[-1],0)

    def test_second_pass_mismatch_and_db_sha256_stop(self,sleep):
        p,raw,r,_=fixture(65536);bus=Cartridge(raw);read=bus.pce_read
        def mismatch(a,n):
            out=read(a,n);return b'\xff'*n if len(bus.events)==6 else out
        bus.pce_read=mismatch
        with self.assertRaisesRegex(ProtocolError,'Two PCE ROM reads differ'):
            dump_pce(bus,p,PceDatabase([r]))
        with self.assertRaisesRegex(ProtocolError,'SHA-256'):
            dump_pce(Cartridge(raw),p,PceDatabase([dict(r,sha256='0'*64)]))

    def test_ambiguous_data_order_stops(self,sleep):
        p,raw,r,_=fixture(65536)
        reverse=raw.translate(BIT_REVERSE)
        second=dict(r,title='Other.pce',sha1=hashlib.sha1(reverse).hexdigest(),sha256=hashlib.sha256(reverse).hexdigest())
        with self.assertRaisesRegex(ProtocolError,'Ambiguous'):
            dump_pce(Cartridge(raw),p,PceDatabase([r,second]))

    def test_cancel_after_sf2_mapping_restores_bank(self,sleep):
        p,raw,r,_=fixture(2621440,'Sf2Dash');bus=Cartridge(raw,'Sf2Dash');calls=[0]
        def cancel(*args):
            calls[0]+=1
            if calls[0]==2:raise UserCancelled('cancel')
        with self.assertRaises(UserCancelled):dump_pce(bus,p,PceDatabase([r]),cancel)
        self.assertEqual(bus.bank,0);self.assertEqual(bus.events[-1],'W,8176,255')
        self.assertEqual(bus.modes[-1],0)

    def test_changed_cart_stops_before_mapping_writes(self,sleep):
        p,raw,r,_=fixture(2621440,'Sf2Dash');bus=Cartridge(b'\xff'*len(raw),'Sf2Dash')
        with self.assertRaisesRegex(ProtocolError,'changed'):dump_pce(bus,p,PceDatabase([r]))
        self.assertFalse(any(x.startswith('W') for x in bus.events));self.assertEqual(bus.modes[-1],0)

    def test_incomplete_read_and_empty_database_stop(self,sleep):
        p,raw,r,_=fixture();bus=Cartridge(raw);read=bus.pce_read
        bus.pce_read=lambda a,n:read(a,n) if n==SAMPLE else b'ab'
        with self.assertRaisesRegex(ProtocolError,'Incomplete'):dump_pce(bus,p,PceDatabase([r]))
        with self.assertRaisesRegex(ProtocolError,'No PCE DB sizes'):dump_pce(Cartridge(raw),p,PceDatabase([]))


@patch('cart_station_pce.time.sleep')
class PceServiceTests(unittest.TestCase):
    def service(self,folder,raw,r):
        paths=Path(folder)/'paths.json'
        paths.write_text(json.dumps(dict(version=1,pc=dict(NES=folder,PCE=str(Path(folder)/'pce'),SGX=str(Path(folder)/'sgx')))))
        service=Service(Path(folder),database=Database([]),pce_database=PceDatabase([r]),
                        environment='pc',paths_config=paths,follow_system_paths=True)
        service.adapter=service.transport=Cartridge(raw)
        return service

    def test_save_reuse_file_verification_and_pce_mgl_index_zero(self,sleep):
        p,raw,r,payload=fixture()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=self.service(folder,raw,r)
            for _ in range(3):state=service.poll()
            self.assertTrue(state.supported);self.assertEqual(service.destination,root/'pce')
            self.assertEqual(state.profile.title,r['title']);self.assertEqual(state.profile.prg_size,len(payload))
            self.assertFalse(state.saved);self.assertFalse((root/'pce').exists())
            event_count=len(service.adapter.events)
            service.poll();self.assertEqual(len(service.adapter.events),event_count)
            path,created=service.capture(lambda *args:None)
            self.assertTrue(created);self.assertEqual(path.read_bytes(),payload)
            self.assertTrue(verified_file(path,service.profile));self.assertFalse(service.capture(lambda *args:None)[1])
            (root/'_Console').mkdir();(root/'_Console/TurboGrafx16_test.rbf').write_bytes(b'core')
            core=dict(rbf='_Console/TurboGrafx16',index=0,type='f',delay=2)
            mgl=create_mgl(path,core,root/'runtime',root,service.profile)
            self.assertEqual(ET.parse(mgl).find('file').get('index'),'0')
            path.write_bytes(bytes(len(payload)));self.assertFalse(verified_file(path,service.profile))

    def test_sgx_classification_routes_to_own_path_and_extension(self,sleep):
        p,raw,r,payload=fixture(sgx=True)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=self.service(folder,raw,r)
            for _ in range(3):service.poll()
            path,created=service.capture(lambda *args:None)
            self.assertEqual(service.profile.system,'SGX');self.assertEqual(path.parent,root/'sgx')
            self.assertEqual(path.suffix,'.sgx');self.assertTrue(created)

    def test_cancel_never_saves_and_closes_reader(self,sleep):
        p,raw,r,_=fixture()
        with tempfile.TemporaryDirectory() as folder:
            service=self.service(folder,raw,r)
            for _ in range(3):service.poll()
            bus=service.adapter
            def cancel(stage,*args):
                if stage=='Read 2/2':raise UserCancelled('cancel')
            with self.assertRaises(UserCancelled):service.capture(cancel)
            self.assertFalse((Path(folder)/'pce').exists());self.assertTrue(bus.closed)

    def test_cli_probe_has_no_fabricated_title_size_or_order(self,sleep):
        import cart_station_cli
        p,raw,r,_=fixture();bus=Cartridge(raw)
        with patch('cart_station_cli.open_transport',return_value=bus),patch('cart_station_cli.Adapter',return_value=bus), \
             patch.object(sys,'argv',['cart_station.py','--port','COM5','probe-pce']), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            cart_station_cli.main()
        result=json.loads(output.getvalue());self.assertEqual(result['state'],'sample_only')
        self.assertIsNone(result['rom_size']);self.assertEqual(result['data_order'],'unverified')
        self.assertEqual(result['controller'],'Linear');self.assertTrue(bus.closed)

    def test_cli_identify_resolves_title_without_saving(self,sleep):
        import cart_station_cli
        p,raw,r,_=fixture();bus=Cartridge(raw)
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'db.json';db.write_text(json.dumps(dict(version=1,records=[r])))
            with patch('cart_station_cli.open_transport',return_value=bus),patch('cart_station_cli.Adapter',return_value=bus), \
                 patch.object(sys,'argv',['cart_station.py','--port','COM5','identify-pce','--pce-db',str(db)]), \
                 patch('cart_station_service.save_verified_rom') as save,patch('sys.stdout',new_callable=io.StringIO) as output:
                cart_station_cli.main()
            result=json.loads(output.getvalue());self.assertEqual(result['state'],'identified')
            self.assertEqual(result['title'],r['title']);self.assertEqual(result['rom_size'],len(raw))
            save.assert_not_called();self.assertTrue(bus.closed)

    def test_identification_cancel_never_creates_files(self,sleep):
        _,raw,r,_=fixture()
        with tempfile.TemporaryDirectory() as folder:
            service=self.service(folder,raw,r);bus=service.adapter
            def cancel(*args):raise UserCancelled('stop identification')
            service.identification_progress=cancel
            with self.assertRaises(UserCancelled):
                for _ in range(3):service.poll()
            self.assertTrue(bus.closed);self.assertFalse((Path(folder)/'pce').exists())

    def test_legacy_paths_core_indices_and_explicit_config(self,sleep):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'paths.json';text='{"version":1,"mister":{},"pc":{}}';path.write_text(text)
            for system in ('PCE','SGX'):
                self.assertEqual(default_output_directory('mister',system,path),Path('/media/fat/games/TGFX16'))
            self.assertEqual(read_core_config(path,'PCE')['index'],0)
            self.assertEqual(read_core_config(path,'SGX')['index'],1)
            self.assertEqual(path.read_text(),text)
            path.write_text('{"version":1,"PCE":{"rbf":"_Console/TurboGrafx16","type":"f","index":1,"delay":2}}')
            with self.assertRaises(ProtocolError):read_core_config(path,'PCE')

    def test_database_scope_and_unknown_size_presentation(self,sleep):
        p,raw,r,payload=fixture();db=PceDatabase.load(data('pce_profiles.reference.json'))
        self.assertEqual(len(db.index),409)
        self.assertEqual(sum(x['platform']=='SGX' for x in db.index.values()),5)
        self.assertTrue(any(x['title']=='Moto Roader (Japan).pce' for x in db.index.values()))
        self.assertEqual(profile_columns(p),('Linear','Auto (DB)','PCE'))
        self.assertIn('Sample detected',profile_details(p))
        with self.assertRaises(ProtocolError):p.filename()
        matched=replace(PceDatabase([r]).match(p,payload),data_order='native')
        self.assertEqual(matched.filename(),r['title']);validate_payload(matched,payload)
        with self.assertRaises(ProtocolError):validate_payload(matched,payload[:-2])
