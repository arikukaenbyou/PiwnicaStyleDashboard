"""CPU, IOWait, memory, network, uptime/load and physical disks -- no root needed."""
import json
import os
import subprocess

from .. import sysfs


def default_lsblk():
    out = subprocess.run(['lsblk', '--json', '-b', '-o', 'NAME,MODEL,SIZE,TYPE,TRAN,RM,MOUNTPOINTS'],
                         capture_output=True, text=True, timeout=5).stdout
    return json.loads(out)


def default_statvfs(mount):
    st = os.statvfs(mount)
    return st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize


def default_route_iface():
    """Interface that carries the default route (/proc/net/route), else the first physical NIC."""
    for line in (sysfs.read('/proc/net/route', '') or '').splitlines()[1:]:
        f = line.split()
        if len(f) > 3 and f[1] == '00000000' and int(f[3], 16) & 1:  # destination 0.0.0.0, RTF_UP
            return f[0]
    for d in sysfs.glob('/sys/class/net/*'):
        name = os.path.basename(d)
        if name != 'lo' and sysfs.exists(d + '/device'):
            return name
    return None


def physical_disks(tree):
    """lsblk tree -> [{'name', 'model', 'size', 'tran', 'mounts'}] for real, non-removable disks."""
    disks = []
    for d in tree.get('blockdevices', []):
        if d.get('type') != 'disk' or d['name'].startswith(('loop', 'zram', 'sr', 'ram')):
            continue
        if d.get('rm') in (True, '1', 1):
            continue
        mounts = []

        def walk(n):
            for m in n.get('mountpoints') or []:
                if m and m.startswith('/') and m not in mounts:
                    mounts.append(m)
            for c in n.get('children') or []:
                walk(c)
        walk(d)
        disks.append({'name': d['name'], 'model': (d.get('model') or '').strip(), 'size': int(d.get('size') or 0),
                      'tran': d.get('tran') or '', 'mounts': mounts})
    return disks


class SystemCollector:
    def __init__(self, iface='auto', lsblk=default_lsblk, statvfs=default_statvfs):
        self.lsblk, self.statvfs = lsblk, statvfs
        self.iface = default_route_iface() if iface in (None, '', 'auto') else iface
        self.prev = {}
        self.disks, self.disks_at = [], -1e9
        self.freq_files = sysfs.glob('/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq')

    def rate(self, key, value, now):
        """Per-second rate against the previous sample."""
        pv = self.prev.get(key)
        self.prev[key] = (value, now)
        if pv is None or pv[0] is None or value is None or now <= pv[1]:
            return 0.0
        return max(0.0, (value - pv[0]) / (now - pv[1]))

    def sample(self, now):
        d = {}
        # CPU + IOWait from the aggregate "cpu" line
        lines = (sysfs.read('/proc/stat', '') or '').splitlines()
        f = [int(x) for x in lines[0].split()[1:9]] if lines else [0] * 8
        total, idle, iowait = sum(f), f[3], f[4]
        pt = self.prev.get('cpu')
        self.prev['cpu'] = (total, idle, iowait)
        if pt and total > pt[0]:
            dt = total - pt[0]
            d['cpu'] = 100 * (dt - (idle - pt[1]) - (iowait - pt[2])) / dt
            d['iowait'] = 100 * (iowait - pt[2]) / dt
        else:
            d['cpu'] = d['iowait'] = 0.0
        d['threads'] = sum(1 for ln in lines if ln.startswith('cpu') and ln[3:4].isdigit())
        freqs = [x for x in (sysfs.read_int(f) for f in self.freq_files) if x]
        d['cpu_ghz'] = sum(freqs) / len(freqs) / 1e6 if freqs else None

        # memory
        mi = {}
        for ln in (sysfs.read('/proc/meminfo', '') or '').splitlines():
            k, _, v = ln.partition(':')
            mi[k] = int(v.split()[0]) * 1024 if v.split() else 0
        d['mem_total'] = mi.get('MemTotal', 0)
        d['mem_used'] = d['mem_total'] - mi.get('MemAvailable', 0)
        d['mem'] = 100 * d['mem_used'] / d['mem_total'] if d['mem_total'] else 0.0
        d['swap_used'] = mi.get('SwapTotal', 0) - mi.get('SwapFree', 0)

        # network
        d['iface'] = self.iface
        if self.iface:
            d['rx'] = self.rate('rx', sysfs.read_int(f'/sys/class/net/{self.iface}/statistics/rx_bytes'), now)
            d['tx'] = self.rate('tx', sysfs.read_int(f'/sys/class/net/{self.iface}/statistics/tx_bytes'), now)
        else:
            d['rx'] = d['tx'] = 0.0

        # system
        d['uptime'] = float((sysfs.read('/proc/uptime', '0') or '0').split()[0])
        la = (sysfs.read('/proc/loadavg', '0 0 0 0/0') or '0 0 0 0/0').split()
        d['load'] = [float(x) for x in la[:3]]
        d['procs'] = int(la[3].split('/')[1]) if '/' in la[3] else 0
        d['kernel'] = sysfs.read('/proc/sys/kernel/osrelease', '') or os.uname().release

        # physical disks (lsblk every 60 s, usage/IO every sample)
        if now - self.disks_at >= 60 or not self.disks:
            self.disks_at = now
            try:
                self.disks = physical_disks(self.lsblk())
            except Exception:  # noqa: BLE001 -- keep the last good list
                pass
        disks = []
        for dk in self.disks:
            total = used = 0
            for m in dk['mounts']:
                try:
                    t, u = self.statvfs(m)
                except OSError:
                    continue
                total += t
                used += u
            st = (sysfs.read(f'/sys/block/{dk["name"]}/stat', '') or '').split()
            rd = int(st[2]) * 512 if len(st) > 6 else None
            wr = int(st[6]) * 512 if len(st) > 6 else None
            disks.append(dict(dk, total=total, used=used, pct=100 * used / total if total else 0.0,
                              rd=self.rate('rd_' + dk['name'], rd, now), wr=self.rate('wr_' + dk['name'], wr, now)))
        d['disks'] = disks
        return d
