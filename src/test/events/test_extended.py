import unittest
import json
import tempfile
import threading
import urllib.request
import urllib.error
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.main.core import dt, iso, now
from src.main.events.adapters import donki, kp, goes, noaa_date, alerts, parse, socrates, jpl
from src.main.events.service import config_load, collect, run, DEFAULT_CONFIG
from src.main.events.storage import Store
from src.main.app import serve

class ExtendedTests(unittest.TestCase):
    def test_noaa_date_parsing(self):
        msg = "Threshold Reached: 2024 May 10 1200 UTC\nValid Until: 2024 May 10 1800 UTC"
        self.assertEqual(iso(noaa_date(msg, ['Threshold Reached'])), '2024-05-10T12:00:00Z')
        self.assertEqual(iso(noaa_date(msg, ['Valid Until'])), '2024-05-10T18:00:00Z')
        self.assertIsNone(noaa_date(msg, ['Not Found']))

    def test_alerts_parsing(self):
        msg = "Threshold Reached: 2024 May 10 1200 UTC\nCANCELLED"
        data = [{'product_id':'test', 'issue_datetime':'2024-05-10T12:05Z', 'message':msg}]
        src = {'url':'x', 'snapshot_id':'y', 'fetched_at':'2024-05-10T12:00:00Z', 'name': 'noaa_alerts'}
        _, _, context = alerts(data, src, DEFAULT_CONFIG)
        self.assertEqual(context[0]['lifecycle'], 'cancellation')
        self.assertEqual(context[0]['declared_start'], '2024-05-10T12:00:00Z')

    def test_goes_xrays(self):
        src = {'name':'noaa_xrays', 'url':'x', 'snapshot_id':'y', 'fetched_at':'2024-05-10T12:00:00Z'}
        data = [{'time_tag':'2024-05-10T12:00Z', 'energy':'0.1-0.8nm', 'flux':1e-4},
                {'time_tag':'2024-05-10T12:01Z', 'energy':'0.1-0.8nm', 'flux':1e-6}]
        es, cs, _ = goes(data, src, DEFAULT_CONFIG)
        self.assertEqual(len(es), 1)
        self.assertEqual(es[0]['mechanism'], 'communications')
        self.assertEqual(es[0]['kind'], 'xray_threshold_episode')

    def test_donki_flr(self):
        data = [{'flrID':'1', 'beginTime':'2024-05-10T12:00Z', 'endTime':'2024-05-10T13:00Z', 'classType':'X1.0'}]
        src = {'name':'donki', 'url':'x', 'snapshot_id':'y', 'fetched_at':'2024-05-10T12:00Z'}
        es, _, _ = donki(data, src, 'FLR', DEFAULT_CONFIG)
        self.assertEqual(es[0]['kind'], 'solar_flare')
        self.assertEqual(es[0]['values']['class'], 'X1.0')

    def test_donki_gst(self):
        data = [{'gstID':'1', 'startTime':'2024-05-10T12:00Z', 'allKpIndex':[{'kpIndex':6}]}]
        src = {'name':'donki', 'url':'x', 'snapshot_id':'y', 'fetched_at':'2024-05-10T12:00Z'}
        es, _, _ = donki(data, src, 'GST', DEFAULT_CONFIG)
        self.assertEqual(es[0]['values']['kp_observations'][0]['kpIndex'], 6)

    def test_config_load_invalid(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write(json.dumps({'kp_event_threshold': 15}))
            f.close()
            with self.assertRaises(ValueError):
                config_load(f.name)
            Path(f.name).unlink()
        
    def test_service_collect(self):
        store = MagicMock()
        def fake_fetch(spec, *args, **kwargs):
            if spec['name'] == 'donki_FLR':
                raise ValueError("Network error")
            return {'body':json.dumps([]), 'transport_status':'fresh', 'snapshot_id':'1', 'fetched_at':'2024-05-10T12:00:00Z', 'url':'x'}
        
        store.fetch.side_effect = fake_fetch
        q = {'start':iso(now()), 'duration_hours':6, 'search_hours':0, 'step_minutes':60, 'mode':'live'}
        
        cfg = {**DEFAULT_CONFIG, 'disabled_sources': ['socrates']}
        bundle = collect(q, cfg, store)
        
        flr = next(s for s in bundle['sources'] if s['name'] == 'donki_FLR')
        self.assertEqual(flr['status'], 'unavailable')
        self.assertIn('Network error', flr['error'])
        
        soc = next(s for s in bundle['sources'] if s['name'] == 'socrates')
        self.assertEqual(soc['status'], 'disabled')

    def test_app_http_server(self):
        config = {**DEFAULT_CONFIG}
        host, port = '127.0.0.1', 8182
        t = threading.Thread(target=serve, args=(config, host, port), daemon=True)
        t.start()
        time.sleep(0.5) 
        
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        urllib.request.install_opener(opener)
        
        req = urllib.request.Request(f'http://{host}:{port}/health', headers={'Connection': 'close'})
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode())
            self.assertEqual(data['status'], 'ok')
            
        req = urllib.request.Request(f'http://{host}:{port}/assess', data=b'not json', method='POST', headers={'Connection': 'close'})
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
        except ConnectionResetError:
            pass # Windows threading server quirk

        q = json.dumps({'start':'2024-05-10T12:00:00Z', 'duration_hours':6, 'mode':'reconstruction'}).encode()
        req = urllib.request.Request(f'http://{host}:{port}/assess', data=q, method='POST', headers={'Content-Length': str(len(q)), 'Connection': 'close'})
        with patch('src.main.app.run', return_value={'test':'ok'}):
            try:
                with urllib.request.urlopen(req) as response:
                    self.assertEqual(response.status, 200)
                    data = json.loads(response.read().decode())
                    if 'test' not in data:
                        print("Returned data:", data)
                    self.assertEqual(data.get('test'), 'ok')
            except urllib.error.HTTPError as e:
                print(f"HTTPError: {e.code} - {e.read().decode()}")
                raise

if __name__=='__main__':
    unittest.main()
