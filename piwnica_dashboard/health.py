"""Heartbeat for outside watchers (Luneta): a small JSON file, rewritten every few seconds from
the GTK main loop, in the user's runtime directory (tmpfs, never on disk).

    $XDG_RUNTIME_DIR/piwnica-dashboard/health.json

A file that stops changing while its pid is alive means a hung main loop; a missing file or a
dead pid means the dashboard is not running. Panel sources report their trouble in an `error`
field ("no token", a request error); "connecting…" is the normal first moments, not a problem.

Schema 1 (fields are only added, never renamed):
    {"schema": 1, "pid": 1234, "version": "0.1.0", "started": <unix s>, "at": <unix s>,
     "interval": 10, "paused": false, "mascot": "ready",
     "alerts": [{"source": "proxmox", "error": "no token"}]}
mascot: "off" (not configured / missing files), "loading", "ready", "hidden" (a game runs),
or "error: <reason>".
"""
import json
import os

SCHEMA = 1
INTERVAL = 10  # seconds between writes; a watcher treats ~4 missed writes as a hang
FILE = 'health.json'
TRANSIENT = ('connecting…',)


def default_path(env=os.environ):
    base = env.get('XDG_RUNTIME_DIR') or f'/tmp/piwnica-dashboard-{os.getuid()}'
    return os.path.join(base, 'piwnica-dashboard', FILE)


def alerts(data):
    """[{'source', 'error'}] of the panel sources that report trouble, sorted by name."""
    out = []
    for name, v in sorted((data or {}).items()):
        if isinstance(v, dict) and v.get('error') and v['error'] not in TRANSIENT:
            out.append({'source': name, 'error': str(v['error'])[:160]})
    return out


def mascot_state(m):
    """State of the Live2D window (mascot.Mascot or None) for the heartbeat."""
    if m is None:
        return 'off'
    if getattr(m, 'error', None):
        return f'error: {m.error}'[:160]
    if not m.ready:
        return 'loading'
    return 'hidden' if m.paused else 'ready'


def snapshot(now, started, version, paused, mascot, data, pid=None):
    return {'schema': SCHEMA, 'pid': pid or os.getpid(), 'version': version, 'started': round(started, 1),
            'at': round(now, 1), 'interval': INTERVAL, 'paused': bool(paused), 'mascot': mascot,
            'alerts': alerts(data)}


def write(path, doc):
    """Atomic replace (temp file + rename): a reader never sees half a file."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, path)


def remove(path):
    """On a clean exit: no file = not running (a watcher does not wait for it to go stale)."""
    try:
        os.unlink(path)
    except OSError:
        pass
