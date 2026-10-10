"""One sample per second of everything the dashboard shows, plus 60 s of history."""
import math
import time
from collections import deque

from .collect import gpu as gpu_mod
from .collect.infra import InfraPoller
from .collect.pacman import PacmanCollector
from .collect.afterlife import AfterlifeCollector
from .collect.forge import ForgeCollector
from .collect.nfs import NfsCollector
from .collect.proxmox import ProxmoxCollector
from .collect.stream import StreamCollector
from .collect.system import SystemCollector
from .collect.temps import DEFAULT_LIMIT, VirtualSensor, find_sensors
from .collect.updates import UpdatesCollector
from .collect.builds import BuildsCollector
from .collect.claude import ClaudeCollector

HIST = 60
HIST_KEYS = ('cpu', 'gpu', 'mem', 'iowait', 'rx', 'tx', 'dl')


class Collector:
    def __init__(self, cfg=None, system=None, gpu='auto', pacman='auto', clock=time.monotonic, infra='auto'):
        cfg = cfg or {}
        if infra == 'auto':
            infra = InfraPoller.from_config(cfg)
            infra = infra.start() if infra else None
        self.infra = infra
        self.clock = clock
        self.system = system or SystemCollector(iface=cfg.get('network', {}).get('interface', 'auto'))
        self.gpu = gpu_mod.detect(cfg.get('gpu', {}).get('backend', 'auto')) if gpu == 'auto' else gpu
        self.temp_overrides = cfg.get('temps', {})
        pc = cfg.get('pacman', {})
        self.pacman = pacman if pacman != 'auto' else (
            PacmanCollector(iface=self.system.iface, pending_interval=int(pc.get('pending_interval', 1800)),
                            aur=bool(pc.get('aur', True))) if pc.get('enabled', True) else None)
        self.sensors = None
        self.temps, self.temps_at = [], -1e9
        self.hist = {k: deque(maxlen=HIST) for k in HIST_KEYS}
        self.data = {}
        self.extra = {}  # panel name -> collector with .sample()
        if cfg.get('nfs', {}).get('enabled', True):
            self.extra['nfs'] = NfsCollector()
        px = cfg.get('proxmox', {})
        if px.get('enabled', True) and px.get('host'):
            self.extra['proxmox'] = ProxmoxCollector(px['host'], px.get('token_file', ''), px.get('fingerprint', ''),
                                                     px.get('node', 'auto'), ssh_user=px.get('ssh_user', 'root'))
            self.extra['stream'] = StreamCollector()
        al = cfg.get('afterlife', {})
        if al.get('enabled', True):
            self.extra['afterlife'] = AfterlifeCollector(al.get('base_url', 'https://ariku.pl'),
                                                         al.get('token_file', '~/.config/piwnica-dashboard/luneta.token'))
        up = cfg.get('updates', {})
        if up.get('enabled', True):  # the ariku.pl inventory, with the same device token as Luneta
            self.extra['updates'] = UpdatesCollector(al.get('base_url', 'https://ariku.pl'),
                                                     al.get('token_file', '~/.config/piwnica-dashboard/luneta.token'),
                                                     bool(up.get('show_ok', False)), cfg.get('_path'),
                                                     int(up.get('interval', 300)), focus=up.get('focus', 'security'))
        cl = cfg.get('claude', {})
        if cl.get('enabled', True):
            self.extra['claude'] = ClaudeCollector(cl.get('credentials', '~/.claude/.credentials.json'),
                                                   int(cl.get('interval', 60)))
        fg = cfg.get('forge', {})
        if fg.get('enabled', True):
            self.extra['forge'] = ForgeCollector(fg.get('comfyui', 'http://127.0.0.1:8188'))
        bu = cfg.get('builds', {})
        if bu.get('enabled', True):
            self.extra['builds'] = BuildsCollector(bu.get('forgejo_url', 'https://git.ariku.pl'),
                                                   bu.get('token_file', '~/.config/piwnica-dashboard/forgejo.token'),
                                                   bu.get('repos', ['afterlife', 'luneta', 'companion_app']),
                                                   int(bu.get('interval', 30)))
        names = {'afterlife': ('luneta', 'now'), 'stream': ()}
        self.panels = (['pacman'] if self.pacman else []) + [n for k in self.extra for n in names.get(k, (k,))]

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
        d['pacman'] = self.pacman.sample() if self.pacman else None
        for name, col in self.extra.items():
            try:
                if name == 'afterlife':
                    d['luneta'], d['now'] = col.sample()
                else:
                    d[name] = col.sample()
            except Exception as e:  # noqa: BLE001 -- one broken source must not take the dashboard down
                d[name] = {'error': f'{type(e).__name__}: {e}'[:120]}
        d['dl'] = (d['pacman'] or {}).get('speed', 0.0)
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
             'temps': self._sensors, 'pacman': self.demo_pacman(t), **self.demo_homelab(t),
             'infra': {'counts': {'critical': 1, 'warning': 2, 'info': 7}, 'error': None,
                       'top': [{'level': 'critical', 'title': 'backup-nas', 'reason': 'not responding for 20 min'},
                               {'level': 'warning', 'title': 'media-server', 'reason': '12 updates waiting for 34 days'}]}}
        d['dl'] = d['pacman']['speed']
        for k in HIST_KEYS:
            self.hist[k].append(d.get(k) or 0.0)
        self.data = d
        return d

    @staticmethod
    def demo_pacman(t):
        """A -Syu halfway through its downloads."""
        got = min(3.4e9, 1.2e9 + t * 2.4e7)
        return {'state': 'download', 'tool': 'pacman -Syu', 'elapsed': 72.0 + t, 'total': 112, 'done': 0,
                'pkg': 'linux-firmware-nvidia', 'pkg_pct': 62.0, 'pkg_eta': 14.0, 'eta': 220.0, 'pct': 31.0,
                'dl_bytes': got, 'dl_total': 3.4e9, 'dl_files': (37, 104), 'speed': 2.4e7 + 4e6 * math.sin(t / 3),
                'size_delta': 312 * 2 ** 20, 'warnings': 0, 'errors': 0,
                'last': {'ts': time.time() - 3 * 86400 - 3600, 'pkgs': 87, 'dur': 250.0}, 'pending': (112, 2),
                'reboot': ['kernel'], 'pacnew': ['/etc/pacman.conf.pacnew', '/etc/locale.gen.pacnew'],
                'daily': [0, 0, 41, 0, 0, 0, 3, 0, 0, 0, 0, 87, 0, 0, 2, 0, 0, 0, 0, 56, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]}

    @staticmethod
    def demo_homelab(t):
        """NFS shares, a Proxmox host, Luneta, the companion and Kuźnia -- invented, plausible values."""
        T = 2 ** 40
        share = lambda name, used, total, rd=0.0, wr=0.0: {  # noqa: E731
            'name': name, 'target': f'/mnt/{name}', 'source': f'10.0.0.5:/srv/{name}', 'server': '10.0.0.5',
            'mounted': True, 'total': total, 'used': used, 'pct': 100 * used / total, 'rd': rd, 'wr': wr, 'hung': False}
        now = time.time()
        return {
            'nfs': [share('media', 3.6 * T, 3.9 * T, 4.2e6), share('backup', 2.1 * T, 3.9 * T, 0, 1.1e6),
                    share('download', 0.4 * T, 0.9 * T), share('projects', 61 * 2 ** 30, 400 * 2 ** 30),
                    {'name': 'archive', 'target': '/mnt/archive', 'source': '10.0.0.5:/srv/archive', 'server': '10.0.0.5',
                     'mounted': False, 'rd': 0.0, 'wr': 0.0}],
            'proxmox': {'node': 'pve', 'cpu': 9 + 6 * math.sin(t / 5), 'cores': 4, 'load': [0.6, 0.6, 0.6],
                        'mem_used': 22.6 * 2 ** 30, 'mem_total': 39 * 2 ** 30, 'uptime': 703_000, 'guests': 26, 'running': 23,
                        'down': ['bazarr', 'lidarr', 'readarr'], 'error': None, 'age': 2.0,
                        'top': [{'name': 'portfolio', 'cpu': 4.0, 'mem': 6 * 2 ** 30}],
                        'storages': [{'name': n, 'pct': p, 'used': 0, 'total': 0, 'active': True}
                                     for n, p in (('nas1', 99.8), ('nas2', 93.4), ('local-lvm', 88.2), ('ssd', 68.2))],
                        'nfs': [{'name': 'nas1', 'source': '192.168.1.100:/tank/media', 'used': 3.6 * T,
                                 'total': 3.9 * T, 'pct': 92.3, 'active': True},
                                {'name': 'nas2', 'source': '192.168.1.100:/tank/backup', 'used': 2.1 * T,
                                 'total': 3.9 * T, 'pct': 53.8, 'active': True}],
                        'disks': [{'name': '/dev/sda', 'model': 'Seagate IronWolf', 'type': 'hdd', 'size': 4 * T,
                                   'used': 'LVM', 'health': 'OK'},
                                  {'name': '/dev/nvme0n1', 'model': 'Samsung SSD', 'type': 'ssd', 'size': T,
                                   'used': 'GPT', 'health': 'OK'}],
                        'backup': {'last_end': now - 8 * 86400, 'last_status': 'job errors', 'next_run': now + 23 * 86400,
                                   'schedule': 'monthly'},
                        'cpu_hist': [9 + 6 * math.sin(i / 5) for i in range(60)]},
            'stream': {'live': True, 'peer': '10.0.0.23', 'bps': 6.1e6, 'for': 4980.0},
            'luneta': {'games': [
                {'key': 'blue_archive', 'name': 'Blue Archive', 'daily_in': 3 * 3600 + 720, 'weekly_in': 3 * 86400, 'done': 3,
                 'tasks': 5, 'critical_left': 1, 'stale': False, 'premium': {'label': 'Pyroxene', 'amount': 6417, 'pulls': 53},
                 'event': {'name': 'Final Restriction Release', 'ends_in': 41 * 3600}, 'advice': None, 'age': 600},
                {'key': 'nte', 'name': 'NTE', 'daily_in': 9 * 3600, 'weekly_in': 4 * 86400, 'done': 6, 'tasks': 8,
                 'critical_left': 0, 'stale': False, 'premium': {'label': 'Annulith', 'amount': 210, 'pulls': 1},
                 'event': {'name': 'Stamina Recharge', 'ends_in': 10 * 86400}, 'advice': None, 'age': 600}],
                'analysis': {'running': True, 'index': 10, 'of': 102, 'done': 9, 'errors': 0, 'stage': 'audio 95%',
                             'etaS': 34306, 'mediaDoneS': 88468, 'mediaTotalS': 359109},
                'agent_running': True, 'heartbeat': now - 20, 'agent_quit': False, 'remote': True, 'remote_error': None},
            'now': {'error': None, 'now': [
                {'quest': {'title': 'Odpisać na maile od drukarni', 'type': 'DEADLINE'}, 'estimateMin': 25,
                 'reasons': ['termin za 3 h']},
                {'quest': {'title': 'Przejrzeć nagranie z wczoraj'}, 'estimateMin': 40, 'reasons': []}],
                'fixedSoon': [{'title': 'Stream', 'startAt': None, 'type': 'FIXED'}],
                'chaos': {'score': 32, 'level': 'busy', 'levelLabel': 'pod kontrolą', 'yesterday': 28,
                          'biggest': {'label': 'zaległe', 'count': 4}},
                'majster': {'totalXp': 120, 'level': 4, 'percent': 37}},
            'updates': {'error': None, 'total': 31, 'hidden_ok': 23, 'show_ok': False, 'age': 140.0,
                        'counts': {'critical': 1, 'attention': 2, 'update': 4, 'stale': 1, 'unknown': 0, 'ok': 23},
                        'items': [
                            {'name': 'MikroTik', 'kind': 'router', 'status': 'critical', 'version': '7.19.2',
                             'reason': 'wersja 7.19.2 < 7.23.4 (aktywnie wykorzystywane CVE w RouterOS)'},
                            {'name': 'jellyfin', 'kind': 'container', 'status': 'attention',
                             'reason': 'kontener w stanie restarting'},
                            {'name': 'portfolio', 'kind': 'lxc', 'status': 'attention',
                             'reason': '12 aktualizacji czeka od 34 dni'},
                            {'name': 'Immich', 'kind': 'service', 'status': 'update', 'version': '3.0.3',
                             'latestVersion': 'v3.3.1', 'reason': 'dostępna v3.3.1'},
                            {'name': 'Home Assistant', 'kind': 'addon', 'status': 'update', 'version': '2026.9.3',
                             'latestVersion': '2026.10.0', 'reason': 'dostępna 2026.10.0'},
                            {'name': 'traefik', 'kind': 'container', 'status': 'update',
                             'reason': 'nowszy obraz w rejestrze'},
                            {'name': 'n8n', 'kind': 'lxc', 'status': 'update', 'reason': '3 aktualizacji do zainstalowania'},
                            {'name': 'tasmota-garaż', 'kind': 'device', 'status': 'stale', 'reason': None}]},
            'forge': {'comfy': {'running': 1, 'pending': 2, 'vram_used': 9.1 * 2 ** 30, 'vram_total': 16 * 2 ** 30},
                      'comfy_up': True, 'softstart': {'active': 'active', 'max_mhz': 2000, 'watts': 150}},
            'claude': {'error': None, 'plan': 'max', 'age': 12.0,
                       'limits': [{'kind': 'session', 'label': '5h session', 'pct': 51.0, 'severity': 'normal',
                                   'resets_at': time.time() + 2 * 3600 + 14 * 60},
                                  {'kind': 'weekly_all', 'label': 'week', 'pct': 52.0, 'severity': 'normal',
                                   'resets_at': time.time() + 5 * 86400 + 18 * 3600}],
                       'extra': None, 'breakdown': [{'name': 'Claude Code', 'pct': 99.0}, {'name': 'Cowork', 'pct': 1.0}]},
            'builds': {
                'error': None, 'total': 3, 'active': 2, 'age': 4.0,
                'items': [
                    {'repo': 'luneta', 'full_name': 'ariku/luneta', 'name': 'Luneta',
                     'version': 'v0.13.0', 'ref': 'v0.13.0', 'status': 'running',
                     'workflow': 'ci.yml', 'job': 'test', 'jobs_total': 1, 'jobs_done': 0,
                     'pct': None, 'elapsed': 580.0 + t, 'duration': None, 'queued_time': None,
                     'queued_runs': 1, 'age': None, 'commit_title': 'v0.13.0: aktualizacje z Forgejo z fallbackiem na ariku.pl',
                     'commit_sha': '3a69884b'},
                    {'repo': 'afterlife', 'full_name': 'ariku/afterlife', 'name': 'Afterlife',
                     'version': 'v1.0.0', 'ref': 'main', 'status': 'waiting',
                     'workflow': 'ci.yml', 'job': 'build', 'jobs_total': 1, 'jobs_done': 0,
                     'pct': 0, 'elapsed': None, 'duration': None, 'queued_time': 2760.0,
                     'queued_runs': 4, 'age': None, 'commit_title': "Merge pull request 'radar: na żywo + anty-spam' (#68)",
                     'commit_sha': '7e2c910a'},
                    {'repo': 'companion_app', 'full_name': 'ariku/companion_app', 'name': 'Companion (Android)',
                     'version': 'v0.5.0', 'ref': 'ci/forgejo-android', 'status': 'failure',
                     'workflow': 'android.yml', 'job': 'build', 'jobs_total': 1, 'jobs_done': 1,
                     'pct': 100, 'elapsed': None, 'duration': 4768.0, 'queued_time': None,
                     'queued_runs': 0, 'age': 3600.0, 'commit_title': 'ci: setup-android tylko platform-tools',
                     'commit_sha': '41f82b09'},
                ],
            },
        }
