"""Progress of a running pacman / yay update, and a summary of past ones -- no root needed.

pacman runs as root, so everything comes from what any user can read:
- /proc/<pid>/comm|cmdline: is pacman (or yay / makepkg) running, and with which operation;
- /var/log/pacman.log, followed like `tail -f`: phases (transaction started / completed),
  every installed package, the hooks, .pacnew files; parsed whole once for the history
  (seconds per package, how long the hooks take) that seeds the time estimates;
- `pacman -Qu` / `pacman -Si` / `pacman -Qi` (no database lock): targets and their sizes;
- /var/cache/pacman/pkg: finished downloads appear there. The file being downloaded sits in a
  root-only temporary directory, so when it cannot be read the bytes in flight are estimated from
  the network counter of the default interface.
Which package pacman is unpacking is not visible from outside: the per-package bar in the
install phase is an estimate from the speed so far (shown with a "~").
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime

from .. import sysfs

LOG = '/var/log/pacman.log'
CACHE = '/var/cache/pacman/pkg'
PKG_ACTIONS = ('upgraded', 'installed', 'reinstalled', 'downgraded', 'removed')
KERNELS = re.compile(r'^linux(-lts|-zen|-hardened|-rt|-rt-lts)?$')
# upgraded after boot -> a reboot picks up the new version
REBOOT_PKGS = re.compile(r'^(linux(-lts|-zen|-hardened|-rt|-rt-lts)?|systemd|glibc|mesa|nvidia.*|amd-ucode|intel-ucode'
                         r'|linux-firmware.*|xorg-server|dbus|dbus-broker)$')
UNITS = {'B': 1, 'KiB': 2 ** 10, 'MiB': 2 ** 20, 'GiB': 2 ** 30, 'TiB': 2 ** 40}
# pacman options that take a value (the next argument is not a target)
VALUE_OPTS = {'-r', '-b', '--root', '--dbpath', '--cachedir', '--config', '--gpgdir', '--hookdir', '--logfile',
              '--arch', '--ignore', '--ignoregroup', '--overwrite', '--assume-installed', '--print-format',
              '--color', '--sysroot'}
LONG_OPS = {'--sync': 'S', '--upgrade': 'U', '--remove': 'R', '--query': 'Q', '--database': 'D',
            '--deptest': 'T', '--files': 'F'}
LONG_FLAGS = {'--refresh': 'y', '--sysupgrade': 'u', '--downloadonly': 'w', '--info': 'i', '--search': 's',
              '--list': 'l', '--groups': 'g', '--print': 'p', '--clean': 'c'}
QUERY_FLAGS = set('islgpc')  # -Si, -Ss, ... only read, nothing to show
# what makepkg is doing, from the programs running under it (first match wins)
STAGES = (('package', {'fakeroot', 'faked', 'strip'}),
          ('compile', {'cc1', 'cc1plus', 'cc', 'gcc', 'g++', 'c++', 'clang', 'clang++', 'rustc', 'cargo', 'go',
                       'ld', 'ld.lld', 'mold', 'make', 'ninja', 'javac', 'java', 'dotnet', 'node', 'npm'}),
          ('configure', {'configure', 'cmake', 'meson', 'autoreconf', 'autoconf'}),
          ('sources', {'git', 'git-remote-http', 'curl', 'wget', 'bsdtar', 'unzip'}))
SHELLS = {'bash', 'sh', 'makepkg', 'sudo', 'env', 'fakeroot', 'faked', 'tee'}
HIST_RUNS = 30


def parse_log_line(line):
    """'[2026-10-08T21:24:08+0200] [ALPM] upgraded foo (1 -> 2)' -> (epoch, 'ALPM', 'upgraded foo (1 -> 2)')."""
    if not line.startswith('[') or line[25:28] != '] [':
        return None
    try:
        ts = datetime.strptime(line[1:25], '%Y-%m-%dT%H:%M:%S%z').timestamp()
    except ValueError:
        return None
    src, _, msg = line[28:].partition('] ')
    return ts, src, msg.rstrip('\n')


def parse_cmdline(argv):
    """pacman argv -> {'op': 'S'|'U'|'R'|..., 'flags': set, 'targets': [...]}."""
    op, flags, targets = None, set(), []
    it = iter(argv[1:])
    for a in it:
        if a == '--':
            targets.extend(it)
            break
        if a.startswith('--'):
            key = a.split('=', 1)[0]
            if key in LONG_OPS:
                op = LONG_OPS[key]
            elif key in LONG_FLAGS:
                flags.add(LONG_FLAGS[key])
            elif key in VALUE_OPTS and '=' not in a:
                next(it, None)
        elif a.startswith('-') and len(a) > 1:
            for ch in a[1:]:
                if ch in 'SURQDTF':
                    op = ch
                else:
                    flags.add(ch)
            if a in VALUE_OPTS:
                next(it, None)
        else:
            targets.append(a)
    return {'op': op, 'flags': flags, 'targets': targets}


def is_update(cmd):
    """A pacman operation that changes the system (not a query)."""
    return cmd['op'] in ('S', 'U', 'R') and not (cmd['op'] == 'S' and cmd['flags'] & QUERY_FLAGS)


def title(cmd):
    return f'pacman -{cmd["op"]}{"".join(sorted(cmd["flags"] & set("yuw"), key="yuw".index))}'


def parse_size(s):
    num, _, unit = s.strip().partition(' ')
    try:
        return int(float(num) * UNITS.get(unit, 1))
    except ValueError:
        return 0


def parse_info(text):
    """`LC_ALL=C pacman -Si/-Qi` output -> {name: {'version', 'arch', 'csize', 'isize'}}."""
    out, cur = {}, {}
    for line in text.splitlines() + ['']:
        if not line.strip():
            if cur.get('Name'):
                out[cur['Name']] = {'version': cur.get('Version', ''), 'arch': cur.get('Architecture', ''),
                                    'csize': parse_size(cur.get('Download Size', '0')),
                                    'isize': parse_size(cur.get('Installed Size', '0'))}
            cur = {}
            continue
        key, sep, val = line.partition(' : ')
        if sep and not line.startswith(' '):
            cur[key.strip()] = val.strip()
    return out


def pkg_name(path):
    """'/x/foo-bar-1:2.0-1-x86_64.pkg.tar.zst' -> 'foo-bar'."""
    base = os.path.basename(path)
    base = base.split('.pkg.tar', 1)[0]
    return base.rsplit('-', 3)[0] if base.count('-') >= 3 else base


class LogState:
    """State machine over pacman.log lines: the current run, plus history of finished runs."""

    def __init__(self):
        self.run = None
        self.per_pkg = deque(maxlen=HIST_RUNS)      # seconds per package of past transactions
        self.hooks = deque(maxlen=HIST_RUNS)        # seconds of the hook phase
        self.hooks_kernel = deque(maxlen=HIST_RUNS)  # same, transactions with a kernel
        self.last_syu = None
        self.upgraded_at = {}                        # package -> last time it changed
        self.pacnew = []
        self.daily = Counter()                       # date -> packages changed
        self.aur_installs = []                       # (time, package dir) of `pacman -U` from yay / paru caches

    def feed(self, line):
        p = parse_log_line(line)
        if p is None:
            return
        ts, src, msg = p
        if src == 'PACMAN' and msg.startswith("Running '"):
            cmd = parse_cmdline(msg[9:].rstrip("'").split())
            if cmd['op'] == 'U':
                for t in cmd['targets']:
                    m = re.search(r'/(?:yay|paru/clone)/([^/]+)/', t)
                    if m:
                        self.aur_installs.append((ts, m.group(1)))
            if is_update(cmd):
                self.finish(ts)
                self.run = {'start': ts, 'cmd': cmd, 'syu': False, 'tx_start': None, 'tx_end': None, 'done': 0,
                            'last_pkg': None, 'last_ts': ts, 'hook': None, 'hook_ts': None, 'kernel': False,
                            'end': ts, 'warnings': 0, 'errors': 0}
            elif self.run is not None and self.run['tx_end'] and ts - self.run['end'] < 600:
                self.run['end'] = ts  # yay's `pacman -D --asdeps` right after the hooks closes them
            return
        if src == 'ALPM' and msg.endswith('.pacnew') and ' installed as ' in msg:
            path = msg.rsplit(' ', 1)[1]
            if path not in self.pacnew:
                self.pacnew.append(path)
        run = self.run
        if run is None:
            return
        run['end'] = ts
        if msg == 'starting full system upgrade':
            run['syu'] = True
        elif msg == 'transaction started':
            run['tx_start'] = ts
        elif msg == 'transaction completed':
            run['tx_end'] = ts
        elif src == 'ALPM' and msg.startswith("running '") and msg.endswith("'..."):
            run['hook'], run['hook_ts'] = msg[9:-4], ts
        elif src == 'ALPM' and msg.split(' ', 1)[0] in PKG_ACTIONS and msg.endswith(')'):
            action, name = msg.split(' ', 2)[:2]
            run['done'] += 1
            run['last_pkg'], run['last_ts'] = name, ts
            run['kernel'] = run['kernel'] or bool(KERNELS.match(name))
            if action != 'removed':
                self.upgraded_at[name] = ts
            self.daily[datetime.fromtimestamp(ts).date()] += 1
        elif msg.startswith('warning:'):
            run['warnings'] += 1
        elif msg.startswith('error:'):
            run['errors'] += 1

    def finish(self, next_ts=None):
        """Close the current run into the history."""
        run, self.run = self.run, None
        if not run or not run['tx_end'] or not run['done']:
            return
        if run['done'] >= 3:
            self.per_pkg.append(max(0.0, run['tx_end'] - run['tx_start']) / run['done'])
        if run['hook_ts']:
            (self.hooks_kernel if run['kernel'] else self.hooks).append(max(0.0, run['end'] - run['tx_end']))
        if run['syu']:
            self.last_syu = summary(run)

    def last(self):
        """The last finished -Syu, including the current run once its transaction is done."""
        r = self.run
        if r and r['syu'] and r['tx_end']:
            return summary(r)
        return self.last_syu

    def sec_per_pkg(self):
        return sum(self.per_pkg) / len(self.per_pkg) if self.per_pkg else 0.5

    def hook_secs(self, kernel):
        h = (self.hooks_kernel if kernel else self.hooks) or self.hooks or self.hooks_kernel
        return sum(h) / len(h) if h else 3.0


def summary(run):
    return {'ts': run['start'], 'pkgs': run['done'], 'dur': max(0.0, run['end'] - run['start'])}


def reboot_needed(release, upgraded_at, boot_ts):
    """Reasons a reboot would pick up new versions: the running kernel's modules are gone
    (the kernel was upgraded), or a core package changed after boot."""
    reasons = []
    if release and not sysfs.exists(f'/usr/lib/modules/{release}'):
        reasons.append('kernel')
    for n in sorted(n for n, ts in upgraded_at.items() if ts > boot_ts and REBOOT_PKGS.match(n)):
        n = 'linux-firmware' if n.startswith('linux-firmware') else n  # one line for its 15 split packages
        if n not in reasons and not (KERNELS.match(n) and 'kernel' in reasons):
            reasons.append(n)
    return reasons


def pkgbuild_dir(cwd):
    """makepkg's working directory (it cds into src/<tarball>) -> the directory with the PKGBUILD."""
    d = cwd
    for _ in range(4):
        if os.path.exists(os.path.join(d, 'PKGBUILD')):
            return d
        d = os.path.dirname(d)
    return cwd


def build_name(cwd):
    return os.path.basename(pkgbuild_dir(cwd))


def ninja_find(pkgdir):
    """(build dir, number of build steps) of a ninja build under the package's src/, or None.
    Reads build.ninja (tens of MB for big projects) -- call it off the main loop."""
    import glob as _glob
    hits = []
    for pat in ('src/build.ninja', 'src/*/build.ninja', 'src/*/*/build.ninja'):
        hits += _glob.glob(os.path.join(pkgdir, pat))
    if not hits:
        return None
    path = max(hits, key=os.path.getmtime)
    total = 0
    try:
        with open(path, 'rb') as f:
            for ln in f:
                if ln.startswith(b'build ') and b': phony' not in ln:
                    total += 1
    except OSError:
        return None
    return (os.path.dirname(path), total) if total else None


def ninja_done(build_dir):
    """Steps finished so far: lines of .ninja_log without its header."""
    try:
        with open(os.path.join(build_dir, '.ninja_log'), 'rb') as f:
            data = f.read()
    except OSError:
        return None
    return data.count(b'\n') - data.count(b'# ninja log')


def descendants(root):
    """Command names of every process under root (from /proc/*/stat)."""
    kids = {}
    try:
        pids = [p for p in os.listdir(sysfs.p('/proc')) if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        st = sysfs.read(f'/proc/{pid}/stat', '') or ''
        lp, rp = st.find('('), st.rfind(')')
        try:
            ppid = int(st[rp + 2:].split()[1])
        except (IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append((int(pid), st[lp + 1:rp]))
    out, todo = [], [root]
    while todo:
        for pid, comm in kids.get(todo.pop(), []):
            out.append(comm)
            todo.append(pid)
    return out


def build_stage(argv, comms):
    """(stage, detail): 'compile', 'cc1plus x12'."""
    if '--nobuild' in argv:
        return 'sources', None
    for stage, names in STAGES:
        hit = Counter(c for c in comms if c in names)
        if hit:
            name, n = hit.most_common(1)[0]
            return stage, f'{name} ×{n}' if n > 1 else name
    busy = [c for c in comms if c not in SHELLS]
    return 'build', busy[-1] if busy else None


def aur_caches():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return [os.path.join(base, 'yay'), os.path.join(base, 'paru', 'clone')]


def built_since(caches, since):
    """{package dir: time its newest .pkg.tar.* was written} for packages built after `since`
    -- yay keeps every build in its cache, so this is what the current yay run has done so far."""
    out = {}
    for cache in caches:
        try:
            dirs = os.listdir(cache)
        except OSError:
            continue
        for d in dirs:
            full = os.path.join(cache, d)
            try:
                if os.stat(full).st_mtime < since:  # a new file in it touches the directory
                    continue
                files = [f for f in os.listdir(full) if '.pkg.tar' in f and not f.endswith(('.sig', '.part'))]
            except OSError:
                continue
            ends = [os.stat(os.path.join(full, f)).st_mtime for f in files]
            ends = [t for t in ends if t >= since]
            if ends:
                out[d] = max(ends)
    return out


def queued_since(caches, since):
    """Package dirs yay fetched after `since`: at its start yay pulls the PKGBUILD of everything it is
    going to build (new dependencies too), which rewrites the clone's .git/index."""
    out = set()
    for cache in caches:
        try:
            dirs = os.listdir(cache)
        except OSError:
            continue
        for d in dirs:
            for sub in ('.git/index', '.git/FETCH_HEAD'):
                try:
                    if os.stat(os.path.join(cache, d, sub)).st_mtime >= since:
                        out.add(d)
                        break
                except OSError:
                    pass
    return out


def builds_file():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'piwnica-dashboard', 'aur-builds.json')


def processes():
    """[(pid, comm, argv)] of pacman, yay, paru and makepkg."""
    out = []
    try:
        pids = [p for p in os.listdir(sysfs.p('/proc')) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        comm = sysfs.read(f'/proc/{pid}/comm', '')
        if comm not in ('pacman', 'yay', 'paru', 'makepkg'):
            continue
        try:
            with open(sysfs.p(f'/proc/{pid}/cmdline'), 'rb') as f:
                argv = [a.decode(errors='ignore') for a in f.read().split(b'\0') if a]
        except OSError:
            continue
        out.append((int(pid), comm, argv))
    return out


def proc_start(pid, now):
    """Wall-clock start of a process, or None."""
    stat = sysfs.read(f'/proc/{pid}/stat', '') or ''
    uptime = (sysfs.read('/proc/uptime', '') or '').split()
    try:
        ticks = int(stat.rsplit(')', 1)[1].split()[19])
        return now - float(uptime[0]) + ticks / os.sysconf('SC_CLK_TCK')
    except (IndexError, ValueError):
        return None


def default_run(argv):
    env = dict(os.environ, LC_ALL='C')
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env).stdout


class PacmanCollector:
    def __init__(self, iface=None, pending_interval=1800, aur=True, log=LOG, run=default_run, wall=time.time,
                 background=True, builds_path=None, caches=None):
        self.iface, self.log_path = iface, log
        self.run_cmd, self.wall = run, wall
        self.pending_interval, self.aur = pending_interval, aur
        self.background = background
        self.state = LogState()
        self.offset = 0
        self.read_log()
        self.release = os.uname().release
        self.pid = None             # pacman process being followed
        self.targets = None         # {name: info} once known
        self.targets_for = None
        self.cached0 = set()        # targets already in the cache when the run started
        self.dl_hist = deque(maxlen=6)
        self.rx0 = None
        self.max_pct = 0.0
        self.build = None           # AUR package being built: {'pkg', 'start', 'last', 'real'}
        self.queue = None           # this yay run: {'start', 'built', 'total'}
        self.builds_path = builds_path or builds_file()
        self.caches = caches or aur_caches()
        try:
            with open(self.builds_path) as f:
                self.build_hist = json.load(f)  # package -> seconds of its last builds
        except (OSError, ValueError):
            self.build_hist = {}
        self.pending = None         # (repo, aur) counts
        self.pending_at = self.wall() - pending_interval + 60  # first check a minute after start
        self.pending_busy = False
        self.scan_at, self.procs = -1e9, []

    # ------------------------------------------------------------ inputs
    def read_log(self):
        try:
            with open(sysfs.p(self.log_path), 'rb') as f:
                size = os.fstat(f.fileno()).st_size
                if size < self.offset:  # rotated / truncated
                    self.offset = 0
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b'\n') + 1  # a half-written last line waits for the next read
        for line in data[:end].decode(errors='replace').splitlines():
            self.state.feed(line)
        self.offset += end

    def spawn(self, fn):
        if self.background:
            threading.Thread(target=fn, daemon=True).start()
        else:
            fn()

    def load_targets(self, cmd, key):
        """Targets of the run and their sizes (in the background: pacman -Si takes a moment)."""
        def work():
            names = []
            if cmd['op'] == 'S' and 'u' in cmd['flags']:
                names += [ln.split()[0] for ln in self.run_cmd(['pacman', '-Qu']).splitlines() if ln.strip()]
            if cmd['op'] == 'U':
                names += [pkg_name(t) for t in cmd['targets']]
            else:
                names += [t.rsplit('/', 1)[-1] for t in cmd['targets']]
            names = list(dict.fromkeys(names))
            info = {n: {'version': '', 'arch': '', 'csize': 0, 'isize': 0} for n in names}
            if names and cmd['op'] == 'S':
                info.update({k: v for k, v in parse_info(self.run_cmd(['pacman', '-Si', '--'] + names)).items() if k in info})
            old = parse_info(self.run_cmd(['pacman', '-Qi', '--'] + names)) if names else {}
            for n, i in info.items():
                i['old_isize'] = old.get(n, {}).get('isize', 0)
                i['file'] = f'{n}-{i["version"]}-{i["arch"]}.pkg.tar.zst' if i['version'] else None
            if cmd['op'] == 'R':
                for n, i in info.items():
                    i['isize'], i['csize'] = 0, 0
            if self.targets_for == key:
                self.cached0 = {n for n, i in info.items() if self.cache_size(i) is not None}
                self.targets = info
        self.spawn(work)

    def check_pending(self, now):
        if self.pending_busy or not self.pending_interval or now - self.pending_at < self.pending_interval:
            return
        self.pending_at, self.pending_busy = now, True

        def work():
            try:
                repo = aur = None
                if shutil.which('checkupdates'):
                    repo = sum(1 for ln in self.run_cmd(['checkupdates']).splitlines() if ln.strip())
                if self.aur and shutil.which('yay'):
                    aur = sum(1 for ln in self.run_cmd(['yay', '-Qua']).splitlines() if ln.strip())
                self.pending = (repo, aur)
            except Exception:  # noqa: BLE001 -- offline, timeout: keep the last count
                pass
            finally:
                self.pending_busy = False
        self.spawn(work)

    def cache_size(self, info):
        if not info.get('file'):
            return None
        try:
            return os.stat(sysfs.p(f'{CACHE}/{info["file"]}')).st_size
        except OSError:
            return None

    def parts(self):
        """{filename: bytes} of downloads in progress, when readable."""
        out = {}
        for pat in (f'{CACHE}/*.part', f'{CACHE}/download-*/*.part'):
            for path in sysfs.glob(pat):
                try:
                    out[os.path.basename(path)[:-5]] = os.stat(sysfs.p(path)).st_size
                except OSError:
                    pass
        return out

    def rx_bytes(self):
        return sysfs.read_int(f'/sys/class/net/{self.iface}/statistics/rx_bytes') if self.iface else None

    # ------------------------------------------------------------ sample
    def sample(self, now=None):
        now = self.wall() if now is None else now
        self.read_log()
        active = self.pid is not None or self.build is not None or self.queue is not None
        if active or now - self.scan_at >= 2:  # idle: look for pacman every 2 s
            self.scan_at, self.procs = now, processes()
        procs = self.procs
        pac = [(pid, parse_cmdline(argv)) for pid, comm, argv in procs if comm == 'pacman']
        pac = [(pid, cmd) for pid, cmd in pac if is_update(cmd)]
        helpers = [(pid, comm) for pid, comm, _ in procs if comm in ('yay', 'paru')]
        helper = helpers[0][1] if helpers else None
        builds = [(pid, argv) for pid, comm, argv in procs if comm == 'makepkg']
        d = self.idle_info(now)
        self.track_build(now, builds, helpers[0] if helpers else None)
        if pac:
            pid, cmd = pac[0]
            d.update(self.pacman_progress(now, pid, cmd))
            d['tool'] = helper or title(cmd)
        elif builds or helper:
            self.reset()
            d.update(self.aur_progress(now, builds))
            d['tool'] = helper or 'makepkg'
        else:
            if self.pid is not None:
                self.pending_at = now - self.pending_interval + 10  # the update changed what is pending
            self.reset()
        self.check_pending(now)
        return d

    # ------------------------------------------------------------ AUR builds (yay / makepkg)
    def track_build(self, now, builds, helper):
        """Follow which package makepkg builds; a finished build's time goes to the history."""
        pkg = pkgdir = None
        if builds:
            try:
                pkgdir = pkgbuild_dir(os.readlink(sysfs.p(f'/proc/{builds[0][0]}/cwd')))
                pkg = os.path.basename(pkgdir)
            except OSError:
                pkg = None
        b = self.build
        if b and b['pkg'] != pkg:
            if b['real'] and b['last'] - b['start'] > 20:  # yay's source-only passes (--nobuild) do not count
                self.save_build(b['pkg'], b['last'] - b['start'])
            self.build = b = None
        if pkg:
            if b is None:
                starts = [proc_start(p, now) for p, _ in builds]
                self.build = b = {'pkg': pkg, 'dir': pkgdir, 'start': min([t for t in starts if t] or [now]), 'last': now,
                                  'real': False, 'ninja': None, 'ninja_at': -1e9, 'steps': deque(maxlen=300)}
            b['last'] = now
            b['real'] = b['real'] or any('--nobuild' not in argv for _, argv in builds)
        if helper and (self.queue is None or self.queue['pid'] != helper[0]):
            self.start_queue(now, helper)
        if not helper and not builds:
            self.queue = None
        q = self.queue
        if q is not None and now - q['scan_at'] >= 15:
            q['scan_at'] = now
            self.spawn(lambda q=q: q.update(built=built_since(self.caches, q['start']),
                                            queued=queued_since(self.caches, q['start'])))

    def start_queue(self, now, helper):
        """A yay run found (maybe long after it started): everything it will build was fetched into its
        cache at the start, what it built so far is there as new package files."""
        pid, _comm = helper
        self.queue = {'pid': pid, 'start': proc_start(pid, now) or now, 'built': {}, 'queued': set(), 'scan_at': -1e9}

    def save_build(self, pkg, secs):
        self.build_hist[pkg] = (self.build_hist.get(pkg, []) + [round(secs)])[-5:]
        try:
            os.makedirs(os.path.dirname(self.builds_path), exist_ok=True)
            tmp = self.builds_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.build_hist, f)
            os.replace(tmp, self.builds_path)
        except OSError:
            pass

    def build_estimate(self, pkg):
        h = sorted(self.build_hist.get(pkg) or [])
        return h[len(h) // 2] if h else None

    def aur_progress(self, now, builds):
        b, q = self.build, self.queue
        out = {'state': 'aur' if b else 'prep', 'pkg': b['pkg'] if b else None,
               'elapsed': now - (q['start'] if q else b['start'] if b else now)}
        if b:
            if builds and (now - b.get('stage_at', -1e9) >= 3 or b.get('stage_pid') != builds[0][0]):  # walks /proc: ~15 ms
                b['stage_pid'] = builds[0][0]
                b['stage_at'], b['stage'] = now, build_stage(builds[0][1], descendants(builds[0][0]))
            out['stage'], out['stage_detail'] = b.get('stage') or (None, None)
            out['build_s'] = now - b['start']
            est = self.build_estimate(b['pkg']) if b['real'] else None
            if b['real'] and b['ninja'] is None and now - b['ninja_at'] > 30:  # cmake writes it a bit later
                b['ninja_at'] = now

                def find(b=b):
                    b['ninja'] = ninja_find(b['dir']) or None
                self.spawn(find)
            nj = b['ninja']
            done = ninja_done(nj[0]) if nj else None
            if done is not None:
                # real progress: ninja's finished steps, ETA from the pace over the last few minutes
                b['steps'].append((now, done))
                t0, d0 = b['steps'][0]
                rate = (done - d0) / (now - t0) if now - t0 >= 60 else 0  # the first steps are cheap codegen
                out['pkg_pct'] = min(99.0, 100 * done / nj[1])
                out['steps'] = (min(done, nj[1]), nj[1])
                if rate > 0:
                    out['pkg_eta'] = max(0.0, nj[1] - done) / rate
                elif est:
                    out['pkg_eta'] = max(0.0, est - out['build_s'])
            elif est:
                out['pkg_pct'] = min(99.0, 100 * out['build_s'] / est)
                out['pkg_eta'] = max(0.0, est - out['build_s'])
        if q:
            # done: built in this run, or installed from the cache (yay reuses a package built earlier)
            built = dict(q['built'])
            for ts, name in self.state.aur_installs:
                if ts >= q['start']:
                    built.setdefault(name, ts)
            cur = b['pkg'] if b and b['pkg'] not in built else None
            left = sorted(q['queued'] - set(built) - {cur})
            done = len(built)
            total = done + len(left) + (1 if cur else 0) if (q['queued'] or built or cur) else None
            out.update(done=done, total=total, queue_left=left)
            # time per package in this run: gaps between consecutive finished builds
            ends = sorted([q['start']] + list(built.values()))
            gaps = [t1 - t0 for t0, t1 in zip(ends, ends[1:]) if t1 > t0]
            avg = sum(gaps) / len(gaps) if gaps else None
            out['avg_build'] = avg
            if total:
                cur_eta = out.get('pkg_eta') if cur else 0.0
                if cur_eta is None and cur and avg:
                    cur_eta = max(0.0, avg - out.get('build_s', 0))
                per = [self.build_estimate(n) or avg for n in left]
                if cur_eta is not None and all(per):
                    out['eta'] = cur_eta + sum(per)
                frac = (out.get('pkg_pct') or 0) / 100 if cur else 0
                out['pct'] = min(99.0, 100 * (done + frac) / total)
        return out

    def reset(self):
        self.pid, self.targets, self.targets_for, self.rx0, self.max_pct = None, None, None, None, 0.0
        self.dl_hist.clear()

    def idle_info(self, now):
        st = self.state
        boot = now - float((sysfs.read('/proc/uptime', '0') or '0').split()[0])
        today = datetime.fromtimestamp(now).date()
        return {'state': 'idle', 'last': st.last(), 'pending': self.pending,
                'reboot': reboot_needed(self.release, st.upgraded_at, boot),
                'pacnew': [p for p in st.pacnew if sysfs.exists(p)],
                'daily': [st.daily.get(datetime.fromordinal(today.toordinal() - i).date(), 0) for i in range(29, -1, -1)]}

    def pacman_progress(self, now, pid, cmd):
        st, run = self.state, self.state.run
        started = proc_start(pid, now)
        if run is not None and started is not None and run['start'] < started - 5:
            run = None  # the log's last run is an older pacman; this one has not logged yet
        key = (pid, run['start'] if run else None)
        if self.pid != pid:
            self.reset()
            self.pid = pid
        if self.targets_for != key and run is not None and (run['syu'] or 'u' not in cmd['flags'] or run['tx_start']):
            self.targets_for, self.targets = key, None
            self.load_targets(cmd, key)
        tg = self.targets or {}
        total = len(tg)
        out = {'elapsed': now - (run['start'] if run else started or now), 'total': total, 'done': 0,
               'warnings': run['warnings'] if run else 0, 'errors': run['errors'] if run else 0,
               'size_delta': sum(i['isize'] - i['old_isize'] for i in tg.values()) if tg else None}
        if cmd['op'] == 'R' and tg:
            out['size_delta'] = -sum(i['old_isize'] for i in tg.values())
        per_pkg = st.sec_per_pkg()
        kernel = any(KERNELS.match(n) for n in tg)
        hooks_s = st.hook_secs(kernel)

        # downloads
        dl_total = dl_done = 0
        pending, done_files = [], 0
        for n, i in tg.items():
            if n in self.cached0 or not i['csize']:
                continue
            dl_total += i['csize']
            got = self.cache_size(i)
            if got is None:
                pending.append((n, i))
            else:
                dl_done += i['csize']
                done_files += 1
        parts = self.parts()
        rx = self.rx_bytes()
        if self.rx0 is None and rx is not None:
            self.rx0 = rx - dl_done
        pkg = pkg_pct = None
        exact = False
        if pending and parts:
            cand = [(n, i, parts[i['file']]) for n, i in pending if i['file'] in parts]
            if cand:
                n, i, got = max(cand, key=lambda c: c[1]['csize'])
                inflight = sum(c[2] for c in cand)
                pkg, pkg_pct, exact = n, 100 * got / max(1, i['csize']), True
        if not exact:
            inflight = max(0, min((rx - self.rx0 - dl_done) if rx is not None and self.rx0 is not None else 0,
                                  dl_total - dl_done))
            if pending:  # one download at a time: the smallest package still bigger than the bytes in flight
                bigger = [(n, i) for n, i in pending if i['csize'] > inflight] or pending
                n, i = min(bigger, key=lambda c: c[1]['csize'])
                pkg, pkg_pct = '~' + n, min(99.0, 100 * inflight / max(1, i['csize']))
        got_bytes = dl_done + inflight
        self.dl_hist.append((now, got_bytes))
        t0, b0 = self.dl_hist[0]
        speed = (got_bytes - b0) / (now - t0) if now > t0 else 0.0
        out.update(dl_bytes=got_bytes, dl_total=dl_total, dl_files=(done_files, done_files + len(pending)), speed=speed)

        install_left = total * per_pkg
        if run is None or (not run['tx_start'] and not tg):
            out['state'] = 'sync'
            eta = None
        elif not run['tx_start']:
            left = dl_total - got_bytes
            dl_eta = left / speed if speed > 0 else None
            out['state'] = 'download' if pending else 'verify'
            if pending:
                cur = tg.get(pkg.lstrip('~')) if pkg else None
                out['pkg_eta'] = (cur['csize'] * (1 - pkg_pct / 100) / speed) if cur and speed > 0 else None
            if not pending:
                eta = install_left + hooks_s
            else:
                eta = dl_eta + install_left + hooks_s if dl_eta is not None else None
        elif not run['tx_end']:
            done = run['done']
            if done >= 3:
                per_pkg = (run['last_ts'] - run['tx_start']) / done or per_pkg
            out.update(state='install', done=min(done, total) if total else done)
            since = now - run['last_ts'] if done else now - run['tx_start']
            pkg_pct = min(99.0, 100 * since / max(per_pkg, 0.1))
            pkg = f'~ {done + 1}/{total}' if total else '~'
            out['last_pkg'] = run['last_pkg'] if done else None
            out['pkg_eta'] = max(0.0, per_pkg - since)
            eta = max(0, total - done) * per_pkg - min(since, per_pkg) + hooks_s
        else:
            out.update(state='hooks', done=run['done'], hook=run['hook'],
                       hook_s=now - run['hook_ts'] if run['hook_ts'] else 0.0, last_pkg=run['last_pkg'])
            pkg = run['hook']
            pkg_pct = None
            eta = max(1.0, hooks_s - (now - run['tx_end']))
        out['pkg'], out['pkg_pct'] = pkg, pkg_pct
        out['eta'] = None if eta is None else max(0.0, eta)
        if eta is not None and out['elapsed'] > 0:
            self.max_pct = max(self.max_pct, min(99.0, 100 * out['elapsed'] / (out['elapsed'] + eta)))
        out['pct'] = self.max_pct
        return out
