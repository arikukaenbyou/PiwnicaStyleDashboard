"""scripts/update-homelab.py: reading apt simulations (security, removals, kept back) and the community-scripts app."""
import importlib.util
import os
import unittest

spec = importlib.util.spec_from_file_location(
    'update_homelab', os.path.join(os.path.dirname(__file__), '..', 'scripts', 'update-homelab.py'))
uh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uh)

# the remote helpers run on the node; compile them here to test the parsers they share
remote = {}
exec(compile(uh.REMOTE_SCRIPT.split('\ndef main():')[0], 'remote', 'exec'), remote)

APT_UPGRADE = """\
Reading package lists...
Calculating upgrade...
The following packages have been kept back:
  linux-image-amd64 systemd
The following packages will be upgraded:
  libssl3 openssl tzdata
3 upgraded, 0 newly installed, 0 to remove and 2 not upgraded.
Inst libssl3 [3.0.15-1~deb12u1] (3.0.17-1~deb12u2 Debian-Security:12/stable-security [amd64])
Inst openssl [3.0.15-1~deb12u1] (3.0.17-1~deb12u2 Debian:12.11/stable, Debian-Security:12/stable-security [amd64])
Inst tzdata [2024b-0+deb12u1] (2025b-0+deb12u1 Debian:12.11/stable-updates [all])
Conf libssl3 (3.0.17-1~deb12u2 Debian-Security:12/stable-security [amd64])
"""
UBUNTU = 'Inst curl [7.81.0-1ubuntu1.18] (7.81.0-1ubuntu1.20 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])'
DIST = """\
Remv proxmox-ve [8.4.0]
Inst pve-manager [8.4.0] (8.4.14 Proxmox:8.4/bookworm [amd64])
"""


class TestSimulation(unittest.TestCase):
    def test_remote_split(self):
        lines, kept = remote['simulation'](APT_UPGRADE)
        self.assertEqual(len(lines), 3)
        self.assertEqual(kept, ['linux-image-amd64', 'systemd'])

    def test_security_and_removals(self):
        p = uh.parse_simulation(remote['simulation'](APT_UPGRADE)[0] + [UBUNTU])
        self.assertEqual(p['packages'], ['libssl3', 'openssl', 'tzdata', 'curl'])
        self.assertEqual(p['security'], ['libssl3', 'openssl', 'curl'])
        self.assertEqual(p['removed'], [])
        lines = remote['simulation'](DIST)[0]
        self.assertEqual(uh.parse_simulation(lines)['removed'], ['proxmox-ve'])
        self.assertEqual(remote['removes_protected'](lines), ['proxmox-ve'])

    def test_most_security_first(self):
        plan = {'targets': [{'key': 'lxc:101', 'name': 'LXC 101 a', 'lines': [UBUNTU]},
                            {'key': 'host', 'name': 'Proxmox host', 'lines': ['Inst tzdata [1] (2 Debian:12/stable [all])']},
                            {'key': 'lxc:102', 'name': 'LXC 102 b',
                             'lines': remote['simulation'](APT_UPGRADE)[0]}]}
        self.assertEqual([t['key'] for t in uh.summarize(plan)], ['lxc:102', 'lxc:101', 'host'])

    def test_apply_options(self):
        self.assertIn('Dpkg::Options::=--force-confold', remote['CONFFILES'])
        self.assertIn('DPkg::Lock::Timeout=300', remote['LOCK'])


class TestCommunityScript(unittest.TestCase):
    def test_slug_from_usr_bin_update(self):
        body = ('set -a; [ -f /etc/profile.d/90-http-proxy.sh ] && . /etc/profile.d/90-http-proxy.sh; set +a; '
                'bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/forgejo-runner.sh)"')
        remote['run'] = lambda args, timeout=900: (0, body, '')
        self.assertEqual(remote['community_script'](['pct', 'exec', '102', '--']), 'forgejo-runner')
        remote['run'] = lambda args, timeout=900: (1, '', 'No such file')
        self.assertIsNone(remote['community_script'](['pct', 'exec', '103', '--']))


if __name__ == '__main__':
    unittest.main()
