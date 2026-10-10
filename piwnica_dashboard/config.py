"""Configuration: ~/.config/piwnica-dashboard/config.toml, written on first start.

Everything defaults to "auto" and is detected at start-up; the file only exists so that
a user can override a guess. ``piwnica-dashboard detect`` prints what was detected.
"""
import copy
import os
import subprocess
import tomllib

DEFAULTS = {
    'display': {'monitor': 'auto', 'traces_on_other_monitors': True, 'tint': 0.45, 'fps': 30,
                'dashboard_in_games': True, 'bar_fx': True,
                'panels': ['pacman', 'updates', 'builds', 'proxmox', 'claude', 'nfs', 'luneta', 'now', 'forge']},
    'holdout': {'path': 'auto'},
    'network': {'interface': 'auto'},
    'gpu': {'backend': 'auto'},
    'temps': {'cpu_limit': 0, 'gpu_limit': 0, 'nvme_limit': 0},
    'infra': {'url': '', 'token': '', 'interval': 300},
    'pacman': {'enabled': True, 'pending_interval': 1800, 'aur': True},
    'nfs': {'enabled': True},
    'proxmox': {'enabled': True, 'host': '', 'token_file': '~/.config/piwnica-dashboard/proxmox.token',
                'fingerprint': '', 'node': 'auto', 'ssh_user': 'root'},
    'afterlife': {'enabled': True, 'base_url': 'https://ariku.pl', 'token_file': '~/.config/piwnica-dashboard/luneta.token'},
    'forge': {'enabled': True, 'comfyui': 'http://127.0.0.1:8188'},
    'updates': {'enabled': True, 'show_ok': False, 'focus': 'security', 'interval': 300},
    'claude': {'enabled': True, 'credentials': '~/.claude/.credentials.json', 'interval': 60},
    'builds': {'enabled': True, 'forgejo_url': 'https://git.ariku.pl',
               'token_file': '~/.config/piwnica-dashboard/forgejo.token',
               'repos': ['afterlife', 'luneta', 'companion_app'],
               'interval': 30},
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
# While a game runs (or a fullscreen window has focus) the traces on the other monitors
# switch off; the dashboard monitor keeps running -- set to false to switch it off too.
dashboard_in_games = true
# CRT look on the bars (df -h, sensors): scanlines and a scanning beam sweeping along them.
bar_fx = true
# Extra panels, most wanted first; each goes into the free space left by the ones before it and is
# skipped when nothing fits (sysmon, df -h and sensors always come first).
panels = ["pacman", "updates", "builds", "proxmox", "claude", "nfs", "luneta", "now", "forge"]

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

[infra]
# Optional: a JSON endpoint listing what needs attention in your homelab (see README).
# Empty url = off. The token is sent as "Authorization: Bearer <token>" -- keep this
# file private (chmod 600) if you set it.
url = ""
token = ""
interval = 300

[pacman]
# Panel with the progress of a running pacman / yay update (download, install, hooks, AUR builds
# with time estimates) and, between updates, the last -Syu, pending updates, reboot needed, .pacnew.
enabled = true
# Seconds between checks for pending updates (checkupdates from pacman-contrib, `yay -Qua`
# when aur = true); they use the network. 0 = never.
pending_interval = 1800
aur = true

[nfs]
# Every nfs share from /etc/fstab: usage, traffic, and whether the server answers.
enabled = true

[proxmox]
# Proxmox VE host: load, guests (and which are down), storages, NFS shares, physical disks and backup job;
# the stream line shows whether this PC is sending RTMP. For example: host = "192.168.1.100", node = "ariku".
# Needs a read-only API token, e.g. on the PVE host:
#   pveum user add dash@pve; pveum acl modify / --users dash@pve --roles PVEAuditor
#   pveum user token add dash@pve dashboard --privsep 0
# then put "dash@pve!dashboard=<secret>" into token_file (chmod 600), and the certificate's
# fingerprint (openssl s_client -connect HOST:8006 | openssl x509 -noout -fingerprint -sha256).
# Disk SMART and guest package/Docker inventory require SSH access (default root) to the PVE node.
# The scan runs read-only every 30 min using pct and QEMU Guest Agent; it installs nothing.
enabled = true
host = ""
token_file = "~/.config/piwnica-dashboard/proxmox.token"
fingerprint = ""
node = "auto"
ssh_user = "root"

[afterlife]
# Luneta: game resets, dailies, events and the recording analysis, read from the local Luneta agent.
# With a device token from ariku.pl/luneta in token_file the states refresh from the server and
# the "co teraz" panel (companion /api/companion/now) works.
enabled = true
base_url = "https://ariku.pl"
token_file = "~/.config/piwnica-dashboard/luneta.token"

[forge]
# Kuźnia on this PC: ComfyUI queue and the gpu-softstart clock cap.
enabled = true
comfyui = "http://127.0.0.1:8188"

[updates]
# What in the homelab needs an update or an intervention (Proxmox, LXC/VM, Docker, services, router,
# PCs, IoT), as judged by the ariku.pl inventory; uses the Luneta device token from [afterlife].
enabled = true
# false = only what needs attention, true = also everything that is ok.
# Picked up while the dashboard runs, no restart needed.
show_ok = false
# "security" = what an attacker could use: security updates, known-vulnerable versions, outages, the
# Proxmox host, the router, network services and community-scripts apps; routine updates (IoT firmware,
# game-server images, HA add-ons, plain package updates in guests) are only counted.
# "all" = every item. Also picked up without a restart.
focus = "security"
# Seconds between refreshes.
interval = 300

[builds]
# Forgejo CI builds (git.ariku.pl): monitor long-running builds (afterlife, luneta, companion_app).
# Needs a personal access token from git.ariku.pl (Settings → Applications → Generate Token)
# in token_file (chmod 600).
enabled = true
forgejo_url = "https://git.ariku.pl"
token_file = "~/.config/piwnica-dashboard/forgejo.token"
repos = ["afterlife", "luneta", "companion_app"]
interval = 30

[claude]
# Claude plan limits (5h session, week, extra usage), the numbers Claude Code's /usage shows.
# Reads Claude Code's OAuth token from `credentials` (read-only, never refreshed here) and asks
# api.anthropic.com/api/oauth/usage -- an undocumented endpoint, the panel says so when it changes.
enabled = true
credentials = "~/.claude/.credentials.json"
# Seconds between refreshes.
interval = 60
"""


def config_path():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'piwnica-dashboard', 'config.toml')


def load(path=None, create=True):
    path = path or config_path()
    cfg = copy.deepcopy(DEFAULTS)
    cfg['_path'] = path  # for switches re-read while running (updates.show_ok)
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


def read_value(path, section, key, default=None):
    """One value from the config file as it is now (for switches that work without a restart)."""
    try:
        with open(path, 'rb') as f:
            return tomllib.load(f).get(section, {}).get(key, default)
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return default


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
