"""Proxmox VE host: node load, guests, storages and the backup job -- over the API with a read-only
token (role PVEAuditor) and a pinned certificate fingerprint (PVE's certificate is self-signed).

Token file: one line `user@realm!tokenid=secret` (what `pveum user token add` prints), mode 600.
The auditor role cannot list backup files, so the backup state comes from the vzdump tasks.
"""
import os
import time
from collections import deque

from .poll import Poller, get_json
from .pve_inventory import inventory_summary, parse_smart, scan_guest_inventory


def read_token(path):
    try:
        with open(os.path.expanduser(path)) as f:
            tok = f.read().strip()
    except OSError:
        return None
    return tok if '!' in tok and '=' in tok else None


def summarize_nfs(configs, resources, node):
    """Configured NFS storages on this node, enriched with their available capacity."""
    usage = {r.get('storage'): r for r in resources if r.get('type') == 'storage'}
    out = []
    for config in configs:
        if config.get('type') != 'nfs':
            continue
        nodes = config.get('nodes')
        if nodes and node not in str(nodes).split(','):
            continue
        name = config.get('storage')
        if not name:
            continue
        state = usage.get(name, {})
        total, used = state.get('maxdisk', 0), state.get('disk', 0)
        out.append({
            'name': name,
            'source': ':'.join(part for part in (config.get('server'), config.get('export')) if part),
            'used': used,
            'total': total,
            'pct': 100 * used / total if total else None,
            'active': state.get('status') == 'available' and not config.get('disable'),
        })
    return sorted(out, key=lambda s: (s['pct'] is None, -(s['pct'] or 0), s['name']))


def summarize_disks(disks):
    """PVE disk inventory, excluding serial numbers which are not useful in the panel."""
    out = []
    for disk in disks:
        path = disk.get('devpath') or disk.get('name')
        if not path:
            continue
        out.append({
            'name': path,
            'model': ' '.join(part for part in (disk.get('vendor'), disk.get('model')) if part).strip(),
            'type': disk.get('type') or '',
            'size': disk.get('size') or 0,
            'used': disk.get('used') or 'unknown',
            'health': disk.get('health'),
            'wearout': disk.get('wearout'),
        })
    return sorted(out, key=lambda d: d['name'])


def summarize(status, resources, node, storage_config=(), disks=()):
    """The panel's view of /nodes/<node>/status + /cluster/resources."""
    mem = status.get('memory') or {}
    guests = [r for r in resources if r.get('type') in ('lxc', 'qemu') and not r.get('template')]
    storages = [r for r in resources if r.get('type') == 'storage' and r.get('node', node) == node]
    running = [g for g in guests if g.get('status') == 'running']
    return {
        'node': node,
        'cpu': 100 * float(status.get('cpu') or 0),
        'cores': (status.get('cpuinfo') or {}).get('cpus'),
        'load': [float(x) for x in status.get('loadavg') or []],
        'mem_used': mem.get('used', 0), 'mem_total': mem.get('total', 0),
        'uptime': status.get('uptime', 0),
        'guests': len(guests), 'running': len(running),
        'down': sorted(g.get('name') or str(g.get('vmid')) for g in guests if g.get('status') != 'running'),
        'top': sorted(({'name': g.get('name'), 'cpu': 100 * float(g.get('cpu') or 0), 'mem': g.get('mem', 0)}
                       for g in running), key=lambda g: -g['mem'])[:3],
        'storages': sorted(({'name': s['storage'], 'used': s.get('disk', 0), 'total': s.get('maxdisk', 0),
                             'pct': 100 * s.get('disk', 0) / s['maxdisk'] if s.get('maxdisk') else 0.0,
                             'active': s.get('status') == 'available'} for s in storages), key=lambda s: -s['pct']),
        'nfs': summarize_nfs(storage_config, resources, node),
        'disks': summarize_disks(disks),
    }


def backup_state(tasks, jobs):
    """Last vzdump run (the job's own task: id '') and the next scheduled one."""
    runs = [t for t in tasks if t.get('type', 'vzdump') == 'vzdump' and t.get('endtime')]
    last = max(runs, key=lambda t: t['endtime']) if runs else None
    nxt = min((j['next-run'] for j in jobs if j.get('enabled', 1) and j.get('next-run')), default=None)
    return {'last_end': last['endtime'] if last else None, 'last_status': last.get('status') if last else None,
            'next_run': nxt, 'schedule': (jobs[0].get('schedule') if jobs else None)}


class ProxmoxCollector:
    def __init__(self, host, token_file, fingerprint='', node='auto', background=True, fetch=get_json,
                 scan=scan_guest_inventory, ssh_user='root'):
        self.base = f'https://{host}:8006/api2/json'
        self.token = read_token(token_file)
        self.pin, self.node, self.fetch = fingerprint, node, fetch
        self.ssh_user = ssh_user
        self.cpu_hist = deque(maxlen=60)
        self.main = Poller(self.fetch_main, 5, background)
        self.backups = Poller(self.fetch_backups, 300, background)
        self.disk_inventory = Poller(self.fetch_disks, 60, background)
        self.smart_inventory = Poller(self.fetch_smart, 300, background)
        self.storage_config = Poller(self.fetch_storage_config, 300, background)
        self.guests = Poller(lambda: scan(host, self.ssh_user), 1800, background)

    def get(self, path):
        return self.fetch(self.base + path, {'Authorization': f'PVEAPIToken={self.token}'}, self.pin)['data']

    def fetch_main(self):
        if self.node in (None, '', 'auto'):
            self.node = self.get('/nodes')[0]['node']
        out = summarize(self.get(f'/nodes/{self.node}/status'), self.get('/cluster/resources'), self.node)
        self.cpu_hist.append(out['cpu'])
        return out

    def fetch_backups(self):
        node = self.node if self.node not in (None, '', 'auto') else self.get('/nodes')[0]['node']
        return backup_state(self.get(f'/nodes/{node}/tasks?typefilter=vzdump&limit=20&source=all'), self.get('/cluster/backup'))

    def fetch_disks(self):
        node = self.node if self.node not in (None, '', 'auto') else self.get('/nodes')[0]['node']
        return summarize_disks(self.get(f'/nodes/{node}/disks/list'))

    def fetch_smart(self):
        node = self.node if self.node not in (None, '', 'auto') else self.get('/nodes')[0]['node']
        disks = self.disk_inventory.value or self.fetch_disks()
        return {disk['name']: parse_smart(self.get(f'/nodes/{node}/disks/smart?disk={disk["name"]}'))
                for disk in disks}

    def fetch_storage_config(self):
        configs = self.get('/storage')
        resources = self.get('/cluster/resources')
        node = self.node if self.node not in (None, '', 'auto') else self.get('/nodes')[0]['node']
        return summarize_nfs(configs, resources, node)

    def sample(self):
        if not self.token:
            return {'error': 'no token'}
        m, b = self.main.poll(), self.backups.poll()
        disks, smart = self.disk_inventory.poll(), self.smart_inventory.poll()
        nfs, guests = self.storage_config.poll(), self.guests.poll()
        d = dict(m.value or {})
        d['error'] = m.error if (m.error or not m.value) else None
        if m.error is None and not m.value:
            d['error'] = 'connecting…'
        d['age'] = time.time() - m.at if m.at else None
        d['backup'] = b.value
        d['cpu_hist'] = self.cpu_hist
        d['disks'] = disks.value or []
        d['disk_error'] = disks.error
        d['smart'] = smart.value or {}
        d['smart_error'] = smart.error
        d['nfs'] = nfs.value or []
        d['nfs_error'] = nfs.error
        d['inventory'] = guests.value or []
        d['inventory_error'] = guests.error
        d['inventory_at'] = guests.at
        d['inventory_summary'] = inventory_summary(d['inventory'], d['disks'], d['smart'])
        return d
