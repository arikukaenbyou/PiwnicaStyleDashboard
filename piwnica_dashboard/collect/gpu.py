"""GPU load/clock/power for Intel (i915, xe), AMD (amdgpu) and NVIDIA (nvidia-smi).

``detect()`` picks the card that drives a connected display (else the first one) and
returns a backend with ``sample(now) -> dict``:
    busy (%), mhz, max_mhz, cap_mhz (current clock limit, e.g. a soft-start cap), watts, name
Missing values are None; the dashboard simply leaves them out.
"""
import os
import subprocess

from .. import sysfs

VENDORS = {'0x8086': 'intel', '0x1002': 'amd', '0x10de': 'nvidia'}


def _rate(prev, key, value, now):
    pv = prev.get(key)
    prev[key] = (value, now)
    if pv is None or pv[0] is None or value is None or now <= pv[1]:
        return None
    return max(0.0, (value - pv[0]) / (now - pv[1]))


def list_cards():
    """[(card_path, vendor, has_connected_display)] for real DRM cards."""
    cards = []
    for dev in sysfs.glob('/sys/class/drm/card[0-9]*/device'):
        card = os.path.dirname(dev)
        vendor = VENDORS.get(sysfs.read(dev + '/vendor', ''))
        if not vendor:
            continue
        name = os.path.basename(card)
        connected = any(sysfs.read(c + '/status') == 'connected' for c in sysfs.glob(f'/sys/class/drm/{name}-*'))
        cards.append((card, vendor, connected))
    return cards


def detect(backend='auto', runner=subprocess.run):
    cards = list_cards()
    if backend not in ('auto', '', None):
        cards = [c for c in cards if c[1] == backend] or cards
    cards.sort(key=lambda c: not c[2])  # cards driving a display first
    for card, vendor, _ in cards:
        if vendor == 'intel':
            return IntelGPU(card)
        if vendor == 'amd':
            return AmdGPU(card)
        if vendor == 'nvidia':
            gpu = NvidiaGPU(runner)
            if gpu.available():
                return gpu
    return None


def _hwmon(card):
    hw = sysfs.glob(card + '/device/hwmon/hwmon*')
    return hw[0] if hw else None


class IntelGPU:
    """Busy % the way intel_gpu_top computes it: engine time from DRM fdinfo
    (/proc/<pid>/fdinfo, drm-engine-* for i915, drm-cycles-* for xe), summed over clients
    (deduplicated by drm-client-id), reported for the busiest engine class.
    Only processes of the current user are readable -- games, browsers, compositors;
    root-owned Xorg is not, which is negligible. RC6 residency is only a fallback: with
    several monitors the GPU rarely sleeps although it is idle, so it over-reports."""

    vendor = 'intel'

    def __init__(self, card):
        self.card = card
        self.hwmon = _hwmon(card)
        self.driver = os.path.basename(sysfs.realpath(card + '/device/driver')) or 'i915'
        self.name = 'Intel Arc' if self.hwmon else 'Intel'  # discrete cards expose hwmon, iGPUs do not
        self.prev = {}
        self.fds, self.fds_at = [], -1e9

    def _freq(self, name):
        # i915: card/gt_act_freq_mhz ; xe: card/device/tile0/gt0/freq0/act_freq
        v = sysfs.read_int(f'{self.card}/gt_{name}_freq_mhz')
        if v is None:
            xe = {'act': 'act_freq', 'max': 'max_freq', 'RP0': 'rp0_freq'}.get(name)
            v = sysfs.read_int(f'{self.card}/device/tile0/gt0/freq0/{xe}') if xe else None
        return v

    def _engine_busy(self, now):
        if now - self.fds_at >= 5:  # rescan who holds the GPU every 5 s, read counters every sample
            self.fds_at = now
            fds = []
            for fd in sysfs.glob('/proc/[0-9]*/fd/*'):
                try:
                    if sysfs.readlink(fd).startswith('/dev/dri/'):
                        fds.append(fd.replace('/fd/', '/fdinfo/'))
                except OSError:
                    continue
            self.fds = fds
        busy, total, cap, seen = {}, {}, {}, set()
        for fi in self.fds:
            txt = sysfs.read(fi)
            if not txt or 'drm-client-id' not in txt:
                continue
            kv = {k.strip(): v.strip() for k, _, v in (ln.partition(':') for ln in txt.splitlines())}
            cid = kv.get('drm-client-id')
            if cid in seen:
                continue
            seen.add(cid)
            for k, v in kv.items():
                if k.startswith('drm-engine-capacity-'):
                    cap[k[20:]] = int(v)
                elif k.startswith('drm-engine-'):          # i915: nanoseconds busy
                    busy[k[11:]] = busy.get(k[11:], 0) + int(v.split()[0])
                elif k.startswith('drm-total-cycles-'):    # xe: busy cycles / total cycles
                    total[k[17:]] = max(total.get(k[17:], 0), int(v.split()[0]))
                elif k.startswith('drm-cycles-'):
                    busy[k[11:]] = busy.get(k[11:], 0) + int(v.split()[0])
        if not seen:
            return None
        pb = self.prev.get('engines')
        self.prev['engines'] = (busy, total, now)
        if not pb or now <= pb[2]:
            return 0.0
        util = []
        for e, b in busy.items():
            db = max(0, b - pb[0].get(e, b))
            if e in total:  # xe
                dt = total[e] - pb[1].get(e, total[e])
                util.append(db / dt if dt > 0 else 0.0)
            else:
                util.append(db / ((now - pb[2]) * 1e9 * cap.get(e, 1)))
        return max(0.0, min(100.0, 100 * max(util, default=0.0)))

    def sample(self, now):
        busy = self._engine_busy(now)
        if busy is None:
            rc6 = sysfs.read_int(self.card + '/gt/gt0/rc6_residency_ms')
            r = _rate(self.prev, 'rc6', rc6, now)
            busy = max(0.0, min(100.0, 100 * (1 - r / 1000))) if r is not None else 0.0
        e = sysfs.read_int(self.hwmon + '/energy1_input') if self.hwmon else None
        w = _rate(self.prev, 'energy', e, now)
        return {'busy': busy, 'mhz': self._freq('act'), 'max_mhz': self._freq('RP0'), 'cap_mhz': self._freq('max'),
                'watts': w / 1e6 if w is not None else None, 'name': self.name}


class AmdGPU:
    vendor = 'amd'

    def __init__(self, card):
        self.card = card
        self.hwmon = _hwmon(card)
        self.name = 'AMD Radeon'

    def _sclk(self):
        """(current, max) MHz from pp_dpm_sclk ('1: 2500Mhz *')."""
        cur = mx = None
        for ln in (sysfs.read(self.card + '/device/pp_dpm_sclk', '') or '').splitlines():
            try:
                mhz = int(ln.split(':')[1].split('Mhz')[0].split('MHz')[0])
            except (IndexError, ValueError):
                continue
            mx = max(mx or 0, mhz)
            if ln.rstrip().endswith('*'):
                cur = mhz
        if cur is None and self.hwmon:
            hz = sysfs.read_int(self.hwmon + '/freq1_input')
            cur = hz // 1_000_000 if hz else None
        return cur, mx

    def sample(self, now):
        cur, mx = self._sclk()
        uw = None
        if self.hwmon:
            uw = sysfs.read_int(self.hwmon + '/power1_average') or sysfs.read_int(self.hwmon + '/power1_input')
        return {'busy': float(sysfs.read_int(self.card + '/device/gpu_busy_percent', 0)), 'mhz': cur, 'max_mhz': mx,
                'cap_mhz': None, 'watts': uw / 1e6 if uw else None, 'name': self.name}


class NvidiaGPU:
    """nvidia-smi is slow-ish (~30 ms), so it is queried every 2 s and cached."""
    vendor = 'nvidia'
    QUERY = 'utilization.gpu,clocks.gr,clocks.max.gr,power.draw,temperature.gpu,name'

    def __init__(self, runner=subprocess.run):
        self.run = runner
        self.cache, self.at = None, -1e9
        self.name = 'NVIDIA'
        self.temp = None
        self.slowdown = None

    def _smi(self, *args):
        try:
            r = self.run(['nvidia-smi', *args], capture_output=True, text=True, timeout=3)
            return r.stdout if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def available(self):
        out = self._smi('-q', '-d', 'TEMPERATURE')
        if out is None:
            return False
        for ln in out.splitlines():
            if 'Slowdown Temp' in ln and 'N/A' not in ln:
                try:
                    self.slowdown = int(ln.split(':')[1].split()[0])
                except (IndexError, ValueError):
                    pass
        return True

    def sample(self, now):
        if self.cache is None or now - self.at >= 2:
            self.at = now
            out = self._smi(f'--query-gpu={self.QUERY}', '--format=csv,noheader,nounits')
            if out:
                f = [x.strip() for x in out.splitlines()[0].split(',')]
                num = lambda s: float(s) if s.replace('.', '', 1).isdigit() else None  # noqa: E731
                self.temp = num(f[4])
                self.name = f[5] or self.name
                self.cache = {'busy': num(f[0]) or 0.0, 'mhz': int(num(f[1]) or 0), 'max_mhz': int(num(f[2]) or 0),
                              'cap_mhz': None, 'watts': num(f[3]), 'name': self.name}
        return dict(self.cache or {'busy': 0.0, 'mhz': None, 'max_mhz': None, 'cap_mhz': None, 'watts': None,
                                   'name': self.name})
