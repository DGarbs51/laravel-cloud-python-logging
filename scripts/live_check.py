#!/usr/bin/env python3
"""Live check of laravel_cloud_logging on a Laravel Cloud environment.

1. python scripts/live_check.py command <env>
   Prints a `cpx cloud command:run` command that ships this package and script
   inside the --cmd (a base64 zipapp), so nothing has to be deployed. Run it.
   command:run output is never logged, but lines written to the socket are.
2. python scripts/live_check.py verify <app> <env> <marker> <from> <to>
   Reads the window back with `cpx cloud environment:logs --json` and checks
   types, levels, the exception entry, 40/40 concurrent lines and the large record.
Then check the dashboard Logs page by hand: level tags/colours and the exception chain.
"""

import base64
import io
import json
import logging
import shlex
import subprocess
import sys
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEVELS = ('DEBUG', 'INFO', 'NOTICE', 'WARNING', 'ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY')


def emit(marker):
    import laravel_cloud_logging as lcl

    lcl.configure('DEBUG')
    log = logging.getLogger('live_check')
    tag = f'live {marker}'
    start = datetime.now(timezone.utc)
    for name in LEVELS:
        log.log(logging.getLevelName(name), f'{tag} level {name}')
    log.info(f'{tag} extra', extra={'order_id': 7, 'source': 'nginx-app', 'context': 'x',
                                     'logger': 'http.log.access.log0', '_cloud_event': 'exception'})
    try:
        try:
            raise KeyError('inner')
        except KeyError as inner:
            raise RuntimeError('outer') from inner
    except RuntimeError:
        log.exception(f'{tag} exception')
    token = lcl.cloud_request_id.set(f'req-{marker}')
    log.info(f'{tag} request')
    lcl.cloud_request_id.reset(token)
    log.warning(f'{tag} large', extra={'blob': 'x' * 600_000})
    threads = [threading.Thread(target=lambda i=i: [log.info(f'{tag} concurrent {i} {"p" * 20000}') for _ in range(5)])
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logging.shutdown()
    end = datetime.now(timezone.utc)
    print(json.dumps({'marker': marker, 'from': start.isoformat(timespec='seconds'),
                      'to': (end + timedelta(seconds=2)).isoformat(timespec='seconds'),
                      'handler_address': lcl.CloudHandler().address}))


def command(env):
    """Bundle the package and this script into a zipapp and print the command:run line."""
    root = Path(__file__).resolve().parents[1]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(__file__, '__main__.py')
        for path in (root / 'src' / 'laravel_cloud_logging').glob('*.py'):
            z.write(path, f'laravel_cloud_logging/{path.name}')
    payload = base64.b64encode(buf.getvalue()).decode()
    marker = uuid.uuid4().hex[:12]
    inner = (f"python3 -c 'import base64;open(\"/tmp/lcl-live.pyz\",\"wb\").write(base64.b64decode(\"{payload}\"))'"
             f' && python3 /tmp/lcl-live.pyz emit {marker}')
    print('cpx cloud command:run', shlex.quote(env), shlex.quote(f'--cmd={inner}'))
    print(f'# marker: {marker}', file=sys.stderr)


def fetch(app, env, low, high):
    """Collect a window; the API returns at most 100 rows, so split until under the cap."""
    out = subprocess.run(['cpx', 'cloud', 'environment:logs', app, env, f'--from={low.isoformat()}',
                          f'--to={high.isoformat()}', '--json'], capture_output=True, text=True, check=True).stdout
    batch = json.loads(out)
    batch = batch.get('logs', []) if isinstance(batch, dict) else batch  # empty window: {"logs": []}
    if len(batch) >= 100 and (high - low).total_seconds() > 1:
        middle = low + (high - low) / 2
        return fetch(app, env, low, middle) + fetch(app, env, middle, high)
    if len(batch) >= 100:
        print(f'WARNING: {low}..{high} hit the 100-row cap; counts may be low', file=sys.stderr)
    return batch


def verify(app, env, marker, start, end):
    low, high = datetime.fromisoformat(start), datetime.fromisoformat(end)
    entries, seen = [], set()
    while low < high:
        stop = min(low + timedelta(seconds=5), high)
        for e in fetch(app, env, low, stop):
            key = json.dumps(e, sort_keys=True)
            if key not in seen and f'live {marker}' in str(e.get('message', '')):
                seen.add(key)
                entries.append(e)
        low = stop
    tag = f'live {marker} '
    by_case = {}
    for e in entries:
        by_case.setdefault(e['message'][len(tag):].split(' ')[0], []).append(e)
    levels = {e['message'][len(tag) + 6:]: e.get('level') for e in by_case.get('level', [])}
    results = {
        'all_application': sorted({e.get('type') for e in entries}) == ['application'],
        'levels_seen': levels,
        'all_levels_present': sorted(levels) == sorted(LEVELS),
        'one_exception_entry': len(by_case.get('exception', [])) == 1,
        'exception_has_chain': 'previous' in json.dumps(by_case.get('exception', [{}])[0]),
        'request_id_present': f'req-{marker}' in json.dumps(by_case.get('request', [])),
        'extra_kept_in_context': len(by_case.get('extra', [])) == 1,
        'concurrent_40': len(by_case.get('concurrent', [])) == 40,
        'concurrent_whole': all(e['message'].endswith('p' * 100) for e in by_case.get('concurrent', [])),
        'large_record_json_at_warning': [e.get('level') for e in by_case.get('large', [])] == ['warning'],
        'entries': len(entries),
    }
    print(json.dumps(results, indent=2))
    for case in ('exception', 'large', 'extra'):
        sample = by_case.get(case, [{}])[0]
        print(f'--- {case} entry:\n' + json.dumps(sample, ensure_ascii=False)[:2000])
    checks = [v for k, v in results.items() if isinstance(v, bool)]
    sys.exit(0 if all(checks) else 1)


if __name__ == '__main__':
    mode, *rest = sys.argv[1:] or ['help']
    if mode == 'emit' and len(rest) == 1:
        emit(rest[0])
    elif mode == 'command' and len(rest) == 1:
        command(rest[0])
    elif mode == 'verify' and len(rest) == 5:
        verify(*rest)
    else:
        sys.exit(__doc__)
