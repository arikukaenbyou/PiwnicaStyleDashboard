"""The animated layer itself, pure cairo (no GTK): faint PCB traces, signal pulses with an
afterglow, glowing pads, sparse falling glyph columns, the holdout cut-out and the dashboard.

``tick()`` advances the animation and returns the dirty rectangles; ``draw(cr)`` paints.
Only changed areas are repainted, so a 30 fps animation stays cheap.
"""
import math
import random

import cairo
import numpy as np

from .draw import ACCENT, Dashboard
from .layout import layout

GRID = 16
TRACE = (0.10, 0.30, 0.10)
GLYPHS = '0123456789ABCDEF01{}[]<>/\\|=+*#$%&;:'
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


def make_traces(w, h, rnd, blocked=()):
    """PCB-like polylines on a grid: straight runs with 45-degree bends, no overlaps.
    blocked: rectangles (x, y, w, h) no trace may cross (dashboard panels)."""
    cols, rows = w // GRID, h // GRID
    used = set()
    for bx, by, bw, bh in blocked:
        for cx in range(bx // GRID - 1, (bx + bw) // GRID + 2):
            for cy in range(by // GRID - 1, (by + bh) // GRID + 2):
                used.add((cx, cy))
    traces = []
    for _ in range(int(cols * rows / 9)):
        x, y = rnd.randrange(1, cols - 1), rnd.randrange(1, rows - 1)
        if (x, y) in used:
            continue
        d = rnd.choice([0, 2, 4, 6]) if rnd.random() < 0.8 else rnd.randrange(8)
        pts, cells = [(x, y)], [(x, y)]
        for _seg in range(rnd.randint(2, 6)):
            dx, dy = DIRS[d]
            ok = 0
            for _ in range(rnd.randint(2, 12)):
                nx, ny = x + dx, y + dy
                if not (1 <= nx < cols - 1 and 1 <= ny < rows - 1) or (nx, ny) in used or (nx, ny) in cells:
                    break
                x, y = nx, ny
                cells.append((x, y))
                ok += 1
            if ok == 0:
                break
            pts.append((x, y))
            d = (d + rnd.choice([-1, 1])) % 8
        if len(cells) < 5:
            continue
        for cx, cy in cells:  # keep neighbours free so traces never touch
            for ex in (-1, 0, 1):
                for ey in (-1, 0, 1):
                    used.add((cx + ex, cy + ey))
        poly = [(px * GRID + GRID / 2, py * GRID + GRID / 2) for px, py in pts]
        segs, total = [], 0.0
        for a, b in zip(poly, poly[1:]):
            ln = math.hypot(b[0] - a[0], b[1] - a[1])
            segs.append((a, b, total, ln))
            total += ln
        traces.append({'poly': poly, 'segs': segs, 'len': total})
    return traces


def point_at(tr, s):
    s = max(0.0, min(tr['len'], s))
    for a, b, start, ln in tr['segs']:
        if s <= start + ln:
            t = (s - start) / ln if ln else 0
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
    return tr['poly'][-1]


def holdout_mask(surface, w, h):
    """bool H x W from a holdout surface (alpha > 0 = character)."""
    if surface is None:
        return np.zeros((h, w), bool)
    a = np.frombuffer(surface.get_data(), np.uint8).reshape(h, surface.get_stride())
    return a[:, 3:w * 4:4] > 0


def load_holdout(path, w, h):
    """cairo surface of exactly w x h, or None."""
    if not path:
        return None
    try:
        surf = cairo.ImageSurface.create_from_png(path)
    except (cairo.Error, OSError, MemoryError):
        return None
    return surf if (surf.get_width(), surf.get_height()) == (w, h) else None


class Scene:
    def __init__(self, w, h, seed=1000, tint=0.45, holdout=None, collector=None, glyphs=True, bar_fx=True, panels=None,
                 fixed=None):
        self.W, self.H = w, h
        self.rnd = random.Random(seed)
        self.tint = tint
        self.holdout = holdout
        self.dash = None
        if collector is not None:
            from .layout import EXTRA
            have = getattr(collector, 'panels', EXTRA)  # only panels with a configured source get space
            extra = [p for p in (panels or EXTRA) if p in have]
            rects = layout(holdout_mask(holdout, w, h), extra, fixed)
            moved = [n for n in (fixed or {}) if n in rects and tuple(rects[n]) != tuple(fixed[n])]
            if moved:  # a stale [layout] after a new wallpaper: say so instead of a silent shuffle
                print(f'layout: {", ".join(moved)} placed automatically, the fixed spot does not fit', flush=True)
            self.dash = Dashboard(rects, collector, fx=bar_fx)
            self.dash.tick()
            self.dash.render()  # bar positions known from the first frame (bar fx)
        self.glyphs = glyphs and self.dash is None  # glyph columns would run through the panels
        self.traces = make_traces(w, h, self.rnd, self.dash.rects.values() if self.dash else ())
        self.static = self.render_static()
        self.pulses, self.pads, self.streams = [], [], []

    def cut_holdout(self, cr):
        if self.holdout is not None:
            # DEST_OUT removes source_alpha x mask_alpha: the source must be fully opaque, or
            # whatever was drawn last (a translucent trace colour, a gradient) leaves part of
            # the layer over the character
            cr.set_operator(cairo.OPERATOR_DEST_OUT)
            cr.set_source_rgba(0, 0, 0, 1)
            cr.mask_surface(self.holdout, 0, 0)

    def render_static(self):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.W, self.H)
        cr = cairo.Context(surf)
        cr.set_source_rgba(0, 0, 0, self.tint)
        cr.paint()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for tr in self.traces:
            cr.set_source_rgba(*TRACE, 0.55)
            cr.set_line_width(2)
            cr.move_to(*tr['poly'][0])
            for pt in tr['poly'][1:]:
                cr.line_to(*pt)
            cr.stroke()
            for i, (x, y) in enumerate((tr['poly'][0], tr['poly'][-1])):
                cr.set_source_rgba(*TRACE, 0.8)
                cr.arc(x, y, 3.5 if i else 2.5, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(0, 0, 0, 0.9)
                cr.arc(x, y, 1.3, 0, 2 * math.pi)
                cr.fill()
        blocked = list(self.dash.rects.values()) if self.dash else []
        for _ in range(max(2, self.W * self.H // 350000)):  # a few IC footprints for the PCB feel
            x, y = self.rnd.randrange(40, max(41, self.W - 160)), self.rnd.randrange(40, max(41, self.H - 120))
            w, h = self.rnd.choice([(96, 64), (64, 64), (128, 48)])
            if any(x < bx + bw and bx < x + w and y < by + bh and by < y + h for bx, by, bw, bh in blocked):
                continue
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.rectangle(x, y, w, h)
            cr.fill()
            cr.set_source_rgba(*TRACE, 0.7)
            cr.set_line_width(1)
            cr.rectangle(x + 0.5, y + 0.5, w, h)
            cr.stroke()
            for px in range(x + 8, x + w - 4, 8):
                for py in (y - 4, y + h + 1):
                    cr.rectangle(px, py, 3, 3)
            cr.fill()
        self.cut_holdout(cr)
        return surf

    # ---------------------------------------------------------------- animation
    def tick(self):
        dirty = []
        if self.traces and len(self.pulses) < max(4, self.W * self.H // 110000) and self.rnd.random() < 0.18:
            self.pulses.append({'tr': self.rnd.choice(self.traces), 's': 0.0, 'v': self.rnd.uniform(2.5, 6.0),
                                'rev': self.rnd.random() < 0.5, 'tail': self.rnd.uniform(90, 200)})
        alive = []
        for p in self.pulses:
            dirty.append(self.pulse_box(p))
            p['s'] += p['v']
            if p['s'] - p['tail'] > p['tr']['len']:
                continue
            if p['s'] >= p['tr']['len'] and not p.get('hit'):
                p['hit'] = True
                end = p['tr']['poly'][0] if p['rev'] else p['tr']['poly'][-1]
                self.pads.append([end[0], end[1], 1.0])
            dirty.append(self.pulse_box(p))
            alive.append(p)
        self.pulses = alive

        for pad in self.pads:
            dirty.append((pad[0] - 14, pad[1] - 14, 28, 28))
            pad[2] -= 0.025
        self.pads = [p for p in self.pads if p[2] > 0]

        if self.glyphs and len(self.streams) < max(1, self.W // 700) and self.rnd.random() < 0.02:
            self.streams.append([self.rnd.randrange(0, self.W // 14) * 14, -20.0, self.rnd.uniform(1.5, 3.5),
                                 [self.rnd.choice(GLYPHS) for _ in range(self.rnd.randint(10, 24))], 0])
        live = []
        for s in self.streams:
            dirty.append(self.stream_box(s))
            s[1] += s[2]
            s[4] += 1
            if s[4] % 6 == 0:
                s[3][self.rnd.randrange(len(s[3]))] = self.rnd.choice(GLYPHS)
            if s[1] - len(s[3]) * 16 > self.H:
                continue
            dirty.append(self.stream_box(s))
            live.append(s)
        self.streams = live
        if self.dash:
            dirty.extend(self.dash.fx_tick())
        return [(int(x), int(y), int(w) + 1, int(h) + 1) for x, y, w, h in dirty]

    def dash_tick(self):
        """Refresh dashboard data; returns the panel rectangles to repaint."""
        if not self.dash:
            return []
        self.dash.tick()
        return [(x - 4, y - 4, w + 8, h + 8) for x, y, w, h in self.dash.rects.values()]

    def pulse_box(self, p):
        tr, s1 = p['tr'], p['s']
        s0 = s1 - p['tail']
        xs, ys = [], []
        for k in range(9):
            s = s0 + (s1 - s0) * k / 8
            x, y = point_at(tr, tr['len'] - s if p['rev'] else s)
            xs.append(x)
            ys.append(y)
        for a, b, start, ln in tr['segs']:  # corners between the samples
            for (x, y), sp in ((a, start), (b, start + ln)):
                sp2 = tr['len'] - sp if p['rev'] else sp
                if s0 <= sp2 <= s1:
                    xs.append(x)
                    ys.append(y)
        m = 16
        return min(xs) - m, min(ys) - m, max(xs) - min(xs) + 2 * m, max(ys) - min(ys) + 2 * m

    @staticmethod
    def stream_box(s):
        return s[0] - 2, s[1] - len(s[3]) * 16 - 4, 18, len(s[3]) * 16 + 24

    # ---------------------------------------------------------------- drawing
    def draw(self, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_surface(self.static, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for p in self.pulses:
            tr, head, steps, prev = p['tr'], p['s'], 14, None
            for k in range(steps + 1):
                s = head - p['tail'] * (1 - k / steps)
                if s < 0 or s > tr['len']:
                    prev = None
                    continue
                pt = point_at(tr, tr['len'] - s if p['rev'] else s)
                if prev is not None:
                    a = (k / steps) ** 2
                    cr.set_source_rgba(*ACCENT, 0.05 + 0.55 * a)
                    cr.set_line_width(2 + 1.5 * a)
                    cr.move_to(*prev)
                    cr.line_to(*pt)
                    cr.stroke()
                prev = pt
            if head <= tr['len']:
                x, y = point_at(tr, tr['len'] - head if p['rev'] else head)
                g = cairo.RadialGradient(x, y, 0, x, y, 12)
                g.add_color_stop_rgba(0, 0.85, 1, 0.9, 0.9)
                g.add_color_stop_rgba(0.3, *ACCENT, 0.45)
                g.add_color_stop_rgba(1, *ACCENT, 0)
                cr.set_source(g)
                cr.arc(x, y, 12, 0, 2 * math.pi)
                cr.fill()
        for x, y, life in self.pads:
            g = cairo.RadialGradient(x, y, 0, x, y, 12)
            g.add_color_stop_rgba(0, *ACCENT, 0.7 * life)
            g.add_color_stop_rgba(1, *ACCENT, 0)
            cr.set_source(g)
            cr.arc(x, y, 12, 0, 2 * math.pi)
            cr.fill()
        cr.select_font_face('JetBrains Mono', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(13)
        for x, y, _v, glyphs, _age in self.streams:
            n = len(glyphs)
            for i, ch in enumerate(glyphs):
                if i == 0:
                    cr.set_source_rgba(0.8, 1, 0.85, 0.55)
                else:
                    cr.set_source_rgba(*ACCENT, 0.32 * (1 - i / n))
                cr.move_to(x, y - i * 16)
                cr.show_text(ch)
        self.cut_holdout(cr)
        if self.dash:
            cr.set_operator(cairo.OPERATOR_OVER)
            self.dash.draw(cr)
