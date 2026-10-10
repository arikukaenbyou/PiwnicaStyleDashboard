"""piwnica-dashboard run | detect | screenshot | inventory | updates"""
import argparse
import os
import sys

from . import __version__
from . import config as config_mod


def cmd_detect(cfg):
    from .collect import gpu as gpu_mod
    from .collect.system import SystemCollector
    from .collect.temps import find_sensors
    sysc = SystemCollector(iface=cfg['network']['interface'])
    d = sysc.sample(0.0)
    g = gpu_mod.detect(cfg['gpu']['backend'])
    print(f'config:    {config_mod.config_path()}')
    try:
        from .app import monitors
        mons = monitors()
        i = config_mod.pick_monitor(mons, cfg['display']['monitor'])
        for j, m in enumerate(mons):
            hold = config_mod.resolve_holdout(cfg['holdout']['path'], m['name'], m['w'], m['h'])
            print(f'monitor:   {m["name"]} {m["w"]}x{m["h"]}{"  <- dashboard" if j == i else ""}'
                  f'{"  holdout " + hold if hold else ""}')
    except Exception as e:  # noqa: BLE001 -- detect must work without a display too
        print(f'monitor:   (no display: {e})')
    print(f'gpu:       {g.vendor + " / " + g.name if g else "none supported"}')
    print(f'network:   {d["iface"] or "none"}')
    for dk in d['disks']:
        print(f'disk:      {dk["name"]} {dk["model"]} {" ".join(dk["mounts"])}')
    for s in find_sensors(cfg['temps'], {dk['name']: dk['model'] for dk in d['disks']}):
        print(f'sensor:    {s.label:<16} limit {s.limit:g} C  (yellow from {s.yellow:g}, red from {s.red:g})')
    if cfg['pacman'].get('enabled', True):
        import shutil

        from .collect.pacman import PacmanCollector
        pc = PacmanCollector(pending_interval=0, background=False)
        last = pc.state.last()
        tools = ', '.join(t for t in ('checkupdates', 'yay') if shutil.which(t)) or 'none'
        print(f'pacman:    {len(pc.state.per_pkg)} past runs in the log, '
              f'{pc.state.sec_per_pkg():.2f} s/package, last -Syu {last["pkgs"] if last else 0} packages; '
              f'pending via: {tools}')
    if cfg.get('claude', {}).get('enabled', True):
        from .collect.claude import read_credentials
        cr = read_credentials(cfg.get('claude', {}).get('credentials', '~/.claude/.credentials.json'))
        print(f'claude:    {"login found, plan " + str(cr["plan"]) if cr else "no Claude Code login"}')
    if cfg.get('builds', {}).get('enabled', True):
        from .collect.builds import read_token
        bu = cfg.get('builds', {})
        tok = read_token(bu.get('token_file', '~/.config/piwnica-dashboard/forgejo.token'))
        repos = ', '.join(bu.get('repos', ['afterlife', 'luneta', 'companion_app']))
        print(f'builds:    {bu.get("forgejo_url", "https://git.ariku.pl")}, repos: {repos}; token: {"ok" if tok else "missing"}')


def cmd_screenshot(args, cfg):
    import cairo
    from .collector import DemoCollector
    from .scene import Scene, load_holdout
    w, h = (int(x) for x in args.size.split('x'))
    holdout = load_holdout(args.holdout, w, h)
    scene = Scene(w, h, seed=args.seed, tint=float(cfg['display']['tint']), holdout=holdout, panels=cfg['display'].get('panels'),
                  collector=DemoCollector() if args.demo else None)
    for _ in range(args.frames):  # let a few pulses appear
        scene.tick()
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(out)
    if args.wallpaper:
        wall = cairo.ImageSurface.create_from_png(args.wallpaper)
        cr.scale(w / wall.get_width(), h / wall.get_height())
        cr.set_source_surface(wall, 0, 0)
        cr.paint()
        cr.identity_matrix()
    layer = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    scene.draw(cairo.Context(layer))
    cr.set_source_surface(layer, 0, 0)
    cr.paint()
    out.write_to_png(args.out)
    print(f'wrote {args.out}' + (f', panels: {scene.dash.rects}' if scene.dash else ''))


def cmd_inventory(cfg, as_json=False):
    import json
    from .collect.proxmox import ProxmoxCollector

    px = cfg.get('proxmox', {})
    if not px.get('enabled', True) or not px.get('host'):
        print('Proxmox is not configured; set [proxmox].host in the dashboard config.')
        return 1
    collector = ProxmoxCollector(
        px['host'], px.get('token_file', ''), px.get('fingerprint', ''), px.get('node', 'auto'),
        background=False, ssh_user=px.get('ssh_user', 'root'),
    )
    data = collector.sample()
    if data.get('error') == 'no token':
        print('Proxmox API token is missing or invalid.')
        return 1
    if as_json:
        print(json.dumps(data, indent=2, default=list))
        return 0

    print(f'Proxmox node: {data.get("node") or "unavailable"}')
    if data.get('error'):
        print(f'  API error: {data["error"]}')
    print('\nPhysical disks / SMART')
    for disk in data.get('disks') or []:
        smart = data.get('smart', {}).get(disk['name']) or {}
        facts = [smart.get('health') or 'SMART unavailable']
        for label, key, suffix in (('temp', 'temperature', 'C'), ('hours', 'hours', 'h'),
                                   ('cycles', 'cycles', ''), ('wear', 'wear', '%'),
                                   ('reallocated', 'reallocated', ''), ('pending', 'pending', ''),
                                   ('uncorrectable', 'uncorrectable', ''), ('media errors', 'media_errors', ''),
                                   ('error log', 'error_log_entries', '')):
            if smart.get(key) is not None:
                facts.append(f'{label} {smart[key]}{suffix}')
        print(f'  {disk["name"]:<14} {disk["model"]:<28} {disk["size"] / 2**40:.2f} TiB  '
              f'{disk.get("used", "unknown")}  ' + ' · '.join(facts))
    if data.get('disk_error'):
        print(f'  SMART/disk API error: {data["disk_error"]}')

    print('\nLXC / VM application inventory')
    for guest in data.get('inventory') or []:
        kind = guest['type'].upper()
        state = guest.get('status') or 'unknown'
        print(f'  {kind} {guest["id"]} {guest["name"]} — {state}')
        if guest.get('os'):
            print(f'    OS: {guest["os"]}')
        if guest.get('error'):
            print(f'    Scan: {guest["error"]}')
            continue
        for app in guest.get('app_versions') or []:
            print(f'    app: {app}')
        for package in guest.get('packages') or []:
            print(f'    pkg: {package}')
        updates = guest.get('updates') or []
        print(f'    cached package updates: {len(updates)}')
        for update in updates:
            print(f'      update: {update}')
        if guest.get('docker_error'):
            print(f'    Docker: {guest["docker_error"]}')
        for container in guest.get('docker') or []:
            print(f'    Docker: {container["name"]:<24} {container["image"]:<40} {container["status"]}')
    if data.get('inventory_error'):
        print(f'\nGuest scan error: {data["inventory_error"]}')
    print('\nUpdate checks use package-manager metadata already cached in each guest; '
          'Docker output reports local image tags and does not contact registries.')
    return 0


def cmd_updates(cfg, show_all=False, as_json=False):
    import json
    from .collect.updates import UpdatesCollector

    al = cfg.get('afterlife', {})
    up = cfg.get('updates', {})
    show_ok = show_all or bool(up.get('show_ok', False))
    focus = 'all' if show_all else up.get('focus', 'security')
    data = UpdatesCollector(al.get('base_url', 'https://ariku.pl'), al.get('token_file', '~/.config/piwnica-dashboard/luneta.token'),
                            show_ok, background=False, focus=focus).sample()
    if data.get('error') == 'no token':
        print('Luneta device token missing: add a device on ariku.pl/luneta and save its token to '
              f'{al.get("token_file", "~/.config/piwnica-dashboard/luneta.token")}')
        return 1
    if 'items' not in data:
        print(f'inventory unavailable: {data.get("error")}')
        return 1
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    c = data['counts']
    print(' · '.join(f'{s} {c[s]}' for s in c) + f'  ({data["total"]} items)')
    for it in data['items']:
        ver = it.get('version') or ''
        if it.get('latestVersion') and it['latestVersion'] != ver:
            ver = f'{ver} → {it["latestVersion"]}'.strip()
        print(f'  {it["status"]:<9} {it.get("kind") or "?":<9} {it.get("name") or it.get("id"):<28} '
              f'{ver:<24} {it.get("reason") or ""}'.rstrip())
    if data.get('hidden_routine'):
        print(f'  … {data["hidden_routine"]} routine updates hidden (IoT, game servers, add-ons, guest packages without '
              'security fixes; --all, or [updates] focus = "all")')
    if data['hidden_ok']:
        print(f'  … {data["hidden_ok"]} ok hidden (--all, or [updates] show_ok = true)')
    return 0


def cmd_claude(cfg, as_json=False):
    import json
    import time
    from .collect.claude import ClaudeCollector
    from .draw import fmt_left

    cl = cfg.get('claude', {})
    data = ClaudeCollector(cl.get('credentials', '~/.claude/.credentials.json'), background=False).sample()
    if 'limits' not in data:
        print(f'Claude limits unavailable: {data.get("error")}')
        return 1
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print(f'Claude{" " + data["plan"] if data.get("plan") else ""} limits:')
    now = time.time()
    for lim in data['limits']:
        reset = f'  resets in {fmt_left(lim["resets_at"] - now)}' if lim.get('resets_at') else ''
        print(f'  {lim["label"]:<12} {lim["pct"]:>4.0f}%{reset}' + (f'  ({lim["severity"]})' if lim['severity'] != 'normal' else ''))
    if data.get('extra'):
        print(f'  extra usage  {data["extra"]["pct"] or 0:>4.0f}%')
    if data.get('breakdown'):
        print('  week by product: ' + ', '.join(f'{r["name"]} {r["pct"]:.0f}%' for r in data['breakdown']))
    return 0


def cmd_builds(cfg, as_json=False):
    import json
    from .collect.builds import BuildsCollector

    bu = cfg.get('builds', {})
    data = BuildsCollector(
        bu.get('forgejo_url', 'https://git.ariku.pl'),
        bu.get('token_file', '~/.config/piwnica-dashboard/forgejo.token'),
        bu.get('repos', ['afterlife', 'luneta', 'companion_app']),
        background=False,
    ).sample()
    if data.get('error') == 'no token':
        print('Forgejo token missing: create one on git.ariku.pl (Settings → Applications) and save to '
              f'{bu.get("token_file", "~/.config/piwnica-dashboard/forgejo.token")}')
        return 1
    if 'items' not in data:
        print(f'builds unavailable: {data.get("error")}')
        return 1
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    active = data.get('active', 0)
    print(f'Forgejo CI builds (git.ariku.pl): {active} active / {data.get("total", 0)} monitored')
    for it in data['items']:
        dur = f' {it["duration"] / 60:.1f}m' if it.get('duration') else (f' {it["elapsed"] / 60:.1f}m' if it.get('elapsed') else '')
        wf = it.get('workflow') or ''
        job = f' ({it["job"]})' if it.get('job') else ''
        q = f' (+{it["queued_runs"]} queued)' if it.get('queued_runs') else ''
        print(f'  {it["status"].upper():<9} {it["name"]:<22} {it.get("version") or "?":<10} '
              f'{wf}{job}{dur}{q}'.rstrip())
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='piwnica-dashboard', description='Animated PCB desktop layer with a system dashboard.')
    ap.add_argument('--version', action='version', version=__version__)
    ap.add_argument('--config', help='config file (default: ~/.config/piwnica-dashboard/config.toml)')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('run', help='start the desktop layer (default)')
    sub.add_parser('detect', help='show detected monitors, GPU, network, disks and sensors')
    inv = sub.add_parser('inventory', help='show Proxmox disk SMART, guest packages and Docker image tags')
    inv.add_argument('--json', action='store_true', help='print raw inventory as JSON')
    upd = sub.add_parser('updates', help='what in the homelab needs an update or an intervention (ariku.pl inventory)')
    upd.add_argument('--all', action='store_true', help='also list everything that is ok')
    upd.add_argument('--json', action='store_true', help='print the view as JSON')
    cla = sub.add_parser('claude', help='Claude plan limits (5h session, week), as /usage shows them')
    cla.add_argument('--json', action='store_true', help='print the limits as JSON')
    bld = sub.add_parser('builds', help='Forgejo Actions build progress and versions')
    bld.add_argument('--json', action='store_true', help='print the builds view as JSON')
    sc = sub.add_parser('screenshot', help='render one frame to a PNG (no window needed)')
    sc.add_argument('out')
    sc.add_argument('--size', default='1200x1920')
    sc.add_argument('--demo', action='store_true', help='dashboard with fake data (no real machine data)')
    sc.add_argument('--holdout')
    sc.add_argument('--wallpaper')
    sc.add_argument('--frames', type=int, default=120)
    sc.add_argument('--seed', type=int, default=1002)
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config, create=args.cmd in (None, 'run'))
    if args.cmd == 'detect':
        return cmd_detect(cfg)
    if args.cmd == 'screenshot':
        return cmd_screenshot(args, cfg)
    if args.cmd == 'inventory':
        return cmd_inventory(cfg, args.json)
    if args.cmd == 'updates':
        return cmd_updates(cfg, args.all, args.json)
    if args.cmd == 'claude':
        return cmd_claude(cfg, args.json)
    if args.cmd == 'builds':
        return cmd_builds(cfg, args.json)
    try:
        os.nice(10)
    except OSError:
        pass
    from .app import run
    return run(cfg)


if __name__ == '__main__':
    sys.exit(main())
