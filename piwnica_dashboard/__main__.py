"""piwnica-dashboard run | detect | screenshot"""
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


def cmd_screenshot(args, cfg):
    import cairo
    from .collector import DemoCollector
    from .scene import Scene, load_holdout
    w, h = (int(x) for x in args.size.split('x'))
    holdout = load_holdout(args.holdout, w, h)
    scene = Scene(w, h, seed=args.seed, tint=float(cfg['display']['tint']), holdout=holdout,
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog='piwnica-dashboard', description='Animated PCB desktop layer with a system dashboard.')
    ap.add_argument('--version', action='version', version=__version__)
    ap.add_argument('--config', help='config file (default: ~/.config/piwnica-dashboard/config.toml)')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('run', help='start the desktop layer (default)')
    sub.add_parser('detect', help='show detected monitors, GPU, network, disks and sensors')
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
    try:
        os.nice(10)
    except OSError:
        pass
    from .app import run
    return run(cfg)


if __name__ == '__main__':
    sys.exit(main())
