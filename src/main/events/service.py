import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from src.main.utils import now, dt, iso
from src.main.events.core import validate_query, assess
from src.main.events.storage import Store
from src.main.events.adapters import parse, DONKI_KINDS

# ---------------------------------------------------------------------------
# Клиенты API — URL-ы теперь живут в apis/, не здесь
# ---------------------------------------------------------------------------
from src.main.apis.noaa_client import NoaaClient
from src.main.apis.donki_client import DonkiClient
from src.main.apis.celestrak_client import CelesTrakClient
from src.main.apis.jpl_client import JplClient
from src.main.apis.http_client import HttpClient

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    'database': 'data/events/eva.sqlite3', 'timeout_seconds': 20, 'retry_count': 1,
    'max_response_bytes': 25000000, 'workers': 4,
    'proton_threshold_pfu': 10, 'xray_threshold_w_m2': 1e-5,
    'kp_event_threshold': 5, 'conjunction_buffer_minutes': 30,
    'norad_id': 25544, 'lookback_days': 7, 'disabled_sources': [],
    'ttl_noaa_seconds': 300, 'ttl_donki_seconds': 1800,
    'ttl_socrates_seconds': 28800, 'ttl_jpl_seconds': 86400,
}


def config_load(path=None):
    config = {**DEFAULT_CONFIG}
    if path:
        config.update(json.loads(Path(path).read_text()))
    if not 0 <= config['kp_event_threshold'] <= 9 or not 1 <= config['conjunction_buffer_minutes'] <= 120:
        raise ValueError('Invalid event thresholds')
    if not 1 <= config['lookback_days'] <= 30 or not 1 <= config['workers'] <= 8:
        raise ValueError('lookback_days: 1..30; workers: 1..8')
    return config


def specs(q, config):
    """
    Формирует список спецификаций источников данных.

    URL-ы берутся из клиентов apis/ через build_url() / статические константы.
    """
    start, duration, search, _, mode, cutoff = validate_query(q)
    end = start + timedelta(hours=duration + search)
    anchor = cutoff or (now() if mode == 'live' else end)
    first = (min(start, anchor) - timedelta(days=config['lookback_days'])).date().isoformat()
    last = anchor.date().isoformat()

    donki_client = DonkiClient()
    noaa_client = NoaaClient()
    celestrak_client = CelesTrakClient(iss_norad=config['norad_id'])
    jpl_client = JplClient()

    result = []

    def add(name, url, ttl, parser_format='json'):
        result.append(dict(name=name, url=url, ttl_seconds=ttl, format=parser_format))

    # DONKI — URL-ы строятся через DonkiClient.get_url()
    for kind in [*DONKI_KINDS, 'WSAEnlilSimulations']:
        extra = {'location': 'Earth'} if kind == 'IPS' else None
        url = donki_client.get_url(kind, first, last, extra)
        add('donki_' + kind, url, config['ttl_donki_seconds'])

    if mode != 'reconstruction':
        # NOAA — URL-ы из констант модуля noaa_client
        from src.main.apis.noaa_client import URL_KP, URL_PROTONS, URL_XRAYS, URL_ALERTS
        add('noaa_kp',      URL_KP,      config['ttl_noaa_seconds'])
        add('noaa_protons', URL_PROTONS, config['ttl_noaa_seconds'])
        add('noaa_xrays',   URL_XRAYS,   config['ttl_noaa_seconds'])
        add('noaa_alerts',  URL_ALERTS,  config['ttl_noaa_seconds'])

        # SOCRATES — URL из CelesTrakClient
        from src.main.apis.celestrak_client import URL_SOCRATES
        add('socrates', URL_SOCRATES, config['ttl_socrates_seconds'], 'csv')

    # JPL — URL строится через JplClient.build_url()
    if mode != 'as_of':
        jpl_url = jpl_client.build_url(
            date_min=(start - timedelta(days=1)).date().isoformat(),
            date_max=(end + timedelta(days=1)).date().isoformat(),
            body='Earth',
            dist_max='0.05',
        )
        add('jpl_cad', jpl_url, config['ttl_jpl_seconds'])

    return result


def collect(q, config, store, offline=False):
    _, _, _, _, mode, cutoff = validate_query(q)

    # Один HttpClient на весь прогон (шарим retry/timeout настройки)
    http_client = HttpClient(
        timeout=config.get('timeout_seconds', 20),
        retries=config.get('retry_count', 1),
        max_bytes=config.get('max_response_bytes', 25_000_000),
    )

    def one(spec):
        src = {**spec, 'status': 'unavailable', 'error': None}
        if spec['name'] in config['disabled_sources']:
            return {**src, 'status': 'disabled'}, [], [], []
        try:
            raw = store.fetch(
                spec, config,
                force=bool(q.get('force_refresh', False)),
                offline=offline,
                cutoff=cutoff,
                http_client=http_client,
            )
            src.update({k: v for k, v in raw.items() if k not in ('body', 'headers')})
            data = json.loads(raw['body']) if spec['format'] == 'json' else raw['body']
            events, coverage, context = parse(spec['name'], data, src, config)
            src['status'] = raw['transport_status']
            src['event_count'] = len(events)
            src['context_count'] = len(context)
            src['coverage_semantics'] = 'event_catalog_not_complete_exposure_coverage'
            if spec['name'] in ('noaa_kp', 'noaa_protons', 'noaa_xrays'):
                src['coverage_semantics'] = 'selected_channel_only_not_total_mechanism'
                src['data_valid_until'] = max((c['end'] for c in coverage), default=None)
                if not coverage or max(dt(c['end']) for c in coverage) < (cutoff or now()) - timedelta(minutes=20):
                    src['status'] = 'out_of_date'
            if src['status'] in ('stale', 'out_of_date') and mode != 'as_of':
                coverage = []
            return src, events, coverage, context
        except Exception as exc:
            # Сломанный провайдер не ломает остальные механизмы
            src['status'] = 'unavailable'
            src['error'] = f'{type(exc).__name__}: {exc}'
            return src, [], [], []

    bundle = dict(events=[], coverage=[], context=[], sources=[], limitations=[
        'Research event-feature service, not authorization for real EVA.',
        'No station orbit, shielding, dosimetry, or local plasma/link model in this component.',
        'Meteoroids and untracked fragments: no individual-object feed; coverage remains unknown.',
        'An event catalog with zero records does not prove absence of hazards.',
        'As-of uses only previously captured snapshots; a newly downloaded historical catalog is reconstruction.',
        'Unknown event ends are possible ongoing signals, not confirmed exposure duration.',
        'Lookback bounds available history; a long-running event can start before it.',
    ])
    with ThreadPoolExecutor(max_workers=config['workers']) as pool:
        for source, events, coverage, context in pool.map(one, specs(q, config)):
            bundle['sources'].append(source)
            bundle['events'].extend(events)
            bundle['coverage'].extend(coverage)
            bundle['context'].extend(context)
    if mode == 'reconstruction':
        bundle['sources'].append(dict(name='socrates', status='archive_unavailable',
                                      error='Current conjunction CSV must not substitute a historical report.'))
    bundle['sources'].append(dict(name='meteoroids', status='model_required',
                                  error='Requires meteoroid environment model and geometry, not asteroid CAD counts.'))
    return bundle


def run(q, config, offline=False):
    store = Store(config['database'])
    bundle = collect(q, config, store, offline)
    result = assess(bundle, q)
    result['configuration'] = config
    result['coverage_intervals'] = bundle['coverage']
    result['run_id'] = store.save_run(result)
    return result
