"""ariku.pl homelab inventory: what in the ecosystem (Proxmox host, LXC/VM, Docker containers, services,
router, PCs, IoT) needs an update or an intervention.

The server collects and judges everything (afterlife lib/inventoryRules.ts); the dashboard only shows
its verdict, the same list the companion app gets. GET /api/infra/inventory with the Luneta device token
(owner only). The list of versions is a vulnerability map: it stays in memory, never on disk.
"""
import os
import time

from .afterlife import iso, read_token
from .poll import Poller, get_json

# most urgent first, like the server's STATUS_ORDER
STATUS_ORDER = ('critical', 'attention', 'update', 'stale', 'unknown', 'ok')


# focus = "security": what an attacker could use stays on screen whatever its status -- the hypervisor,
# the router, network services and community-scripts apps (cs:*); for everything else (IoT firmware,
# game-server images, HA add-ons, plain package updates in guests) only critical / attention.
EXPOSED_KINDS = ('host', 'router', 'service')
ALWAYS = ('critical', 'attention')


def exposed(item):
    return item.get('kind') in EXPOSED_KINDS or str(item.get('id') or '').startswith('cs:')


def view(doc, now, show_ok=False, fetched_at=None, focus='security'):
    """The panel's data from an /api/infra/inventory document fetched at `fetched_at`."""
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    items = sorted((i for i in doc.get('items') or [] if isinstance(i, dict)),
                   key=lambda i: (rank.get(i.get('status'), len(STATUS_ORDER)), not exposed(i),
                                  (i.get('name') or '').lower()))
    counts = {s: 0 for s in STATUS_ORDER}
    for i in items:
        if i.get('status') in counts:
            counts[i['status']] += 1
    shown = [i for i in items if show_ok or i.get('status') != 'ok']
    routine = 0
    if focus == 'security':
        kept = [i for i in shown if i.get('status') in ALWAYS + ('ok',) or exposed(i)]  # ok is there only with show_ok
        routine, shown = len(shown) - len(kept), kept
    fetched_at = fetched_at or iso(doc.get('generatedAt'))
    return {
        'counts': counts, 'total': len(items), 'hidden_ok': 0 if show_ok else counts['ok'], 'show_ok': show_ok,
        'focus': focus, 'hidden_routine': routine,
        'items': [{k: i.get(k) for k in ('id', 'name', 'kind', 'status', 'reason', 'version', 'latestVersion',
                                         'pendingUpdates', 'os')} for i in shown],
        'age': now - fetched_at if fetched_at else None,
    }


class Setting:
    """[updates] <key> from config.toml, re-read when the file changes -- the switch works without a restart."""

    def __init__(self, path, key, default):
        self.path, self.key, self.value, self.mtime = path, key, default, None

    def get(self):
        from .. import config
        try:
            m = os.stat(self.path).st_mtime if self.path else None
        except OSError:
            m = None
        if m is not None and m != self.mtime:
            self.mtime = m
            self.value = config.read_value(self.path, 'updates', self.key, self.value)
        return self.value


class UpdatesCollector:
    def __init__(self, base_url='https://ariku.pl', token_file='~/.config/piwnica-dashboard/luneta.token',
                 show_ok=False, config_path=None, interval=300, background=True, fetch=get_json, clock=time.time,
                 focus='security'):
        self.base, self.fetch, self.clock = base_url.rstrip('/'), fetch, clock
        self.token_file = token_file
        self.token = read_token(token_file)
        self.show_ok = Setting(config_path, 'show_ok', show_ok)
        self.focus = Setting(config_path, 'focus', focus)
        self.poller = Poller(self.fetch_inventory, interval, background, clock)

    def fetch_inventory(self):
        return self.fetch(f'{self.base}/api/infra/inventory', {'Authorization': f'Bearer {self.token}'}, timeout=10)

    def sample(self):
        if self.token is None:
            self.token = read_token(self.token_file)  # created later: picked up without a restart
        if not self.token:
            return {'error': 'no token'}
        p = self.poller.poll()
        if not p.value:
            return {'error': p.error or 'connecting…'}
        out = view(p.value, self.clock(), bool(self.show_ok.get()), p.at, self.focus.get())
        out['error'] = p.error  # a failed refresh keeps the last list, with the error underneath
        return out
