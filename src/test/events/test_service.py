import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import json
from contextlib import closing
from datetime import timedelta
from src.main.utils import dt, iso, now
from src.main.events.core import event, assess, union_minutes, validate_query, group_count
from src.main.events.adapters import socrates, donki, kp, goes, jpl
from src.main.events.service import DEFAULT_CONFIG, collect
from src.main.events.storage import Store
from src.main.demo import make_demo

SRC = dict(name='test',url='https://example.invalid',snapshot_id='test',fetched_at='2024-05-10T09:00:00Z')
Q = dict(start='2024-05-10T12:00:00Z',duration_hours=6,search_hours=0,mode='reconstruction')


class EventTests(unittest.TestCase):
    def test_no_double_count_overlap(self):
        r = make_demo(DEFAULT_CONFIG)
        self.assertEqual(r['windows'][0]['factors']['geomagnetic']['known_interval_overlap_minutes'],180)
        self.assertEqual(r['windows'][0]['factors']['tracked_debris']['known_interval_overlap_minutes'],60)
        self.assertEqual(r['recommendation']['status'],'insufficient_evidence')

    def test_unknown_end_not_invented(self):
        e=event('s','sep','radiation','2024-05-10T10:00Z',None,SRC,{},temporal='open')
        r=assess(dict(events=[e],sources=[]),Q)['windows'][0]['factors']['radiation']
        self.assertEqual(r['possible_ongoing_event_ids'],['s'])
        self.assertEqual(r['known_interval_overlap_minutes'],0)

    def test_half_open_boundaries(self):
        es=[event('left','x','radiation','2024-05-10T11:00Z','2024-05-10T12:00Z',SRC,{}),
            event('right','x','radiation','2024-05-10T18:00Z',None,SRC,{},temporal='instant')]
        r=assess(dict(events=es,sources=[]),Q)
        self.assertEqual(r['windows'][0]['factors']['radiation']['event_count'],0)

    def test_asof_excludes_newly_downloaded_old_catalog(self):
        e=event('s','sep','radiation','2024-05-10T12:00Z',None,
                {**SRC,'fetched_at':'2026-09-18T10:00:00Z'}, {},published='2024-05-09T10:00Z')
        r=assess(dict(events=[e],sources=[]),{**Q,'mode':'as_of','as_of':'2024-05-10T11:00Z'})
        self.assertEqual(r['events'],[])
        self.assertEqual(r['exclusions_after_cutoff'],1)

    def test_asof_accepts_captured_forecast(self):
        e=event('s','cme','geomagnetic','2024-05-10T12:00Z','2024-05-10T15:00Z',SRC,{},basis='external_forecast')
        r=assess(dict(events=[e],sources=[]),{**Q,'mode':'as_of','as_of':'2024-05-10T11:00Z'})
        self.assertEqual(len(r['events']),1)

    def test_offsets_and_invalid_requests(self):
        self.assertEqual(iso(dt('2024-05-10T15:00:00+03:00')),Q['start'])
        for patchq in ({'duration_hours':9},{'duration_hours':float('nan')},{'search_hours':25},{'start':'2024-05-10T12:00'},{'step_minutes':0}):
            with self.assertRaises(ValueError): validate_query({**Q,**patchq})

    def test_socrates_filters_both_columns(self):
        body='NORAD_CAT_ID_1,NORAD_CAT_ID_2,TCA,TCA_RANGE,TCA_RELATIVE_SPEED,MAX_PROB\n1,2,2024-05-10 13:00:00,1,2,0.01\n1,25544,2024-05-10 13:00:00,1,2,0.01\n'
        events,_,_=socrates(body,SRC,DEFAULT_CONFIG)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['values']['maximum_probability_bound'],.01)
        self.assertNotIn('collision_probability',events[0]['values'])

    def test_enlil_does_not_leak_observed_ips(self):
        raw=[{'simulationID':'s','estimatedShockArrivalTime':'2024-05-10T12:00Z',
              'estimatedDuration':6,'modelCompletionTime':'2024-05-09T11:00Z',
              'ipsList':[{'eventTime':'2024-05-11T00:00Z'}]}]
        es,_,_=donki(raw,SRC,'WSAEnlilSimulations',DEFAULT_CONFIG)
        self.assertIsNone(es[0]['published_at'])
        self.assertNotIn('2024-05-11',json.dumps(es))

    def test_jpl_time_not_mislabeled_utc(self):
        body={'signature':{'version':'1.5'},'count':1,'fields':['des','orbit_id','jd','cd','dist','dist_min','dist_max','v_rel','t_sigma_f'],
              'data':[['a','1','2460441','2024-May-10 12:00','.01','.009','.011','12','00:02']]}
        es,_,cs=jpl(body,SRC,DEFAULT_CONFIG)
        self.assertEqual(es,[])
        self.assertEqual(cs[0]['time_scale'],'TDB')

    def test_goes_gaps_and_energy_filter(self):
        src={**SRC,'name':'noaa_protons'}
        raw=[{'time_tag':f'2024-05-10T{t}Z','satellite':18,'energy':'>=10 MeV','flux':f}
             for t,f in [('12:00',15),('12:05',20),('12:30',30),('12:35',2)]]
        raw.append({'time_tag':'2024-05-10T12:10Z','satellite':18,'energy':'>=1 MeV','flux':500})
        es,cs,_=goes(raw,src,DEFAULT_CONFIG)
        self.assertEqual(union_minutes([(dt(c['start']),dt(c['end'])) for c in cs]),10)
        self.assertEqual(len(es),2)
        self.assertEqual(es[0]['end'],'2024-05-10T12:05:00Z')

    def test_transitive_event_groups(self):
        es=[{'id':'a','related_ids':['x']},{'id':'b','related_ids':['x','y']},{'id':'c','related_ids':['y']}]
        self.assertEqual(group_count(es),1)

    def test_empty_does_not_mean_safe(self):
        r=assess(dict(events=[],sources=[]),Q)
        self.assertIsNone(r['recommendation']['preferred_window'])
        self.assertEqual(r['windows'][0]['factors']['radiation']['status'],'insufficient_data')

    def test_offline_failure_and_historical_isolation(self):
        with tempfile.TemporaryDirectory() as path:
            c={**DEFAULT_CONFIG,'database':str(Path(path)/'a.db')}
            b=collect(Q,c,Store(c['database']),offline=True)
            self.assertFalse(b['events'])
            self.assertTrue(all(s['status'] in ('unavailable','archive_unavailable','model_required') for s in b['sources']))
            self.assertFalse(any(s['name']=='noaa_kp' for s in b['sources']))

    def test_cache_cutoff_and_failed_network(self):
        with tempfile.TemporaryDirectory() as path:
            store=Store(Path(path)/'a.db')
            spec={'url':'https://example.invalid','ttl_seconds':1}
            with closing(store.connect()) as db, db:
                db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?)',('old',spec['url'],'2024-05-10T00:00:00Z','[]','{}'))
            self.assertIsNone(store.last(spec['url'],dt('2024-05-09T00:00Z')))
            from src.main.apis.http_client import HttpClientError
            with patch('src.main.events.storage.HttpClient.get_text', side_effect=HttpClientError('network unavailable')):
                r=store.fetch(spec,{**DEFAULT_CONFIG,'retry_count':0})
            self.assertEqual(r['transport_status'],'stale')
            self.assertIsNotNone(r['error'])

if __name__=='__main__': unittest.main()
