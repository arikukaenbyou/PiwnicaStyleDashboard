"""pacman / yay progress: command lines, pacman.log history, phases of a run on a fake /proc and log."""
import os
import unittest
from datetime import datetime

from piwnica_dashboard.collect import pacman
from tests.fake import FakeRoot

T0 = datetime(2026, 10, 8, 21, 0, 0).astimezone().timestamp()


def line(dt, src, msg):
    stamp = datetime.fromtimestamp(T0 + dt).astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')
    return f'[{stamp}] [{src}] {msg}\n'


# an earlier -Syu with a kernel (slow hooks), then a yay AUR install
HISTORY = ''.join([
    line(-9000, 'PACMAN', "Running 'pacman -Syu'"),
    line(-9000, 'PACMAN', 'synchronizing package lists'),
    line(-8990, 'PACMAN', 'starting full system upgrade'),
    line(-8900, 'ALPM', 'transaction started'),
    line(-8896, 'ALPM', 'upgraded linux (7.2.8.arch1-1 -> 7.2.9.arch1-1)'),
    line(-8892, 'ALPM', 'upgraded glibc (2.43-1 -> 2.44-1)'),
    line(-8888, 'ALPM', 'upgraded pacman (7.0.0-1 -> 7.1.0-1)'),
    line(-8880, 'ALPM', 'upgraded mkinitcpio (39-1 -> 40-1)'),
    line(-8880, 'ALPM', 'warning: /etc/mkinitcpio.conf installed as /etc/mkinitcpio.conf.pacnew'),
    line(-8880, 'ALPM', 'transaction completed'),
    line(-8880, 'ALPM', "running '60-mkinitcpio-remove.hook'..."),
    line(-8870, 'ALPM', "running '90-mkinitcpio-install.hook'..."),
    line(-8840, 'ALPM', "running 'texinfo-install.hook'..."),
    line(-8000, 'PACMAN', "Running 'pacman -U --config /etc/pacman.conf -- /home/u/.cache/yay/foo-bin/foo-bin-1.2-1-x86_64.pkg.tar.zst'"),
    line(-8000, 'ALPM', 'transaction started'),
    line(-7999, 'ALPM', 'installed foo-bin (1.2-1)'),
    line(-7999, 'ALPM', 'transaction completed'),
    line(-7999, 'PACMAN', "Running 'pacman -D -q --asdeps --config /etc/pacman.conf -- foo-bin'"),
])

SI = """Repository      : core
Name            : glibc
Version         : 2.45-1
Architecture    : x86_64
Optional Deps   : gd: for memusagestat
                  perl: for mtrace
Download Size   : 10.00 MiB
Installed Size  : 50.00 MiB

Repository      : extra
Name            : zstd
Version         : 1:1.6.0-1
Architecture    : x86_64
Download Size   : 512.00 KiB
Installed Size  : 2.00 MiB

"""
QI = """Name            : glibc
Version         : 2.44-1
Installed Size  : 49.00 MiB

Name            : zstd
Version         : 1:1.5.7-1
Installed Size  : 2.50 MiB

"""


class TestParsing(unittest.TestCase):
    def test_cmdline(self):
        c = pacman.parse_cmdline(['pacman', '-S', '-y', '-u', '--config', '/etc/pacman.conf', '--', 'extra/apache', 'w3m'])
        self.assertEqual((c['op'], c['flags'], c['targets']), ('S', {'y', 'u'}, ['extra/apache', 'w3m']))
        self.assertTrue(pacman.is_update(c))
        self.assertEqual(pacman.title(c), 'pacman -Syu')
        self.assertFalse(pacman.is_update(pacman.parse_cmdline(['pacman', '-Si', '--', 'glibc'])))
        self.assertFalse(pacman.is_update(pacman.parse_cmdline(['pacman', '-Qu'])))
        self.assertFalse(pacman.is_update(pacman.parse_cmdline(['pacman', '-D', '-q', '--asdeps', 'x'])))
        c = pacman.parse_cmdline(['pacman', '--sync', '--refresh', '--sysupgrade', '--noconfirm'])
        self.assertEqual((c['op'], c['flags'], c['targets']), ('S', {'y', 'u'}, []))
        self.assertEqual(pacman.parse_cmdline(['pacman', '-Rns', 'foo'])['op'], 'R')

    def test_info_and_names(self):
        info = pacman.parse_info(SI)
        self.assertEqual(info['glibc'], {'version': '2.45-1', 'arch': 'x86_64', 'csize': 10 * 2 ** 20, 'isize': 50 * 2 ** 20})
        self.assertEqual(info['zstd']['csize'], 512 * 1024)
        self.assertEqual(pacman.pkg_name('/c/foo-bar-1:2.0-1-x86_64.pkg.tar.zst'), 'foo-bar')

    def test_log_line(self):
        ts, src, msg = pacman.parse_log_line('[2026-10-08T21:24:08+0200] [ALPM] upgraded foo (1 -> 2)\n')
        self.assertEqual((src, msg), ('ALPM', 'upgraded foo (1 -> 2)'))
        self.assertEqual(ts, datetime.fromisoformat('2026-10-08T21:24:08+02:00').timestamp())
        self.assertIsNone(pacman.parse_log_line('[2019-01-01 12:00] [ALPM] old format'))

    def test_history(self):
        st = pacman.LogState()
        for ln in HISTORY.splitlines():
            st.feed(ln)
        st.finish()
        self.assertEqual(st.last(), {'ts': T0 - 9000, 'pkgs': 4, 'dur': 160.0})
        self.assertAlmostEqual(st.sec_per_pkg(), 5.0)          # 20 s / 4 packages
        self.assertEqual(list(st.hooks_kernel), [40.0])        # hooks until the last hook line
        self.assertEqual(st.pacnew, ['/etc/mkinitcpio.conf.pacnew'])
        self.assertIn('foo-bin', st.upgraded_at)
        self.assertEqual(sum(st.daily.values()), 5)


class TestReboot(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRoot()

    def tearDown(self):
        self.fake.close()

    def test_reasons(self):
        self.fake.mkdir('/usr/lib/modules/7.2.9-arch1-1')
        up = {'glibc': 200, 'linux-firmware-intel': 200, 'linux-firmware-amd': 200, 'firefox': 200, 'mesa': 50}
        self.assertEqual(pacman.reboot_needed('7.2.9-arch1-1', up, boot_ts=100), ['glibc', 'linux-firmware'])
        self.assertEqual(pacman.reboot_needed('7.2.8-arch1-1', {'linux': 200}, boot_ts=100), ['kernel'])


class TestRun(unittest.TestCase):
    """A -Syu followed through its phases on a fake /proc, cache and pacman.log."""

    def setUp(self):
        self.fake = FakeRoot()
        self.fake.put('/proc/uptime', '5000.0 1000.0')
        self.fake.mkdir(f'/usr/lib/modules/{os.uname().release}')
        self.fake.mkdir('/var/cache/pacman/pkg')
        self.log = HISTORY
        self.fake.put('/var/log/pacman.log', self.log)
        self.calls = []
        self.now = T0

        def run(argv):
            self.calls.append(argv[:2])
            return {'-Qu': 'glibc 2.44-1 -> 2.45-1\nzstd 1:1.5.7-1 -> 1:1.6.0-1\n', '-Si': SI, '-Qi': QI,
                    '-Qua': 'a 1-1 -> 2-1\npython2 2.7.18-1 -> 2.7.18-2\nc 1-1 -> 2-1\n'}.get(argv[1], '')
        self.yay = os.path.join(self.fake.root, 'home/u/.cache/yay')
        self.pc = pacman.PacmanCollector(pending_interval=0, run=run, wall=lambda: self.now, background=False,
                                         builds_path=os.path.join(self.fake.root, 'builds.json'), caches=[self.yay])

    def tearDown(self):
        self.fake.close()

    def append(self, *lines):
        self.log += ''.join(line(*ln) for ln in lines)
        self.fake.put('/var/log/pacman.log', self.log)

    def proc(self, pid, comm, argv):
        self.fake.put(f'/proc/{pid}/comm', comm)
        with open(self.fake.root + f'/proc/{pid}/cmdline', 'wb') as f:
            f.write(b'\0'.join(a.encode() for a in argv) + b'\0')

    def test_phases(self):
        d = self.pc.sample()
        self.assertEqual(d['state'], 'idle')
        self.assertEqual(d['last']['pkgs'], 4)
        self.assertEqual(d['reboot'], [])  # the history is from before this boot

        self.proc(321, 'pacman', ['pacman', '-Syu'])
        self.append((0, 'PACMAN', "Running 'pacman -Syu'"), (0, 'PACMAN', 'synchronizing package lists'))
        self.now += 2  # idle: processes are looked at every 2 s
        self.assertEqual(self.pc.sample()['state'], 'sync')

        self.append((3, 'PACMAN', 'starting full system upgrade'))
        self.now += 3
        d = self.pc.sample()
        self.assertEqual((d['state'], d['tool'], d['total']), ('download', 'pacman -Syu', 2))
        self.assertEqual(d['dl_total'], 10 * 2 ** 20 + 512 * 1024)
        self.assertEqual(d['size_delta'], (50 - 49) * 2 ** 20 - 512 * 1024)
        self.assertEqual(d['dl_files'], (0, 2))

        self.fake.put('/var/cache/pacman/pkg/zstd-1:1.6.0-1-x86_64.pkg.tar.zst', 'x' * 100)
        self.now += 2
        d = self.pc.sample()
        self.assertEqual(d['dl_files'], (1, 2))
        self.assertEqual(d['pkg'], '~glibc')  # the one left to download
        self.assertGreater(d['speed'], 0)
        self.assertIsNotNone(d['eta'])

        self.fake.put('/var/cache/pacman/pkg/glibc-2.45-1-x86_64.pkg.tar.zst', 'x')
        self.now += 2
        self.assertEqual(self.pc.sample()['state'], 'verify')

        self.append((10, 'ALPM', 'transaction started'), (12, 'ALPM', 'upgraded zstd (1:1.5.7-1 -> 1:1.6.0-1)'))
        self.now = T0 + 13
        d = self.pc.sample()
        self.assertEqual((d['state'], d['done'], d['last_pkg'], d['pkg']), ('install', 1, 'zstd', '~ 2/2'))
        self.assertGreater(d['eta'], 0)
        pct = d['pct']

        self.append((14, 'ALPM', 'upgraded glibc (2.44-1 -> 2.45-1)'), (14, 'ALPM', 'transaction completed'),
                    (14, 'ALPM', "running '30-systemd-update.hook'..."))
        self.now = T0 + 16
        d = self.pc.sample()
        self.assertEqual((d['state'], d['pkg'], d['hook_s']), ('hooks', '30-systemd-update.hook', 2))
        self.assertGreaterEqual(d['pct'], pct)  # the total bar never goes back

        os.remove(self.fake.root + '/proc/321/comm')
        self.now += 1
        d = self.pc.sample()
        self.assertEqual(d['state'], 'idle')
        self.assertEqual((d['last']['ts'], d['last']['pkgs']), (T0, 2))
        self.assertEqual(d['reboot'], ['glibc'])  # upgraded after boot (uptime 5000 s)
        self.assertEqual(sum(1 for c in self.calls if c[1] == '-Si'), 1)  # sizes asked once per run

    def queued(self, name, t):
        """yay fetched the PKGBUILD clone at t."""
        git = os.path.join(self.yay, name, '.git')
        os.makedirs(git, exist_ok=True)
        open(os.path.join(git, 'index'), 'w').close()
        os.utime(os.path.join(git, 'index'), (t, t))

    def built(self, name, t):
        self.queued(name, T0 + 10)
        d = os.path.join(self.yay, name)
        f = os.path.join(d, f'{name}-1-1-x86_64.pkg.tar.zst')
        open(f, 'w').close()
        for path in (f, d):
            os.utime(path, (t, t))

    def test_queries_are_not_updates_and_aur_build(self):
        self.proc(400, 'pacman', ['pacman', '-Qu'])
        self.assertEqual(self.pc.sample()['state'], 'idle')
        # yay started at T0 (found later, at T0 + 400): it fetched a, b, c, d and python2; a and b were built
        # since, d (built in an earlier run) was installed from the cache, an older build does not count
        self.built('a', T0 + 100)
        self.built('b', T0 + 300)
        self.built('old', T0 - 1000)
        os.utime(os.path.join(self.yay, 'old/.git/index'), (T0 - 1000, T0 - 1000))
        self.queued('c', T0 + 10)
        self.queued('d', T0 + 10)
        self.append((350, 'PACMAN', "Running 'pacman -U --config /etc/pacman.conf -- /home/u/.cache/yay/d/d-1-1-x86_64.pkg.tar.zst'"))
        self.now = T0 + 400
        ticks = int((T0 - (self.now - 5000)) * os.sysconf('SC_CLK_TCK'))
        self.fake.put('/proc/500/stat', '500 (yay) ' + ' '.join(['S'] + ['0'] * 18 + [str(ticks)] + ['0'] * 5))
        build = os.path.join(self.yay, 'python2')
        self.queued('python2', T0 + 10)
        os.makedirs(os.path.join(build, 'src/Python-2.7.18'))
        open(os.path.join(build, 'PKGBUILD'), 'w').close()
        self.proc(500, 'yay', ['yay'])
        self.proc(501, 'makepkg', ['/usr/bin/bash', '/usr/bin/makepkg', '-f'])
        os.symlink(os.path.join(build, 'src/Python-2.7.18'), self.fake.root + '/proc/501/cwd')
        d = self.pc.sample()
        self.assertEqual((d['state'], d['tool'], d['pkg']), ('aur', 'yay', 'python2'))
        # done a, b, d; now python2; left c -> 3 of 5; gaps 100, 200, 50 s -> ~117 s a package
        self.assertEqual((d['done'], d['total'], d['queue_left']), (3, 5, ['c']))
        self.assertAlmostEqual(d['avg_build'], 350 / 3)
        self.assertAlmostEqual(d['eta'], 2 * 350 / 3)

    def test_build_stage_and_ninja(self):
        self.assertEqual(pacman.build_stage(['makepkg', '--nobuild'], []), ('sources', None))
        self.assertEqual(pacman.build_stage(['makepkg'], ['bash', 'ninja', 'clang++', 'clang++']), ('compile', 'clang++ ×2'))
        self.assertEqual(pacman.build_stage(['makepkg'], ['bash', 'fakeroot', 'strip']), ('package', 'fakeroot'))
        b = os.path.join(self.yay, 'webkit/src/build')
        os.makedirs(b)
        with open(os.path.join(b, 'build.ninja'), 'w') as f:
            f.write('include rules.ninja\nbuild a.o: CXX a.cpp\nbuild b.o: CXX b.cpp\nbuild all: phony a.o b.o\n')
        with open(os.path.join(b, '.ninja_log'), 'w') as f:
            f.write('# ninja log v7\n1\t2\t3\ta.o\tx\n')
        self.assertEqual(pacman.ninja_find(os.path.join(self.yay, 'webkit')), (b, 2))
        self.assertEqual(pacman.ninja_done(b), 1)


if __name__ == '__main__':
    unittest.main()
