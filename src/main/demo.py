"""Synthetic fixtures, deliberately labelled; never used on provider failure."""
from src.main.events.adapters import donki, kp, socrates
from src.main.core import assess


def make_demo(config):
    src = dict(name='synthetic_demo', url='https://example.invalid/synthetic',
               snapshot_id='SYNTHETIC-NOT-OBSERVED',
               fetched_at='2024-05-10T09:00:00Z', status='synthetic',
               event_count=0, context_count=0)
    enlil = [{'simulationID':'demo-cme', 'estimatedShockArrivalTime':'2024-05-10T12:00Z',
              'estimatedDuration':3, 'cmeInputs':[{'cmeid':'demo-eruption'}]}]
    e, _, _ = donki(enlil,src,'WSAEnlilSimulations',config)
    ke,kc,_ = kp([{'time_tag':'2024-05-10T12:00Z','kp':7,'observed':'predicted'},
                   {'time_tag':'2024-05-10T15:00Z','kp':3,'observed':'predicted'},
                   {'time_tag':'2024-05-10T18:00Z','kp':2,'observed':'predicted'}],src,config)
    se,_,_ = socrates('NORAD_CAT_ID_1,NORAD_CAT_ID_2,TCA,TCA_RANGE,TCA_RELATIVE_SPEED,MAX_PROB\n25544,99999,2024-05-10 13:30:00,0.8,12.1,0.001\n',src,config)
    src['event_count'] = len(e + ke + se)
    bundle = dict(events=e+ke+se,coverage=kc,context=[],sources=[src],
                  limitations=['SYNTHETIC DEMO: all events and values are invented for software verification.'])
    result = assess(bundle,dict(start='2024-05-10T12:00:00Z',duration_hours=6,
                               search_hours=6,step_minutes=60,mode='reconstruction'))
    result['synthetic'] = True
    result['coverage_intervals'] = kc
    return result
