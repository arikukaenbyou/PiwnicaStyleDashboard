"""Claude plan limits: the 5-hour session, the weekly limits and extra usage, as Claude Code's /usage shows them,
plus the 228 and 228-2 CLIProxyAPI (Antigravity) accounts and ChatGPT (Codex) limits when configured.

GET https://api.anthropic.com/api/oauth/usage with Claude Code's own OAuth access token from
~/.claude/.credentials.json. The endpoint is not documented, so every field is optional here. The file is
re-read on every poll (Claude Code refreshes the token about every 8 h) and never written: refreshing the
token here would rotate it and log Claude Code out. An expired token keeps the last numbers on screen.
"""
import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

from .afterlife import iso
from .poll import Poller, get_json

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
CODEX_USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
LIMIT_LABELS = {'session': '5h session', 'weekly_all': 'week', 'weekly_opus': 'week opus',
                'weekly_sonnet': 'week sonnet'}
# older responses without limits[]
WINDOWS = (('five_hour', '5h session'), ('seven_day', 'week'), ('seven_day_opus', 'week opus'),
           ('seven_day_sonnet', 'week sonnet'))
EXPIRED = 'token expired · Claude Code refreshes it on its next use'

REMOTE_QUOTA_SCRIPT = r'''
import glob
import json
import os
import urllib.request

ACCOUNTS = (('228', '/root/.cli-proxy-api'), ('228-2', '/root/.cli-proxy-api-second'))
URLS = (
    'https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary',
    'https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary',
    'https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary',
)
UA = 'antigravity/cli/1.0.13 (aidev_client; os_type=darwin; arch=arm64)'

out = []
for name, directory in ACCOUNTS:
    paths = sorted(glob.glob(os.path.join(directory, 'antigravity-*.json')))
    if not paths:
        out.append({'name': name, 'error': 'no credentials'})
        continue
    try:
        with open(paths[0]) as f:
            cred = json.load(f)
    except (OSError, ValueError) as e:
        out.append({'name': name, 'error': f'{type(e).__name__}: {e}'[:80]})
        continue
    tok = cred.get('access_token')
    proj = cred.get('project_id') or 'aicode-consumers'
    if not isinstance(tok, str) or not tok:
        out.append({'name': name, 'error': 'no access token'})
        continue
    body = json.dumps({'project': proj}).encode()
    headers = {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json', 'User-Agent': UA}
    groups, last_err = None, None
    for url in URLS:
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=5) as resp:
                doc = json.loads(resp.read().decode())
            if isinstance(doc.get('groups'), list):
                groups = doc['groups']
                break
        except Exception as e:
            last_err = str(e).replace(tok, '…')[:80]
    if groups is not None:
        out.append({'name': name, 'groups': groups})
    else:
        out.append({'name': name, 'error': last_err or 'quota unavailable'})
claude_cache = None
try:
    with open('/root/.claude/vscode-claude-status-cache.json') as f:
        claude_cache = json.load(f)
except (OSError, ValueError):
    pass
print(json.dumps({'accounts': out, 'claude_cache': claude_cache}))
'''


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


def read_codex_credentials(path='~/.codex/auth.json'):
    """{'token', 'account_id'} from Codex's auth.json, or None."""
    if not path:
        return None
    try:
        with open(os.path.expanduser(path)) as f:
            d = json.load(f)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(d, dict):
        return None
    tokens = d.get('tokens') if isinstance(d.get('tokens'), dict) else d
    tok = tokens.get('access_token')
    if not isinstance(tok, str) or not tok:
        return None
    acc = tokens.get('account_id')
    return {'token': tok, 'account_id': acc if isinstance(acc, str) and acc else None}


def resolve_proxy_host(proxy_host='auto', settings_path='~/.claude/settings.json', env=None):
    """Hostname of the CLIProxyAPI host from config or ~/.claude/settings.json, or None."""
    if not proxy_host:
        return None
    if proxy_host != 'auto':
        return proxy_host if re.fullmatch(r'[A-Za-z0-9.-]+', str(proxy_host)) else None
    env = env if env is not None else os.environ
    urls = [env.get('ARIKU_GEMINI_URL'), env.get('ARIKU_GEMINI_URL_FALLBACK')]
    try:
        with open(os.path.expanduser(settings_path)) as f:
            s_env = (json.load(f).get('env') or {})
        urls.extend([s_env.get('ARIKU_GEMINI_URL'), s_env.get('ARIKU_GEMINI_URL_FALLBACK')])
    except (OSError, ValueError, AttributeError):
        pass
    for u in urls:
        if isinstance(u, str) and u:
            host = urlparse(u).hostname
            if host and re.fullmatch(r'[A-Za-z0-9.-]+', host):
                return host
    return None


def scan_antigravity_quota(host, ssh_user='root', timeout=15, runner=subprocess.run):
    """Read-only quota check for the 228 and 228-2 CLIProxyAPI accounts over SSH."""
    if not host or not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise ValueError('invalid proxy host')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', ssh_user):
        raise ValueError('invalid SSH user')
    command = [
        'ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
        '-o', 'StrictHostKeyChecking=yes', f'{ssh_user}@{host}', 'python3', '-',
    ]
    proc = runner(command, input=REMOTE_QUOTA_SCRIPT, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or f'ssh exited {proc.returncode}').strip()
        raise OSError(detail[:120])
    try:
        result = json.loads(proc.stdout)
    except ValueError as e:
        raise ValueError('invalid JSON from proxy quota scan') from e
    if not isinstance(result.get('accounts'), list):
        raise ValueError('proxy quota response has no accounts list')
    return result


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ts(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) / 1000 if v > 1e11 else float(v)
    return iso(v)


def _group_max_pct(group):
    pcts = [1.0 - _num(b.get('remainingFraction')) for b in (group.get('buckets') or [])
            if isinstance(b, dict) and _num(b.get('remainingFraction')) is not None]
    return max(pcts, default=-1.0)


def status_cache_limits(cache_doc):
    """Fallback Claude Code 5h/week limits from vscode-claude-status-cache.json when /api/oauth/usage 429s."""
    u = (cache_doc or {}).get('usageData') if isinstance(cache_doc, dict) else None
    if not isinstance(u, dict):
        return []
    out = []
    for key, rkey, kind, label in (('utilization5h', 'reset5hAt', 'session', '5h session'),
                                   ('utilization7d', 'reset7dAt', 'weekly_all', 'week')):
        val = _num(u.get(key))
        if val is None:
            continue
        pct = round(val * 100, 1) if val <= 1.0 else round(val, 1)
        out.append({
            'kind': kind, 'label': label, 'short_label': label, 'group': 'Claude', 'pct': pct,
            'severity': 'critical' if pct >= 90 else 'warning' if pct >= 70 else 'normal',
            'resets_at': _ts(u.get(rkey)),
        })
    return out


def _append_buckets(limits, buckets_list, kind_prefix, label_5h, label_week, short_5h, short_week, pool_name, group_title):
    buckets = {b.get('window'): b for b in (buckets_list or []) if isinstance(b, dict)}
    for win_key, kind_suf, label, short in (('5h', '5h', label_5h, short_5h),
                                            ('weekly', 'weekly', label_week, short_week)):
        b = buckets.get(win_key)
        if not b or _num(b.get('remainingFraction')) is None:
            continue
        pct = max(0.0, min(100.0, round((1.0 - _num(b['remainingFraction'])) * 1000) / 10))
        limits.append({
            'kind': f'{kind_prefix}_{kind_suf}',
            'label': label,
            'short_label': short,
            'group': group_title,
            'pct': pct,
            'severity': 'critical' if pct >= 90 else 'warning' if pct >= 70 else 'normal',
            'resets_at': iso(b.get('resetTime')),
            'pool': pool_name,
        })


def antigravity_view(doc, pool='auto'):
    """(limits, error) for the shared Antigravity pool (Gemini + Opus/Sonnet 5.5)."""
    limits, errors, gemini_g, claude_g = [], [], [], []
    for acc in (doc or {}).get('accounts') or []:
        if not isinstance(acc, dict):
            continue
        name = str(acc.get('name') or '?')
        if acc.get('error'):
            errors.append(f'{name}: {acc["error"]}')
            continue
        for g in (acc.get('groups') or []):
            if not isinstance(g, dict):
                continue
            dn = str(g.get('displayName') or '').lower()
            if 'gemini' in dn:
                gemini_g.append(g)
            elif 'claude' in dn:
                claude_g.append(g)
    if pool in ('auto', 'gemini') and gemini_g:
        group = max(gemini_g, key=_group_max_pct)
        _append_buckets(limits, group.get('buckets'), 'antigravity_gemini',
                        'gemini 5h', 'gemini week', 'gemini 5h', 'gemini week', 'gemini', 'Antigravity')
    if pool in ('auto', 'claude') and claude_g:
        cgrp = max(claude_g, key=_group_max_pct)
        _append_buckets(limits, cgrp.get('buckets'), 'antigravity_opus_5_5',
                        'opus 5.5 5h', 'opus 5.5 wk', 'opus 5.5 5h', 'opus 5.5 wk', 'claude', 'Antigravity')
    err = (' · '.join(errors) if errors and not limits else None)
    return limits, err


def codex_view(doc):
    """ChatGPT / Codex rate limits from https://chatgpt.com/backend-api/wham/usage."""
    rl = (doc or {}).get('rate_limit') if isinstance(doc, dict) else None
    if not isinstance(rl, dict):
        return []
    pw, sw = rl.get('primary_window'), rl.get('secondary_window')
    both = isinstance(pw, dict) and isinstance(sw, dict)
    out = []
    for key, w in (('primary', pw), ('secondary', sw)):
        if not isinstance(w, dict) or _num(w.get('used_percent')) is None:
            continue
        pct = max(0.0, min(100.0, _num(w['used_percent'])))
        secs = _num(w.get('limit_window_seconds')) or 0
        if secs and secs <= 5 * 3600:
            label, short = 'chatgpt 5h', '5h'
        elif secs and secs <= 7 * 86400:
            label, short = 'chatgpt week', 'week'
        elif both:
            label, short = 'chatgpt 30d', '30d'
        else:
            label, short = 'chatgpt', ('30d' if secs >= 20 * 86400 else 'limit')
        out.append({
            'kind': f'chatgpt_{key}',
            'label': label,
            'short_label': short,
            'group': 'ChatGPT',
            'pct': pct,
            'severity': 'critical' if pct >= 90 or rl.get('limit_reached') else 'warning' if pct >= 70 else 'normal',
            'resets_at': _ts(w.get('reset_at')),
        })
    return out


def view(doc, now, fetched_at=None):
    """The panel's data from an /api/oauth/usage document."""
    limits = []
    for lim in doc.get('limits') or []:
        if not isinstance(lim, dict) or _num(lim.get('percent')) is None:
            continue
        kind = lim.get('kind') or '?'
        lbl = LIMIT_LABELS.get(kind, kind.replace('_', ' '))
        limits.append({'kind': kind, 'label': lbl, 'short_label': lbl, 'group': 'Claude',
                       'pct': _num(lim['percent']), 'severity': lim.get('severity') or 'normal',
                       'resets_at': iso(lim.get('resets_at'))})
    if not limits:
        for key, label in WINDOWS:
            w = doc.get(key)
            if isinstance(w, dict) and _num(w.get('utilization')) is not None:
                limits.append({'kind': key, 'label': label, 'short_label': label, 'group': 'Claude',
                               'pct': _num(w['utilization']), 'severity': 'normal',
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
                 clock=time.time, proxy_host='', ssh_user='root', pool='auto', proxy_fetch=scan_antigravity_quota,
                 codex_credentials=''):
        self.path, self.fetch, self.clock = credentials, fetch, clock
        self.proxy_host_cfg, self.ssh_user, self.pool = proxy_host, ssh_user, pool
        self.proxy_fetch = proxy_fetch
        self.codex_path = codex_credentials
        self.creds = None
        self.codex_creds = None
        self.poller = Poller(self.fetch_usage, interval, background, clock)
        self.proxy_poller = Poller(self.fetch_proxy_usage, interval, background, clock) if proxy_host else None
        self.codex_poller = Poller(self.fetch_codex_usage, interval, background, clock) if codex_credentials else None

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

    def fetch_proxy_usage(self):
        host = resolve_proxy_host(self.proxy_host_cfg)
        if not host:
            return {'accounts': []}
        return self.proxy_fetch(host, ssh_user=self.ssh_user)

    def fetch_codex_usage(self):
        c = self.codex_creds
        if not c:
            raise OSError('no codex credentials')
        headers = {'Authorization': f'Bearer {c["token"]}', 'Content-Type': 'application/json',
                   'User-Agent': 'codex-tui/0.149.1 (Linux; x86_64)'}
        if c.get('account_id'):
            headers['ChatGPT-Account-Id'] = c['account_id']
        try:
            return self.fetch(CODEX_USAGE_URL, headers, timeout=10)
        except OSError as e:
            raise OSError(str(e).replace(c['token'], '…')) from None

    def sample(self):
        self.creds = read_credentials(self.path)
        self.codex_creds = read_codex_credentials(self.codex_path) if self.codex_path else None
        proxy_limits, proxy_err, proxy_at, fallback_claude = [], None, None, []
        if self.proxy_poller and resolve_proxy_host(self.proxy_host_cfg):
            pp = self.proxy_poller.poll()
            proxy_err = pp.error.split(': ', 1)[-1] if pp.error else None
            if pp.value:
                proxy_limits, acc_err = antigravity_view(pp.value, self.pool)
                fallback_claude = status_cache_limits(pp.value.get('claude_cache'))
                proxy_err = proxy_err or acc_err
                proxy_at = pp.at
        codex_limits, codex_err, codex_at = [], None, None
        if self.codex_poller and self.codex_creds:
            cp = self.codex_poller.poll()
            codex_err = cp.error.split(': ', 1)[-1] if cp.error else None
            if cp.value:
                codex_limits = codex_view(cp.value)
                codex_at = cp.at
        extra_limits = proxy_limits + codex_limits
        extra_at = proxy_at or codex_at
        if not self.creds:
            if fallback_claude or extra_limits:
                now = self.clock()
                return {'limits': fallback_claude + extra_limits, 'extra': None, 'breakdown': [],
                        'age': now - extra_at if extra_at else None, 'plan': None,
                        'error': ' · '.join(filter(None, ('no local credentials', proxy_err, codex_err)))}
            return {'error': 'no credentials'}
        p = self.poller.poll()
        err = p.error.split(': ', 1)[-1] if p.error else None  # Poller prefixes the exception type
        if err and not p.value and fallback_claude and err.startswith('HTTP 429'):
            err = None
        combined_err = ' · '.join(filter(None, (err, proxy_err, codex_err))) or None
        if not p.value:
            if fallback_claude or extra_limits:
                now = self.clock()
                return {'limits': fallback_claude + extra_limits, 'extra': None, 'breakdown': [],
                        'age': now - extra_at if extra_at else None, 'plan': self.creds['plan'],
                        'error': combined_err}
            return {'error': err or 'connecting…', 'plan': self.creds['plan']}
        out = view(p.value, self.clock(), p.at)
        out['limits'].extend(extra_limits)
        out['plan'] = self.creds['plan']
        out['error'] = combined_err  # a failed refresh keeps the last numbers, with the error underneath
        return out
