"""Configuration: ~/.config/piwnica-dashboard/config.toml, written on first start.

Everything defaults to "auto" and is detected at start-up; the file only exists so that
a user can override a guess. ``piwnica-dashboard detect`` prints what was detected.
"""
import copy
import os
import subprocess
import tomllib

DEFAULTS = {
    'display': {'monitor': 'auto', 'traces_on_other_monitors': True, 'tint': 0.45, 'fps': 30},
    'holdout': {'path': 'auto'},
    'network': {'interface': 'auto'},
    'gpu': {'backend': 'auto'},
    'temps': {'cpu_limit': 0, 'gpu_limit': 0, 'nvme_limit': 0},
}

TEMPLATE = """\
# PiwnicaStyleDashboard -- every value can stay "auto" (detected at start-up).
# `piwnica-dashboard detect` shows what was detected on this machine.

[display]
# Monitor for the dashboard: "auto" = first portrait monitor, else the primary one;
# or a connector name as reported by xrandr, e.g. "DP-1".
monitor = "auto"
# Animated PCB traces (without panels) on the other monitors too.
traces_on_other_monitors = true
# Darkening of the wallpaper under the traces, 0..1.
tint = 0.45
fps = 30

[holdout]
# Mask that keeps the animation and the panels off a character on the wallpaper:
# a PNG of the monitor's exact size, alpha > 0 where the character is.
# "auto" = holdout-<W>x<H>.png next to the monitor's XFCE wallpaper;
# a directory = holdout-<W>x<H>.png inside it; a file; or "" for none.
path = "auto"

[network]
# "auto" = the interface with the default route.
interface = "auto"

[gpu]
# "auto", "intel", "amd" or "nvidia".
backend = "auto"

[temps]
# Temperature limits in C, 0 = automatic (hardware-reported or vendor spec).
# AMD CPUs do not report TjMax: 90 C is assumed -- check "Max. Operating Temperature"
# on your CPU's product page and set cpu_limit if it differs.
cpu_limit = 0
gpu_limit = 0
nvme_limit = 0
"""


def config_path():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'piwnica-dashboard', 'config.toml')


def load(path=None, create=True):
    path = path or config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if not os.path.exists(path):
        if create:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(TEMPLATE)
        return cfg
    with open(path, 'rb') as f:
        user = tomllib.load(f)
    for section, values in user.items():
        if isinstance(values, dict):
            cfg.setdefault(section, {}).update(values)
    return cfg


def pick_monitor(monitors, wanted='auto'):
    """monitors: [{'name': 'DP-1', 'w': 1200, 'h': 1920, 'primary': False}, ...] -> index.
    A named monitor wins; "auto" = first portrait, else primary, else the first."""
    if not monitors:
        return None
    if wanted not in ('auto', '', None):
        for i, m in enumerate(monitors):
            if m['name'] == wanted:
                return i
    for i, m in enumerate(monitors):
        if m['h'] > m['w']:
            return i
    for i, m in enumerate(monitors):
        if m.get('primary'):
            return i
    return 0


def xfce_wallpaper(connector, run=subprocess.run):
    """Wallpaper file of a monitor in XFCE (xfconf), or None."""
    try:
        r = run(['xfconf-query', '-c', 'xfce4-desktop', '-p',
                 f'/backdrop/screen0/monitor{connector}/workspace0/last-image'], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    path = r.stdout.strip() if r.returncode == 0 else ''
    return path or None


def resolve_holdout(setting, connector, w, h, wallpaper_of=xfce_wallpaper):
    """Path of the holdout PNG for a monitor, or None."""
    name = f'holdout-{w}x{h}.png'
    setting = os.path.expanduser(setting or '')
    if not setting:
        return None
    if setting == 'auto':
        wall = wallpaper_of(connector) if connector else None
        cand = os.path.join(os.path.dirname(wall), name) if wall else None
        return cand if cand and os.path.isfile(cand) else None
    if os.path.isdir(setting):
        cand = os.path.join(setting, name)
        return cand if os.path.isfile(cand) else None
    return setting if os.path.isfile(setting) else None
