"""GTK side: one transparent window per monitor in the "below" layer (above the wallpaper
and desktop icons, under every normal window), click-through, paused while a fullscreen
window (a game, a video) has focus.

Click-through detail that matters: the empty input shape is set on the *widget*
(GTK keeps it and re-applies it on map/size-allocate) and re-applied after every show.
Setting it once on the GdkWindow at "realize" gets reset by GTK after a hide/show, and a
full-screen window then swallows every click on the desktop.
"""
import signal
import subprocess

import cairo
import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import config as config_mod  # noqa: E402
from .collector import Collector  # noqa: E402
from .scene import Scene, load_holdout  # noqa: E402

TITLE = 'piwnica-dashboard'


def monitors():
    """[{'name', 'w', 'h', 'x', 'y', 'primary'}] from GDK (connector names like xrandr)."""
    disp, screen = Gdk.Display.get_default(), Gdk.Screen.get_default()
    out = []
    for i in range(disp.get_n_monitors()):
        m = disp.get_monitor(i)
        g = m.get_geometry()
        out.append({'name': screen.get_monitor_plug_name(i) or f'monitor{i}', 'w': g.width, 'h': g.height,
                    'x': g.x, 'y': g.y, 'primary': m.is_primary()})
    return out


def is_fullscreen_focused():
    try:
        act = subprocess.run(['xprop', '-root', '_NET_ACTIVE_WINDOW'], capture_output=True, text=True, timeout=1).stdout
        wid = act.strip().split()[-1]
        if not wid.startswith('0x') or int(wid, 16) == 0:
            return False
        st = subprocess.run(['xprop', '-id', wid, '_NET_WM_STATE'], capture_output=True, text=True, timeout=1).stdout
        return '_NET_WM_STATE_FULLSCREEN' in st
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return False


class Board(Gtk.Window):
    def __init__(self, mon, scene):
        super().__init__(type=Gtk.WindowType.TOPLEVEL, title=TITLE)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is None or not screen.is_composited():
            raise SystemExit('piwnica-dashboard: needs a compositing window manager '
                             '(XFCE: Window Manager Tweaks > Compositor; otherwise e.g. picom)')
        self.set_visual(visual)
        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_keep_below(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()
        self.move(mon['x'], mon['y'])
        self.set_default_size(mon['w'], mon['h'])
        self.scene = scene
        self.input_shape_combine_region(cairo.Region())
        for sig in ('realize', 'map-event', 'size-allocate'):
            self.connect(sig, self.click_through)
        self.connect('draw', lambda _w, cr: self.scene.draw(cr) or False)
        self.show_all()
        self.click_through()

    def click_through(self, *_):
        self.input_shape_combine_region(cairo.Region())
        win = self.get_window()
        if win is not None:
            win.input_shape_combine_region(cairo.Region(), 0, 0)
        return False

    def invalidate(self, rects):
        for x, y, w, h in rects:
            self.queue_draw_area(x, y, w, h)


class App:
    def __init__(self, cfg):
        disp = cfg['display']
        mons = monitors()
        dash_i = config_mod.pick_monitor(mons, disp.get('monitor', 'auto'))
        self.boards = []
        for i, m in enumerate(mons):
            if i != dash_i and not disp.get('traces_on_other_monitors', True):
                continue
            hold_path = config_mod.resolve_holdout(cfg['holdout'].get('path', 'auto'), m['name'], m['w'], m['h'])
            holdout = load_holdout(hold_path, m['w'], m['h'])
            collector = Collector(cfg) if i == dash_i else None
            scene = Scene(m['w'], m['h'], seed=1000 + i, tint=float(disp.get('tint', 0.45)), holdout=holdout, collector=collector)
            role = 'dashboard' if collector else 'traces'
            print(f'{TITLE}: {m["name"]} {m["w"]}x{m["h"]} {role}, holdout: {hold_path or "none"}'
                  + (f', panels: {scene.dash.rects}' if scene.dash else ''), flush=True)
            self.boards.append(Board(m, scene))
        self.paused = False
        fps = max(5, min(60, int(disp.get('fps', 30))))
        GLib.timeout_add(1000 // fps, self.tick)
        GLib.timeout_add(1000, self.dash_tick)
        GLib.timeout_add(1500, self.check_fullscreen)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self.cycle)
        for s in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, s, self.quit)

    def tick(self):
        if not self.paused:
            for b in self.boards:
                b.invalidate(b.scene.tick())
        return True

    def dash_tick(self):
        if not self.paused:
            for b in self.boards:
                try:
                    b.invalidate(b.scene.dash_tick())
                except Exception as e:  # noqa: BLE001 -- one bad sample must not stop the refresh
                    print(f'{TITLE}: sample failed: {e}', flush=True)
        return True

    def check_fullscreen(self):
        fs = is_fullscreen_focused()
        if fs and not self.paused:
            self.paused = True
            for b in self.boards:
                b.hide()
        elif not fs and self.paused:
            self.paused = False
            for b in self.boards:
                b.show_all()
                b.click_through()
        return True

    def cycle(self):
        """SIGUSR1: hide/show every window -- used by the click-through check."""
        for b in self.boards:
            b.hide()
            b.show_all()
            b.click_through()
        return True

    def quit(self):
        Gtk.main_quit()
        return False


def run(cfg):
    App(cfg)
    Gtk.main()
