"""GB/GBC tests use generated cartridge bytes; never open a reader."""
from _paths import ROOT, data  # noqa: F401
import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cart_station_transport import Adapter
from cart_station_protocol import ProtocolError, packet
from cart_station_gb import (GbDatabase, parse_header, inspect_header, dump_gb, validate_payload,
                             verified_file, read_rom)
from cart_station_manager import Screen, render
from cart_station_service import Service, UserCancelled
from cart_station_nes_db import Database
from cart_station_environment import default_output_directory
from cart_station_presentation import profile_details, profile_columns
from test_cart_station import Wire, status


def fixture(cart=1, size_code=3, cgb=0, title='TEST ROM', variant=0):
    size = 32768 << size_code
    payload = bytearray(b''.join(bytes(((bank + variant) & 255, bank >> 8)) * 8192
                                 for bank in range(size // 16384)))
    payload[0x104:0x134] = bytes.fromhex('CEED6666CC0D000B03730083000C000D0008111F8889000EDCCC6EE6DDDDD999BBBB67636E0EECCCDDDC999FBBB9333E')
    payload[0x134:0x150] = bytes(28)
    label = title.encode('ascii')[:11 if cgb else 16]
    payload[0x134:0x134 + len(label)] = label
    if cgb:
        payload[0x143] = cgb
    payload[0x147], payload[0x148] = cart, size_code
    payload[0x14d] = (-sum(payload[0x134:0x14d]) - 25) & 255
    checksum = sum(payload) & 65535
    payload[0x14e:0x150] = checksum.to_bytes(2, 'big')
    payload = bytes(payload)
    profile = parse_header(payload[0x100:0x150])
    suffix = '.gbc' if cgb else '.gb'
    record = dict(system=profile.system, title='Database Title' + str(variant) + suffix,
        size=size, sha1=hashlib.sha1(payload).hexdigest(), sha256=hashlib.sha256(payload).hexdigest())
    return profile, payload, record


class Cartridge:
    """Independent hardware model of fixed and switchable ROM windows."""
    def __init__(self, payload):
        self.payload = payload
        self.cart = payload[0x147]
        self.bank, self.high, self.mode_value = 1, 0, 0
        self.events, self.modes = [], []

    def status(self):
        return 5

    def mode(self, value):
        self.modes.append(value)

    def gb_register(self, address, value):
        self.events.append(('W', address, value))
        assert address in (0x2100, 0x3000, 0x4000, 0x6000)
        if address == 0x2100:
            self.bank = value
        elif address in (0x3000, 0x4000):
            self.high = value
        else:
            self.mode_value = value

    def gb_read(self, address, size):
        self.events.append(('R', address, size))
        if self.cart in (0, 8, 9):
            offset = address
        elif address < 0x4000:
            offset = address
        else:
            if self.cart in (1, 2, 3):
                bank = (self.high << 5) | ((self.bank & 31) or 1)
            elif self.cart in (5, 6):
                bank = (self.bank & 15) or 1
            elif 0x0f <= self.cart <= 0x13:
                bank = (self.bank & 127) or 1
            else:
                bank = (self.high << 8) | self.bank
            offset = bank * 16384 + address - 0x4000
        return self.payload[offset:offset + size]


@patch('cart_station_gb.time.sleep')
class GbReaderTests(unittest.TestCase):
    def test_rom_bank_sequences_match_reference_traces(self, sleep):
        reference = json.loads(data('gb_reference_traces.json').read_text())
        class TraceBus:
            def __init__(self):self.trace=[]
            def gb_register(self,address,value):self.trace.append('W,%d,%d'%(address,value))
            gb_mbc6_register = gb_register
            def gb_tama5_bank(self, bank):
                self.gb_register(0xa001, 1)
                self.gb_register(0xa000, bank // 16)
                self.gb_register(0xa001, 0)
                if bank:
                    self.gb_register(0xa000, bank % 16)
            def gb_read(self,address,size):
                self.trace.append('R,%d,%d'%(address,size))
                return bytes(size)
        for case in reference['cases']:
            controller={'None': 'ROM', 'Tama5': 'TAMA5'}.get(case['controller'], case['controller'])
            base,_,_=fixture()
            profile=replace(base,controller=controller,prg_size=case['size'])
            bus=TraceBus();read_rom(bus,profile)
            expected = case['trace']
            if controller in ('HuC3', 'MBC7'):
                # Keep the baseline trace unchanged in the fixture; make
                # the documented seven-bit ROM corrections explicit here.
                expected = []
                for bank in range(case['size'] // 16384):
                    original = case['trace'][bank * 3:bank * 3 + 3]
                    self.assertEqual(len(original), 3)
                    expected.append('W,8448,%d' % bank)
                    if controller == 'HuC3':
                        expected.append('W,16384,0')
                    expected.append(original[2])
            self.assertEqual(bus.trace, expected)

    def test_header_fields_cgb_modes_and_controller_types(self, sleep):
        for cart, expected in ((0,'ROM'), (8,'ROM'), (3,'MBC1'), (6,'MBC2'),
                               (0x10,'MBC3'), (0x1e,'MBC5'), (0xff,'HuC1')):
            profile, _, _ = fixture(cart=cart)
            self.assertEqual(profile.controller, expected)
        for flag in (0x80, 0xc0):
            profile, _, _ = fixture(cgb=flag)
            self.assertEqual(profile.system, 'GBC')
            self.assertEqual(profile.title, 'TEST ROM')

    def test_bad_header_logo_and_checksum_stop(self, sleep):
        profile, _, _ = fixture()
        for index in (4, 0x34, 0x4d):
            damaged = bytearray(profile.header)
            damaged[index] ^= 1
            with self.assertRaises(ProtocolError):
                parse_header(bytes(damaged))
        with self.assertRaises(ProtocolError):
            parse_header(profile.header[:-1])

    def test_header_is_read_twice_without_bank_or_save_writes(self, sleep):
        expected, payload, _ = fixture()
        bus = Cartridge(payload)
        self.assertEqual(inspect_header(bus), expected)
        self.assertEqual(bus.events, [('R', 0x100, 80)] * 2)
        self.assertEqual(bus.modes, [1, 0])

    def test_unstable_header_and_transport_error_cleanup(self, sleep):
        _, payload, _ = fixture()
        for responses in ([payload[0x100:0x150], bytes(80)], ProtocolError('disconnected')):
            bus = Cartridge(payload)
            bus.gb_read = Mock(side_effect=responses)
            with self.assertRaises(ProtocolError):
                inspect_header(bus)
            self.assertEqual(bus.modes, [1, 0])

    def test_full_reads_for_each_controller_and_cgb(self, sleep):
        for cart, size, flag in ((0,0,0),(1,4,0),(5,3,0),(0x13,6,0),(0x1b,6,0x80),(0x19,8,0xc0)):
            with self.subTest(cart=cart,size=size):
                profile, payload, record = fixture(cart, size, flag)
                bus = Cartridge(payload)
                matched, result = dump_gb(bus, profile, GbDatabase([record]))
                self.assertEqual(result, payload)
                self.assertEqual(matched.raw_sha1, record['sha1'])
                self.assertEqual(matched.title, record['title'])
                self.assertEqual(bus.modes, [1, 0])
                bank_reads = [e for e in bus.events if e[0] == 'R' and e[2] == 16384]
                self.assertEqual(len(bank_reads), 2 * len(payload) // 16384)
                self.assertTrue(all(e[1] in (0x2100,0x3000,0x4000,0x6000) for e in bus.events if e[0]=='W'))

    def test_mbc5_high_bit_boundary_and_fresh_second_read(self, sleep):
        profile, payload, record = fixture(0x19, 8, 0x80)
        bus = Cartridge(payload)
        dump_gb(bus, profile, GbDatabase([record]))
        self.assertIn(('W',0x3000,1), bus.events)
        low_writes = [e[2] for e in bus.events if e[:2] == ('W',0x2100)]
        self.assertEqual(low_writes, list(range(256))*4)

    def test_unsupported_geometry_does_not_touch_bus(self, sleep):
        for cart, size in ((1,7),(0,1),(5,4),(0x13,7),(0xfc,6),(0x0b,3)):
            profile, _, _ = fixture(cart,size)
            bus = Mock()
            with self.assertRaises(ProtocolError):
                dump_gb(bus,profile,GbDatabase([]))
            self.assertEqual(bus.mock_calls,[])

    def test_changed_cartridge_rejected_before_bank_write(self, sleep):
        profile, _, record = fixture()
        _, other, _ = fixture(variant=1)
        bus = Cartridge(other)
        # Change metadata as well as payload so the pre-write check detects it.
        bus.payload = other[:0x134]+b'X'+other[0x135:]
        with self.assertRaisesRegex(ProtocolError,'cartridge changed'):
            dump_gb(bus,profile,GbDatabase([record]))
        self.assertFalse(any(e[0]=='W' for e in bus.events))
        self.assertEqual(bus.modes,[1,0])

    def test_different_second_read_is_not_saved(self, sleep):
        profile,payload,record=fixture()
        bus=Cartridge(payload)
        def progress(stage,done,total):
            if stage=='Read 2/2':
                bus.payload=payload[:-1]+bytes((payload[-1]^1,))
        with self.assertRaisesRegex(ProtocolError,'Two GB ROM reads differ'):
            dump_gb(bus,profile,GbDatabase([record]),progress)
        self.assertEqual(bus.modes,[1,0])

    def test_wrong_bank_and_unknown_hash_fail(self, sleep):
        profile,payload,record=fixture()
        for wrong_bank in (False,True):
            bus=Cartridge(payload)
            if wrong_bank:
                bus.gb_register=lambda *args:None
            with self.assertRaisesRegex(ProtocolError,'not in the database'):
                dump_gb(bus,profile,GbDatabase([record] if wrong_bank else []))
            self.assertEqual(bus.modes,[1,0])

    def test_wrong_db_sha256_is_rejected(self,sleep):
        profile,payload,record=fixture()
        record['sha256']='0'*64
        with self.assertRaisesRegex(ProtocolError,'SHA-256 mismatch'):
            dump_gb(Cartridge(payload),profile,GbDatabase([record]))

    def test_payload_header_and_length_are_verified(self,sleep):
        profile,payload,record=fixture()
        matched=GbDatabase([record]).match(profile,payload)
        for changed in (payload[:-1],payload[:0x134]+b'X'+payload[0x135:]):
            with self.assertRaises(ProtocolError):validate_payload(matched,changed)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/matched.filename();path.write_bytes(payload)
            self.assertTrue(verified_file(path,matched))
            path.write_bytes(payload[:-1]);self.assertFalse(verified_file(path,matched))

    def test_name_needs_db_match_and_cannot_escape_directory(self,sleep):
        profile,payload,record=fixture(cgb=0x80)
        with self.assertRaises(ProtocolError):profile.filename()
        record['title']='../CON:bad.gbc'
        matched=GbDatabase([record]).match(profile,payload)
        self.assertNotIn('/',matched.filename())
        self.assertTrue(matched.filename().endswith('.gbc'))

    def test_service_poll_capture_reuse_and_launch(self,sleep):
        profile,payload,record=fixture(0x1b,3,0x80)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);dest=root/'games/Gameboy';(root/'_Console').mkdir()
            (root/'_Console/Gameboy_test.rbf').write_bytes(b'core')
            service=Service(dest,database=Database([]),gb_database=GbDatabase([record]),sd_root=root)
            bus=Cartridge(payload);service.adapter=bus
            for _ in range(3):state=service.poll()
            self.assertTrue(state.supported);self.assertFalse(state.saved)
            self.assertEqual(state.destination,dest)
            path,created=service.capture(lambda *args:None)
            self.assertTrue(created);self.assertEqual(path.read_bytes(),payload)
            self.assertEqual(path.suffix,'.gbc');self.assertEqual(service.profile.title,record['title'])
            self.assertFalse(service.capture(lambda *args:None)[1])
            self.assertEqual(service.existing(),path)
            mgl=ET.parse(service.prepare_game())
            self.assertEqual(mgl.find('rbf').text,'_Console/Gameboy')
            self.assertEqual(mgl.find('file').get('path'),str(path.resolve()))
            self.assertEqual(mgl.find('file').get('index'),'1')

    def test_service_conflict_is_preserved_and_verified_alternate_used(self,sleep):
        profile,payload,record=fixture()
        matched=GbDatabase([record]).match(profile,payload)
        with tempfile.TemporaryDirectory() as folder:
            dest=Path(folder);old=dest/matched.filename();old.write_bytes(b'old data')
            service=Service(dest,database=Database([]),gb_database=GbDatabase([record]))
            service.adapter,service.profile,service.selected_id=Cartridge(payload),profile,profile.id
            path,created=service.capture(lambda *args:None)
            self.assertTrue(created);self.assertIn('.verified-',path.name)
            self.assertEqual(old.read_bytes(),b'old data');self.assertEqual(service.existing(),path)

    def test_cancel_and_unknown_db_create_no_file(self,sleep):
        profile,payload,record=fixture()
        with tempfile.TemporaryDirectory() as folder:
            for cancel in (False,True):
                dest=Path(folder)/str(cancel)
                service=Service(dest,database=Database([]),gb_database=GbDatabase([record] if cancel else []))
                bus=Cartridge(payload);service.adapter,service.profile,service.selected_id=bus,profile,profile.id
                def progress(stage,done,total):
                    if cancel and stage=='Read 2/2':raise UserCancelled('cancel')
                with self.assertRaises(UserCancelled if cancel else ProtocolError):service.capture(progress)
                self.assertFalse(dest.exists());self.assertEqual(bus.modes,[1,0])

    def test_configured_system_paths_switch_without_changing_nes(self,sleep):
        profile,payload,record=fixture(cgb=0x80)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'paths.json'
            config.write_text(json.dumps({'version':1,'pc':{'NES':str(root/'nes'),'GB':str(root/'gb'),'GBC':str(root/'gbc')}}))
            service=Service(root/'nes',database=Database([]),environment='pc',paths_config=config,follow_system_paths=True)
            service.adapter=Cartridge(payload)
            for _ in range(3):service.poll()
            self.assertEqual(service.destination,root/'gbc')
            self.assertEqual(default_output_directory('pc','NES',config),root/'nes')

    def test_legacy_path_config_gets_safe_gb_defaults(self,sleep):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'paths.json';text=json.dumps({'version':1,'mister':{'NES':'/media/fat/games/NES'}});config.write_text(text)
            self.assertEqual(default_output_directory('mister','GBC',config),Path('/media/fat/games/GBC'))
            self.assertEqual(default_output_directory('mister','GB',config),Path('/media/fat/games/Gameboy'))
            self.assertEqual(config.read_text(),text)

    def test_embedded_database_includes_owned_titles(self,sleep):
        database=GbDatabase.load(data('gb_profiles.reference.json'))
        values=list(database.index.values())
        self.assertGreater(len(values),3000)
        self.assertTrue(any(x['title']=='Mario no Picross (Japan) (SGB Enhanced).gb' and x['size']==262144 for x in values))
        self.assertTrue(any(x['title'].startswith('Medarot 2 - Kabuto Version (Japan)') and x['size']==2097152 for x in values))

    def test_duplicate_conflicting_db_identity_is_rejected(self,sleep):
        _,_,record=fixture()
        with self.assertRaises(ProtocolError):GbDatabase([record,dict(record,title='other')])
        for key,value in (('sha1','bad'),('system','NES'),('size',True),('sha256','bad')):
            with self.assertRaises(ProtocolError):GbDatabase([dict(record,**{key:value})])

    def test_tv_presentation_uses_controller_and_rom_instead_of_nes_mapper(self,sleep):
        profile,_,_=fixture(cgb=0x80)
        screen=Screen(profile=profile,candidates=(profile,),supported=True)
        text=render(screen,Path('/media/fat/games/Gameboy'))
        self.assertIn('MBC1',text);self.assertIn('GBC',text)
        self.assertNotIn('Mapper ',profile_details(profile))
        self.assertEqual(profile_columns(profile),('MBC1','256 KiB','GBC'))


class GbWireTests(unittest.TestCase):
    def test_read_uses_1b_and_appended_status5(self):
        wire=Wire(packet(0,80)+b'P'*80+status(5),fragment=3)
        self.assertEqual(Adapter(wire).gb_read(0x100,80),b'P'*80)
        self.assertEqual(wire.writes,[packet(0x1b,8,80,0x100,80),packet(6,0,4)])
        self.assertFalse(wire.replies)

    def test_read_rejects_wrong_status(self):
        wire=Wire(packet(0,2)+b'PP'+status(3))
        with self.assertRaisesRegex(ProtocolError,'GB cartridge disappeared'):
            Adapter(wire).gb_read(0,2)

    def test_write_uses_1c_and_two_acks_without_ram_enable(self):
        wire=Wire(bytes(16),fragment=1)
        Adapter(wire).gb_register(0x2100,31)
        self.assertEqual(wire.writes,[packet(0x1c,8,1,0x2100,1),b'\x1f'])
        self.assertFalse(wire.replies)

    def test_invalid_reads_writes_never_send(self):
        wire=Wire(b'');adapter=Adapter(wire)
        for address,size in ((0xa000,80),(0x7fff,2),(0,0),(True,80),(0,16385)):
            with self.assertRaises(ValueError):adapter.gb_read(address,size)
        for address,value in ((0,10),(0xa000,1),(0x2100,256),(0x3000,2),(0x4000,8),(0x6000,2),(0x2100,True)):
            with self.assertRaises(ValueError):adapter.gb_register(address,value)
        self.assertEqual(wire.writes,[])


if __name__=='__main__':unittest.main()
