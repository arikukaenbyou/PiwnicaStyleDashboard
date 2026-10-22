"""ariku.pl Proxmox/LXC updater: is an update running, how did the last one end, which host/LXC failed.

The updater itself lives in afterlife (panel SYSTEM → Updater, lib/systemUpdater.ts) and is the only thing that
installs updates; the dashboard only reads its status. GET /api/infra/system-updater/status with its own read-only
service token (SYSTEM_UPDATER_STATUS_TOKEN) -- not the Luneta device token, not the Proxmox token. The server
already trims and masks the log; nothing here is written to disk.
"""
import os
import re
import time

from .afterlife import iso
from .poll import Poller, get_json

BUSY_INTERVAL = 15  # seconds between refreshes while an update runs
STALE_FACTOR = 3    # no successful refresh for this many intervals = the numbers on screen are old
TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{32,256}')
STATES = ('running', 'succeeded', 'failed', 'stale')
TARGET_STATES = ('running', 'succeeded', 'failed', 'unknown')


def read_status_token(path):
    try:
        with open(os.path.expanduser(path)) as f:
            tok = f.read().strip()
    except OSError:
        return None
    return tok if TOKEN_RE.fullmatch(tok) else None


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


def view(doc, now, fetched_at, interval):
    """The panel's data from a status document; anything missing or malformed degrades to None, never raises."""
    doc = doc if isinstance(doc, dict) else {}
    cur = doc.get('current') if isinstance(doc.get('current'), dict) else None
    run = None
    if cur and cur.get('state') in STATES:
        stage = cur.get('stage') if isinstance(cur.get('stage'), dict) else None
        started = iso(cur.get('startedAt'))
        duration = cur.get('durationSeconds')
        if cur['state'] in ('running', 'stale') and started is not None and fetched_at is not None:
            duration = max(0, int(now - started))  # keeps counting between refreshes
        run = {
            'id': _text(cur.get('id'), 36),
            'kind': cur.get('kind') if cur.get('kind') in ('check', 'apply') else None,
            'state': cur['state'],
            'started': started,
            'finished': iso(cur.get('finishedAt')),
            'duration': duration if isinstance(duration, (int, float)) else None,
            'stage': (f'{_text(stage.get("target"), 20) or "?"} {_text(stage.get("message"), 160) or ""}'.strip()
                      if stage else None),
            'error': _text(cur.get('error'), 300),
            'log': [_text(line, 300) for line in (cur.get('logTail') or [])[-6:] if isinstance(line, str)],
        }
    targets = []
    for t in doc.get('targets') or []:
        if isinstance(t, dict) and isinstance(t.get('key'), str):
            targets.append({'key': t['key'][:20], 'name': _text(t.get('name'), 60) or t['key'][:20],
                            'state': t.get('state') if t.get('state') in TARGET_STATES else 'unknown',
                            'error': _text(t.get('error'), 300), 'at': iso(t.get('at'))})
    lc = doc.get('lastCheck') if isinstance(doc.get('lastCheck'), dict) else None
    last_check = None
    if lc:
        last_check = {'at': iso(lc.get('at')),
                      'pending': lc.get('pendingTargets') if isinstance(lc.get('pendingTargets'), int) else None,
                      'security': lc.get('securityUpdates') if isinstance(lc.get('securityUpdates'), int) else None}
    age = now - fetched_at if fetched_at else None
    return {
        'configured': doc.get('configured') is not False,
        'run': run,
        'targets': targets,
        'last_check': last_check,
        'age': age,
        'stale': age is not None and age > STALE_FACTOR * interval,
    }


class UpdaterStatusCollector:
    def __init__(self, base_url='https://ariku.pl', token_file='~/.config/piwnica-dashboard/updater-status.token',
                 interval=300, background=True, fetch=get_json, clock=time.time):
        self.base, self.fetch, self.clock = base_url.rstrip('/'), fetch, clock
        self.token_file, self.interval = token_file, interval
        self.token = read_status_token(token_file)
        self.poller = Poller(self.fetch_status, interval, background, clock)

    def fetch_status(self):
        return self.fetch(f'{self.base}/api/infra/system-updater/status',
                          {'Authorization': f'Bearer {self.token}'}, timeout=10)

    def sample(self):
        if self.token is None:
            self.token = read_status_token(self.token_file)  # created later: picked up without a restart
        if not self.token:
            return {'error': 'no token'}
        p = self.poller.poll()
        if not p.value:
            return {'error': p.error or 'connecting…'}
        out = view(p.value, self.clock(), p.at, self.interval)
        running = out['run'] is not None and out['run']['state'] == 'running'
        self.poller.interval = BUSY_INTERVAL if running else self.interval
        out['error'] = p.error  # a failed refresh keeps the last status, with the error underneath
        return out
