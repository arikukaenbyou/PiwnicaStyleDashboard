"""Is a game running? Then the effects switch off -- also while it is windowed or alt-tabbed,
which the fullscreen check alone misses.

Looks for the launchers' own markers in /proc: Steam's `reaper SteamLaunch` (native and Proton
games), Lutris' `lutris-wrapper`, gamescope, and any Windows program under Wine/Proton
(Heroic, plain wine) that is not one of Wine's own background services.
"""
import os

MARKERS = (b'SteamLaunch', b'lutris-wrapper', b'gamescope')
WINE_SERVICES = {
    'wineserver', 'wineboot.exe', 'services.exe', 'explorer.exe', 'winedevice.exe', 'plugplay.exe',
    'svchost.exe', 'rpcss.exe', 'conhost.exe', 'start.exe', 'rundll32.exe', 'tabtip.exe',
    'steam.exe', 'steamwebhelper.exe', 'steamerrorreporter.exe',
}


def game_running(proc='/proc'):
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f'{proc}/{pid}/cmdline', 'rb') as f:
                cmd = f.read()
        except OSError:
            continue
        if not cmd:
            continue
        if any(m in cmd for m in MARKERS):
            return True
        exe = cmd.split(b'\0', 1)[0].replace(b'\\', b'/').rsplit(b'/', 1)[-1].decode(errors='ignore').lower()
        if exe.endswith('.exe') and exe not in WINE_SERVICES:
            return True
    return False
