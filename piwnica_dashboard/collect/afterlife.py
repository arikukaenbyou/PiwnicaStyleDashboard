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
import socket
import sqlite3
import time
from datetime import datetime

from .poll import Poller, get_json

LUNETA_DIR = '~/.local/share/pl.ariku.luneta'
GAME_NAMES = {'blue_archive': 'Blue Archive', 'nte': 'NTE', 'snowbreak': 'Snowbreak'}
KNOWN_HOSTS = {
    '192.168.1.23': 'felix-pc',
    '192.168.1.228': 'homelab',
    '127.0.0.1': 'localhost',
}


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


def resolve_node_name(addr):
    if not addr:
        return '?'
    ip = str(addr).split(':')[0]
    return KNOWN_HOSTS.get(ip, ip)


def format_node_entry(item):
    if isinstance(item, str):
        return resolve_node_name(item)
    if isinstance(item, dict):
        name = resolve_node_name(item.get('node') or item.get('name') or '?')
        scan = item.get('scan') or item.get('stage') or item.get('scan_type')
        filename = item.get('file') or item.get('current')
        if filename:
            filename = os.path.basename(filename)
        pct = f" {int(item['pct'])}%" if 'pct' in item else ""
        scan_str = f"[{scan}{pct}]" if scan else ""
        parts = [scan_str, filename or ""]
        details = " ".join(filter(None, parts))
        return f"{name}: {details}" if details else name
    return str(item)


def analysis_nodes(luneta_db_path=None, progress_data=None):
    local_name = socket.gethostname() or 'ariku-pc'
    p = progress_data or {}
    if p.get('nodes'):
        raw = p['nodes']
        if isinstance(raw, dict):
            return [format_node_entry({'node': k, **v} if isinstance(v, dict) else {'node': k, 'scan': str(v)}) for k, v in raw.items()]
        return [format_node_entry(n) for n in raw]
    if p.get('peer'):
        peer_name = resolve_node_name(p['peer'])
        return [local_name, peer_name] if peer_name != local_name else [local_name]
    if p.get('node'):
        node_name = resolve_node_name(p['node'])
        return [local_name, node_name] if node_name != local_name else [local_name]

    kv = {}
    if luneta_db_path and os.path.exists(os.path.expanduser(luneta_db_path)):
        c = None
        try:
            c = sqlite3.connect(f'file:{os.path.expanduser(luneta_db_path)}?mode=ro', uri=True, timeout=1)
            kv = dict(c.execute("select key, value from kv where key in ('analysis_agent', 'analysis_speech_remote', 'analysis_agent_tandem')"))
        except (sqlite3.Error, OSError):
            pass
        finally:
            if c:
                c.close()

    agent = kv.get('analysis_agent')
    speech = kv.get('analysis_speech_remote')
    tandem = kv.get('analysis_agent_tandem') == '1'

    if agent:
        remote_name = resolve_node_name(agent)
        return [local_name, remote_name] if tandem else [remote_name]
    if speech:
        remote_name = resolve_node_name(speech)
        return [local_name, f'{remote_name} (mowa)']
    return [local_name]


def library_stats(db_path):
    path = os.path.expanduser(db_path)
    if not os.path.exists(path):
        return None
    c = None
    try:
        c = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=1)
        row = c.execute(
            'select count(*), '
            'count(case when status = "done" then 1 end), '
            'count(case when status = "error" then 1 end), '
            'coalesce(sum(case when status = "done" then duration end), 0), '
            'coalesce(sum(duration), 0) '
            'from recordings'
        ).fetchone()
        if not row or not row[0]:
            return None
        total, done, errors, done_s, total_s = row
        return {
            'running': False,
            'paused': False,
            'dead': False,
            'of': total,
            'done': done,
            'errors': errors,
            'mediaDoneS': done_s,
            'mediaTotalS': total_s,
        }
    except (sqlite3.Error, OSError):
        return None
    finally:
        if c:
            c.close()


def analysis_progress(path, db_path=None, luneta_db_path=None):
    p = None
    try:
        with open(os.path.expanduser(path)) as f:
            p = json.load(f)
    except (OSError, ValueError):
        p = None

    if p:
        out = {k: p.get(k) for k in ('running', 'paused', 'index', 'of', 'done', 'errors', 'stage', 'etaS',
                                     'mediaDoneS', 'mediaTotalS', 'nodes', 'node', 'peer', 'by_scan')}
        if out.get('running') and p.get('pid') and not os.path.exists(f'/proc/{p["pid"]}'):
            out['running'], out['dead'] = False, True  # the file says running, its process is gone
    elif db_path:
        out = library_stats(db_path)
    else:
        out = None

    if out:
        ldb = luneta_db_path or os.path.join(os.path.dirname(os.path.expanduser(path)), 'luneta.db')
        out['nodes'] = analysis_nodes(ldb, p or out)
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
            'analysis': analysis_progress(
                os.path.join(self.dir, 'analiza.db.progress.json'),
                os.path.join(self.dir, 'analiza.db'),
                os.path.join(self.dir, 'luneta.db'),
            ),
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
