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
import signal

import cairo
import gi
import numpy as np

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# JavaScriptCore suspends threads for its GC with SIGUSR1 unless told otherwise, and app.py takes
# SIGUSR1 (GLib.unix_signal_add) for its hide/show cycle: the GC then waits forever for a thread that
# never stops and the whole dashboard hangs. Set before WebKit loads.
os.environ.setdefault('JSC_SIGNAL_FOR_GC', str(int(signal.SIGUSR2)))

try:
    gi.require_version('WebKit2', '4.1')
    from gi.repository import WebKit2  # noqa: E402
except (ImportError, ValueError):  # webkit2gtk-4.1 not installed: the helpers below still work
    WebKit2 = None

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mascot.html')


def act_path(env=os.environ):
    """Where `piwnica-dashboard mascot-act` leaves a request for the running character."""
    base = env.get('XDG_RUNTIME_DIR') or f'/tmp/piwnica-dashboard-{os.getuid()}'
    return os.path.join(base, 'piwnica-dashboard', 'mascot-act')


def request_act(names, path=None):
    """Asks the running character to play these motions / expressions now (the motion first,
    the faces right after it, since a motion resets the face when it starts)."""
    path = path or act_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump([str(n) for n in names], f)
    os.replace(tmp, path)


def take_act(path=None):
    """The pending request (and gone from disk), or [] without one or with a broken one."""
    path = path or act_path()
    try:
        with open(path, encoding='utf-8') as f:
            names = json.load(f)
    except (OSError, ValueError):
        return []
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return [str(n) for n in names] if isinstance(names, list) else []


def act_js(names):
    """JS playing names in turn, 0.4 s apart."""
    return ' '.join(f'setTimeout(() => mascot.act({json.dumps(n)}), {i * 400});' for i, n in enumerate(names))


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
           'acts': acts(paths['model'], sec, (w, h)),
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

        self.cut = []  # [x, y, w, h] in window pixels where the window shows nothing (set_cutouts)
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
        GLib.timeout_add_seconds(1, self.poll_act)

    def click_through(self, *_):
        self.input_shape_combine_region(cairo.Region())
        win = self.get_window()
        if win is not None:
            win.input_shape_combine_region(cairo.Region(), 0, 0)
            if self.cut:
                shown = cairo.Region(cairo.RectangleInt(0, 0, self.rect[2], self.rect[3]))
                for x, y, w, h in self.cut:
                    shown.subtract(cairo.RectangleInt(x, y, w, h))
                win.shape_combine_region(shown, 0, 0)
        return False

    def set_cutouts(self, panels):
        """[mascot] under_panels: the dashboard panels ({name: (x, y, w, h)} on the monitor) show
        through the character -- the window's own shape leaves them out, nothing is drawn twice."""
        self.cut = cutouts(self.rect, panels)
        self.click_through()

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

    def poll_act(self):
        """A `mascot-act` request: one stat a second when there is none."""
        if self.ready and not self.paused and os.path.exists(act_path()):
            names = take_act()
            if names:
                self.log(f'piwnica-dashboard: mascot asked to {" ".join(names)}')
                self.js(act_js(names))
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


def cutouts(rect, panels):
    """The parts of the panels (x, y, w, h on the monitor) inside the window at rect = [x, y, w, h],
    in window pixels."""
    rx, ry, rw, rh = rect
    out = []
    for x, y, w, h in panels:
        x0, y0 = max(x, rx), max(y, ry)
        x1, y1 = min(x + w, rx + rw), min(y + h, ry + rh)
        if x1 > x0 and y1 > y0:
            out.append([x0 - rx, y0 - ry, x1 - x0, y1 - y0])
    return out


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


def _duration(path, default=5.0):
    try:
        with open(path, encoding='utf-8') as f:
            return float(json.load(f)['Meta']['Duration'])
    except (OSError, ValueError, KeyError, TypeError):
        return default


def idle_plan(model_path, cap=15.0, skip=()):
    """[(index, seconds)] of the model's Idle motions -- every pose it takes at rest, so the holdout
    covers all of them (an arm or the hair swings past any single frame). [] without an Idle group."""
    return [(i, max(1.0, min(cap, _duration(path))))
            for i, (_name, path) in enumerate(idle_motions(model_path)) if i not in skip]


# -- acts: what the character does on its own now and then (a gesture, a face, a dance) ----------

ACT_KINDS = ('gesture', 'emote', 'dance', 'idle')


def _model_refs(model_path):
    try:
        with open(model_path, encoding='utf-8') as f:
            refs = json.load(f)['FileReferences']
        return refs if isinstance(refs, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _catalog(model_path, sec):
    """{motion group: {'category', 'tags', 'face', 'duration'}} from [mascot] act_catalog: a JSON of
    {<any key>: {'model3': <path>, 'motions': {...}}}, the part whose model3 has this model's file
    name (else the first one). {} without a catalogue."""
    path = os.path.expanduser(sec.get('act_catalog') or '')
    if not path:
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        parts = [p for p in data.values() if isinstance(p, dict) and isinstance(p.get('motions'), dict)]
    except (OSError, ValueError, AttributeError):
        return {}
    name = os.path.basename(model_path)
    for part in parts:
        if os.path.basename(str(part.get('model3', ''))) == name:
            return part['motions']
    return parts[0]['motions'] if parts else {}


def act_motions(model_path, sec):
    """[{'name', 'kind', 'face', 'seconds'}] of the model's motion groups outside Idle (the first
    motion of each). kind is 'gesture', 'dance' or 'idle': from the catalogue's category, else from
    the group name (dance*, idle*). Left out: names and catalogue tags in act_skip, names in idle_skip."""
    cat = _catalog(model_path, sec)
    skip = set(sec.get('act_skip') or ()) | set(sec.get('idle_skip') or ())
    base = os.path.dirname(model_path)
    out = []
    for group, entries in (_model_refs(model_path).get('Motions') or {}).items():
        if group == 'Idle' or not isinstance(entries, list) or not entries:
            continue
        e = cat.get(group)
        e = e if isinstance(e, dict) else {}
        if group in skip or skip.intersection(str(t) for t in e.get('tags') or ()):
            continue
        low = group.lower()
        kind = e.get('category') if e else ('dance' if low.startswith('dance') else 'idle' if low.startswith('idle') else '')
        try:
            secs = float(e['duration'])
        except (KeyError, TypeError, ValueError):
            secs = _duration(os.path.join(base, str(entries[0].get('File', ''))))
        out.append({'name': group, 'kind': kind if kind in ('dance', 'idle') else 'gesture',
                    'face': bool(e.get('face', True)), 'seconds': secs})
    return out


def act_every(sec):
    """[shortest, longest] pause between two acts in seconds, at least 5 s."""
    try:
        lo, hi = (float(v) for v in sec.get('act_every', (20, 60)))
    except (TypeError, ValueError):
        lo, hi = 20.0, 60.0
    lo = max(5.0, lo)
    return [lo, max(lo, hi)]


def acts_cache_path():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'piwnica-dashboard', 'mascot-acts.json')


def _acts_key(model_path, sec, size):
    """What a `mascot-acts` measurement holds for: this model (file times), box size and zoom."""
    moc = os.path.join(os.path.dirname(model_path), str(_model_refs(model_path).get('Moc', '')))
    times = []
    for p in (model_path, moc):
        try:
            times.append(os.path.getmtime(p))
        except OSError:
            times.append(0)
    return {'model': os.path.abspath(model_path), 'mtime': times, 'size': [int(v) for v in size],
            'zoom': float(sec.get('zoom', 1.0))}


def clipped_acts(model_path, sec, size):
    """Motions `mascot-acts` found cut off by the window edge, when its measurement is of this
    model, size and zoom; else nothing is known and nothing is left out."""
    try:
        with open(acts_cache_path(), encoding='utf-8') as f:
            data = json.load(f)
        if all(data.get(k) == v for k, v in _acts_key(model_path, sec, size).items()):
            return {str(n) for n in data.get('clipped') or ()}
    except (OSError, ValueError, AttributeError):
        pass
    return set()


def acts(model_path, sec, size=(0, 0)):
    """What mascot.html may play by itself: the names per kind in [mascot] acts ('emote' = the
    model's expressions), 'plain' = motions that leave the face alone, 'every' and 'log'."""
    on = sec.get('acts')
    on = [k for k in (ACT_KINDS if on is None else on) if k in ACT_KINDS]
    out = {k: [] for k in ACT_KINDS}
    out.update(plain=[], every=act_every(sec), log=bool(sec.get('act_log')))
    if not on:
        return out
    clipped = clipped_acts(model_path, sec, size)
    for m in act_motions(model_path, sec):
        if m['kind'] in on and m['name'] not in clipped:
            out[m['kind']].append(m['name'])
            if not m['face']:
                out['plain'].append(m['name'])
    if 'emote' in on:
        skip = set(sec.get('act_skip') or ())
        for e in _model_refs(model_path).get('Expressions') or ():
            name = e.get('Name') if isinstance(e, dict) else None
            if name and name not in skip:
                out['emote'].append(name)
    return out


def is_clipped(alpha, base, band=2, slack=30, need=40):
    """True when `alpha` (everything a motion drew) reaches the left, right or top edge of the box
    where `base` (the rest pose) does not: the window would cut the motion off there. Edge pixels
    within `slack` px of ones the rest pose already touches do not count; `need` new ones do."""
    grow = np.ones(2 * slack + 1)
    new = 0
    for side in (np.s_[:, :band], np.s_[:, -band:], np.s_[:band, :]):
        axis = 0 if side[0] != slice(None) else 1
        a = (alpha[side] > 16).any(axis=axis)
        b = np.convolve((base[side] > 16).any(axis=axis), grow, mode='same') > 0
        new += int((a & ~b).sum())
    return new >= need


def _record(m, plan, on_frame, finish, log, every_ms=400, settle=0.0):
    """Runs plan = [(js or None, seconds)] on the Mascot m once its page is ready, handing the
    alpha of a snapshot every `every_ms` (after `settle` s into a step) to on_frame(step, alpha);
    then finish() and the Gtk main loop ends. A step without js gets one frame, at its end."""
    st = {'step': -1, 'start': 0.0, 'until': 0.0, 'busy': False}

    def snapped(view, res, step):
        st['busy'] = False
        try:
            a = surface_alpha(view.get_snapshot_finish(res))
        except (GLib.Error, cairo.Error) as e:
            log(f'piwnica-dashboard: snapshot failed: {e}')
            return
        on_frame(step, a)

    def tick():
        now = GLib.get_monotonic_time() / 1e6
        if now >= st['until'] and not st['busy']:
            st['step'] += 1
            if st['step'] >= len(plan):
                finish()
                Gtk.main_quit()
                return False
            js, secs = plan[st['step']]
            if js is not None:
                m.js(js)
            st['start'], st['until'] = now, now + secs
            return True
        if not st['busy'] and now >= st['start'] + settle and (
                plan[st['step']][0] is not None or now >= st['until'] - every_ms / 1000):
            st['busy'] = True
            m.view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.TRANSPARENT_BACKGROUND,
                                None, snapped, st['step'])
        return True

    m.on_ready = lambda: GLib.timeout_add(every_ms, tick)
    limit = 30 + sum(s for _js, s in plan) * 2
    GLib.timeout_add_seconds(int(limit), lambda: (log('piwnica-dashboard: the model did not load or got stuck'),
                                                  Gtk.main_quit()))
    Gtk.main()


def _still(cfg, log):
    """([mascot] with the acts off, paths) for a measuring run, or (None, None) with files missing."""
    sec = dict(cfg['mascot'], acts=[])
    paths, missing = files(sec)
    if missing:
        log(f'piwnica-dashboard: [mascot] files missing: {", ".join(missing)}')
        return None, None
    return sec, paths


def write_holdout(cfg, mon, out, log=print, every_ms=400):
    """Opens the character where `run` puts it, looking straight ahead, plays each Idle motion in
    turn and saves the outline of all the frames together as a holdout PNG of the monitor's size.
    0 on success."""
    sec, paths = _still(cfg, log)
    if sec is None:
        return 1
    m = Mascot(mon, sec, paths, log=log)
    m.paused = True  # no cursor: the outline of the straight pose
    # no Idle group: one frame after the idle fade-in, as before
    idles = idle_plan(paths['model'], skip=idle_skip(paths['model'], sec))
    hide = f'mascot.hide({json.dumps([str(p) for p in sec.get("holdout_hide") or ()])});'
    plan = [(f'{hide} mascot.play({i})', secs) for i, secs in idles] or [(hide, 2.5)]
    st = {'code': 1, 'acc': None, 'frames': 0}

    def frame(_step, a):
        st['acc'] = a if st['acc'] is None or st['acc'].shape != a.shape else np.maximum(st['acc'], a)
        st['frames'] += 1

    def finish():
        if st['acc'] is None:
            log('piwnica-dashboard: no frame captured')
            return
        try:
            write_alpha_png(holdout(st['acc'], m.rect, mon['w'], mon['h']), out)
            log(f'piwnica-dashboard: holdout {mon["w"]}x{mon["h"]} from {st["frames"]} frames '
                f'of {len(idles)} idle motion(s) -> {out}')
            st['code'] = 0
        except (cairo.Error, OSError) as e:
            log(f'piwnica-dashboard: cannot write the holdout: {e}')

    _record(m, plan, frame, finish, log, every_ms)
    return st['code']


def write_acts(cfg, mon, log=print, every_ms=400, cap=12.0):
    """Plays every motion outside Idle where `run` puts the character and notes the ones the window
    edge cuts off (is_clipped against the rest pose) in acts_cache_path(); acts() then leaves them
    out. Takes about the length of all the motions together. 0 on success."""
    sec, paths = _still(cfg, log)
    if sec is None:
        return 1
    motions = act_motions(paths['model'], sec)
    if not motions:
        log('piwnica-dashboard: the model has no motions outside Idle')
        return 1
    m = Mascot(mon, sec, paths, log=log)
    m.paused = True
    # step 0, the rest pose to compare with: every frame of the first Idle motion (the hair sways)
    plan = [('mascot.play(0)', 8.0)] + [(f'mascot.act({json.dumps(a["name"])})', max(1.0, min(cap, a['seconds'])) + 0.4)
                                        for a in motions]
    seen = {}

    def frame(step, a):
        seen[step] = a if step not in seen or seen[step].shape != a.shape else np.maximum(seen[step], a)

    st = {'code': 1}

    def finish():
        if 0 not in seen or len(seen) < 2:
            log('piwnica-dashboard: no frame captured')
            return
        clipped = sorted(a['name'] for i, a in enumerate(motions, 1)
                         if i in seen and seen[i].shape == seen[0].shape and is_clipped(seen[i], seen[0]))
        path = acts_cache_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({**_acts_key(paths['model'], sec, m.rect[2:]), 'measured': len(seen) - 1,
                           'clipped': clipped}, f, indent=1)
        except OSError as e:
            log(f'piwnica-dashboard: cannot write {path}: {e}')
            return
        log(f'piwnica-dashboard: {len(seen) - 1} motions measured, {len(clipped)} cut off by the window '
            f'edge and left out: {", ".join(clipped) or "none"} -> {path}')
        st['code'] = 0

    # the first frames of a step still blend out of the motion before it
    _record(m, plan, frame, finish, log, every_ms, settle=0.6)
    return st['code']
