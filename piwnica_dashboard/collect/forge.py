"""Kuźnia on this PC: is ComfyUI up and busy, and is the A770's soft-start clock cap in place.

The server's Kuźnia switch (open / cooling / closed) needs a Discord session, so the panel shows
what the GPU itself is doing: ComfyUI's queue (127.0.0.1:8188) and the gpu-softstart service, which
keeps the card's clock and power capped (hard resets of this machine came from uncapped load spikes).
"""
import subprocess
import time

from .poll import Poller, get_json

CAP_CONF = '/etc/systemd/system/gpu-softstart.service.d/cap.conf'


def parse_cap(text):
    """SOFTSTART_MAX_CAP / SOFTSTART_POWER_W from the drop-in."""
    out = {}
    for ln in (text or '').splitlines():
        for key, name in (('SOFTSTART_MAX_CAP=', 'max_mhz'), ('SOFTSTART_POWER_W=', 'watts')):
            if key in ln:
                try:
                    out[name] = int(ln.split(key, 1)[1].strip().strip('"'))
                except ValueError:
                    pass
    return out


def default_softstart():
    active = subprocess.run(['systemctl', 'is-active', 'gpu-softstart'], capture_output=True, text=True, timeout=3).stdout.strip()
    try:
        with open(CAP_CONF) as f:
            cap = parse_cap(f.read())
    except OSError:
        cap = {}
    return dict(cap, active=active)


class ForgeCollector:
    def __init__(self, comfyui='http://127.0.0.1:8188', softstart=default_softstart, background=True,
                 fetch=get_json, clock=time.time):
        self.url, self.fetch = comfyui.rstrip('/'), fetch
        self.comfy = Poller(self.fetch_comfy, 3, background, clock)
        self.soft = Poller(softstart, 30, background, clock)

    def fetch_comfy(self):
        q = self.fetch(f'{self.url}/queue', timeout=2)
        try:
            dev = (self.fetch(f'{self.url}/system_stats', timeout=2).get('devices') or [{}])[0]
        except OSError:
            dev = {}
        return {'running': len(q.get('queue_running') or []), 'pending': len(q.get('queue_pending') or []),
                'vram_used': (dev.get('vram_total') or 0) - (dev.get('vram_free') or 0), 'vram_total': dev.get('vram_total')}

    def sample(self):
        c, s = self.comfy.poll(), self.soft.poll()
        return {'comfy': c.value if not c.error else None, 'comfy_up': bool(c.value) and not c.error,
                'softstart': s.value}
