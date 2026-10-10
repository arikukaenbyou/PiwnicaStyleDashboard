"""Claude plan limits: the 5-hour session, the weekly limits and extra usage, as Claude Code's /usage shows them.

GET https://api.anthropic.com/api/oauth/usage with Claude Code's own OAuth access token from
~/.claude/.credentials.json. The endpoint is not documented, so every field is optional here. The file is
re-read on every poll (Claude Code refreshes the token about every 8 h) and never written: refreshing the
token here would rotate it and log Claude Code out. An expired token keeps the last numbers on screen.
"""
import json
import os
import time

from .afterlife import iso
from .poll import Poller, get_json

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
LIMIT_LABELS = {'session': '5h session', 'weekly_all': 'week', 'weekly_opus': 'week opus',
                'weekly_sonnet': 'week sonnet'}
# older responses without limits[]
WINDOWS = (('five_hour', '5h session'), ('seven_day', 'week'), ('seven_day_opus', 'week opus'),
           ('seven_day_sonnet', 'week sonnet'))
EXPIRED = 'token expired · Claude Code refreshes it on its next use'


def read_credentials(path):
    """{'token', 'expires_at', 'plan'} from Claude Code's credentials file, or None."""
    try:
        with open(os.path.expanduser(path)) as f:
            o = json.load(f).get('claudeAiOauth') or {}
    except (OSError, ValueError, AttributeError):
        return None
    tok = o.get('accessToken')
    if not isinstance(tok, str) or not tok:
        return None
    exp = o.get('expiresAt')
    return {'token': tok, 'expires_at': exp / 1000 if isinstance(exp, (int, float)) else None,
            'plan': o.get('subscriptionType') if isinstance(o.get('subscriptionType'), str) else None}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def view(doc, now, fetched_at=None):
    """The panel's data from an /api/oauth/usage document."""
    limits = []
    for lim in doc.get('limits') or []:
        if not isinstance(lim, dict) or _num(lim.get('percent')) is None:
            continue
        kind = lim.get('kind') or '?'
        limits.append({'kind': kind, 'label': LIMIT_LABELS.get(kind, kind.replace('_', ' ')),
                       'pct': _num(lim['percent']), 'severity': lim.get('severity') or 'normal',
                       'resets_at': iso(lim.get('resets_at'))})
    if not limits:
        for key, label in WINDOWS:
            w = doc.get(key)
            if isinstance(w, dict) and _num(w.get('utilization')) is not None:
                limits.append({'kind': key, 'label': label, 'pct': _num(w['utilization']), 'severity': 'normal',
                               'resets_at': iso(w.get('resets_at'))})
    ex = doc.get('extra_usage') or {}
    extra = None
    if isinstance(ex, dict) and ex.get('is_enabled'):
        extra = {'pct': _num(ex.get('utilization')), 'used': _num(ex.get('used_credits')),
                 'limit': _num(ex.get('monthly_limit')), 'currency': ex.get('currency') or ''}
    rows = ((doc.get('seven_day_breakdown') or {}).get('rows') or [])
    breakdown = sorted(({'name': r.get('display_name') or r.get('key') or '?', 'pct': _num(r.get('percent'))}
                        for r in rows if isinstance(r, dict) and _num(r.get('percent'))),
                       key=lambda r: -r['pct'])
    return {'limits': limits, 'extra': extra, 'breakdown': breakdown,
            'age': now - fetched_at if fetched_at else None}


class ClaudeCollector:
    def __init__(self, credentials='~/.claude/.credentials.json', interval=60, background=True, fetch=get_json,
                 clock=time.time):
        self.path, self.fetch, self.clock = credentials, fetch, clock
        self.creds = None
        self.poller = Poller(self.fetch_usage, interval, background, clock)

    def fetch_usage(self):
        c = self.creds
        if not c:
            raise OSError('no credentials')
        if c['expires_at'] and c['expires_at'] <= self.clock():
            raise OSError(EXPIRED)
        try:
            return self.fetch(USAGE_URL, {'Authorization': f'Bearer {c["token"]}', 'anthropic-beta': 'oauth-2025-04-20',
                                          'User-Agent': 'piwnica-dashboard'}, timeout=10)
        except OSError as e:
            msg = str(e).replace(c['token'], '…')
            raise OSError(EXPIRED if msg.startswith('HTTP 401') else msg) from None

    def sample(self):
        self.creds = read_credentials(self.path)
        if not self.creds:
            return {'error': 'no credentials'}
        p = self.poller.poll()
        err = p.error.split(': ', 1)[-1] if p.error else None  # Poller prefixes the exception type
        if not p.value:
            return {'error': err or 'connecting…', 'plan': self.creds['plan']}
        out = view(p.value, self.clock(), p.at)
        out['plan'] = self.creds['plan']
        out['error'] = err  # a failed refresh keeps the last numbers, with the error underneath
        return out
