"""One sample per second of everything the dashboard shows, plus 60 s of history."""
import math
import time
from collections import deque

from .collect import gpu as gpu_mod
from .collect.infra import InfraPoller
from .collect.system import SystemCollector
from .collect.temps import DEFAULT_LIMIT, VirtualSensor, find_sensors

HIST = 60
HIST_KEYS = ('cpu', 'gpu', 'mem', 'iowait', 'rx', 'tx')


class Collector:
    def __init__(self, cfg=None, system=None, gpu='auto', clock=time.monotonic, infra='auto'):
        cfg = cfg or {}
        if infra == 'auto':
            infra = InfraPoller.from_config(cfg)
            infra = infra.start() if infra else None
        self.infra = infra
        self.clock = clock
        self.system = system or SystemCollector(iface=cfg.get('network', {}).get('interface', 'auto'))
        self.gpu = gpu_mod.detect(cfg.get('gpu', {}).get('backend', 'auto')) if gpu == 'auto' else gpu
        self.temp_overrides = cfg.get('temps', {})
        self.sensors = None
        self.temps, self.temps_at = [], -1e9
        self.hist = {k: deque(maxlen=HIST) for k in HIST_KEYS}
        self.data = {}

    def _sensors(self, disks):
        if self.sensors is None:
            self.sensors = find_sensors(self.temp_overrides, {d['name']: d['model'] for d in disks})
            if self.gpu is not None and self.gpu.vendor == 'nvidia' and not any(s.kind == 'gpu' for s in self.sensors):
                limit = float(self.temp_overrides.get('gpu_limit') or self.gpu.slowdown or DEFAULT_LIMIT['gpu'])
                self.sensors.insert(1, VirtualSensor('gpu', 'GPU', 'gpu', limit, lambda: self.gpu.temp))
        return self.sensors

    def sample(self):
        now = self.clock()
        d = self.system.sample(now)
        g = self.gpu.sample(now) if self.gpu else None
        d['gpu_info'] = g
        d['gpu'] = g['busy'] if g else 0.0
        # temperatures every 5 s: reading an NVMe temperature is a command to the drive and
        # Super I/O chips are slow -- ~10 ms per round, while temperatures change slowly
        if now - self.temps_at >= 5:
            self.temps_at = now
            self.temps = [(s, s.read()) for s in self._sensors(d['disks'])]
        d['temps'] = [(s, v) for s, v in self.temps if v is not None]
        d['infra'] = self.infra.latest if self.infra else None
        for k in HIST_KEYS:
            self.hist[k].append(d.get(k) or 0.0)
        self.data = d
        return d


class DemoCollector:
    """Plausible fake data for screenshots and tests -- nothing from the real machine."""

    def __init__(self):
        from .collect.temps import Sensor
        self.hist = {k: deque(maxlen=HIST) for k in HIST_KEYS}
        mk = lambda key, label, kind, limit: Sensor(key, label, kind, '', limit)  # noqa: E731
        self._sensors = [(mk('cpu', 'CPU Tctl', 'cpu', 90), 58.0), (mk('gpu', 'GPU Arc', 'gpu', 90), 71.0),
                         (mk('nvme0', 'nvme0 Samsung', 'nvme', 70), 41.0), (mk('nvme1', 'nvme1 Lexar', 'nvme', 70), 57.0),
                         (mk('board', 'Board', 'board', 65), 36.0)]
        self.t = 0
        for _ in range(HIST):
            self.sample()

    def sample(self):
        self.t += 1
        t = self.t
        cpu = 22 + 14 * math.sin(t / 7) + 6 * math.sin(t / 2.3)
        gpu = max(0.0, 48 + 35 * math.sin(t / 9 + 1))
        d = {'cpu': cpu, 'iowait': 0.4 + 0.3 * abs(math.sin(t / 5)), 'threads': 12, 'cpu_ghz': 4.1,
             'mem_total': 32 * 2 ** 30, 'mem_used': 11.4 * 2 ** 30, 'mem': 35.6, 'swap_used': 0,
             'iface': 'eth0', 'rx': 2.1e6 + 1.5e6 * math.sin(t / 4), 'tx': 3.1e5, 'uptime': 187_000, 'load': [1.24, 1.02, 0.88],
             'procs': 412, 'kernel': '6.18.0-arch1-1',
             'gpu': gpu, 'gpu_info': {'busy': gpu, 'mhz': 2200, 'max_mhz': 2400, 'cap_mhz': 2400, 'watts': 142.0, 'name': 'Intel Arc'},
             'disks': [
                 {'name': 'nvme0n1', 'model': 'Samsung SSD 980 500GB', 'mounts': ['/boot', '/'], 'total': 457 * 2 ** 30,
                  'used': 259 * 2 ** 30, 'pct': 56.7, 'rd': 1.2e6, 'wr': 4.5e5},
                 {'name': 'nvme1n1', 'model': 'Lexar SSD NM710 1TB', 'mounts': ['/data'], 'total': 932 * 2 ** 30,
                  'used': 800 * 2 ** 30, 'pct': 85.8, 'rd': 0.0, 'wr': 0.0},
                 {'name': 'sda', 'model': 'WDC WD10EARX', 'mounts': ['/mnt/hdd'], 'total': 932 * 2 ** 30,
                  'used': 885 * 2 ** 30, 'pct': 95.0, 'rd': 0.0, 'wr': 1.1e5}],
             'temps': self._sensors,
             'infra': {'counts': {'critical': 1, 'warning': 2, 'info': 7}, 'error': None,
                       'top': [{'level': 'critical', 'title': 'backup-nas', 'reason': 'not responding for 20 min'},
                               {'level': 'warning', 'title': 'media-server', 'reason': '12 updates waiting for 34 days'}]}}
        for k in HIST_KEYS:
            self.hist[k].append(d.get(k) or 0.0)
        self.data = d
        return d
