"""Synthetic SFC ROMs and wire replies only; never access hardware."""
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
from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter
from cart_station_sfc import (SfcDatabase, BLOCK, LIMITS, parse_header, inspect_header,
    choose_controller, rom_address, read_rom, dump_sfc, validate_payload, verified_file)
from cart_station_service import Service, UserCancelled, read_core_config, create_mgl
from cart_station_nes_db import Database
from cart_station_environment import default_output_directory
from cart_station_presentation import profile_details, profile_columns
from test_cart_station import Wire, status


def fixture(size=1048576, makeup=0x20, chip=0, title=b'SYNTHETIC SFC', actual_size=None):
    h = bytearray(48)
    h[16:37] = title.ljust(21, b' ')
    h[37:44] = bytes((makeup, chip, size.bit_length()-11, 0, 0, 1, 0))
    h[44:48] = bytes.fromhex('cbed3412')
    profile = parse_header(bytes(h))
    size = actual_size or size
    raw = bytearray(bytes(range(256)) * (size//256))
    offset = 0xffb0 if profile.controller == 'HiRom' else 0x40ffb0 if profile.controller == 'ExHiRom' else 0x7fb0
    raw[offset:offset+48] = h
    raw = bytes(raw)
    record = dict(system='SFC', title='Synthetic SFC (Test).sfc', size=len(raw),
                  sha1=hashlib.sha1(raw).hexdigest(), sha256=hashlib.sha256(raw).hexdigest())
    return profile, raw, record


class Cartridge:
    def __init__(self, profile, raw):
        self.profile, self.raw = profile, raw
        self.events, self.modes = [], []
        self.closed = False
        self.override = None

    def status(self): return 1
    def product(self): return dict(build=10, product=4919)
    def snes_mode(self, value): self.modes.append(value)
    def close(self): self.closed = True

    def snes_read(self, address, size):
        self.events.append('R,%d,%d' % (address, size))
        if self.override:
            value = self.override(address, size)
            if value is not None:
                return value
        if address == 0xffb0 and size == 48:
            return self.profile.header
        # Independent SNES address decoder (not rom_address()).
        bank, offset = address >> 16, address & 0xffff
        controller = self.profile.controller
        if controller == 'HiRom':
            logical = ((bank & 0x3f) << 16) + offset
        elif controller == 'ExHiRom':
            logical = ((bank & 0x3f) << 16) + offset + (0 if bank >= 0xc0 else 0x400000)
        elif controller == 'BigLoRom':
            logical = ((bank if bank < 0x80 else bank-0x40) << 15) + (offset & 0x7fff)
        elif controller == 'LoRomAndHiRom' and 0x40 <= bank < 0x80:
            logical = 0x200000 + ((bank-0x40) << 15) + offset
        else:
            logical = ((bank & 0x7f) << 15) + (offset & 0x7fff)
        # Return plausible mirror data for probes outside a smaller ROM.
        logical %= len(self.raw)
        value = self.raw[logical:logical + size]
        return value + self.raw[:size - len(value)]


class SfcWireHeaderTests(unittest.TestCase):
    def test_wire_read_07_and_pipelined_status_01(self):
        wire = Wire(packet(0, 3) + b'rom' + status(1), fragment=1)
        self.assertEqual(Adapter(wire).snes_read(0x808000, 3), b'rom')
        self.assertEqual(wire.writes, [packet(7, 8, 3, 0x808000, 3), packet(6, 0, 4)])

    def test_control_sequence_matches_reference_and_preserves_raw_acks(self):
        wire = Wire(bytes(8*7)); adapter = Adapter(wire)
        adapter.snes_mode(1); adapter.snes_mode(0)
        self.assertEqual(wire.writes, [packet(4,4,0,1), packet(47,8,0,0,1),
            packet(5,0,0), packet(5,0,0), packet(47,8,0,0,0), packet(4,4,0,0), packet(5,0,0)])
        self.assertEqual(len(adapter.control_replies), 7)

    def test_wire_bounds_truncation_changed_cartridge(self):
        adapter = Adapter(Wire(b''))
        for address, size in ((-1,1),(0,0),(0,32769),(0xffffff,2),(True,1)):
            with self.assertRaises(ValueError): adapter.snes_read(address,size)
        self.assertEqual(adapter.transport.writes, [])
        for reply in (packet(0,3)+b'x',packet(0,3)+b'rom'+status(5)):
            with self.assertRaises(ProtocolError): Adapter(Wire(reply)).snes_read(0x8000,3)
        with self.assertRaises(ValueError): adapter.snes_mode(2)

    def test_header_map_size_and_superfx(self):
        for mode, expected in ((0x20,'LoRom'),(0x30,'LoRom'),(0x21,'HiRom'),(0x31,'HiRom'),(0x35,'ExHiRom')):
            p, _, _ = fixture(8388608 if mode==0x35 else 1048576, mode)
            self.assertEqual(p.controller,expected); self.assertIsNone(p.support())
        p, raw, _ = fixture(chip=0x13)
        self.assertEqual(p.chip,'SuperFX'); self.assertIsNone(p.support())
        bus = Cartridge(p,raw)
        self.assertEqual(inspect_header(bus),p); self.assertEqual(bus.modes,[1,0])
        self.assertEqual(bus.events[0],'R,49152,16384')

    def test_invalid_headers_and_unsupported_dedicated_readers(self):
        p, _, _ = fixture()
        for pos, value in ((37,0xff),(39,255),(44,0)):
            h=bytearray(p.header);h[pos]=value
            with self.assertRaises(ProtocolError): parse_header(bytes(h))
        with self.assertRaises(ProtocolError): parse_header(bytes(47))
        for makeup in (0x23,0x32,0x3a):
            special=bytearray(p.header);special[37]=makeup
            self.assertIsNotNone(parse_header(bytes(special)).support())
        self.assertIsNotNone(fixture(chip=0xf3)[0].support())
        for title in (b'MENU PROGRAM',b'ADD-ON BASE CASSETE'):
            self.assertIsNotNone(fixture(title=title)[0].support())

    def test_missing_japanese_codec_only_changes_preview(self):
        p, raw, record = fixture(title=bytes.fromhex('836583588367'))
        with patch('cart_station_md.codecs.getdecoder',side_effect=LookupError('codec unavailable')):
            fallback=parse_header(p.header)
        self.assertEqual(fallback.id,p.id); self.assertEqual(fallback.header,p.header)
        matched=SfcDatabase([record]).match(fallback,raw); validate_payload(matched,raw)

    def test_probe_instability_and_failure_release_modes(self):
        p,raw,_=fixture();bus=Cartridge(p,raw)
        count=[0]
        def unstable(address,size):
            if address==0xffb0:
                count[0]+=1
                return p.header if count[0]==1 else bytes(48)
        bus.override=unstable
        with self.assertRaises(ProtocolError):inspect_header(bus)
        self.assertEqual(bus.modes,[1,0])

    def test_large_lorom_probes_and_big_mapping(self):
        p,raw,_=fixture(4194304);bus=Cartridge(p,raw)
        bus.override=lambda address,size: bytes([0x11 if address==0x400000 else 0x22 if address==0x410000 else 0x33])*size
        self.assertEqual(choose_controller(bus,p).controller,'LoRomAndHiRom')
        h=bytearray(p.header);h[42]=51;h[2]=90;h[5]=65
        big=replace(p,header=bytes(h));bus.events.clear()
        self.assertEqual(choose_controller(bus,big).controller,'BigLoRom');self.assertEqual(bus.events,[])


class SfcRomTests(unittest.TestCase):
    def test_all_five_bank_traces_match_reference(self):
        reference=json.loads(data('sfc_reference_traces.json').read_text(encoding='utf-8-sig'))
        for case in reference['cases']:
            controller,size=case['controller'],case['size']
            with self.subTest(controller=controller):
                actual=['R,%d,%d'%(rom_address(controller,b),BLOCK) for b in range(size//BLOCK)]
                self.assertEqual(actual,case['trace'])
                self.assertEqual(max(rom_address(controller,b)+BLOCK for b in range(size//BLOCK))<=0x1000000,True)

    def test_independent_bus_reconstructs_five_maps_and_edge_banks(self):
        for controller in LIMITS:
            mode=0x35 if controller=='ExHiRom' else 0x21 if controller=='HiRom' else 0x20
            p,raw,_=fixture(LIMITS[controller],mode)
            # Bank-specific contents catch accidental address aliases.
            raw=bytes().join(bytes([b])*BLOCK for b in range(len(raw)//BLOCK))
            p=replace(p,controller=controller)
            self.assertEqual(read_rom(Cartridge(p,raw),p,len(raw)),raw)
        for bank in (-1,128):
            with self.assertRaises(ValueError):rom_address('LoRom',bank)

    def test_two_fresh_passes_match_full_db_and_superfx(self):
        p,raw,record=fixture(chip=0x13);bus=Cartridge(p,raw);stages=[]
        matched,actual=dump_sfc(bus,p,SfcDatabase([record]),lambda *args:stages.append(args))
        self.assertEqual(actual,raw); self.assertEqual(matched.filename(),record['title'])
        self.assertEqual(bus.events.count('R,32768,32768'),2)
        self.assertEqual(bus.modes,[1,0,1,0]);self.assertTrue(any(s[0]=='Read 2/2' for s in stages))
        with self.assertRaises(ProtocolError):p.filename()

    def test_non_power_size_requires_recorded_full_hash(self):
        p,raw,record=fixture(2097152,actual_size=1572864)
        matched,actual=dump_sfc(Cartridge(p,raw),p,SfcDatabase([record]))
        self.assertEqual(len(actual),1572864);self.assertEqual(matched.prg_size,len(actual))

    def test_unknown_db_and_sha256_mismatch_stop_without_second_pass(self):
        p,raw,record=fixture()
        for records in ([],[dict(record,sha1='0'*40)],[dict(record,sha256='0'*64)]):
            bus=Cartridge(p,raw)
            with self.assertRaises(ProtocolError):dump_sfc(bus,p,SfcDatabase(records))
            self.assertEqual(bus.modes,[1,0])
            self.assertLessEqual(bus.events.count('R,32768,32768'),1)

    def test_second_pass_mismatch_incomplete_cancel_and_header_changed(self):
        p,raw,record=fixture()
        bus=Cartridge(p,raw);count=[0]
        def mismatch(address,size):
            if address==0x18000 and size==BLOCK:
                count[0]+=1
                if count[0]>1:return bytes(BLOCK)
        bus.override=mismatch
        with self.assertRaisesRegex(ProtocolError,'Two SFC'):dump_sfc(bus,p,SfcDatabase([record]))
        self.assertEqual(bus.modes[-1],0)
        bus=Cartridge(p,raw);bus.override=lambda a,s: b'' if s==BLOCK else None
        with self.assertRaisesRegex(ProtocolError,'Incomplete'):dump_sfc(bus,p,SfcDatabase([record]))
        bus=Cartridge(p,raw)
        def cancel(stage,*args):
            if stage=='Read 1/2':raise UserCancelled('cancel')
        with self.assertRaises(UserCancelled):dump_sfc(bus,p,SfcDatabase([record]),cancel)
        self.assertEqual(bus.modes,[1,0])
        other,_,_=fixture(title=b'OTHER CART');bus=Cartridge(other,raw)
        with self.assertRaisesRegex(ProtocolError,'changed'):dump_sfc(bus,p,SfcDatabase([record]))
        self.assertNotIn('R,32768,32768',bus.events)

    def test_payload_validation_rejects_length_header_hash_and_sha256(self):
        p,raw,record=fixture();matched=SfcDatabase([record]).match(p,raw)
        for bad in (raw[:-1],bytes(len(raw))):
            with self.assertRaises(ProtocolError):validate_payload(matched,bad)
        with self.assertRaises(ProtocolError):validate_payload(replace(matched,raw_sha1='0'*40),raw)
        with self.assertRaises(ProtocolError):validate_payload(replace(matched,file_sha256='0'*64),raw)


class SfcServiceTests(unittest.TestCase):
    def service(self,root,p,raw,record):
        config=root/'paths.json'
        config.write_text(json.dumps(dict(version=1,pc=dict(SFC=str(root/'sfc')))))
        service=Service(root,database=Database([]),sfc_database=SfcDatabase([record]),
                        environment='pc',paths_config=config,follow_system_paths=True)
        service.adapter=service.transport=Cartridge(p,raw)
        return service

    def test_header_only_poll_save_reuse_conflict_and_verified_mgl(self):
        p,raw,record=fixture()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=self.service(root,p,raw,record)
            for _ in range(3):state=service.poll()
            self.assertTrue(state.supported);self.assertFalse(state.saved)
            self.assertFalse((root/'sfc').exists());self.assertEqual(service.profile.title,p.title)
            self.assertNotIn('R,32768,32768',service.adapter.events)
            path,created=service.capture(lambda *args:None)
            self.assertTrue(created);self.assertEqual(path.parent,root/'sfc');self.assertTrue(verified_file(path,service.profile))
            self.assertFalse(service.capture(lambda *args:None)[1])
            for _ in range(3):state=service.poll()
            self.assertEqual(state.profile.title,record['title']);self.assertTrue(state.saved)
            (root/'_Console').mkdir();(root/'_Console/SNES_test.rbf').write_bytes(b'core')
            mgl=create_mgl(path,read_core_config(system='SFC'),root/'runtime',root,service.profile)
            self.assertEqual(ET.parse(mgl).find('file').get('index'),'1')
            path.write_bytes(b'incomplete')
            self.assertFalse(verified_file(path,service.profile))
            alternate,created=service.capture(lambda *args:None)
            self.assertTrue(created);self.assertNotEqual(alternate,path);self.assertEqual(path.read_bytes(),b'incomplete')

    def test_service_cancel_closes_no_saved_file(self):
        p,raw,record=fixture()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=self.service(root,p,raw,record)
            for _ in range(3):service.poll()
            bus=service.adapter
            def cancel(stage,*args):
                if stage=='Read 2/2':raise UserCancelled('cancel')
            with self.assertRaises(UserCancelled):service.capture(cancel)
            self.assertTrue(bus.closed);self.assertFalse((root/'sfc').exists())

    def test_paths_fallback_explicit_override_and_core_index(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'paths.json';config.write_text('{"version":1,"pc":{},"mister":{}}')
            self.assertEqual(default_output_directory('mister','SFC',config),Path('/media/fat/games/SNES'))
            config.write_text('{"version":1,"mister":{"SFC":"/media/fat/games/custom"}}')
            self.assertEqual(default_output_directory('mister','SFC',config),Path('/media/fat/games/custom'))
            config.write_text('{"version":1,"SFC":{"rbf":"_Console/SNES","index":0,"type":"f","delay":2}}')
            with self.assertRaises(ProtocolError):read_core_config(config,'SFC')
            config.write_text('{"version":1}');self.assertEqual(read_core_config(config,'SFC')['index'],1)

    def test_cli_identify_header_no_save_and_confirmation_decline(self):
        import cart_station_cli
        p,raw,_=fixture()
        for command in ('identify-sfc','dump-sfc'):
            bus=Cartridge(p,raw)
            with patch('cart_station_cli.open_transport',return_value=bus),patch('cart_station_cli.Adapter',return_value=bus), \
                 patch.object(sys,'argv',['cart_station.py','--port','COM5',command]), \
                 patch('sys.stdin.isatty',return_value=True),patch('builtins.input',return_value='n'), \
                 patch('cart_station_service.save_verified_rom') as save,patch('sys.stdout',new_callable=io.StringIO) as output:
                cart_station_cli.main()
            result=json.loads(output.getvalue());self.assertEqual(result['state'],'header_only')
            self.assertEqual(result['system'],'SFC');self.assertTrue(bus.closed)
            save.assert_not_called();self.assertNotIn('R,32768,32768',bus.events)

    def test_reference_metadata_and_target_titles_present(self):
        db=SfcDatabase.load()
        self.assertEqual(len(db.index),3693)
        titles=[r['title'] for r in db.index.values()]
        self.assertTrue(any('Itadaki Street 2' in title for title in titles))
        self.assertTrue(any('Star Fox (Japan)' in title for title in titles))
        self.assertIn('SFC',profile_details(fixture()[0]));self.assertEqual(profile_columns(fixture()[0])[2],'SFC')


if __name__=='__main__':
    unittest.main()
