"""Live2D character on the desktop whose gaze follows the mouse cursor.

A transparent WebKit window in the "below" layer, click-through like the dashboard boards
(see app.py), with mascot.html drawing the model through pixi-live2d-display. Nothing of the
model ships with this repo: the .model3.json, Cubism Core (closed Live2D licence) and the two
pixi scripts are read from the paths in [mascot].

The window never gets mouse events (it is click-through), so the cursor is polled from GDK and
handed to the page only when it moved. While a game or a fullscreen window runs, app.py hides
the window and stops the page's ticker.
"""
import json
import os

import cairo
import gi
import numpy as np

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

try:
    gi.require_version('WebKit2', '4.1')
    from gi.repository import WebKit2  # noqa: E402
except (ImportError, ValueError):  # webkit2gtk-4.1 not installed: the helpers below still work
    WebKit2 = None

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mascot.html')


def files(sec):
    """{'model', 'core', 'pixi', 'cubism4'} as absolute paths, or (None, what is missing)."""
    libs = os.path.expanduser(sec.get('libs', ''))
    paths = {'model': os.path.expanduser(sec.get('model', '')), 'core': os.path.expanduser(sec.get('core', '')),
             'pixi': os.path.join(libs, 'pixi.min.js') if libs else '',
             'cubism4': os.path.join(libs, 'cubism4.min.js') if libs else ''}
    missing = [k for k, p in paths.items() if not p or not os.path.isfile(p)]
    return (None, missing) if missing else (paths, [])


def place(sec, mw, mh):
    """[x, y, w, h] of the window inside a mw x mh monitor: [mascot] rect, or bottom centre,
    half the monitor wide and as tall as a 2:3 (waist-up) model needs."""
    r = sec.get('rect', 'auto')
    if isinstance(r, (list, tuple)) and len(r) == 4:
        x, y, w, h = (int(v) for v in r)
        x, y = max(0, min(x, mw - 1)), max(0, min(y, mh - 1))
        return [x, y, max(1, min(w, mw - x)), max(1, min(h, mh - y))]
    w = mw // 2
    h = min(mh, w * 3 // 2)
    return [(mw - w) // 2, mh - h, w, h]


def page_html(paths, w, h, sec):
    skip = idle_skip(paths['model'], sec)
    cfg = {'width': w, 'height': h, 'fps': max(5, min(60, int(sec.get('fps', 30)))), 'idle_skip': skip,
           'zoom': float(sec.get('zoom', 1.0)), 'eyes': float(sec.get('eyes', 0.2)),
           'reach': max(1.0, float(sec.get('reach', 700))),
           'fade': max(0.0, min(1.0, float(sec.get('fade', 0.0)))),
           **{k: 'file://' + p for k, p in paths.items()}}
    with open(PAGE, encoding='utf-8') as f:
        return f.read().replace('/*CONFIG*/{}', json.dumps(cfg))


def _box(a, r):
    """Mean over a (2r+1)^2 square, edges clamped (summed-area table)."""
    if r <= 0:
        return a.astype(np.float32)
    p = np.pad(a.astype(np.float32), r + 1, mode='edge')
    s = p.cumsum(0).cumsum(1)
    n = 2 * r + 1
    out = s[n:, n:] - s[:-n, n:] - s[n:, :-n] + s[:-n, :-n]
    return out[:a.shape[0], :a.shape[1]] / (n * n)


def holdout(alpha, rect, mw, mh, margin=40):
    """Holdout mask (mh x mw uint8 alpha) from the model's alpha drawn at rect = [x, y, w, h]:
    the outline grown by `margin` px (the head turns and the hair swings past the still pose)
    with a soft edge, like the hand-made masks."""
    x, y, w, h = rect
    full = np.zeros((mh, mw), np.float32)
    full[y:y + h, x:x + w] = (alpha[:h, :w] > 16)
    grown = _box(full, margin) > 0
    return np.clip(_box(grown, max(1, margin // 3)) * 255, 0, 255).astype(np.uint8)


def write_alpha_png(a, path):
    h, w = a.shape
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    buf = np.ndarray((h, surf.get_stride() // 4), np.uint32, surf.get_data())
    buf[:, :w] = a.astype(np.uint32) << 24  # premultiplied black, alpha only
    surf.mark_dirty()
    surf.write_to_png(path)


def surface_alpha(surf):
    """Alpha channel of a cairo ARGB32 surface as an h x w uint8 array."""
    img = cairo.ImageSurface(cairo.FORMAT_ARGB32, surf.get_width(), surf.get_height())
    cr = cairo.Context(img)
    cr.set_source_surface(surf, 0, 0)
    cr.paint()
    img.flush()
    h, w = img.get_height(), img.get_width()
    buf = np.ndarray((h, img.get_stride() // 4), np.uint32, img.get_data())
    return (buf[:, :w] >> 24).astype(np.uint8)


class Mascot(Gtk.Window):
    def __init__(self, mon, sec, paths, log=print):
        super().__init__(type=Gtk.WindowType.TOPLEVEL, title='piwnica-dashboard-mascot')
        self.log = log
        x, y, w, h = self.rect = place(sec, mon['w'], mon['h'])
        self.mon = mon
        self.origin = (mon['x'] + x, mon['y'] + y)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_keep_below(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        self.stick()
        self.move(*self.origin)
        self.set_default_size(w, h)

        ucm = WebKit2.UserContentManager()
        ucm.connect('script-message-received::mascot', self.on_message)
        ucm.register_script_message_handler('mascot')
        self.view = WebKit2.WebView.new_with_user_content_manager(ucm)
        st = self.view.get_settings()
        # the page and the model are local files; WebGL refuses textures from file:// otherwise
        st.set_allow_file_access_from_file_urls(True)
        st.set_allow_universal_access_from_file_urls(True)
        st.set_enable_webgl(True)
        st.set_enable_write_console_messages_to_stdout(True)
        self.view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        self.add(self.view)
        self.view.load_html(page_html(paths, w, h, sec), 'file:///')

        self.ready = False
        self.error = None  # the page's last "error: …" message (health.py reports it)
        self.on_ready = None
        self.last = None
        self.paused = False
        for sig in ('realize', 'map-event', 'size-allocate'):
            self.connect(sig, self.click_through)
        self.show_all()
        self.click_through()
        GLib.timeout_add(1000 // max(5, min(60, int(sec.get('fps', 30)))), self.poll)

    def click_through(self, *_):
        self.input_shape_combine_region(cairo.Region())
        win = self.get_window()
        if win is not None:
            win.input_shape_combine_region(cairo.Region(), 0, 0)
        return False

    def on_message(self, _ucm, result):
        msg = result.get_js_value().to_string()
        self.log(f'piwnica-dashboard: mascot {msg}')
        if msg.startswith('error'):
            self.error = msg.partition(':')[2].strip() or msg
        if msg.startswith('ready') and not self.ready:
            self.ready = True
            if self.on_ready:
                self.on_ready()

    def js(self, code):
        self.view.evaluate_javascript(code, -1, None, None, None, None, None)

    def poll(self):
        if not self.ready or self.paused:
            return True
        seat = Gdk.Display.get_default().get_default_seat()
        _screen, px, py = seat.get_pointer().get_position()
        pos = (px - self.origin[0], py - self.origin[1])
        if pos != self.last:
            self.last = pos
            self.js(f'mascot.look({pos[0]},{pos[1]})')
        return True

    def pause(self, p):
        """Hidden and frozen while a game runs; back where it was afterwards."""
        if p == self.paused:
            return
        self.paused = p
        if self.ready:
            self.js(f'mascot.pause({"true" if p else "false"})')
        if p:
            self.hide()
        else:
            self.show_all()
            self.click_through()


def idle_motions(model_path):
    """[(name, path)] of the model's Idle motions, name = file name without .motion3.json;
    [] when the model cannot be read or has no Idle group."""
    try:
        with open(model_path, encoding='utf-8') as f:
            idles = json.load(f)['FileReferences'].get('Motions', {}).get('Idle', [])
        return [(os.path.basename(e['File']).split('.')[0], os.path.join(os.path.dirname(model_path), e['File']))
                for e in idles]
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return []


def idle_skip(model_path, sec):
    """Indexes in the Idle group of the motions named in [mascot] idle_skip."""
    names = set(sec.get('idle_skip') or ())
    return [i for i, (name, _p) in enumerate(idle_motions(model_path)) if name in names]


def idle_plan(model_path, cap=15.0, skip=()):
    """[(index, seconds)] of the model's Idle motions -- every pose it takes at rest, so the holdout
    covers all of them (an arm or the hair swings past any single frame). [] without an Idle group."""
    plan = []
    for i, (_name, path) in enumerate(idle_motions(model_path)):
        if i in skip:
            continue
        try:
            with open(path, encoding='utf-8') as f:
                d = float(json.load(f)['Meta']['Duration'])
        except (OSError, ValueError, KeyError, TypeError):
            d = 5.0
        plan.append((i, max(1.0, min(cap, d))))
    return plan


def write_holdout(cfg, mon, out, log=print, every_ms=400):
    """Opens the character where `run` puts it, looking straight ahead, plays each Idle motion in
    turn and saves the outline of all the frames together as a holdout PNG of the monitor's size.
    0 on success."""
    sec = cfg['mascot']
    paths, missing = files(sec)
    if missing:
        log(f'piwnica-dashboard: [mascot] files missing: {", ".join(missing)}')
        return 1
    m = Mascot(mon, sec, paths, log=log)
    m.paused = True  # no cursor: the outline of the straight pose
    # no Idle group: one frame after the idle fade-in, as before
    plan = idle_plan(paths['model'], skip=idle_skip(paths['model'], sec)) or [(None, 2.5)]
    st = {'code': 1, 'step': -1, 'until': 0.0, 'busy': False, 'acc': None, 'frames': 0}

    def finish():
        if st['acc'] is None:
            log('piwnica-dashboard: no frame captured')
        else:
            try:
                write_alpha_png(holdout(st['acc'], m.rect, mon['w'], mon['h']), out)
                log(f'piwnica-dashboard: holdout {mon["w"]}x{mon["h"]} from {st["frames"]} frames '
                    f'of {len(plan)} idle motion(s) -> {out}')
                st['code'] = 0
            except (cairo.Error, OSError) as e:
                log(f'piwnica-dashboard: cannot write the holdout: {e}')
        Gtk.main_quit()

    def snapped(view, res):
        st['busy'] = False
        try:
            a = surface_alpha(view.get_snapshot_finish(res))
        except (GLib.Error, cairo.Error) as e:
            log(f'piwnica-dashboard: snapshot failed: {e}')
            return
        st['acc'] = a if st['acc'] is None or st['acc'].shape != a.shape else np.maximum(st['acc'], a)
        st['frames'] += 1

    def tick():
        now = GLib.get_monotonic_time() / 1e6
        if now >= st['until'] and not st['busy']:
            st['step'] += 1
            if st['step'] >= len(plan):
                finish()
                return False
            i, secs = plan[st['step']]
            if i is not None:
                m.js(f'mascot.play({i})')
            st['until'] = now + secs
            return True
        if not st['busy'] and (plan[st['step']][0] is not None or now >= st['until'] - every_ms / 1000):
            st['busy'] = True
            m.view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.TRANSPARENT_BACKGROUND,
                                None, snapped)
        return True

    m.on_ready = lambda: GLib.timeout_add(every_ms, tick)
    limit = 30 + sum(s for _i, s in plan) * 2
    GLib.timeout_add_seconds(int(limit), lambda: (log('piwnica-dashboard: the model did not load or got stuck'),
                                                  Gtk.main_quit()))
    Gtk.main()
    return st['code']
