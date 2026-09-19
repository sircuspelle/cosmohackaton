"""SQLite raw response cache with immutable snapshots and bounded network requests."""
import hashlib
import json
import sqlite3
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
from contextlib import closing
from src.main.events.core import now, iso, dt


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
            row = db.execute('SELECT id,url,fetched,body,headers FROM snapshots WHERE url=? AND fetched<=? ORDER BY fetched DESC LIMIT 1',
                             (url, iso(cutoff or now()))).fetchone()
        return dict(zip(('snapshot_id','url','fetched_at','body','headers'),row)) if row else None

    def fetch(self, spec, config, force=False, offline=False, cutoff=None):
        cached = self.last(spec['url'], cutoff)
        age = (now()-dt(cached['fetched_at'])).total_seconds() if cached else None
        if cached and (offline or cutoff or (not force and age < spec['ttl_seconds'])):
            return {**cached, 'transport_status': 'cached' if age < spec['ttl_seconds'] else 'stale', 'error': None}
        if offline or cutoff:
            raise ValueError('No stored response available before cutoff' if cutoff else 'No cached response')
        error = None
        for attempt in range(config['retry_count']+1):
            try:
                request = Request(spec['url'], headers={'User-Agent':'EVA-Event-Research/1.0', 'Accept':'application/json,text/csv,text/plain,*/*'})
                with urlopen(request, timeout=config['timeout_seconds']) as response:
                    raw = response.read(config['max_response_bytes']+1)
                    if len(raw) > config['max_response_bytes']:
                        raise ValueError('Response exceeds configured byte limit')
                    body = raw.decode('utf-8-sig')
                    headers = json.dumps(dict(response.headers), ensure_ascii=False)
                fetched = iso(now())
                digest = hashlib.sha256((spec['url']+'\n'+fetched+'\n'+body).encode()).hexdigest()
                with closing(self.connect()) as db, db:
                    db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)', (digest,spec['url'],fetched,body,headers))
                return dict(snapshot_id=digest, url=spec['url'], fetched_at=fetched, body=body,
                            headers=headers, transport_status='fresh', error=None)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                error = f'{type(exc).__name__}: {exc}'
                if isinstance(exc, HTTPError) and exc.code not in (429,500,502,503,504):
                    break
                if attempt < config['retry_count']:
                    time.sleep(min(2**attempt, 4))
        if cached:
            return {**cached, 'transport_status':'stale', 'error':error}
        raise ValueError(error)

    def save_run(self, result):
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        rid = hashlib.sha256(raw.encode()).hexdigest()[:24]
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?)', (rid,iso(now()),raw))
        return rid

    def run(self, rid):
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT result FROM runs WHERE id=?',(rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def snapshot(self, sid):
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT url,fetched,body,headers FROM snapshots WHERE id=?',(sid,)).fetchone()
        return dict(zip(('url','fetched_at','body','headers'),row)) if row else None
