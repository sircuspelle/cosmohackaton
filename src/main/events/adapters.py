"""Provider-specific parsers. Unknown schema => explicit source error, never empty success."""
import csv
import io
import re
from datetime import timedelta, datetime
from src.main.utils import dt, iso, number
from src.main.events.core import event

DONKI_KINDS = {'FLR': ('solar_flare', 'communications', 'flrID', 'beginTime'),
 'SEP': ('solar_particle_event', 'radiation', 'sepID', 'eventTime'),
 'CME': ('cme_observed_at_sun', 'geomagnetic', 'activityID', 'startTime'),
 'GST': ('geomagnetic_storm', 'geomagnetic', 'gstID', 'startTime'),
 'IPS': ('interplanetary_shock', 'geomagnetic', 'activityID', 'eventTime'),
 'HSS': ('high_speed_stream', 'geomagnetic', 'hssID', 'eventTime'),
 'RBE': ('radiation_belt_enhancement', 'radiation', 'rbeID', 'eventTime'),
 'MPC': ('magnetopause_crossing', 'geomagnetic', 'mpcID', 'eventTime')}


def records(data):
    if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
        raise ValueError('Expected JSON array of objects')
    return data


def donki(data, src, kind, config):
    out = []
    for r in records(data):
        if kind == 'WSAEnlilSimulations':
            # Only top-level Earth forecast. ipsList contains later observed outcomes.
            if not r.get('estimatedShockArrivalTime'):
                continue
            start = dt(r['estimatedShockArrivalTime'], provider=True)
            hours = number(r['estimatedDuration']) if r.get('estimatedDuration') is not None else None
            if hours is not None and hours < 0:
                raise ValueError('Negative ENLIL duration')
            out.append(event(r['simulationID'], 'cme_arrival_earth', 'geomagnetic', start,
                start+timedelta(hours=hours) if hours is not None else None, src, r,
                basis='external_forecast', temporal='interval' if hours is not None else 'open',
                values={'estimated_duration_hours': hours, 'model_completed_at': r.get('modelCompletionTime'),
                        'arrival_uncertainty_hours': None},
                related=[x['cmeid'] for x in r.get('cmeInputs') or [] if x.get('cmeid')],
                rule='Earth ENLIL arrival plus model duration; no local ISS dose conversion',
                limitations=['Model completion is not publication time.',
                              'Latest mutable catalog; ipsList deliberately excluded.',
                              'Arrival uncertainty not provided by this record.']))
            continue
        name, mechanism, idkey, timekey = DONKI_KINDS[kind]
        if kind == 'IPS' and r.get('location') != 'Earth':
            continue
        related = [x['activityID'] for x in r.get('linkedEvents') or [] if x.get('activityID')]
        instruments = [x.get('displayName', '') for x in r.get('instruments') or []]
        basis = 'external_forecast' if instruments and all(x.startswith('MODEL:') for x in instruments) else 'observation'
        relevance = 'context_only' if kind in ('CME', 'MPC', 'RBE') else 'earth_environment_proxy'
        values = {'instruments': instruments}
        if kind == 'FLR':
            values['class'] = r.get('classType')
        if kind == 'GST':
            # Kp timestamps denote interval observation times; do not invent storm end.
            values['kp_observations'] = r.get('allKpIndex') or []
        temporal = 'instant' if kind in ('CME', 'IPS', 'MPC') else 'interval' if r.get('endTime') else 'open'
        out.append(event(r[idkey], name, mechanism, r[timekey], r.get('endTime'), src, r,
            basis=basis, relevance=relevance, temporal=temporal, published=r.get('submissionTime'),
            values=values, related=related, rule='Provider event catalog; end is unknown unless explicitly given',
            limitations=['Catalog completeness is not continuous sensor coverage.',
                          'Measured/modelled environment is not dose inside a spacesuit.',
                          'Unknown end must not be replaced by arbitrary six-hour duration.']))
    return out, [], []


def kp(data, src, config):
    if not isinstance(data, list):
        raise ValueError('Expected Kp list')
    if data and isinstance(data[0], list):
        data = [dict(zip(data[0], row)) for row in data[1:]]
    out, coverage = [], []
    for r in records(data):
        start = dt(r['time_tag'], provider=True)
        end = start+timedelta(hours=3)
        value = number(r['kp'])
        if not 0 <= value <= 9:
            raise ValueError('Kp outside 0..9')
        basis = {'observed': 'observation', 'estimated': 'external_estimate',
                 'predicted': 'external_forecast', 'forecast': 'external_forecast'}.get(r['observed'])
        if basis is None:
            raise ValueError('Unknown Kp record type')
        coverage.append(dict(mechanism='geomagnetic', start=iso(start), end=iso(end),
                             fetched_at=src['fetched_at'], snapshot_id=src['snapshot_id'],
                             scope='planetary_kp_only', basis=basis))
        if value < config['kp_event_threshold']:
            continue
        out.append(event('kp:'+iso(start), 'geomagnetic_kp_episode', 'geomagnetic', start, end,
                         src, r, basis=basis, values={'kp': value, 'kp_unit': 'dimensionless',
                                                      'noaa_scale': r.get('noaa_scale')},
                         rule=f"Kp >= {config['kp_event_threshold']} over source 3h bin",
                         limitations=['Planetary proxy; not a station-local radiation measurement.',
                                       'Publication timestamp absent; fetched_at retained separately.']))
    return out, coverage, []


def noaa_date(text, labels):
    for label in labels:
        m = re.search(label+r':\s*(\d{4} [A-Za-z]{3} \d{1,2} \d{4}) UTC', text, re.I)
        if m:
            return dt(datetime.strptime(m.group(1), '%Y %b %d %H%M'), provider=True)
    return None


def alerts(data, src, config):
    context = []
    # Alert lifecycle/cancellations are deliberately NOT turned into active intervals.
    # Kp/DONKI supply normalized events; raw bulletins remain explicit review evidence.
    for r in records(data):
        message = r['message'].replace('\\r', '\r').replace('\\n', '\n')
        start = noaa_date(message, ['Valid From', 'Threshold Reached', 'Begin Time'])
        end = noaa_date(message, ['Now Valid Until', 'Valid Until', 'Valid To', 'End Time'])
        context.append(dict(kind='noaa_bulletin', product_id=r['product_id'],
            published_at=iso(dt(r['issue_datetime'], provider=True)),
            declared_start=iso(start) if start else None, declared_end=iso(end) if end else None,
            lifecycle='cancellation' if re.search(r'CANCEL|ENDED', message, re.I) else 'requires_review',
            message=message, source_url=src['url'], snapshot_id=src['snapshot_id'],
            usage='Evidence only; extensions/cancellations not used as independent risk increments.'))
    return [], [], context


def socrates(data, src, config):
    reader = csv.DictReader(io.StringIO(data))
    required = {'NORAD_CAT_ID_1','NORAD_CAT_ID_2','TCA','TCA_RANGE','TCA_RELATIVE_SPEED','MAX_PROB'}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError('Unexpected SOCRATES CSV header')
    out = []
    for r in reader:
        if str(config['norad_id']) not in (r['NORAD_CAT_ID_1'], r['NORAD_CAT_ID_2']):
            continue
        start = dt(r['TCA'], provider=True)
        distance, speed = number(r['TCA_RANGE']), number(r['TCA_RELATIVE_SPEED'])
        maximum_probability = number(r['MAX_PROB'])
        if distance < 0 or speed < 0 or not 0 <= maximum_probability <= 1:
            raise ValueError('Invalid SOCRATES values')
        other = '2' if r['NORAD_CAT_ID_1']==str(config['norad_id']) else '1'
        name = r.get('OBJECT_NAME_'+other, '')
        subtype = 'fragment' if 'DEB' in name else 'rocket_body' if 'R/B' in name else 'payload_or_unknown'
        padding = timedelta(minutes=config['conjunction_buffer_minutes'])
        out.append(event('conjunction:'+':'.join([r['NORAD_CAT_ID_1'],r['NORAD_CAT_ID_2'],iso(start)]),
            'iss_conjunction', 'tracked_debris', start-padding, start+padding, src, r,
            relevance='iss_conjunction', basis='external_forecast',
            values=dict(tca=iso(start), miss_distance_km=distance, relative_speed_km_s=speed,
                maximum_probability_bound=maximum_probability, object_name=name, object_type=subtype,
                other_norad_id=r['NORAD_CAT_ID_'+other],
                buffer_minutes=config['conjunction_buffer_minutes'],
                dse_1_days=number(r['DSE_1']) if r.get('DSE_1') else None,
                dse_2_days=number(r['DSE_2']) if r.get('DSE_2') else None),
            rule='ISS-filtered conjunction; symmetric review buffer around TCA',
            limitations=['Review buffer is a configurable planning rule, not timing covariance.',
                          'MAX_PROB is not the actual collision probability.',
                          'GP screening does not cover untracked debris or meteoroids.',
                          'No report-generation timestamp or completeness proof from CSV alone.']))
    return out, [], []


def jpl(data, src, config):
    if not isinstance(data, dict) or data.get('signature', {}).get('version') != '1.5':
        raise ValueError('Unsupported JPL CAD schema version')
    rows = [dict(zip(data.get('fields', []), row)) for row in data.get('data', [])]
    if len(rows) != int(data['count']):
        raise ValueError('JPL count mismatch')
    context = []
    for r in rows:
        context.append(dict(kind='asteroid_earth_approach', designation=r['des'],
            time_tdb=r['cd'], jd_tdb=number(r['jd']), time_scale='TDB',
            time_uncertainty_3sigma=r['t_sigma_f'], distance_au=number(r['dist']),
            distance_min_3sigma_au=number(r['dist_min']), distance_max_3sigma_au=number(r['dist_max']),
            relative_speed_km_s=number(r['v_rel']), orbit_version=r['orbit_id'],
            source_url=src['url'], snapshot_id=src['snapshot_id'],
            usage='Context only; Earth approach is not ISS conjunction. TDB not relabelled UTC.'))
    return [], [], context


def parse(name, data, src, config):
    if name.startswith('donki_'):
        return donki(data, src, name[6:], config)
    return {'noaa_kp': kp, 'noaa_alerts': alerts, 'noaa_protons': goes, 'noaa_xrays': goes, 'socrates': socrates, 'jpl_cad': jpl}[name](data, src, config)


def goes(data, src, config):
    proton = src['name'] == 'noaa_protons'
    energy = '>=10 MeV' if proton else '0.1-0.8nm'
    threshold = config['proton_threshold_pfu'] if proton else config['xray_threshold_w_m2']
    cadence = 300 if proton else 60
    mechanism = 'radiation' if proton else 'communications'
    kind = 'proton_threshold_episode' if proton else 'xray_threshold_episode'
    samples = {}
    for r in records(data):
        if r['energy'] != energy:
            continue
        # Bad/contaminated samples split intervals; they are not quiet observations.
        t = dt(r['time_tag'], provider=True)
        flux = r.get('flux')
        try:
            flux = number(flux)
        except (TypeError, ValueError):
            flux = None
        if flux is not None and (flux < 0 or r.get('electron_contaminaton') is True or r.get('electron_contamination') is True):
            flux = None
        samples[t] = (flux, r)
    if not samples:
        raise ValueError('Expected GOES energy channel missing')
    out, coverage, active = [], [], None
    ordered = sorted(samples.items())
    for i, (t, (flux, r)) in enumerate(ordered):
        if flux is None:
            active = None
            continue
        # Conservative interpolation only between adjacent valid same-satellite samples.
        nxt = ordered[i+1] if i+1 < len(ordered) else None
        contiguous = nxt and (nxt[0]-t).total_seconds() <= cadence*1.5 and nxt[1][0] is not None and nxt[1][1].get('satellite') == r.get('satellite')
        end = nxt[0] if contiguous else t
        if contiguous:
            coverage.append(dict(mechanism=mechanism, start=iso(t), end=iso(end),
                fetched_at=src['fetched_at'], snapshot_id=src['snapshot_id'],
                scope='goes_selected_channel_interpolation', basis='observation'))
        if flux < threshold:
            active = None
            continue
        if active is not None and active['end'] == iso(t):
            active['end'] = iso(end)
            active['values']['maximum_flux'] = max(active['values']['maximum_flux'], flux)
        else:
            active = event(kind+':'+iso(t), kind, mechanism, t, end, src, r,
                basis='team_calculation', temporal='interval', values={
                    'maximum_flux':flux, 'threshold':threshold,
                    'unit':'protons/(cm^2 s sr)' if proton else 'W/m^2',
                    'energy':energy, 'satellite':r.get('satellite'), 'right_censored':not bool(contiguous)},
                rule='Threshold segments; previous value held only until adjacent valid sample',
                limitations=['Threshold episode is not the official NOAA flare/event lifecycle.',
                             'GOES measurement, not ISS dose or verified radio outage.',
                             'No persistence forecast beyond last measurement; boundaries limited by cadence.'])
            out.append(active)
        active['values']['right_censored'] = not bool(contiguous)
        if not contiguous:
            # Instant sample has no fabricated duration. Previous segments remain bounded.
            if active['start'] == active['end']:
                active['temporal'] = 'instant'
            active = None
    return out, coverage, []
