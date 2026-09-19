"""SQLite raw response cache with immutable snapshots and bounded network requests.

HTTP-загрузка вынесена в src.main.apis.http_client.HttpClient.
Store отвечает только за:
  * кэш сырых ответов в SQLite (snapshots)
  * TTL/offline/cutoff логику
  * сохранение результатов прогонов (runs)
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from contextlib import closing
from src.main.core import now, iso, dt
from src.main.apis.http_client import HttpClient, HttpClientError


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, url TEXT, fetched TEXT, body TEXT, headers TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS snapshot_url_time ON snapshots(url, fetched)')
            db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT, result TEXT)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def last(self, url, cutoff=None):
        with closing(self.connect()) as db, db:
            row = db.execute(
                'SELECT id,url,fetched,body,headers FROM snapshots WHERE url=? AND fetched<=? ORDER BY fetched DESC LIMIT 1',
                (url, iso(cutoff or now()))
            ).fetchone()
        return dict(zip(('snapshot_id', 'url', 'fetched_at', 'body', 'headers'), row)) if row else None

    def _save_snapshot(self, url: str, fetched: str, body: str, headers: str) -> str:
        """Сохраняет тело ответа в SQLite, возвращает snapshot_id."""
        digest = hashlib.sha256((url + '\n' + fetched + '\n' + body).encode()).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)',
                       (digest, url, fetched, body, headers))
        return digest

    def fetch(self, spec, config, force=False, offline=False, cutoff=None,
              http_client: HttpClient | None = None):
        """
        Получает данные для spec с учётом TTL-кэша.

        HTTP-загрузка делегируется http_client (HttpClient).
        Если http_client не передан — создаётся с параметрами из config.
        """
        cached = self.last(spec['url'], cutoff)
        age = (now() - dt(cached['fetched_at'])).total_seconds() if cached else None
        if cached and (offline or cutoff or (not force and age < spec['ttl_seconds'])):
            transport_status = 'cached' if age < spec['ttl_seconds'] else 'stale'
            return {**cached, 'transport_status': transport_status, 'error': None}
        if offline or cutoff:
            raise ValueError('No stored response available before cutoff' if cutoff else 'No cached response')

        # ---- HTTP-загрузка через HttpClient из apis/ ----
        client = http_client or HttpClient(
            timeout=config.get('timeout_seconds', 20),
            retries=config.get('retry_count', 1),
            max_bytes=config.get('max_response_bytes', 25_000_000),
        )
        error = None
        try:
            body = client.get_text(spec['url'])
            fetched = iso(now())
            digest = self._save_snapshot(spec['url'], fetched, body, '{}')
            return dict(
                snapshot_id=digest,
                url=spec['url'],
                fetched_at=fetched,
                body=body,
                headers='{}',
                transport_status='fresh',
                error=None,
            )
        except HttpClientError as exc:
            error = f'{type(exc).__name__}: {exc}'

        if cached:
            return {**cached, 'transport_status': 'stale', 'error': error}
        raise ValueError(error)

    def save_run(self, result):
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        rid = hashlib.sha256(raw.encode()).hexdigest()[:24]
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?)', (rid, iso(now()), raw))
        return rid

    def run(self, rid):
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT result FROM runs WHERE id=?', (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def snapshot(self, sid):
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT url,fetched,body,headers FROM snapshots WHERE id=?', (sid,)).fetchone()
        return dict(zip(('url', 'fetched_at', 'body', 'headers'), row)) if row else None
