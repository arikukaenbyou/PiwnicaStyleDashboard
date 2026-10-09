"""ariku.pl / Luneta: the games' resets, dailies and events, the recording analysis, and the
companion's "what now" (the same engine as the phone app).

Local first: the Luneta agent keeps the last /api/luneta/state per game in its SQLite database
(`state_cache`), its analysis progress in a JSON file and its heartbeats in a log -- all readable
without any token. With a Luneta device token (~/.config/piwnica-dashboard/luneta.token, created on
ariku.pl/luneta as a device) the states are refreshed from the server and /api/companion/now works.
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime

from .poll import Poller, get_json

LUNETA_DIR = '~/.local/share/pl.ariku.luneta'
GAME_NAMES = {'blue_archive': 'Blue Archive', 'nte': 'NTE', 'snowbreak': 'Snowbreak'}


def iso(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None


def read_token(path):
    try:
        with open(os.path.expanduser(path)) as f:
            tok = f.read().strip()
    except OSError:
        return None
    return tok if re.fullmatch(r'lnt_[A-Za-z0-9_-]{43}', tok) else None


def game_view(state, now, fetched_at=None):
    """One game's line in the panel, from an /api/luneta/state document."""
    key = state.get('game') or '?'
    resets = state.get('resetAt') or {}
    daily = iso(resets.get('daily'))
    stale = daily is not None and daily <= now  # the cached state is from before the last reset
    while daily is not None and daily <= now:
        daily += 86400
    weekly = iso(resets.get('weekly'))
    while weekly is not None and weekly <= now:
        weekly += 7 * 86400
    period = (state.get('period') or {}).get('daily')
    tasks = [t for t in state.get('tasks') or [] if t.get('period') == 'daily' and t.get('periodKey', period) == period]
    done = 0 if stale else sum(1 for t in tasks if t.get('done'))  # after a reset nothing is done yet
    critical_left = sum(1 for t in tasks if t.get('priority') == 'critical' and (stale or not t.get('done')))
    premium = next((c for c in state.get('currencies') or [] if c.get('premium')), None)
    amount = None
    if premium:
        amount = next((s.get('amount') for s in state.get('lastSnapshots') or [] if s.get('currency') == premium['key']), None)
    events = sorted((e for e in state.get('events') or [] if (iso(e.get('endsAt')) or 0) > now),
                    key=lambda e: iso(e['endsAt']))
    advice = max(state.get('advice') or [], key=lambda a: a.get('weight', 0), default=None)
    return {
        'key': key, 'name': GAME_NAMES.get(key, key.replace('_', ' ').title()),
        'daily_in': daily - now if daily else None, 'weekly_in': weekly - now if weekly else None,
        'done': done, 'tasks': len(tasks), 'critical_left': critical_left, 'stale': stale,
        'premium': ({'label': premium.get('label'), 'amount': amount,
                     'pulls': amount // premium['pullCost'] if amount is not None and premium.get('pullCost') else None}
                    if premium else None),
        'event': ({'name': events[0].get('name'), 'ends_in': iso(events[0]['endsAt']) - now} if events else None),
        'advice': advice.get('text') if advice else None,
        'age': now - fetched_at if fetched_at else None,
    }


def local_states(db_path):
    """{game: (state, fetched_at)} from the agent's state_cache (read-only, never locks the agent out)."""
    path = os.path.expanduser(db_path)
    if not os.path.exists(path):
        return {}
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=1)
    try:
        return {g: (json.loads(data), at / 1000) for g, data, at in c.execute('select game, data, fetched_at from state_cache')}
    finally:
        c.close()


def agent_status(log_path, tail=65536):
    """(last heartbeat time, quit after it) from the end of luneta.log."""
    try:
        with open(os.path.expanduser(log_path), 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail))
            text = f.read().decode(errors='replace')
    except OSError:
        return None, False
    last_hb, quit_after = None, False
    for ln in text.splitlines():
        m = re.match(r'\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)', ln)
        if not m:
            continue
        if 'heartbeat ok' in ln:
            last_hb, quit_after = iso(m.group(1)), False
        elif 'tray: quit' in ln:
            quit_after = True
    return last_hb, quit_after


def analysis_progress(path):
    try:
        with open(os.path.expanduser(path)) as f:
            p = json.load(f)
    except (OSError, ValueError):
        return None
    out = {k: p.get(k) for k in ('running', 'paused', 'index', 'of', 'done', 'errors', 'stage', 'etaS',
                                 'mediaDoneS', 'mediaTotalS')}
    if out['running'] and p.get('pid') and not os.path.exists(f'/proc/{p["pid"]}'):
        out['running'], out['dead'] = False, True  # the file says running, its process is gone
    return out


def agent_running():
    from .. import sysfs
    try:
        pids = [p for p in os.listdir(sysfs.p('/proc')) if p.isdigit()]
    except OSError:
        return False
    return any(sysfs.read(f'/proc/{p}/comm', '') == 'luneta' for p in pids)


class AfterlifeCollector:
    def __init__(self, base_url='https://ariku.pl', token_file='~/.config/piwnica-dashboard/luneta.token',
                 luneta_dir=LUNETA_DIR, background=True, fetch=get_json, clock=time.time):
        self.base, self.dir, self.fetch, self.clock = base_url.rstrip('/'), luneta_dir, fetch, clock
        self.token_file = token_file
        self.token = read_token(token_file)
        self.local = Poller(self.read_local, 30, background, clock)
        self.remote = Poller(self.fetch_states, 300, background, clock)
        self.now_poll = Poller(self.fetch_now, 60, background, clock)

    def auth(self):
        return {'Authorization': f'Bearer {self.token}'}

    def read_local(self):
        return {
            'states': local_states(os.path.join(self.dir, 'luneta.db')),
            'agent': agent_status(os.path.join(self.dir, 'logs', 'luneta.log')),
            'running': agent_running(),
            'analysis': analysis_progress(os.path.join(self.dir, 'analiza.db.progress.json')),
        }

    def fetch_states(self):
        games = list((self.local.value or {}).get('states') or {}) or list(GAME_NAMES)
        out = {}
        for g in games:
            try:
                out[g] = (self.fetch(f'{self.base}/api/luneta/state?game={g}', self.auth()), self.clock())
            except OSError:
                continue
        return out

    def fetch_now(self):
        return self.fetch(f'{self.base}/api/companion/now', self.auth())

    def sample(self):
        if self.token is None:
            self.token = read_token(self.token_file)  # created later on ariku.pl/luneta: picked up without a restart
        now = self.clock()
        loc = self.local.poll().value or {}
        states = dict(loc.get('states') or {})
        if self.token:
            for g, (st, at) in (self.remote.poll().value or {}).items():
                if g not in states or at > states[g][1]:
                    states[g] = (st, at)
        hb, quit_after = loc.get('agent') or (None, False)
        luneta = {'games': [game_view(st, now, at) for _g, (st, at) in sorted(states.items())],
                  'analysis': loc.get('analysis'), 'agent_running': loc.get('running'),
                  'heartbeat': hb, 'agent_quit': quit_after, 'remote': bool(self.token),
                  'remote_error': self.remote.error if self.token else None}
        if not self.token:
            companion = {'error': 'no token'}
        else:
            p = self.now_poll.poll()
            companion = dict(p.value or {}, error=p.error if (p.error or not p.value) else None)
            if not p.value and not p.error:
                companion['error'] = 'connecting…'
        return luneta, companion
