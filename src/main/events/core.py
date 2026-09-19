"""Pure event/window calculations. Python 3.10+, no third-party dependencies."""
from datetime import datetime, timedelta, timezone
from collections import Counter
import math

from src.main.utils import UTC, dt, iso, now, number
VERSION = '1.0.0'
MECHANISMS = ('radiation', 'geomagnetic', 'communications', 'tracked_debris', 'meteoroids')


def event(eid, kind, mechanism, start, end, source, record, *, basis='observation',
          relevance='earth_environment_proxy', temporal='interval', published=None,
          values=None, related=None, rule='', limitations=None):
    start = dt(start, provider=True)
    end = dt(end, provider=True) if end else None
    if end and end < start:
        raise ValueError('Event end precedes start')
    return dict(id=str(eid), kind=kind, mechanism=mechanism, start=iso(start),
                end=iso(end) if end else None, temporal=temporal, basis=basis,
                relevance=relevance, published_at=iso(dt(published, provider=True)) if published else None,
                source=source['name'], source_url=record.get('link') or source['url'],
                snapshot_id=source['snapshot_id'], fetched_at=source['fetched_at'],
                record_version=record.get('versionId'), related_ids=related or [],
                values=values or {}, rule=rule, limitations=limitations or [],
                confidence='proxy_only' if relevance != 'iss_conjunction' else 'screening_only')


def union_minutes(intervals):
    merged = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return round(sum((b-a).total_seconds()/60 for a, b in merged), 6)


def latest_events(events):
    """One version per provider/event ID; prefer latest declared publication."""
    latest = {}
    for e in events:
        key = (e['source'], e['id'])
        rank = (e.get('published_at') or '', e.get('record_version') or 0, e['fetched_at'])
        old = latest.get(key)
        if old is None or rank > old[0]:
            latest[key] = (rank, e)
    return [x[1] for x in latest.values()]


def group_count(events):
    # Transitive source-declared links; this is NOT a count of independent hazards.
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]
    for e in events:
        root = find(e['id'])
        for other in e['related_ids']:
            parent[find(other)] = root
    return len({find(e['id']) for e in events})


def validate_query(q):
    start = dt(q['start'])
    duration = number(q.get('duration_hours', 6))
    search = number(q.get('search_hours', 0))
    step = number(q.get('step_minutes', 60))
    if not 1 <= duration <= 8 or not 0 <= search <= 24 or not 15 <= step <= 1440:
        raise ValueError('duration_hours: 1..8; search_hours: 0..24; step_minutes: 15..1440')
    mode = q.get('mode', 'live')
    if mode not in ('live', 'reconstruction', 'as_of'):
        raise ValueError('mode: live, reconstruction, as_of')
    cutoff = dt(q['as_of']) if mode == 'as_of' else None
    if cutoff and cutoff > start:
        raise ValueError('as_of must not be later than window start')
    if mode == 'live' and start < now()-timedelta(hours=1):
        raise ValueError('Use reconstruction for historical windows')
    return start, duration, search, step, mode, cutoff


def assess(bundle, q):
    start, duration, search, step, mode, cutoff = validate_query(q)
    events = latest_events(bundle['events'])
    excluded = 0
    if cutoff:
        kept = []
        for e in events:
            # Fail closed: a date printed in a mutable catalog is not an archived version.
            if dt(e['fetched_at']) <= cutoff and (not e['published_at'] or dt(e['published_at']) <= cutoff):
                kept.append(e)
            else:
                excluded += 1
        events = kept
    windows = []
    for i in range(int(search*60//step)+1):
        a = start+timedelta(minutes=i*step)
        b = a+timedelta(hours=duration)
        factors = {}
        for mechanism in MECHANISMS:
            chosen, intervals, unknown = [], [], []
            for e in events:
                if e['mechanism'] != mechanism or e['relevance'] == 'context_only':
                    continue
                s = dt(e['start'])
                t = dt(e['end']) if e['end'] else None
                if e['temporal'] == 'instant':
                    hit = a <= s < b
                elif t:
                    hit = s < b and t > a
                else:
                    hit = s < b
                if not hit:
                    continue
                chosen.append(e)
                if t:
                    intervals.append((max(a, s), min(b, t)))
                elif e['temporal'] != 'instant':
                    unknown.append(e['id'])
            coverage_intervals = []
            for c in bundle.get('coverage', []):
                if c['mechanism'] != mechanism:
                    continue
                if cutoff and dt(c['fetched_at']) > cutoff:
                    continue
                coverage_intervals.append((max(a, dt(c['start'])), min(b, dt(c['end']))))
            coverage = union_minutes(coverage_intervals)/(duration*60)
            known = union_minutes(intervals)
            factors[mechanism] = dict(
                event_count=len(chosen), linked_group_count=group_count(chosen),
                event_types=dict(Counter(e['kind'] for e in chosen)),
                known_interval_overlap_minutes=known,
                possible_ongoing_event_ids=unknown,
                observed_event_count=sum(e['basis']=='observation' for e in chosen),
                forecast_event_count=sum(e['basis']=='external_forecast' for e in chosen),
                coverage_fraction=round(min(coverage, 1), 4),
                coverage_scope='selected_products_only_not_total_hazard_coverage',
                team_calculated_event_count=sum(e['basis']=='team_calculation' for e in chosen),
                source_max_age_minutes=max((max(0, (now()-dt(e['fetched_at'])).total_seconds()/60) for e in chosen), default=None),
                next_event_in_minutes=min(((dt(e['start'])-a).total_seconds()/60 for e in events
                    if e['mechanism']==mechanism and e['relevance']!='context_only' and dt(e['start'])>=a), default=None),
                status=('events_require_review' if chosen else 'no_detected_events') if coverage >= .9999 else
                       ('events_and_missing_data' if chosen else 'insufficient_data'),
                event_ids=[e['id'] for e in chosen])
        for mechanism in MECHANISMS:
            f = factors[mechanism]
            for basis in ('observation', 'external_forecast', 'team_calculation'):
                f[basis+'_overlap_minutes'] = union_minutes([
                    (max(a, dt(e['start'])), min(b, dt(e['end']))) for e in events
                    if e['id'] in f['event_ids'] and e['basis']==basis and e['end']])
        debris = [e for e in events if e['id'] in factors['tracked_debris']['event_ids']]
        factors['tracked_debris']['minimum_miss_distance_km'] = min(
            (e['values']['miss_distance_km'] for e in debris), default=None)
        factors['tracked_debris']['maximum_relative_speed_km_s'] = max(
            (e['values']['relative_speed_km_s'] for e in debris), default=None)
        windows.append(dict(start=iso(a), end=iso(b), duration_hours=duration, factors=factors))
    # Compare event burden only; missing critical mechanisms prohibit recommendation.
    vectors = [tuple(w['factors'][m]['known_interval_overlap_minutes'] for m in MECHANISMS) for w in windows]
    frontier = [i for i, v in enumerate(vectors) if not any(
        all(x <= y for x, y in zip(other, v)) and any(x < y for x, y in zip(other, v))
        for other in vectors)]
    sufficient = all(f['coverage_fraction'] == 1 and not f['possible_ongoing_event_ids']
                     for w in windows for f in w['factors'].values())
    return dict(algorithm_version=VERSION, query=q, generated_at=iso(now()),
                windows=windows, events=events, context=bundle.get('context', []),
                sources=bundle['sources'], exclusions_after_cutoff=excluded,
                event_burden_frontier_indices=frontier,
                recommendation=dict(status='insufficient_evidence' if not sufficient else 'review_candidates',
                    preferred_window=None,
                    reason='No operational risk model; coverage is product-specific. Unknown exposure and alternatives are not zero risk.'),
                limitations=bundle.get('limitations', []))
