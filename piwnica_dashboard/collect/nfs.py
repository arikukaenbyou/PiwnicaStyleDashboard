"""NFS shares: every share from /etc/fstab (and anything else mounted as nfs), usage and traffic.

statvfs() on a `hard` NFS mount whose server is gone blocks until it comes back, so usage is read
in a background thread per share; a share whose read hangs is shown as not answering instead of
freezing the dashboard. Automount shares that are not mounted are left alone (statvfs would mount
them). Traffic comes from /proc/self/mountstats (bytes the server actually sent / received).
"""
import os

from .. import sysfs
from .poll import Poller

HUNG_AFTER = 8  # s without an answer from statvfs


def unescape(s):
    """mountinfo / fstab escape spaces and tabs as \\040 / \\011."""
    return s.replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\')


def fstab_shares(text):
    """[(source, target)] of nfs lines in fstab."""
    out = []
    for ln in (text or '').splitlines():
        f = ln.split()
        if len(f) >= 3 and not f[0].startswith('#') and f[2] in ('nfs', 'nfs4'):
            out.append((unescape(f[0]), unescape(f[1])))
    return out


def mounted_nfs(text):
    """{target: source} of mounted nfs file systems (mountinfo)."""
    out = {}
    for ln in (text or '').splitlines():
        pre, sep, post = ln.partition(' - ')
        if not sep:
            continue
        p, q = pre.split(), post.split()
        if len(p) > 4 and len(q) > 1 and q[0] in ('nfs', 'nfs4'):
            out[unescape(p[4])] = unescape(q[1])
    return out


def mount_bytes(text):
    """{target: (bytes read from the server, bytes written to it)} from mountstats."""
    out, cur = {}, None
    for ln in (text or '').splitlines():
        if ln.startswith('device '):
            f = ln.split()
            cur = unescape(f[4]) if len(f) > 7 and f[7] in ('nfs', 'nfs4') else None
        elif cur and ln.strip().startswith('bytes:'):
            v = ln.split()[1:]
            if len(v) >= 6:
                out[cur] = (int(v[4]), int(v[5]))
    return out


def default_statvfs(path):
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize


class NfsCollector:
    def __init__(self, statvfs=default_statvfs, background=True, clock=None):
        import time
        self.clock = clock or time.time
        self.statvfs, self.background = statvfs, background
        self.pollers = {}   # target -> Poller of its statvfs
        self.prev = {}      # target -> (time, read, written)

    def sample(self):
        now = self.clock()
        mounted = mounted_nfs(sysfs.read('/proc/self/mountinfo', ''))
        shares = dict((t, s) for s, t in fstab_shares(sysfs.read('/etc/fstab', '')))
        for t, s in mounted.items():
            shares.setdefault(t, s)
        traffic = mount_bytes(sysfs.read('/proc/self/mountstats', ''))
        out = []
        for target in sorted(shares):
            source = shares[target]
            d = {'name': os.path.basename(target.rstrip('/')) or target, 'target': target, 'source': source,
                 'server': source.split(':', 1)[0], 'mounted': target in mounted, 'rd': 0.0, 'wr': 0.0}
            if d['mounted']:
                p = self.pollers.get(target)
                if p is None:
                    p = self.pollers[target] = Poller(lambda t=target: self.statvfs(t), 10, self.background, self.clock)
                p.poll()
                if p.value:
                    d['total'], d['used'] = p.value
                    d['pct'] = 100 * d['used'] / d['total'] if d['total'] else 0.0
                d['hung'] = p.stuck(HUNG_AFTER)
                d['error'] = p.error
                if target in traffic:
                    rd, wr = traffic[target]
                    pv = self.prev.get(target)
                    if pv and now > pv[0]:
                        d['rd'] = max(0.0, (rd - pv[1]) / (now - pv[0]))
                        d['wr'] = max(0.0, (wr - pv[2]) / (now - pv[0]))
                    self.prev[target] = (now, rd, wr)
            out.append(d)
        return out
