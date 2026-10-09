"""NFS shares, Proxmox, the stream check, Luneta / companion and Kuźnia: parsers and views."""
import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from piwnica_dashboard.collect import afterlife, forge, nfs, poll, pve_inventory, proxmox, stream
from tests.fake import FakeRoot

MOUNTINFO = """\
36 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
101 36 0:50 / /mnt/nas1 rw,relatime shared:60 - nfs4 10.0.0.5:/srv/nas1 rw,vers=4.2
102 36 0:51 / /mnt/my\\040share rw,relatime shared:61 - nfs 10.0.0.5:/srv/x rw
103 36 0:52 / /mnt/temp rw,relatime shared:62 - autofs systemd-1 rw
"""
FSTAB = """\
#10.0.0.5:/srv/old /mnt/old nfs defaults 0 0
10.0.0.5:/srv/nas1  /mnt/nas1  nfs  defaults,_netdev,x-systemd.automount 0 0
10.0.0.5:/srv/temp  /mnt/temp  nfs  defaults,_netdev,x-systemd.automount 0 0
UUID=abc / ext4 defaults 0 1
"""
MOUNTSTATS = """\
device /dev/nvme0n1p2 mounted on / with fstype ext4
device 10.0.0.5:/srv/nas1 mounted on /mnt/nas1 with fstype nfs4 statvers=1.1
\topts:\trw,vers=4.2
\tbytes:\t100 200 0 0 {rd} {wr} 10 20
"""


class TestNfs(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRoot()
        self.fake.put('/proc/self/mountinfo', MOUNTINFO)
        self.fake.put('/etc/fstab', FSTAB)
        self.now = 1000.0

    def tearDown(self):
        self.fake.close()

    def test_parsers(self):
        self.assertEqual(nfs.mounted_nfs(MOUNTINFO), {'/mnt/nas1': '10.0.0.5:/srv/nas1', '/mnt/my share': '10.0.0.5:/srv/x'})
        self.assertEqual(nfs.fstab_shares(FSTAB), [('10.0.0.5:/srv/nas1', '/mnt/nas1'), ('10.0.0.5:/srv/temp', '/mnt/temp')])
        self.assertEqual(nfs.mount_bytes(MOUNTSTATS.format(rd=5, wr=7)), {'/mnt/nas1': (5, 7)})

    def test_usage_rates_and_automount(self):
        self.fake.put('/proc/self/mountstats', MOUNTSTATS.format(rd=1000, wr=0))
        c = nfs.NfsCollector(statvfs=lambda t: (4000, 3000), background=False, clock=lambda: self.now)
        c.sample()
        self.now += 2
        self.fake.put('/proc/self/mountstats', MOUNTSTATS.format(rd=5000, wr=2000))
        d = {s['name']: s for s in c.sample()}
        self.assertEqual(sorted(d), ['my share', 'nas1', 'temp'])
        self.assertEqual((d['nas1']['pct'], d['nas1']['rd'], d['nas1']['wr']), (75.0, 2000.0, 1000.0))
        self.assertFalse(d['temp']['mounted'])  # automount, not mounted: never touched

    def test_hung_server(self):
        p = poll.Poller(lambda: None, 10, background=False, clock=lambda: self.now)
        p.busy, p.started = True, self.now - 20  # a statvfs stuck for 20 s
        self.assertTrue(p.stuck(nfs.HUNG_AFTER))
        self.assertIs(p.poll(), p)  # no second run while the first hangs
        self.assertTrue(p.busy)


class TestProxmox(unittest.TestCase):
    STATUS = {'cpu': 0.25, 'loadavg': ['0.5', '0.6', '0.7'], 'uptime': 3600, 'cpuinfo': {'cpus': 4},
              'memory': {'used': 2 * 2 ** 30, 'total': 8 * 2 ** 30}}
    RES = [{'type': 'lxc', 'vmid': 100, 'name': 'db', 'status': 'running', 'cpu': 0.1, 'mem': 5},
           {'type': 'qemu', 'vmid': 101, 'name': 'win', 'status': 'stopped'},
           {'type': 'lxc', 'vmid': 9000, 'name': 'tpl', 'status': 'stopped', 'template': 1},
           {'type': 'storage', 'storage': 'nas1', 'node': 'pve', 'disk': 99, 'maxdisk': 100, 'status': 'available'},
           {'type': 'storage', 'storage': 'local', 'node': 'pve', 'disk': 10, 'maxdisk': 100, 'status': 'available'},
           {'type': 'storage', 'storage': 'other', 'node': 'pve2', 'disk': 1, 'maxdisk': 2}]
    STORAGE_CONFIG = [{'storage': 'nas1', 'type': 'nfs', 'server': '192.168.1.100', 'export': '/tank/media'},
                      {'storage': 'nas2', 'type': 'nfs', 'server': '192.168.1.100', 'export': '/tank/backup',
                       'nodes': 'pve2'},
                      {'storage': 'local', 'type': 'dir', 'path': '/var/lib/vz'}]
    DISKS = [{'devpath': '/dev/sda', 'model': 'IronWolf', 'vendor': 'Seagate', 'type': 'hdd', 'size': 4 * 2 ** 40,
              'used': 'LVM', 'health': 'OK', 'serial': 'not shown'},
             {'devpath': '/dev/nvme0n1', 'model': 'SSD', 'type': 'ssd', 'size': 1 * 2 ** 40, 'used': 'GPT'}]

    def test_summary(self):
        s = proxmox.summarize(self.STATUS, self.RES, 'pve', self.STORAGE_CONFIG, self.DISKS)
        self.assertEqual((s['cpu'], s['guests'], s['running'], s['down']), (25.0, 2, 1, ['win']))
        self.assertEqual([(x['name'], x['pct']) for x in s['storages']], [('nas1', 99.0), ('local', 10.0)])
        self.assertEqual(s['nfs'], [{'name': 'nas1', 'source': '192.168.1.100:/tank/media', 'used': 99, 'total': 100,
                                     'pct': 99.0, 'active': True}])
        self.assertEqual([(d['name'], d['size'], d['used']) for d in s['disks']],
                         [('/dev/nvme0n1', 2 ** 40, 'GPT'), ('/dev/sda', 4 * 2 ** 40, 'LVM')])
        self.assertNotIn('serial', s['disks'][0])

    def test_nfs_without_usage_is_still_reported(self):
        shares = proxmox.summarize_nfs(self.STORAGE_CONFIG[:1], [], 'pve')
        self.assertEqual(shares[0]['source'], '192.168.1.100:/tank/media')
        self.assertIsNone(shares[0]['pct'])
        self.assertFalse(shares[0]['active'])

    def test_smart_parsers(self):
        nvme = pve_inventory.parse_smart({
            'type': 'text', 'health': 'PASSED', 'text': 'Temperature: 49 Celsius\n'
            'Percentage Used: 27%\nPower On Hours: 7,956\nPower Cycles: 132\n'
            'Critical Warning: 0x00\nMedia and Data Integrity Errors: 0\nError Information Log Entries: 2\n',
        })
        self.assertEqual((nvme['temperature'], nvme['wear'], nvme['hours'], nvme['cycles']),
                         (49, 27, 7956, 132))
        self.assertEqual((nvme['critical_warning'], nvme['media_errors'], nvme['error_log_entries']), (0, 0, 2))
        ata = pve_inventory.parse_smart({
            'type': 'ata', 'health': 'PASSED', 'wearout': 73,
            'attributes': [
                {'name': 'Temperature_Celsius', 'raw': '27 (Min/Max 15/47)'},
                {'name': 'Power_On_Hours', 'raw': '31103'},
                {'name': 'Power_Cycle_Count', 'raw': '3908'},
                {'name': 'Reallocated_Sector_Ct', 'raw': '0'},
                {'name': 'Current_Pending_Sector', 'raw': '2'},
            ],
        })
        self.assertEqual((ata['temperature'], ata['hours'], ata['cycles'], ata['wear']), (27, 31103, 3908, 73))
        self.assertEqual((ata['reallocated'], ata['pending']), (0, 2))

    def test_inventory_helpers_and_ssh_validation(self):
        guest = pve_inventory.parse_guest_text(
            '__OS__Debian 12\n__APP__n8n=2.27.5\n__PACKAGES__\nn8n=2.27.5\n__UPDATES__\n'
            'libc6/oldstable 2.36-9+deb12u13 amd64 [upgradable from: 2.36-9+deb12u10]\n'
            '__DOCKER__\nweb|nginx:1.27|Up 2 hours\n')
        self.assertEqual(guest['os'], 'Debian 12')
        self.assertEqual(guest['app_versions'], ['n8n=2.27.5'])
        self.assertEqual(guest['packages'], ['n8n=2.27.5'])
        self.assertEqual(len(guest['updates']), 1)
        self.assertEqual(guest['docker'][0]['image'], 'nginx:1.27')
        self.assertEqual(pve_inventory.inventory_summary(
            [{'updates': guest['updates'], 'docker': guest['docker']}], [{'name': '/dev/sda'}],
            {'/dev/sda': {'health': 'PASSED'}}),
            {'disk_count': 1, 'smart_known': 1, 'smart_issues': 0, 'guest_count': 1, 'updates': 1, 'docker_count': 1})
        self.assertRaises(ValueError, pve_inventory.scan_guest_inventory, 'bad host')

        def runner(command, **kwargs):
            self.assertIn('root@192.168.1.100', command)
            self.assertEqual(command[-2:], ['python3', '-'])
            self.assertIn('pct exec', kwargs['input'])
            return SimpleNamespace(returncode=0, stdout='{"guests":[]}', stderr='')
        self.assertEqual(pve_inventory.scan_guest_inventory('192.168.1.100', runner=runner), [])

    def test_backup_state(self):
        tasks = [{'type': 'vzdump', 'endtime': 100, 'status': 'OK'}, {'type': 'vzdump', 'endtime': 200, 'status': 'job errors'},
                 {'type': 'vzdump', 'starttime': 300}]  # still running: no endtime
        b = proxmox.backup_state(tasks, [{'schedule': 'monthly', 'next-run': 999, 'enabled': 1}])
        self.assertEqual((b['last_end'], b['last_status'], b['next_run']), (200, 'job errors', 999))

    def test_collector_with_token(self):
        tok = os.path.join(tempfile.mkdtemp(), 'tok')
        with open(tok, 'w') as f:
            f.write('dash@pve!dashboard=secret\n')
        calls = []

        def fetch(url, headers, pin):
            calls.append((url, headers['Authorization'], pin))
            path = url.split('/api2/json', 1)[1]
            data = {'/nodes': [{'node': 'pve'}], '/nodes/pve/status': self.STATUS, '/cluster/resources': self.RES,
                    '/cluster/backup': [], '/nodes/pve/disks/list': self.DISKS, '/storage': self.STORAGE_CONFIG}.get(
                        path.split('?')[0], [])
            return {'data': data}
        scanned = [{'id': 100, 'name': 'db', 'type': 'lxc', 'status': 'running',
                    'packages': ['mariadb=10.11'], 'updates': [], 'docker': []}]
        c = proxmox.ProxmoxCollector('10.0.0.5', tok, 'AA:BB', background=False, fetch=fetch,
                                     scan=lambda host, user: scanned)
        d = c.sample()
        self.assertEqual((d['node'], d['error'], d['down']), ('pve', None, ['win']))
        self.assertEqual(d['nfs'][0]['source'], '192.168.1.100:/tank/media')
        self.assertEqual([disk['name'] for disk in d['disks']], ['/dev/nvme0n1', '/dev/sda'])
        self.assertIsNone(d['disk_error'])
        self.assertIsNone(d['nfs_error'])
        self.assertEqual(d['inventory'], scanned)
        self.assertEqual(d['inventory_summary']['guest_count'], 1)
        self.assertIsNotNone(d['inventory_at'])
        self.assertTrue(all(a == 'PVEAPIToken=dash@pve!dashboard=secret' and p == 'AA:BB' for _u, a, p in calls))
        self.assertEqual(proxmox.ProxmoxCollector('h', '/nonexistent', background=False).sample(), {'error': 'no token'})


class TestStream(unittest.TestCase):
    SS = ('0 0 192.168.1.2:53412 192.168.1.223:1935\n'
          '\t cubic wscale:7,7 rto:204 bytes_sent:{sent} bytes_acked:1 segs_out:9\n')

    def test_live_and_bitrate(self):
        t = [100.0]
        out = [self.SS.format(sent=1_000_000)]
        c = stream.StreamCollector(ss=lambda: out[0], background=False, clock=lambda: t[0])
        self.assertEqual(c.sample()['peer'], '192.168.1.223')
        t[0], out[0] = 102.0, self.SS.format(sent=2_500_000)
        c.poller.started = -1e9
        self.assertAlmostEqual(c.sample()['bps'], 6e6)
        out[0] = ''
        c.poller.started = -1e9
        self.assertEqual(c.sample(), {'live': False})


class TestAfterlife(unittest.TestCase):
    STATE = {'game': 'blue_archive',
             'resetAt': {'daily': '2026-10-08T19:00:00.000Z', 'weekly': '2026-10-11T19:00:00.000Z'},
             'period': {'daily': '2026-10-08'},
             'tasks': [{'period': 'daily', 'periodKey': '2026-10-08', 'done': True, 'priority': 'critical'},
                       {'period': 'daily', 'periodKey': '2026-10-08', 'done': False, 'priority': 'critical'},
                       {'period': 'weekly', 'periodKey': '2026-W41', 'done': False}],
             'currencies': [{'key': 'pyroxene', 'label': 'Pyroxene', 'premium': True, 'pullCost': 120}],
             'lastSnapshots': [{'currency': 'pyroxene', 'amount': 6417}],
             'events': [{'name': 'Old', 'endsAt': '2026-10-01T00:00:00Z'}, {'name': 'Raid', 'endsAt': '2026-10-12T15:00:00Z'},
                        {'name': 'Soon', 'endsAt': '2026-10-10T15:00:00Z'}],
             'advice': [{'text': 'a', 'weight': 1}, {'text': 'b', 'weight': 9}]}

    def test_game_view_before_and_after_reset(self):
        before = afterlife.iso('2026-10-08T18:00:00Z')
        g = afterlife.game_view(self.STATE, before)
        self.assertEqual((g['name'], g['done'], g['tasks'], g['critical_left'], g['stale']), ('Blue Archive', 1, 2, 1, False))
        self.assertEqual((g['daily_in'], g['premium']['pulls'], g['event']['name'], g['advice']), (3600, 53, 'Soon', 'b'))
        after = afterlife.iso('2026-10-08T20:00:00Z')  # the cached state predates the reset: nothing done yet
        g = afterlife.game_view(self.STATE, after)
        self.assertEqual((g['done'], g['critical_left'], g['stale'], g['daily_in']), (0, 2, True, 23 * 3600))

    def test_local_agent_data_without_token(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, 'logs'))
        db = sqlite3.connect(os.path.join(d, 'luneta.db'))
        db.execute('create table state_cache (game text, data text, fetched_at integer)')
        db.execute('insert into state_cache values (?, ?, ?)', ('blue_archive', json.dumps(self.STATE), 1_000_000))
        db.commit()
        db.close()
        with open(os.path.join(d, 'logs', 'luneta.log'), 'w') as f:
            f.write('[2026-10-08T18:19:15.769Z DEBUG luneta::app::sync] heartbeat ok: deviceId Some(Num(8))\n'
                    '[2026-10-08T18:19:26.769Z INFO  luneta::tray] tray: quit\n')
        with open(os.path.join(d, 'analiza.db.progress.json'), 'w') as f:
            json.dump({'running': True, 'pid': 999999999, 'index': 10, 'of': 102, 'done': 9}, f)
        c = afterlife.AfterlifeCollector(token_file='/nonexistent', luneta_dir=d, background=False,
                                         fetch=lambda *a: self.fail('no network without a token'),
                                         clock=lambda: afterlife.iso('2026-10-08T18:00:00Z'))
        lu, now = c.sample()
        self.assertEqual([g['name'] for g in lu['games']], ['Blue Archive'])
        self.assertEqual((lu['heartbeat'], lu['agent_quit']), (afterlife.iso('2026-10-08T18:19:15.769Z'), True))
        self.assertEqual((lu['analysis']['running'], lu['analysis']['dead']), (False, True))  # its pid is gone
        self.assertEqual(now, {'error': 'no token'})

    def test_token_shape(self):
        p = os.path.join(tempfile.mkdtemp(), 't')
        with open(p, 'w') as f:
            f.write('lnt_' + 'a' * 43 + '\n')
        self.assertEqual(afterlife.read_token(p), 'lnt_' + 'a' * 43)
        with open(p, 'w') as f:
            f.write('something else')
        self.assertIsNone(afterlife.read_token(p))


class TestForge(unittest.TestCase):
    def test_cap_and_comfy_off(self):
        self.assertEqual(forge.parse_cap('[Service]\nEnvironment=SOFTSTART_MAX_CAP=2000\nEnvironment=SOFTSTART_POWER_W=150\n'),
                         {'max_mhz': 2000, 'watts': 150})

        def refused(*a, **k):
            raise ConnectionRefusedError('refused')
        d = forge.ForgeCollector(softstart=lambda: {'active': 'active'}, background=False, fetch=refused).sample()
        self.assertEqual((d['comfy_up'], d['softstart']), (False, {'active': 'active'}))

    def test_comfy_queue(self):
        def fetch(url, timeout=2):
            if url.endswith('/queue'):
                return {'queue_running': [[1]], 'queue_pending': [[2], [3]]}
            return {'devices': [{'vram_total': 16, 'vram_free': 4}]}
        d = forge.ForgeCollector(softstart=lambda: {}, background=False, fetch=fetch).sample()
        self.assertEqual(d['comfy'], {'running': 1, 'pending': 2, 'vram_used': 12, 'vram_total': 16})


class TestPinnedHttps(unittest.TestCase):
    def test_fingerprint_normalisation(self):
        self.assertEqual(poll.norm_fp('AA:bb:0C'), 'aabb0c')


if __name__ == '__main__':
    unittest.main()
