"""Optional infra alerts: polls a JSON endpoint that lists what needs attention in a homelab.

Off unless ``[infra] url`` and ``token`` are set in the config. Polling runs in a daemon
thread, so a slow or dead server never stalls the 1 s sampling or the drawing. Expected
shape (any server can provide it)::

    {"counts": {"critical": 0, "warning": 2, "info": 5},
     "alerts": [{"level": "critical|warning|info", "title": "...", "reason": "..."}]}
"""
import json
import threading
import time
import urllib.request

LEVELS = ('critical', 'warning', 'info')


def summarize(payload, top=3):
    """Counts plus the `top` most urgent alerts (critical first), from a parsed response."""
    counts = payload.get('counts') or {}
    alerts = [a for a in payload.get('alerts') or [] if isinstance(a, dict) and a.get('level') in LEVELS]
    alerts.sort(key=lambda a: LEVELS.index(a['level']))
    return {
        'counts': {lvl: int(counts.get(lvl) or 0) for lvl in LEVELS},
        'top': [{'level': a['level'], 'title': str(a.get('title') or '')[:40], 'reason': str(a.get('reason') or '')[:60]}
                for a in alerts if a['level'] != 'info'][:top],
        'error': None,
    }


def fetch(url, token, timeout=10):
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 -- URL comes from the user's own config
        return json.load(res)


class InfraPoller:
    def __init__(self, url, token, interval=300, fetcher=fetch, clock=time.monotonic):
        self.url, self.token = url, token
        self.interval = max(60, int(interval or 300))
        self.fetcher, self.clock = fetcher, clock
        self.latest = None
        self._stop = threading.Event()

    @classmethod
    def from_config(cls, cfg):
        infra = (cfg or {}).get('infra', {})
        if not infra.get('url') or not infra.get('token'):
            return None
        return cls(infra['url'], infra['token'], infra.get('interval', 300))

    def poll_once(self):
        try:
            self.latest = summarize(self.fetcher(self.url, self.token))
        except Exception as e:  # network, HTTP, JSON -- keep the last good counts, flag the error
            prev = self.latest or {'counts': {lvl: 0 for lvl in LEVELS}, 'top': []}
            self.latest = {**prev, 'error': type(e).__name__}
        return self.latest

    def start(self):
        def run():
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(self.interval)
        threading.Thread(target=run, name='infra-poller', daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
