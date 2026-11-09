"""Raw-header diagnostics never select banks or save a ROM."""
from _paths import ROOT  # noqa: F401
import json
import io
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from cart_station_gb import diagnose_header
from cart_station_protocol import ProtocolError, packet
from cart_station_transport import Adapter, TraceTransport
from test_cart_station import Wire
from test_cart_station_gb import fixture, Cartridge


@patch('cart_station_gb.time.sleep')
class GbDiagnosticTests(unittest.TestCase):
    def test_valid_header_only_no_mbc_probe_or_bank_writes(self,sleep):
        profile,payload,_=fixture(1,5)
        bus=Cartridge(payload);result=diagnose_header(bus)
        self.assertTrue(result['all_headers_valid']);self.assertTrue(result['all_reads_equal'])
        self.assertEqual(bus.events,[('R',0x100,80)]*3)
        self.assertEqual(bus.modes,[1,0])
        self.assertTrue(all(x['logo_matching_bytes']==48 for x in result['reads']))
        self.assertEqual(result['reads'][0]['header_hex'],profile.header.hex())

    def test_invalid_and_unstable_headers_are_preserved_as_evidence(self,sleep):
        profile,payload,_=fixture()
        bus=Cartridge(payload);bus.gb_read=Mock(side_effect=[bytes(80),b'\xff'*80,profile.header])
        result=diagnose_header(bus)
        self.assertFalse(result['all_reads_equal']);self.assertFalse(result['all_headers_valid'])
        self.assertTrue(result['reads'][0]['all_zero']);self.assertTrue(result['reads'][1]['all_ff'])
        self.assertTrue(result['reads'][2]['valid']);json.dumps(result)
        self.assertEqual(bus.events,[]);self.assertEqual(bus.modes,[1,0])

    def test_transport_error_stops_further_reads_and_releases_mode(self,sleep):
        _,payload,_=fixture();bus=Cartridge(payload)
        bus.gb_read=Mock(side_effect=ProtocolError('Truncated response: 8/80 bytes'))
        result=diagnose_header(bus)
        bus.gb_read.assert_called_once_with(0x100,80)
        self.assertIn('Truncated',result['reads'][0]['error'])
        self.assertFalse(result['all_headers_valid']);self.assertEqual(bus.modes,[1,0])

    def test_release_error_does_not_discard_received_headers(self,sleep):
        profile,payload,_=fixture();bus=Cartridge(payload)
        bus.mode=Mock(side_effect=[None,ProtocolError('release failed')])
        result=diagnose_header(bus)
        self.assertEqual(len(result['reads']),3);self.assertEqual(result['release_error'],'release failed')

    def test_wrong_cartridge_and_bad_sample_count_do_not_start_mode(self,sleep):
        bus=Mock();bus.status.return_value=3
        self.assertIn('expected type 5',diagnose_header(bus)['error']);bus.mode.assert_not_called()
        for value in (0,6,True):
            bus.reset_mock()
            with self.assertRaises(ValueError):diagnose_header(bus,value)
            self.assertEqual(bus.mock_calls,[])


class TraceTransportTests(unittest.TestCase):
    def test_fragmented_reads_record_actual_bytes_and_order(self):
        stream=io.StringIO();wire=Wire(b'abcdefgh',fragment=2)
        traced=TraceTransport(wire,stream)
        adapter=Adapter(traced)
        traced.write(b'\x04\x00');self.assertEqual(adapter.exact(8),b'abcdefgh')
        events=[json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([x['operation'] for x in events],['open','write']+['read']*4)
        self.assertEqual(events[1]['hex'],'0400')
        self.assertEqual([x['requested_bytes'] for x in events[2:]],[8,6,4,2])
        self.assertEqual(''.join(x['hex'] for x in events[2:]),b'abcdefgh'.hex())
        self.assertTrue(all(x['duration_ms']>=0 for x in events[1:]))

    def test_errors_and_close_remain_visible(self):
        stream=io.StringIO();wire=Mock();wire.read.side_effect=ProtocolError('timeout')
        traced=TraceTransport(wire,stream)
        with self.assertRaises(ProtocolError):traced.read(80)
        traced.close()
        events=[json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(events[1]['error'],'ProtocolError: timeout')
        self.assertEqual(events[-1]['operation'],'close');wire.close.assert_called_once()

    def test_existing_trace_is_preserved_without_opening_reader(self):
        import cart_station_cli
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'trace.jsonl';target.write_text('old')
            with patch.object(sys,'argv',['cart_station.py','diagnose-gb','--trace',str(target)]), \
                 patch('cart_station_cli.open_transport') as opening:
                with self.assertRaises(FileExistsError):cart_station_cli.main()
            opening.assert_not_called();self.assertEqual(target.read_text(),'old')


class GbConnectionInitTests(unittest.TestCase):
    def test_original_initialization_is_04_zero_without_05_and_ack_preserved(self):
        wire=Wire(bytes.fromhex('0102030405060708'),fragment=2)
        adapter=Adapter(wire);adapter.initialize_connection()
        self.assertEqual(wire.writes,[packet(4,4,0,0)])
        self.assertEqual(list(adapter.control_replies),['0102030405060708'])
        self.assertFalse(wire.replies)

    def test_cli_reset_is_explicit_and_precedes_product_query(self):
        import cart_station_cli
        for reset in (False,True):
            transport=Mock();adapter=Mock();adapter.control_replies=[]
            adapter.product.return_value=dict(build=10,product=4919)
            with patch('cart_station_cli.open_transport',return_value=transport), \
                 patch('cart_station_cli.Adapter',return_value=adapter), \
                 patch('cart_station_gb.diagnose_header',return_value={'reads':[]}), \
                 patch.object(sys,'argv',['cart_station.py','--port','COM5','diagnose-gb']+(['--reset'] if reset else [])), \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                cart_station_cli.main()
            result=json.loads(output.getvalue())
            self.assertEqual(result['connection_initialization'],'04(0)' if reset else 'none')
            calls=[call[0] for call in adapter.mock_calls]
            self.assertEqual(calls,['initialize_connection','product'] if reset else ['product'])
            transport.close.assert_called_once()


if __name__=='__main__':unittest.main()
