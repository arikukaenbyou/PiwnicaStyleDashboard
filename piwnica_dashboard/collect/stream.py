"""Is this PC streaming? An established TCP connection to an RTMP port (1935) means OBS is live;
`ss -ti` gives the bytes sent on it, hence the bitrate. Read in the background every 2 s."""
import subprocess
import time

from .poll import Poller

RTMP_PORTS = ('1935',)


def parse_ss(text):
    """`ss -tinH state established '( dport = :1935 )'` -> [(peer, bytes_sent)]."""
    out, peer = [], None
    for ln in (text or '').splitlines():
        if not ln.startswith((' ', '\t')):
            f = ln.split()
            peer = f[-1] if len(f) >= 4 else None
            if len(f) >= 5 and ':' in f[3] and ':' in f[4]:
                peer = f[4]
        elif peer:
            sent = next((int(x.split(':', 1)[1]) for x in ln.split() if x.startswith('bytes_sent:')), 0)
            out.append((peer, sent))
            peer = None
    return out


def default_ss():
    return subprocess.run(['ss', '-tinH', 'state', 'established', '( dport = :1935 )'],
                          capture_output=True, text=True, timeout=3).stdout


class StreamCollector:
    def __init__(self, ss=default_ss, background=True, clock=time.time):
        self.clock = clock
        self.poller = Poller(lambda: (self.clock(), parse_ss(ss())), 2, background, clock)
        self.prev = None
        self.since = None

    def sample(self):
        p = self.poller.poll()
        if not p.value:
            return {'live': False}
        at, conns = p.value
        if not conns:
            self.prev = self.since = None
            return {'live': False}
        peer, sent = max(conns, key=lambda c: c[1])
        rate = None
        if self.prev and at > self.prev[0] and sent >= self.prev[1]:
            rate = 8 * (sent - self.prev[1]) / (at - self.prev[0])
        if self.prev is None or at > self.prev[0]:
            self.prev = (at, sent)
        self.since = self.since or at
        return {'live': True, 'peer': peer.rsplit(':', 1)[0].strip('[]'), 'bps': rate, 'for': self.clock() - self.since}
