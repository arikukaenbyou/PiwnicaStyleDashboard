"""Background polling for slow sources (network APIs, NFS): the GTK loop only ever reads the last result.

A Poller runs its function in a daemon thread at most every `interval` seconds and never starts a
second run while one is still busy -- a hung NFS server or a dead API host costs one stuck thread,
not a frozen desktop.
"""
import hashlib
import http.client
import json
import ssl
import threading
import time
import urllib.parse


class Poller:
    def __init__(self, fn, interval, background=True, clock=time.time):
        self.fn, self.interval, self.background, self.clock = fn, interval, background, clock
        self.value, self.error, self.at = None, None, None
        self.started = -1e18
        self.busy = False

    def poll(self):
        """Start a run when one is due; returns self (value / error / at / busy)."""
        now = self.clock()
        if not self.busy and now - self.started >= self.interval:
            self.started, self.busy = now, True
            if self.background:
                threading.Thread(target=self._run, daemon=True).start()
            else:
                self._run()
        return self

    def stuck(self, limit):
        """Busy for longer than `limit` seconds (e.g. a hung NFS mount)."""
        return self.busy and self.clock() - self.started > limit

    def _run(self):
        try:
            self.value, self.error = self.fn(), None
        except Exception as e:  # noqa: BLE001 -- shown in the panel, retried next interval
            self.error = f'{type(e).__name__}: {e}'[:120]
        finally:
            self.at, self.busy = self.clock(), False


def fingerprint(der):
    return hashlib.sha256(der).hexdigest()


def norm_fp(fp):
    return (fp or '').replace(':', '').strip().lower()


def get_json(url, headers=None, pin=None, timeout=5):
    """GET a JSON document. https with `pin` (sha256 of the server certificate, as openssl prints it):
    the self-signed certificate is accepted only when its fingerprint matches; without a pin the usual
    CA verification applies."""
    u = urllib.parse.urlsplit(url)
    path = (u.path or '/') + (f'?{u.query}' if u.query else '')
    if u.scheme == 'https':
        if pin:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
        conn.connect()
        if pin and fingerprint(conn.sock.getpeercert(True)) != norm_fp(pin):
            conn.close()
            raise ssl.SSLError(f'certificate of {u.hostname} does not match the pinned fingerprint')
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        conn.request('GET', path, headers=dict(headers or {}, Accept='application/json'))
        r = conn.getresponse()
        body = r.read()
        if r.status >= 400:
            try:
                msg = json.loads(body).get('message') or json.loads(body).get('error')
            except (ValueError, AttributeError):
                msg = None
            raise OSError(f'HTTP {r.status}' + (f' {msg}' if msg else ''))
        return json.loads(body)
    finally:
        conn.close()
