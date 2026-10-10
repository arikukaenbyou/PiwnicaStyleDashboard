#!/usr/bin/env python3
"""Safely update the Proxmox host and running APT-based LXC guests.

The host gets `apt-get dist-upgrade` (Proxmox VE needs it: `upgrade` keeps new kernels and packages with
new dependencies back and leaves the node half-upgraded); guests get `apt-get upgrade`, which never removes
anything. Security updates are listed first. Apps installed by community-scripts (`/usr/bin/update`) are
only listed with their command: that update runs `curl | bash` from GitHub as root, so it is the owner's call.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib


REMOTE_SCRIPT = r'''
import json
import os
import re
import subprocess
import sys


def run(args, timeout=900):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, '', f'{type(error).__name__}: {error}'
    return result.returncode, result.stdout, result.stderr


def detail(stderr, stdout, code):
    return (stderr or stdout or f'command exited {code}').strip()[-1600:]


# wait for apt-daily instead of failing on the lock; keep changed config files without a prompt (no tty here)
LOCK = ['-o', 'DPkg::Lock::Timeout=300']
CONFFILES = ['-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold']
PROTECTED = ('proxmox-ve', 'pve-manager')


def simulation(out):
    """Inst/Remv lines and the kept-back packages from `apt-get -s ...` output."""
    lines, kept, in_kept = [], [], False
    for line in out.splitlines():
        if line.startswith(('Inst ', 'Remv ')):
            lines.append(line)
        if line.startswith('The following packages have been kept back'):
            in_kept = True
        elif in_kept and line.startswith(' '):
            kept.extend(line.split())
        else:
            in_kept = False
    return lines, kept


def removes_protected(lines):
    return [l.split()[1] for l in lines if l.startswith('Remv ') and len(l.split()) > 1 and l.split()[1] in PROTECTED]


def community_script(prefix):
    """The community-scripts app of a guest from its /usr/bin/update, e.g. 'forgejo-runner', or None."""
    rc, out, _ = run(prefix + ['cat', '/usr/bin/update'], 30)
    match = re.search(r'ProxmoxVE/main/ct/([a-z0-9][a-z0-9-]*)\.sh', out) if rc == 0 else None
    return match.group(1) if match else None


def plan_target(key, name, update_command, simulation_command):
    rc, out, err = run(update_command)
    if rc:
        return {'key': key, 'name': name, 'lines': [], 'kept_back': [],
                'error': f'apt-get update failed: {detail(err, out, rc)}'}
    rc, out, err = run(simulation_command)
    if rc:
        return {'key': key, 'name': name, 'lines': [], 'kept_back': [],
                'error': f'apt simulation failed: {detail(err, out, rc)}'}
    lines, kept = simulation(out)
    return {'key': key, 'name': name, 'lines': lines, 'kept_back': kept, 'error': None}


def guests():
    rc, out, err = run(['pvesh', 'get', '/cluster/resources', '--output-format', 'json'], 60)
    if rc:
        raise RuntimeError(detail(err, out, rc))
    resources = json.loads(out)
    return [item for item in resources
            if item.get('type') in ('lxc', 'qemu') and not item.get('template')]


def main():
    if os.geteuid() != 0:
        raise SystemExit('SSH user must be root on the Proxmox node.')
    resources = guests()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'plan'

    if mode == 'plan':
        targets = [plan_target('host', 'Proxmox host',
                               ['apt-get', *LOCK, 'update'],
                               ['apt-get', *LOCK, '-s', 'dist-upgrade'])]
        skipped, apps = [], []
        for item in resources:
            kind, vmid = item.get('type'), item.get('vmid')
            name = item.get('name') or str(vmid)
            key = f'lxc:{vmid}'
            if item.get('status') != 'running':
                skipped.append({'name': f'{kind.upper()} {vmid} {name}',
                                'reason': 'stopped'})
                continue
            if kind == 'qemu':
                skipped.append({'name': f'VM {vmid} {name}',
                                'reason': 'QEMU guest update not enabled'})
                continue
            probe = ['pct', 'exec', str(vmid), '--', 'sh', '-lc',
                     'command -v apt-get >/dev/null 2>&1']
            rc, out, err = run(probe, 30)
            if rc:
                skipped.append({'name': f'LXC {vmid} {name}',
                                'reason': 'APT is unavailable'})
                continue
            targets.append(plan_target(
                key, f'LXC {vmid} {name}',
                ['pct', 'exec', str(vmid), '--', 'apt-get', *LOCK, 'update'],
                ['pct', 'exec', str(vmid), '--', 'apt-get', *LOCK, '-s', 'upgrade']))
            slug = community_script(['pct', 'exec', str(vmid), '--'])
            if slug:
                apps.append({'vmid': vmid, 'name': name, 'app': slug})
        print(json.dumps({'targets': targets, 'skipped': skipped, 'apps': apps}))
        return

    if mode == 'apply':
        selected = sys.argv[2:]
        if not selected or any(key != 'host' and not re.fullmatch(r'lxc:\d+', key)
                               for key in selected):
            raise SystemExit('Invalid or empty update target list.')
        current = guests()
        available_lxc = {
            f'lxc:{item["vmid"]}': item for item in current
            if item.get('type') == 'lxc' and item.get('status') == 'running'
        }
        results = []
        for key in selected:
            if key == 'host':
                name = 'Proxmox host'
                rc, out, err = run(['apt-get', *LOCK, '-s', 'dist-upgrade'], 300)
                blocked = removes_protected(simulation(out)[0]) if rc == 0 else []
                if rc or blocked:
                    results.append({'key': key, 'name': name, 'error': (
                        f'dist-upgrade would remove {", ".join(blocked)}; skipped, check the repositories'
                        if blocked else f'apt simulation failed: {detail(err, out, rc)}')})
                    continue
                command = ['env', 'DEBIAN_FRONTEND=noninteractive',
                           'apt-get', *LOCK, *CONFFILES, 'dist-upgrade', '-y']
            else:
                item = available_lxc.get(key)
                if item is None:
                    results.append({'key': key, 'name': key,
                                    'error': 'LXC is no longer running; skipped'})
                    continue
                rc, out, err = run(
                    ['pct', 'exec', str(item['vmid']), '--', 'sh', '-lc',
                     'command -v apt-get >/dev/null 2>&1'], 30)
                if rc:
                    results.append({'key': key, 'name': key,
                                    'error': 'APT is no longer available; skipped'})
                    continue
                command = ['pct', 'exec', str(item['vmid']), '--', 'env',
                           'DEBIAN_FRONTEND=noninteractive', 'apt-get', *LOCK, *CONFFILES, 'upgrade', '-y']
                name = f'LXC {item["vmid"]} {item.get("name") or ""}'.strip()
            rc, out, err = run(command, 3600)
            result = {'key': key, 'name': name, 'error': None if rc == 0
                      else detail(err, out, rc)}
            if rc == 0:
                if key == 'host':
                    result['reboot_required'] = os.path.exists('/var/run/reboot-required')
                else:
                    check = ['pct', 'exec', key.split(':', 1)[1], '--',
                             'test', '-e', '/var/run/reboot-required']
                    check_rc, _, _ = run(check, 30)
                    result['reboot_required'] = check_rc == 0
            results.append(result)
        print(json.dumps({'results': results}))
        return

    raise SystemExit('Mode must be plan or apply.')


try:
    main()
except (RuntimeError, ValueError, KeyError) as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
'''


def config_path(argument):
    if argument:
        return os.path.expanduser(argument)
    config_home = os.environ.get('XDG_CONFIG_HOME', '~/.config')
    return os.path.join(os.path.expanduser(config_home), 'piwnica-dashboard', 'config.toml')


def load_target(path):
    try:
        with open(path, 'rb') as config_file:
            config = tomllib.load(config_file)
    except OSError as error:
        raise ValueError(f'Cannot read dashboard config {path}: {error}') from error
    proxmox = config.get('proxmox') or {}
    host = proxmox.get('host')
    user = proxmox.get('ssh_user', 'root')
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise ValueError(f'No valid [proxmox].host in {path}.')
    if not isinstance(user, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', user):
        raise ValueError(f'Invalid [proxmox].ssh_user in {path}.')
    return host, user


def ssh_call(host, user, mode, targets=()):
    command = [
        'ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=4',
        '-o', 'StrictHostKeyChecking=yes', f'{user}@{host}', 'python3', '-', mode,
        *targets,
    ]
    # no overall timeout: every remote command has its own, and a dead link ends via ServerAlive
    try:
        result = subprocess.run(command, input=REMOTE_SCRIPT, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f'SSH failed: {error}') from error
    if result.returncode:
        detail = (result.stderr or result.stdout or
                  f'SSH command exited {result.returncode}').strip()
        raise RuntimeError(detail)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError('Proxmox returned invalid JSON; see SSH/Python setup.') from error


def parse_simulation(lines):
    """{'packages', 'security', 'removed'} from the Inst/Remv lines of an apt simulation."""
    packages, security, removed = [], [], []
    for line in lines:
        match = re.match(r'^(Inst|Remv)\s+(\S+)(.*)$', line)
        if not match:
            continue
        if match.group(1) == 'Remv':
            removed.append(match.group(2))
            continue
        packages.append(match.group(2))
        # Inst libssl3 [3.0.15-1~deb12u1] (3.0.17-1~deb12u2 Debian-Security:12/stable-security [amd64])
        if re.search(r'\([^)]*-security\b', match.group(3)):
            security.append(match.group(2))
    return {'packages': packages, 'security': security, 'removed': removed}


def summarize(plan):
    """The plan's targets with parsed packages, most security updates first (the host first on a tie)."""
    targets = [dict(t, **parse_simulation(t.get('lines') or [])) for t in plan.get('targets') or []]
    return sorted(targets, key=lambda t: (-len(t['security']), t.get('key') != 'host', t.get('name') or ''))


def print_plan(targets, plan):
    print('Pakiety do aktualizacji (host: dist-upgrade, LXC: upgrade bez usuwania pakietów):')
    for target in targets:
        packages, security = target['packages'], set(target['security'])
        if target.get('error'):
            print(f'  BŁĄD  {target["name"]}: {target["error"]}')
            continue
        if not packages and not target['removed']:
            print(f'  OK    {target["name"]}: brak aktualizacji')
            continue
        sec = f', bezpieczeństwa: {len(security)}' if security else ''
        print(f'  {target["name"]}: {len(packages)}{sec}')
        for package in sorted(packages, key=lambda p: (p not in security, p)):
            print(f'    {"!" if package in security else "-"} {package}')
        for package in target['removed']:
            print(f'    x {package} (zostanie usunięty)')
        if target.get('kept_back'):
            print(f'    wstrzymane (nowe zależności, nie zostaną zainstalowane): {" ".join(target["kept_back"])}')
    skipped = plan.get('skipped') or []
    if skipped:
        print('Pominięte:')
        for item in skipped:
            print(f'  {item["name"]}: {item["reason"]}')
    apps = plan.get('apps') or []
    if apps:
        print('Aplikacje z community-scripts (nie aktualizowane automatycznie: `update` wykonuje skrypt z GitHuba '
              'jako root; uruchom ręcznie na hoście):')
        for app in apps:
            print(f'  LXC {app["vmid"]} {app["name"]}: {app["app"]}  →  pct exec {app["vmid"]} -- update')
    print('Nie aktualizuje: QEMU VM, Docker, usługi, router, PC ani IoT.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='dashboard TOML config (default: ~/.config/piwnica-dashboard/config.toml)')
    args = parser.parse_args(argv)

    if not shutil.which('ssh'):
        print('Wymagane polecenie ssh nie jest dostępne.', file=sys.stderr)
        return 1
    print('Odświeżę listy pakietów APT na Proxmoxie i uruchomionych LXC; '
          'instalacja pakietów wymaga osobnego potwierdzenia.')
    try:
        host, user = load_target(config_path(args.config))
        plan = ssh_call(host, user, 'plan')
    except (RuntimeError, ValueError) as error:
        print(f'Błąd: {error}', file=sys.stderr)
        return 1

    summary = summarize(plan)
    print_plan(summary, plan)
    targets = [item['key'] for item in summary
               if (item['packages'] or item['removed']) and not item.get('error')]
    if not targets:
        return 1 if any(item.get('error') for item in plan.get('targets') or []) else 0
    try:
        answer = input('Zainstalować te aktualizacje teraz? [t/N] ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        print('\nPrzerwano — niczego nie zainstalowano.')
        return 1
    if answer not in ('t', 'tak', 'y', 'yes'):
        print('Anulowano — niczego nie zainstalowano.')
        return 0

    try:
        result = ssh_call(host, user, 'apply', targets)
    except (RuntimeError, ValueError) as error:
        print(f'Błąd aktualizacji: {error}', file=sys.stderr)
        return 1
    failed = any(item.get('error') for item in plan.get('targets') or [])
    for item in result.get('results') or []:
        if item.get('error'):
            failed = True
            print(f'BŁĄD  {item["name"]}: {item["error"]}')
        else:
            reboot = ' — wymagany reboot' if item.get('reboot_required') else ''
            print(f'OK    {item["name"]}{reboot}')
    print('Skrypt nie uruchamia ponownie hosta ani gości.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
