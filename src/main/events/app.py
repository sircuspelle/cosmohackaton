#!/usr/bin/env python3
"""CLI and localhost HTTP service; standard library only."""
import argparse
import json
import logging
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from src.main.events.core import now, iso, assess
from src.main.events.service import config_load, run
from src.main.events.storage import Store

LOG = logging.getLogger('eva')


def serve(config, host, port):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, data):
            payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/requirements':
                return self.reply(200, {'requirements': {'max_radiation_overlap_minutes':'number', 'max_geomagnetic_overlap_minutes':'number', 'max_communications_overlap_minutes':'number', 'max_tracked_debris_overlap_minutes':'number', 'min_data_coverage_fraction':'0..1'}, 'note':'Передаются в поле requirements запроса'})
            if path == '/health':
                return self.reply(200, {'status':'ok', 'time':iso(now())})
            if path in ('/', '/docs'):
                return self.reply(200, {'service':'EVA event metrics', 'endpoints':{
                    'POST /assess':{'start':'ISO UTC', 'duration_hours':'1..8', 'search_hours':'0..24',
                                    'step_minutes':'15..1440', 'mode':'live|reconstruction|as_of',
                                    'as_of':'required for as_of', 'force_refresh':False},
                    'GET /runs/{id}':'Saved result', 'GET /snapshots/{id}':'Raw response evidence',
                    'GET /health':'Health'}, 'note':'Use app.py --help for offline and demo modes'})
            for prefix, method in [('/runs/', 'run'), ('/snapshots/', 'snapshot')]:
                if path.startswith(prefix):
                    item = getattr(Store(config['database']), method)(path[len(prefix):])
                    return self.reply(200 if item else 404, item or {'error':'not_found'})
            self.reply(404, {'error':'not_found'})

        def do_POST(self):
            if urlparse(self.path).path not in ('/assess','/best-window'):
                return self.reply(404, {'error':'not_found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    return self.reply(413, {'error':'Expected JSON body of at most 64 KiB'})
                q = json.loads(self.rfile.read(length))
                if not isinstance(q, dict):
                    raise ValueError('Expected JSON object')
                result = run(q, config)
                self.reply(200, result)
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {'error':str(exc)})
            except Exception:
                LOG.exception('Assessment failed')
                self.reply(500, {'error':'internal_error'})
    print(f'EVA event metrics: http://{host}:{port}/docs', flush=True)
    ThreadingHTTPServer((host,port),Handler).serve_forever()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config')
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('serve'); s.add_argument('--host', default='127.0.0.1'); s.add_argument('--port', type=int, default=8000)
    s = sub.add_parser('assess'); s.add_argument('--start', default=None)
    s.add_argument('--duration-hours', type=float, default=6); s.add_argument('--search-hours', type=float, default=6)
    s.add_argument('--step-minutes', type=float, default=60)
    s.add_argument('--mode', choices=['live','reconstruction','as_of'], default='live')
    s.add_argument('--as-of'); s.add_argument('--offline', action='store_true'); s.add_argument('--force-refresh', action='store_true')
    s.add_argument('--output', default='result.json')
    s = sub.add_parser('conditions'); s.add_argument('--start', required=True); s.add_argument('--duration-hours', type=float, default=6); s.add_argument('--mode', choices=['live','reconstruction','as_of'], default='live'); s.add_argument('--as-of'); s.add_argument('--energy', default='>=10 MeV'); s.add_argument('--output', default='conditions.json')
    s = sub.add_parser('demo'); s.add_argument('--output', default='demo_result.json')
    s = sub.add_parser('replay'); s.add_argument('run_id'); s.add_argument('--output', default='replayed.json')
    args = p.parse_args()
    config = config_load(args.config)
    if args.command == 'serve':
        return serve(config,args.host,args.port)
    if args.command == 'conditions':
        from src.main.adapter import SpaceDataAdapter
        from src.main.apis.noaa_client import NoaaClient
        from src.main.apis.celestrak_client import CelesTrakClient
        from src.main.apis.spacetrack_client import SpaceTrackClient
        from datetime import datetime, timezone
        parse_dt=lambda x: datetime.fromisoformat(x.replace('Z','+00:00')).astimezone(timezone.utc)
        start=parse_dt(args.start); cutoff=parse_dt(args.as_of) if args.as_of else None
        if args.mode == 'as_of' and cutoff is None: p.error('--as-of is required for mode as_of')
        if cutoff and cutoff > start: p.error('--as-of must not be later than --start')
        adapter=SpaceDataAdapter(noaa=NoaaClient(), celestrak=CelesTrakClient(), spacetrack=SpaceTrackClient())
        ctx=adapter.build_context(as_of=cutoff, energy=args.energy)
        result={'start':args.start,'duration_hours':args.duration_hours,'mode':args.mode,'as_of':args.as_of,'protons':[{'time':x.time.isoformat(),'flux':x.flux} for x in ctx.protons],'iss_tle':ctx.iss_tle.__dict__ if ctx.iss_tle else None,'sep_events':ctx.sep_events,'alerts':ctx.alerts}
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)); print(f'Saved {args.output}')
        return
    if args.command == 'demo':
        from src.main.events.demo import make_demo
        result = make_demo(config)
    elif args.command == 'replay':
        result = Store(config['database']).run(args.run_id)
        if result is None:
            p.error('Run not found')
        # Recompute metrics from saved normalized inputs; raw provider bodies stay in SQLite.
        bundle = {k:result.get(k,[]) for k in ('events','sources','context','limitations')}
        bundle['coverage'] = result.get('coverage_intervals',[])
        result = assess(bundle,result['query'])
    else:
        q = {'start':args.start or iso(now()), 'duration_hours':args.duration_hours,
             'search_hours':args.search_hours, 'step_minutes':args.step_minutes,
             'mode':args.mode, 'force_refresh':args.force_refresh}
        if args.as_of: q['as_of'] = args.as_of
        result = run(q, config, args.offline)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(f"Saved {args.output}: {len(result['windows'])} windows, {len(result['events'])} events; {result['recommendation']['status']}")


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    main()
