"""Local replay snapshots. Observation/epoch time is NOT publication time."""
from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ARCHIVE_DIR = Path(__file__).resolve().parents[2] / 'data' / 'conditions'
START = datetime(2024, 5, 1)
END = datetime(2024, 7, 1)
LOG = logging.getLogger(__name__)

# Минимальный встроенный smoke-набор нужен только для запуска тестов из
# неполной рабочей копии. В поставке архивы лежат в data/conditions/.
_FALLBACK_ROWS = {
    'protons': [
        {'time_tag': '2024-06-15T12:00:00Z', 'energy': '>=10 MeV',
         'flux': 4, 'available_at': '2024-06-15T13:00:00Z'},
    ],
    'kp': [
        {'time_tag': '2024-06-15T09:00:00Z', 'end_time': '2024-06-15T12:00:00Z',
         'kp': 6, 'available_at': '2024-06-15T12:15:00Z'},
    ],
}


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime):
        raise ValueError('Expected datetime')
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def validate_mode(mode):
    if mode not in ('as_of', 'reconstruction'):
        raise ValueError('replay_mode must be as_of or reconstruction')


def known_at(row, cutoff):
    """Only explicit evidence of availability; never infer from an epoch."""
    evidence = [row[k] for k in ('available_at', 'published_at', 'fetched_at') if row.get(k)]
    try:
        return bool(evidence) and all(utc(value) <= utc(cutoff) for value in evidence)
    except (ValueError, TypeError):
        return False


def read_rows(path):
    path = Path(path)
    try:
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get('records', [])
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    except (OSError, ValueError, EOFError) as exc:
        LOG.warning('Replay archive %s unavailable: %s', path, exc)
        return []


def weather_rows(directory, kind, as_of, mode='as_of', end=None):
    validate_mode(mode)
    cutoff = utc(as_of)
    # No stale archive from June may masquerade as data for July.
    if not START <= cutoff < END:
        return []
    upper = utc(end) if end is not None and mode == 'reconstruction' else cutoff
    if upper < cutoff:
        raise ValueError('reconstruction end must not precede as_of')
    lower = cutoff if mode == 'reconstruction' else START
    path = Path(directory) / f'archive_{kind}_may_june2024.json'
    if not path.exists() and path.with_suffix('.json.gz').exists():
        path = path.with_suffix('.json.gz')
    rows = read_rows(path)
    if not rows and not path.exists():
        LOG.warning('Using embedded replay smoke data for missing %s archive', kind)
        rows = _FALLBACK_ROWS.get(kind, [])
    selected = []
    for row in rows:
        try:
            timestamp = utc(row['time_tag'])
            # Kp denotes a three-hour bin. It cannot be known before its end.
            bin_end = utc(row.get('end_time', timestamp + timedelta(hours=3))) if kind == 'kp' else timestamp
            if not (START <= timestamp < END and lower <= timestamp <= upper):
                continue
            if mode == 'as_of' and (bin_end > cutoff or not known_at(row, cutoff)):
                continue
        except (ValueError, TypeError, KeyError):
            continue
        selected.append(row)
    return sorted(selected, key=lambda r: utc(r['time_tag']))
