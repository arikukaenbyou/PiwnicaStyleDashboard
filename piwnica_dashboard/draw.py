"""Cairo drawing of the panels: sysmon.sh (tiles), df -h (disks), sensors (temperatures) and
pacman (update progress, or a summary of past updates between them).

The panels change once a second, so they are rendered into a cached surface on each data tick;
every frame only blits that and draws the CRT beam sweeping along the bars (bar_fx), which is
the only part animated at full fps."""
import math
import time
from datetime import datetime

import cairo

from .collector import HIST

ACCENT = (0.0, 1.0, 0.4)
TEXT = (0.843, 0.988, 0.882)
MUTED = (0.718, 0.969, 0.784)
DIM = (0.31, 0.478, 0.361)
BORDER = (0.102, 0.302, 0.102)
WARN = (1.0, 0.757, 0.027)
DANGER = (1.0, 0.231, 0.231)
PANEL_BG = (0.094, 0.094, 0.094, 0.86)
FONT = 'JetBrains Mono'
GiB = 2 ** 30  # like df -h / free -h
ZONE_COLOR = {'green': ACCENT, 'yellow': WARN, 'red': DANGER}


def fmt_rate(bps):
    return f'{bps / 1e6:.1f} MB/s' if bps >= 1e5 else f'{bps / 1e3:.0f} kB/s'


def fmt_uptime(s):
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    return f'{d}d {h}h' if d else f'{h}h {m}m'


def fmt_eta(s):
    if s is None:
        return '--:--'
    s = int(s)
    return f'{s // 3600}h{s % 3600 // 60:02d}' if s >= 3600 else f'{s // 60}:{s % 60:02d}'


def fmt_ago(ts, now):
    s = now - ts
    return ('just now' if s < 60 else f'{s / 60:.0f}m ago' if s < 3600 else f'{s / 3600:.0f}h ago' if s < 86400
            else f'{s / 86400:.0f}d ago')


def fmt_size(b, sign=False):
    a = abs(b)
    s = f'{a / 2 ** 40:.1f} TB' if a >= 2 ** 40 else f'{a / GiB:.1f} GB' if a >= GiB else f'{a / 2 ** 20:.0f} MB'
    return ('-' if b < 0 else '+') + s if sign else s


def fmt_left(s):
    """Countdown: 42m, 3h12, 1d17h."""
    if s is None:
        return '--'
    s = max(0, int(s))
    if s < 3600:
        return f'{s // 60}m'
    if s < 86400:
        return f'{s // 3600}h{s % 3600 // 60:02d}'
    return f'{s // 86400}d{s % 86400 // 3600}h'


PHASE = {'sync': 'SYNC', 'download': 'DOWNLOAD', 'verify': 'VERIFY', 'install': 'INSTALL', 'hooks': 'HOOKS',
         'aur': 'AUR BUILD', 'prep': 'PREPARING'}


def level_color(v, warn, crit):
    return DANGER if v >= crit else WARN if v >= warn else ACCENT


class Dashboard:
    def __init__(self, rects, collector, fx=True):
        self.rects = rects
        self.c = collector
        self.fx_on = fx
        self.bars = []      # (x, y, filled_w, h, color), recorded while rendering the panels
        self.cache = None
        xs = [x for x, _, _, _ in rects.values()] or [0]
        ys = [y for _, y, _, _ in rects.values()] or [0]
        self.box = (min(xs) - 4, min(ys) - 4,
                    max(x + w for x, _, w, _ in rects.values()) - min(xs) + 8 if rects else 1,
                    max(y + h for _, y, _, h in rects.values()) - min(ys) + 8 if rects else 1)

    def tick(self):
        self.c.sample()
        self.cache = None  # new numbers: re-render the panels on the next draw

    # --- CRT bars: a scanning beam sweeps along every fill (like the electron beam of an old
    # tube), over scanlines baked into the bar. Only the bar strips are redrawn per frame.
    BEAM_PERIOD = 2.8  # s for one sweep

    def fx_tick(self, dt=1 / 30):
        """Advance the beam clock; returns the bar strips to repaint."""
        if not self.fx_on or not self.bars:
            return []
        self.ft = getattr(self, 'ft', 0.0) + dt
        return [(int(x) - 2, int(y) - 3, int(w) + 4, int(h) + 6) for x, y, w, h, _c in self.bars if w >= 2]

    def draw_fx(self, cr):
        t = getattr(self, 'ft', 0.0)
        cr.set_operator(cairo.OPERATOR_ADD)
        for x, y, w, h, (r, g, b) in self.bars:
            if w < 2:
                continue
            bx = x - 46 + ((t / self.BEAM_PERIOD) % 1) * (w + 46)
            beam = cairo.LinearGradient(bx, 0, bx + 46, 0)
            beam.add_color_stop_rgba(0, r, g, b, 0)
            beam.add_color_stop_rgba(0.5, (r + 1) / 2, (g + 1) / 2, (b + 1) / 2, 0.85)
            beam.add_color_stop_rgba(1, r, g, b, 0)
            cr.set_source(beam)
            cr.rectangle(max(x, bx), y - 2, min(x + w, bx + 46) - max(x, bx), h + 4)
            cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)

    # --- primitives
    @staticmethod
    def text(cr, x, y, s, size, color, bold=False, align='left'):
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(size)
        if align == 'right':
            x -= cr.text_extents(s).x_advance
        cr.set_source_rgba(*color, 1)
        cr.move_to(x, y)
        cr.show_text(s)

    def panel(self, cr, rect, title):
        x, y, w, h = rect
        cr.set_source_rgba(*ACCENT, 0.10)  # faint glow
        cr.rectangle(x - 3, y - 3, w + 6, h + 6)
        cr.fill()
        cr.set_source_rgba(*PANEL_BG)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.95)
        cr.rectangle(x, y, w, 22)
        cr.fill()
        cr.set_source_rgba(*BORDER, 1)
        cr.set_line_width(1)
        cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
        cr.stroke()
        cr.set_source_rgba(0.122, 0.42, 0.208, 1)
        cr.move_to(x, y + 22.5)
        cr.line_to(x + w, y + 22.5)
        cr.stroke()
        self.text(cr, x + 10, y + 15, title, 12, ACCENT)
        self.text(cr, x + w - 10, y + 15, '[-] ●', 12, DIM, align='right')

    @staticmethod
    def fit(cr, s, size, maxw, bold=False):
        """s cut with an ellipsis to fit maxw pixels."""
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(size)
        if cr.text_extents(s).x_advance <= maxw:
            return s
        while s and cr.text_extents(s + '…').x_advance > maxw:
            s = s[:-1]
        return s + '…'

    @staticmethod
    def spark(cr, x, y, w, h, hist, vmax=None):
        vals = list(hist)
        if len(vals) < 2:
            return
        vmax = vmax or max(max(vals), 1e-9)
        step = w / (HIST - 1)
        x0 = x + w - step * (len(vals) - 1)
        cr.set_line_width(1.5)
        cr.move_to(x0, y + h - h * min(vals[0], vmax) / vmax)
        for i, v in enumerate(vals[1:], 1):
            cr.line_to(x0 + i * step, y + h - h * min(v, vmax) / vmax)
        cr.set_source_rgba(*ACCENT, 0.85)
        cr.stroke_preserve()
        cr.line_to(x + w, y + h)
        cr.line_to(x0, y + h)
        cr.close_path()
        cr.set_source_rgba(*ACCENT, 0.12)
        cr.fill()

    def bar(self, cr, x, y, w, h, pct, color):
        """Pip-Boy style: a dark track and a segmented fill with a phosphor glow.
        pct None: unknown progress -- only the track, with the beam sweeping along all of it."""
        cr.set_source_rgba(*BORDER, 0.9)
        cr.rectangle(x, y, w, h)
        cr.fill()
        if pct is None:
            self.bars.append((x, y, w, h, color))
            return
        fw = w * max(0, min(100, pct)) / 100
        cr.set_source_rgba(*color, 0.18)  # glow
        cr.rectangle(x, y - 2, fw, h + 4)
        cr.fill()
        cr.set_source_rgba(*color, 0.95)
        seg, gap = 9, 3
        sx = x
        while sx < x + fw:
            cr.rectangle(sx, y, min(seg, x + fw - sx), h)
            sx += seg + gap
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.35)  # scanlines across the fill
        for ly in range(int(y) + 1, int(y + h), 3):
            cr.rectangle(x, ly, fw, 1)
        cr.fill()
        cr.set_source_rgba(1, 0, 0.35, 0.45)  # chromatic aberration at the fill's edge
        cr.rectangle(x - 1, y, 1, h)
        cr.fill()
        cr.set_source_rgba(0, 1, 1, 0.4)
        cr.rectangle(x + fw, y, 1, h)
        cr.fill()
        self.bars.append((x, y, fw, h, color))

    def tile(self, cr, x, y, w, h, label, value, sub, hist=None, vmax=None, color=ACCENT):
        cr.set_source_rgba(*BORDER, 0.8)
        cr.set_line_width(1)
        cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
        cr.stroke()
        if hist is not None:
            self.spark(cr, x + 1, y + h - 40, w - 2, 38, hist, vmax)
        self.text(cr, x + 10, y + 18, label, 11, DIM)
        self.text(cr, x + 10, y + 50, value, 24 if w > 250 else 18, color, bold=True)
        self.text(cr, x + 10, y + 70, sub, 11, MUTED)

    # --- panels
    def render(self):
        """Panels into the cache surface (once per data tick)."""
        bx, by, bw, bh = self.box
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(bw)), max(1, int(bh)))
        cr = cairo.Context(surf)
        cr.translate(-bx, -by)
        self.bars = []
        d = self.c.data
        if d:
            for name, fn in (('sysmon', self.draw_sysmon), ('df', self.draw_df), ('sensors', self.draw_sensors),
                             ('pacman', self.draw_pacman), ('updates', self.draw_updates), ('claude', self.draw_claude),
                             ('builds', self.draw_builds),
                             ('proxmox', self.draw_proxmox), ('nfs', self.draw_nfs),
                             ('luneta', self.draw_luneta), ('now', self.draw_now), ('forge', self.draw_forge)):
                if name in self.rects:
                    fn(cr, self.rects[name], d)
        self.cache = surf

    def draw(self, cr):
        if not self.c.data:
            return
        if self.cache is None:
            self.render()
        cr.set_source_surface(self.cache, self.box[0], self.box[1])
        cr.paint()
        if self.fx_on:
            self.draw_fx(cr)

    def draw_infra(self, cr, rect, infra):
        """Infra alerts in the sysmon title bar, right-aligned: counts, then the top items."""
        if not infra:
            return
        x, y, w, _ = rect
        c = infra['counts']
        cr.set_source_rgba(0, 0, 0, 1)  # hide the '[-] ●' decoration under the strip
        cr.rectangle(x + w * 0.3, y + 2, w * 0.7 - 2, 18)
        cr.fill()
        if c['critical']:
            head, color = f'infra ✕ {c["critical"]} critical · ▲ {c["warning"]}', DANGER
        elif c['warning']:
            head, color = f'infra ▲ {c["warning"]} warning', WARN
        else:
            head, color = f'infra ● ok · {c["info"]} updates', ACCENT
        if infra.get('error'):
            head, color = f'{head} · stale ({infra["error"]})', DIM
        items = ' · '.join(f'{"✕" if a["level"] == "critical" else "▲"} {a["title"]}: {a["reason"]}' for a in infra['top'])
        line = f'{head}   {items}' if items else head
        max_chars = int(w * 0.7 / 7.2)  # ~7.2 px per 12 px JetBrains Mono glyph
        if len(line) > max_chars:
            line = line[:max_chars - 1].rstrip(' ·:') + '…'
        self.text(cr, x + w - 10, y + 15, line, 12, color, align='right')

    def draw_sysmon(self, cr, rect, d):
        self.panel(cr, rect, 'sysmon.sh')
        self.draw_infra(cr, rect, d.get('infra'))
        x, y, w, h = rect
        pad, gap = 12, 10
        tw, th = (w - 2 * pad - 2 * gap) / 3, (h - 22 - 2 * pad - gap) / 2
        hist = self.c.hist
        g = d.get('gpu_info')
        if g:
            gsub = ' · '.join(filter(None, [f'{g["mhz"]} MHz' if g.get('mhz') else '',
                                            f'{g["watts"]:.0f} W' if g.get('watts') is not None else '',
                                            f'lim {g["cap_mhz"]}' if g.get('cap_mhz') and g.get('max_mhz') and g['cap_mhz'] < g['max_mhz'] else '']))
            gpu_tile = (g.get('name') or 'GPU').upper()[:14], f'{d["gpu"]:.0f}%', gsub, hist['gpu'], 100
        else:
            gpu_tile = 'GPU', 'n/a', 'no supported GPU', None, None
        ghz = f'{d["cpu_ghz"]:.1f} GHz · ' if d.get('cpu_ghz') else ''
        la = d['load']
        tiles = [
            ('CPU', f'{d["cpu"]:.0f}%', f'{ghz}{d["threads"]} threads', hist['cpu'], 100),
            gpu_tile,
            ('RAM', f'{d["mem_used"] / GiB:.1f}/{d["mem_total"] / GiB:.0f} GB', f'{d["mem"]:.0f}% · swap {d["swap_used"] / GiB:.1f} GB', hist['mem'], 100),
            ('IOWAIT', f'{d["iowait"]:.1f}%', f'load {la[0]:.2f} {la[1]:.2f} {la[2]:.2f}', hist['iowait'], max(5, max(hist['iowait'], default=0))),
            (f'NET {d.get("iface") or ""}'.strip(), f'↓ {fmt_rate(d["rx"])}', f'↑ {fmt_rate(d["tx"])}', hist['rx'], None),
            ('SYSTEM', f'up {fmt_uptime(d["uptime"])}', f'{d["kernel"].split("-")[0]} · {d["procs"]} proc', None, None),
        ]
        for i, (label, value, sub, hs, vmax) in enumerate(tiles):
            cx = x + pad + (i % 3) * (tw + gap)
            cy = y + 22 + pad + (i // 3) * (th + gap)
            color = level_color(d['cpu'], 80, 95) if label == 'CPU' else level_color(d['mem'], 80, 92) if label == 'RAM' else ACCENT
            self.tile(cr, cx, cy, tw, th, label, value, sub, hs, vmax, color)

    def draw_df(self, cr, rect, d):
        self.panel(cr, rect, 'df -h')
        x, y, w, h = rect
        yy = y + 42
        rows = max(1, (h - 30) // 68)
        for dk in d['disks'][:rows]:
            name = dk['name'][:-2] if dk['name'].startswith('nvme') and dk['name'].endswith('n1') else dk['name']
            color = level_color(dk['pct'], 80, 92)
            self.text(cr, x + 12, yy, name, 12, ACCENT, bold=True)
            self.text(cr, x + 72, yy, dk['model'][:24], 11, MUTED)
            self.text(cr, x + w - 12, yy, f'{dk["pct"]:.0f}%', 13, color, bold=True, align='right')
            self.bar(cr, x + 12, yy + 8, w - 24, 8, dk['pct'], color)
            self.text(cr, x + 12, yy + 32, f'{dk["used"] / GiB:.0f}/{dk["total"] / GiB:.0f} GB', 11, TEXT)
            self.text(cr, x + 132, yy + 32, ' '.join(dk['mounts'])[:22], 11, DIM)
            self.text(cr, x + w - 12, yy + 32, f'R {fmt_rate(dk["rd"])} W {fmt_rate(dk["wr"])}', 10, DIM, align='right')
            yy += 68

    def draw_sensors(self, cr, rect, d):
        self.panel(cr, rect, 'sensors')
        x, y, w, h = rect
        yy = y + 44
        for sensor, val in d['temps'][:6]:
            color = ZONE_COLOR[sensor.zone(val)]
            self.text(cr, x + 12, yy, sensor.label[:18], 12, TEXT)
            self.bar(cr, x + 170, yy - 8, w - 260, 8, 100 * val / sensor.limit, color)
            self.text(cr, x + w - 12, yy, f'{val:.0f}°C', 13, color, bold=True, align='right')
            yy += 26
        g = d.get('gpu_info')
        if g and g.get('mhz'):
            lim = f' / lim {g["cap_mhz"]}' if g.get('cap_mhz') and g.get('max_mhz') and g['cap_mhz'] < g['max_mhz'] else ''
            watts = f' · {g["watts"]:.0f} W' if g.get('watts') is not None else ''
            self.text(cr, x + 12, y + h - 14, f'GPU {g["mhz"]} MHz{lim}{watts}', 11, DIM)

    def draw_pacman(self, cr, rect, d):
        p = d.get('pacman')
        if not p:
            return
        x, y, w, h = rect
        self.panel(cr, rect, p.get('tool') or 'pacman')
        if p['state'] == 'idle':
            self.draw_pacman_idle(cr, rect, p)
            return
        if p['state'] in ('aur', 'prep'):
            self.draw_pacman_aur(cr, rect, p)
            return
        right = x + w - 12
        self.text(cr, x + 12, y + 42, PHASE.get(p['state'], p['state'].upper()), 12, ACCENT, bold=True)
        self.text(cr, right, y + 42, f'run {fmt_eta(p.get("elapsed"))}', 11, DIM, align='right')

        # this package (or hook / AUR build)
        pct = p.get('pkg_pct')
        if p['state'] == 'hooks':
            info = f'{p.get("hook_s", 0):.0f}s'
        elif pct is not None:
            info = f'{pct:.0f}% · {fmt_eta(p.get("pkg_eta"))}'
        else:
            info = ''
        self.text(cr, right, y + 64, info, 11, MUTED, align='right')
        info_w = cr.text_extents(info).x_advance  # font still set by text()
        self.text(cr, x + 12, y + 64, self.fit(cr, p.get('pkg') or '', 11, w - 36 - info_w), 11, TEXT)
        self.bar(cr, x + 12, y + 72, w - 24, 8, pct, ACCENT if pct is not None else MUTED)

        # the whole run
        total, done = p.get('total') or 0, p.get('done') or 0
        if p['state'] in ('download', 'verify') and p.get('dl_files'):
            count = f'{p["dl_files"][0]}/{p["dl_files"][1]} dl'
        else:
            count = f'{done}/{total} pkgs' if total else ''
        self.text(cr, x + 12, y + 102, 'TOTAL', 11, DIM)
        self.text(cr, x + 60, y + 102, count, 11, TEXT)
        self.text(cr, right, y + 102, f'ETA {fmt_eta(p.get("eta"))}', 12, ACCENT, bold=True, align='right')
        self.bar(cr, x + 12, y + 110, w - 24, 8, None if p['state'] == 'sync' else p.get('pct'), ACCENT)

        # speed, sizes, the hook / last package
        if p.get('dl_total') and p['state'] in ('download', 'verify'):
            self.text(cr, x + 12, y + 140, f'↓ {fmt_rate(p.get("speed") or 0)}', 11, TEXT)
            self.text(cr, x + 12, y + 156, f'{fmt_size(p["dl_bytes"])}/{fmt_size(p["dl_total"])}', 11, MUTED)
            self.spark(cr, x + w / 2, y + 128, w / 2 - 12, 30, self.c.hist['dl'])
        notes = []
        if p.get('size_delta'):
            notes.append(f'disk {fmt_size(p["size_delta"], sign=True)}')
        if p.get('last_pkg'):
            notes.append(f'✓ {p["last_pkg"]}')
        self.text(cr, x + 12, y + 178, self.fit(cr, ' · '.join(notes), 11, w - 24), 11, DIM)
        self.draw_pacman_alerts(cr, rect, p, y + h - 14)

    def draw_pacman_aur(self, cr, rect, p):
        """yay / makepkg: the package being built (time from its past builds), the queue, the stage."""
        x, y, w, h = rect
        right = x + w - 12
        self.text(cr, x + 12, y + 42, PHASE[p['state']], 12, ACCENT, bold=True)
        self.text(cr, right, y + 42, f'run {fmt_eta(p.get("elapsed"))}', 11, DIM, align='right')

        pct = p.get('pkg_pct')
        if not p.get('pkg'):
            info = ''
        elif pct is not None:
            info = f'{pct:.0f}% · {fmt_eta(p.get("pkg_eta"))}'
        else:
            info = fmt_eta(p.get('build_s'))
        self.text(cr, right, y + 64, info, 11, MUTED, align='right')
        info_w = cr.text_extents(info).x_advance
        self.text(cr, x + 12, y + 64, self.fit(cr, p.get('pkg') or 'resolving…', 11, w - 36 - info_w), 11, TEXT)
        self.bar(cr, x + 12, y + 72, w - 24, 8, pct, ACCENT if pct is not None else MUTED)

        total, done = p.get('total'), p.get('done') or 0
        self.text(cr, x + 12, y + 102, 'QUEUE', 11, DIM)
        self.text(cr, x + 60, y + 102, f'{min(done + 1, total)}/{total} AUR' if total else 'AUR', 11, TEXT)
        self.text(cr, right, y + 102, f'ETA {fmt_eta(p.get("eta"))}', 12, ACCENT, bold=True, align='right')
        self.bar(cr, x + 12, y + 110, w - 24, 8, p.get('pct') if total else None, ACCENT)

        if p.get('stage'):
            self.text(cr, x + 12, y + 146, 'STAGE', 11, DIM)
            self.text(cr, x + 60, y + 146, p['stage'], 12, ACCENT, bold=True)
            self.text(cr, x + 150, y + 146, self.fit(cr, p.get('stage_detail') or '', 11, w - 162), 11, MUTED)
        facts = []
        if p.get('steps'):
            facts.append(f'ninja {p["steps"][0]}/{p["steps"][1]}')
        if p.get('avg_build'):
            facts.append(f'~{fmt_eta(p["avg_build"])}/pkg')
        if p.get('queue_left'):
            facts.append(f'{len(p["queue_left"])} left to build')
        if facts:
            self.text(cr, x + 12, y + 178, self.fit(cr, ' · '.join(facts), 11, w - 24), 11, DIM)
        self.draw_pacman_alerts(cr, rect, p, y + h - 14)

    def draw_pacman_alerts(self, cr, rect, p, yy):
        x, y, w, h = rect
        alerts = []
        if p.get('errors'):
            alerts.append((f'{p["errors"]} errors', DANGER))
        if p.get('warnings'):
            alerts.append((f'{p["warnings"]} warnings', WARN))
        if p.get('reboot'):
            alerts.append(('reboot: ' + ', '.join(p['reboot']), DANGER if 'kernel' in p['reboot'] else WARN))
        if p.get('pacnew'):
            alerts.append((f'{len(p["pacnew"])} .pacnew', WARN))
        if not alerts:
            return
        s = self.fit(cr, '⚠ ' + ' · '.join(a for a, _ in alerts), 11, w - 24)
        self.text(cr, x + 12, yy, s, 11, alerts[0][1])

    def draw_pacman_idle(self, cr, rect, p):
        x, y, w, h = rect
        right = x + w - 12
        now = time.time()
        last = p.get('last')
        self.text(cr, x + 12, y + 42, 'LAST -Syu', 11, DIM)
        self.text(cr, right, y + 42, 'idle', 11, DIM, align='right')
        if last:
            self.text(cr, x + 12, y + 68, fmt_ago(last['ts'], now), 18, ACCENT, bold=True)
            self.text(cr, right, y + 68, f'{last["pkgs"]} pkgs in {fmt_eta(last["dur"])}', 11, MUTED, align='right')
        else:
            self.text(cr, x + 12, y + 68, 'never', 18, DIM, bold=True)

        pend = p.get('pending')
        self.text(cr, x + 12, y + 92, 'PENDING', 11, DIM)
        if pend is None or pend == (None, None):
            val, color = '…', DIM
        else:
            repo, aur = pend
            val = ' · '.join(filter(None, [f'{repo} repo' if repo is not None else '', f'{aur} AUR' if aur else '']))
            color = WARN if (repo or 0) >= 100 else TEXT if (repo or aur) else ACCENT
            val = val or 'up to date'
        self.text(cr, x + 90, y + 92, val, 12, color, bold=True)

        # packages changed per day, last 30 days
        daily = p.get('daily') or []
        if daily:
            bx, by, bw, bh = x + 12, y + 104, w - 24, 34
            top = max(max(daily), 1)
            step = bw / len(daily)
            cr.set_source_rgba(*BORDER, 0.9)
            cr.rectangle(bx, by + bh, bw, 1)
            cr.fill()
            cr.set_source_rgba(*ACCENT, 0.8)
            for i, n in enumerate(daily):
                if n:
                    bar_h = max(2, bh * math.sqrt(n / top))  # sqrt: one huge day must not flatten the rest
                    cr.rectangle(bx + i * step + 1, by + bh - bar_h, max(1, step - 2), bar_h)
            cr.fill()
            self.text(cr, bx, by + bh + 14, '30 d', 10, DIM)
            self.text(cr, bx + bw, by + bh + 14, f'{sum(daily)} pkgs', 10, DIM, align='right')

        reboot = p.get('reboot') or []
        if reboot:
            self.text(cr, x + 12, y + 182, self.fit(cr, '⚠ reboot: ' + ', '.join(reboot), 11, w - 24), 11,
                      DANGER if 'kernel' in reboot else WARN)
        else:
            self.text(cr, x + 12, y + 182, '✓ no reboot needed', 11, DIM)
        pacnew = p.get('pacnew') or []
        if pacnew:
            names = ' '.join(path.rsplit('/', 1)[-1][:-7] for path in pacnew)
            self.text(cr, x + 12, y + 202, self.fit(cr, f'⚠ {len(pacnew)} .pacnew: {names}', 11, w - 24), 11, WARN)
        else:
            self.text(cr, x + 12, y + 202, '✓ no .pacnew', 11, DIM)

    # --- homelab / afterlife panels
    def row_bar(self, cr, x, y, w, label, pct, value, color=None, lw=None):
        """label | bar | value on one line (y = text baseline)."""
        color = color or level_color(pct, 80, 92)
        lw = lw or 64
        self.text(cr, x, y, self.fit(cr, label, 11, lw - 6), 11, TEXT)
        self.text(cr, x + w, y, value, 11, color, bold=True, align='right')
        vw = cr.text_extents(value).x_advance
        self.bar(cr, x + lw, y - 8, max(10, w - lw - vw - 10), 8, pct, color)

    def panel_error(self, cr, rect, lines):
        x, y, w, h = rect
        for i, (s, color) in enumerate(lines):
            self.text(cr, x + 12, y + 44 + 20 * i, self.fit(cr, s, 11, w - 24), 11, color)

    def draw_proxmox(self, cr, rect, d):
        p, st = d.get('proxmox') or {}, d.get('stream') or {}
        x, y, w, h = rect
        self.panel(cr, rect, f'pve {p.get("node") or ""}'.strip())
        if p.get('error') and not p.get('cpu_hist'):
            self.panel_error(cr, rect, [(f'no data: {p["error"]}', WARN)])
            return
        if p.get('error') or not p.get('node'):
            self.panel_error(cr, rect, [(p.get('error') or 'connecting…', WARN if p.get('error') else DIM)])
            return
        iw = w - 24
        self.row_bar(cr, x + 12, y + 44, iw, 'CPU', p['cpu'], f'{p["cpu"]:.0f}%', level_color(p['cpu'], 70, 90), 46)
        mp = 100 * p['mem_used'] / p['mem_total'] if p['mem_total'] else 0
        self.row_bar(cr, x + 12, y + 62, iw, 'RAM', mp, f'{p["mem_used"] / GiB:.0f}/{p["mem_total"] / GiB:.0f} GB',
                     level_color(mp, 80, 92), 46)
        down = p.get('down') or []
        self.text(cr, x + 12, y + 86, f'{p["running"]}/{p["guests"]} up', 11, ACCENT if not down else WARN, bold=True)
        self.text(cr, x + 100, y + 86, self.fit(cr, ('down: ' + ' '.join(down)) if down else f'up {fmt_uptime(p["uptime"])}',
                                                11, w - 112), 11, WARN if down else DIM)
        yy = y + 108
        for stg in (p.get('storages') or [])[:4]:
            self.row_bar(cr, x + 12, yy, iw, stg['name'], stg['pct'], f'{stg["pct"]:.0f}%', None, 84)
            yy += 18
        audit = p.get('inventory_summary') or {}
        if audit and (p.get('inventory_at') or p.get('inventory_error')):
            smart_color = DANGER if audit['smart_issues'] else DIM
            summary = (f'SMART {audit["smart_known"]}/{audit["disk_count"]}'
                       f' · updates {audit["updates"]} · Docker {audit["docker_count"]}')
            if p.get('inventory_error'):
                summary = f'inventory: {p["inventory_error"]}'
                smart_color = WARN
            self.text(cr, x + 12, y + 180, self.fit(cr, summary, 9, w - 24), 9, smart_color)
        b = p.get('backup')
        if b and b.get('last_end'):
            ok = b.get('last_status') == 'OK'
            nxt = f' · next {fmt_left(b["next_run"] - time.time())}' if b.get('next_run') else ''
            self.text(cr, x + 12, y + h - 36, self.fit(cr, f'backup {b.get("schedule") or ""}: {b["last_status"]} '
                                                           f'{fmt_ago(b["last_end"], time.time())}{nxt}', 11, w - 24),
                      11, DIM if ok else DANGER)
        if st.get('live'):
            rate = f'{st["bps"] / 1e6:.1f} Mb/s' if st.get('bps') else '…'
            self.text(cr, x + 12, y + h - 14, self.fit(cr, f'● LIVE {rate} · {fmt_eta(st.get("for"))} → {st.get("peer")}',
                                                       11, w - 24), 11, DANGER, bold=True)
        else:
            self.text(cr, x + 12, y + h - 14, 'stream off', 11, DIM)

    def draw_nfs(self, cr, rect, d):
        shares = list(d.get('nfs') or [])
        proxmox = d.get('proxmox') or {}
        for share in proxmox.get('nfs') or []:
            server = share['source'].partition(':')[0]
            shares.append({
                'name': f'pve {share["name"]}', 'target': share['name'], 'source': share['source'],
                'server': server, 'mounted': share['active'], 'remote': True,
                'total': share['total'], 'used': share['used'], 'pct': share['pct'], 'rd': 0.0, 'wr': 0.0,
            })
        x, y, w, h = rect
        servers = sorted({s['server'] for s in shares})
        self.panel(cr, rect, f'nfs ({len(shares)}) ' + (', '.join(servers) if servers else ''))
        if not shares:
            msg = 'no NFS shares in /etc/fstab or Proxmox'
            if proxmox.get('nfs_error'):
                msg += f' · PVE: {proxmox["nfs_error"]}'
            self.panel_error(cr, rect, [(msg, WARN if proxmox.get('nfs_error') else DIM)])
            return
        yy = y + 42
        rows = max(1, (h - 30) // 36)
        shown_rows = rows - 1 if proxmox.get('nfs_error') else rows
        for sh in shares[:shown_rows]:
            if sh.get('remote'):
                if sh.get('pct') is not None:
                    self.row_bar(cr, x + 12, yy, w - 24, sh['name'], sh['pct'], f'{sh["pct"]:.0f}%', None, 120)
                    self.text(cr, x + 12, yy + 15, self.fit(cr, sh['source'], 10, w - 24), 10, MUTED)
                else:
                    self.text(cr, x + 12, yy, sh['name'], 12, MUTED, bold=True)
                    status = 'offline' if not sh['mounted'] else 'capacity unknown'
                    self.text(cr, x + w - 12, yy, status, 11, WARN if not sh['mounted'] else DIM, align='right')
                    self.text(cr, x + 12, yy + 15, self.fit(cr, sh['source'], 10, w - 24), 10, DIM)
            elif not sh['mounted']:
                self.text(cr, x + 12, yy, sh['name'], 12, DIM, bold=True)
                self.text(cr, x + w - 12, yy, 'automount · idle', 11, DIM, align='right')
            elif sh.get('hung'):
                self.text(cr, x + 12, yy, sh['name'], 12, DANGER, bold=True)
                self.text(cr, x + w - 12, yy, 'server not answering', 11, DANGER, align='right')
            elif 'pct' in sh:
                self.row_bar(cr, x + 12, yy, w - 24, sh['name'], sh['pct'], f'{sh["pct"]:.0f}%', None, 120)
                io = []
                if sh['rd'] >= 1e3:
                    io.append(f'R {fmt_rate(sh["rd"])}')
                if sh['wr'] >= 1e3:
                    io.append(f'W {fmt_rate(sh["wr"])}')
                self.text(cr, x + 12, yy + 15, f'{fmt_size(sh["used"])}/{fmt_size(sh["total"])}', 10, MUTED)
                self.text(cr, x + w - 12, yy + 15, ' '.join(io), 10, TEXT, align='right')
            else:
                self.text(cr, x + 12, yy, sh['name'], 12, MUTED, bold=True)
                self.text(cr, x + w - 12, yy, sh.get('error') or 'reading…', 11, DIM, align='right')
            yy += 36
        if proxmox.get('nfs_error'):
            self.text(cr, x + 12, y + h - 14, self.fit(cr, f'PVE NFS unavailable: {proxmox["nfs_error"]}', 10, w - 24),
                      10, WARN)

    KIND_TAG = {'host': 'pve', 'lxc': 'lxc', 'vm': 'vm', 'container': 'ctr', 'service': 'svc', 'addon': 'ha',
                'router': 'net', 'pc': 'pc', 'device': 'iot'}
    UPDATE_COLOR = {'critical': DANGER, 'attention': WARN, 'stale': WARN, 'update': ACCENT, 'unknown': DIM, 'ok': DIM}

    def draw_updates(self, cr, rect, d):
        """ariku.pl inventory: what needs an update or an intervention, most urgent first."""
        u = d.get('updates') or {}
        x, y, w, h = rect
        c = u.get('counts') or {}
        warn = c.get('attention', 0) + c.get('stale', 0)
        tally = ' · '.join(f'{n} {label}' for n, label in ((c.get('critical', 0), 'crit'), (warn, 'warn'),
                                                           (c.get('update', 0), 'upd')) if n)
        self.panel(cr, rect, f'updates{" · " + tally if tally else ""}')
        if u.get('error') == 'no token':
            self.panel_error(cr, rect, [('needs a Luneta device token:', WARN),
                                        ('ariku.pl/luneta → add device "Dashboard",', DIM),
                                        ('then save it to', DIM),
                                        ('~/.config/piwnica-dashboard/luneta.token', MUTED)])
            return
        if 'items' not in u:
            self.panel_error(cr, rect, [(u.get('error') or 'connecting…', WARN if u.get('error') not in (None, 'connecting…') else DIM)])
            return
        items = u['items']
        if not u.get('total'):
            self.panel_error(cr, rect, [('inventory is empty', DIM),
                                        ('install the PVE collector: afterlife deploy/inventory', DIM)])
        elif not items:
            self.text(cr, x + 12, y + 46, 'attack surface ok ✓' if u.get('hidden_routine') else 'all ok ✓', 13, ACCENT, bold=True)
            self.text(cr, x + 12, y + 64, f'{u["total"]} items checked', 11, MUTED)
        rows = max(1, (h - 22 - 20 - 26) // 18 + 1)
        if len(items) > rows:
            rows -= 1  # room for "+N more"
        yy = y + 42
        for it in items[:rows]:
            color = self.UPDATE_COLOR.get(it.get('status'), DIM)
            cr.set_source_rgba(*color, 0.95)
            cr.rectangle(x + 12, yy - 8, 7, 7)
            cr.fill()
            kind = 'cs' if str(it.get('id') or '').startswith('cs:') else \
                self.KIND_TAG.get(it.get('kind'), (it.get('kind') or '?')[:3])
            self.text(cr, x + 26, yy, kind, 10, DIM)
            kx = x + 54
            name = self.fit(cr, it.get('name') or it.get('id') or '?', 11, w * 0.5 - (kx - x), bold=True)
            self.text(cr, kx, yy, name, 11, TEXT if it.get('status') != 'ok' else MUTED, bold=True)
            nx = kx + cr.text_extents(name).x_advance + 10
            why = it.get('reason') or ('no data from its source' if it.get('status') == 'stale' else
                                       it.get('version') or it.get('status') or '')
            self.text(cr, x + w - 12, yy, self.fit(cr, why, 10, x + w - 12 - nx), 10, color, align='right')
            yy += 18
        if len(items) > rows:
            self.text(cr, x + 12, yy, f'+{len(items) - rows} more · piwnica-dashboard updates', 10, DIM)
        foot = []
        if u.get('hidden_routine'):
            foot.append(f'{u["hidden_routine"]} routine hidden')
        if u.get('hidden_ok'):
            foot.append(f'{u["hidden_ok"]} ok hidden')
        if u.get('age') is not None:
            foot.append(f'sync {fmt_ago(time.time() - u["age"], time.time())}')
        if u.get('error'):
            foot.append(u['error'])
        self.text(cr, x + 12, y + h - 12, self.fit(cr, ' · '.join(foot), 10, w - 24), 10, WARN if u.get('error') else DIM)

    def draw_claude(self, cr, rect, d):
        """Claude plan limits; the reset countdowns run every frame, the numbers come every minute."""
        c = d.get('claude') or {}
        x, y, w, h = rect
        self.panel(cr, rect, f'claude{" · " + c["plan"] if c.get("plan") else ""}')
        if c.get('error') == 'no credentials':
            self.panel_error(cr, rect, [('no Claude Code login found:', WARN),
                                        ('run `claude` and log in, the panel', DIM),
                                        ('reads ~/.claude/.credentials.json', MUTED)])
            return
        if 'limits' not in c:
            err = c.get('error') or 'connecting…'
            self.panel_error(cr, rect, [(err, DIM if err == 'connecting…' else WARN)])
            return
        now = time.time()
        yy = y + 44
        rows = list(c['limits'])
        ex = c.get('extra')
        if ex and ex.get('pct') is not None:
            used = f' {ex["used"]:.2f}/{ex["limit"]:.0f}{ex["currency"]}' if ex.get('used') is not None and ex.get('limit') else ''
            rows.append({'label': 'extra', 'pct': ex['pct'], 'severity': 'normal', 'resets_at': None, 'value': f'{ex["pct"]:.0f}%{used}'})
        for lim in rows[:max(1, (h - 44 - 20) // 20 + 1)]:
            pct = lim['pct']
            color = DANGER if pct >= 90 or lim.get('severity') not in (None, 'normal') else WARN if pct >= 70 else ACCENT
            left = f' · {fmt_left(lim["resets_at"] - now)}' if lim.get('resets_at') else ''
            self.row_bar(cr, x + 12, yy, w - 24, lim['label'], pct, lim.get('value') or f'{pct:.0f}%{left}', color, 88)
            yy += 20
        if not rows:
            self.text(cr, x + 12, yy, 'no limits in the response', 11, DIM)
        foot = []
        if c.get('error'):
            foot.append(c['error'])
        elif c.get('breakdown'):
            foot.append(' · '.join(f'{r["name"]} {r["pct"]:.0f}%' for r in c['breakdown'][:2]))
        if c.get('age') is not None:
            foot.append(f'sync {fmt_ago(now - c["age"], now)}')
        self.text(cr, x + 12, y + h - 12, self.fit(cr, ' · '.join(foot), 10, w - 24), 10, WARN if c.get('error') else DIM)

    def draw_luneta(self, cr, rect, d):
        lu = d.get('luneta') or {}
        x, y, w, h = rect
        self.panel(cr, rect, 'luneta')
        yy = y + 42
        for g in (lu.get('games') or [])[:2]:
            self.text(cr, x + 12, yy, g['name'], 12, ACCENT, bold=True)
            self.text(cr, x + w - 12, yy, f'reset {fmt_left(g["daily_in"])}', 11, MUTED, align='right')
            pct = 100 * g['done'] / g['tasks'] if g['tasks'] else 0
            color = DANGER if g['critical_left'] and g['daily_in'] is not None and g['daily_in'] < 3 * 3600 else \
                WARN if g['critical_left'] else ACCENT
            prem = g.get('premium')
            pv = f'{prem["amount"]} {prem["label"]}' if prem and prem.get('amount') is not None else ''
            self.row_bar(cr, x + 12, yy + 18, w - 24, f'daily {g["done"]}/{g["tasks"]}', pct, pv, color, 96)
            ev = g.get('event')
            if ev:
                self.text(cr, x + 12, yy + 35, self.fit(cr, f'{fmt_left(ev["ends_in"])} · {ev["name"]}', 10, w - 24), 10, DIM)
            yy += 56
        a = lu.get('analysis')
        if a and a.get('of'):
            state = 'dead' if a.get('dead') else 'paused' if a.get('paused') else 'running' if a.get('running') else 'stopped'
            pct = 100 * (a.get('mediaDoneS') or 0) / a['mediaTotalS'] if a.get('mediaTotalS') else 100 * (a.get('done') or 0) / a['of']
            eta = f' · ETA {fmt_eta(a.get("etaS"))}' if a.get('running') and a.get('etaS') else ''
            self.text(cr, x + 12, yy + 4, self.fit(cr, f'analiza {a.get("done", 0)}/{a["of"]} · {state}{eta}', 11, w - 24), 11,
                      DANGER if a.get('dead') else TEXT)
            self.bar(cr, x + 12, yy + 10, w - 24, 6, pct, ACCENT if a.get('running') else MUTED)
        hb = lu.get('heartbeat')
        if lu.get('agent_running'):
            agent, color = f'agent on · sync {fmt_ago(hb, time.time()) if hb else "…"}', DIM
        else:
            agent, color = f'agent off · last sync {fmt_ago(hb, time.time()) if hb else "never"}', WARN
        if not lu.get('remote'):
            agent += ' · no token'
        self.text(cr, x + 12, y + h - 14, self.fit(cr, agent, 11, w - 24), 11, color)

    def draw_now(self, cr, rect, d):
        n = d.get('now') or {}
        x, y, w, h = rect
        self.panel(cr, rect, 'co teraz')
        if n.get('error') == 'no token':
            self.panel_error(cr, rect, [('needs a Luneta device token:', WARN),
                                        ('ariku.pl/luneta → add device "Dashboard",', DIM),
                                        ('then save it to', DIM),
                                        ('~/.config/piwnica-dashboard/luneta.token', MUTED)])
            return
        if n.get('error'):
            self.panel_error(cr, rect, [(n['error'], WARN if n['error'] != 'connecting…' else DIM)])
            return
        now = n.get('now') or []
        if now:
            q = now[0]
            self.text(cr, x + 12, y + 46, self.fit(cr, q['quest'].get('title') or '?', 13, w - 24, bold=True), 13, TEXT, bold=True)
            why = ' · '.join(filter(None, [f'~{q["estimateMin"]} min' if q.get('estimateMin') else '', *(q.get('reasons') or [])[:2]]))
            self.text(cr, x + 12, y + 64, self.fit(cr, why, 11, w - 24), 11, MUTED)
            if len(now) > 1:
                self.text(cr, x + 12, y + 82, self.fit(cr, 'or: ' + ' · '.join(a['quest'].get('title') or '' for a in now[1:3]),
                                                       10, w - 24), 10, DIM)
        else:
            self.text(cr, x + 12, y + 46, 'nothing urgent', 13, ACCENT, bold=True)
        fixed = (n.get('fixedSoon') or [])[:1]
        if fixed:
            f = fixed[0]
            when = (None if not f.get('startAt') else
                    fmt_left(datetime.fromisoformat(f['startAt'].replace('Z', '+00:00')).timestamp() - time.time()))
            self.text(cr, x + 12, y + 104, self.fit(cr, f'next fixed: {f.get("title")}' + (f' in {when}' if when else ''), 11, w - 24),
                      11, TEXT)
        ch = n.get('chaos')
        if ch:
            col = {'calm': ACCENT, 'busy': WARN, 'heavy': DANGER}.get(ch.get('level'), ACCENT)
            big = ch.get('biggest') or {}
            self.row_bar(cr, x + 12, y + 128, w - 24, 'chaos', min(100, ch.get('score', 0)),
                         f'{ch.get("score", 0)} {ch.get("levelLabel") or ch.get("level") or ""}', col, 64)
            if big.get('label'):
                self.text(cr, x + 12, y + 144, self.fit(cr, f'most: {big["label"]} ({big.get("count", 0)})', 10, w - 24), 10, DIM)
        m = n.get('majster')
        if m:
            self.row_bar(cr, x + 12, y + h - 14, w - 24, f'majster {m.get("level")}', m.get('percent') or 0,
                         f'{m.get("totalXp", 0)} XP', ACCENT, 96)

    def draw_forge(self, cr, rect, d):
        f, g = d.get('forge') or {}, d.get('gpu_info') or {}
        x, y, w, h = rect
        self.panel(cr, rect, 'kuźnia')
        c = f.get('comfy')
        if f.get('comfy_up') and c:
            busy = c['running'] or c['pending']
            self.text(cr, x + 12, y + 44, 'ComfyUI', 12, ACCENT, bold=True)
            self.text(cr, x + w - 12, y + 44, f'running {c["running"]} · queue {c["pending"]}', 11, WARN if busy else MUTED,
                      align='right')
            if c.get('vram_total'):
                vp = 100 * c['vram_used'] / c['vram_total']
                self.row_bar(cr, x + 12, y + 64, w - 24, 'VRAM', vp, f'{c["vram_used"] / GiB:.1f}/{c["vram_total"] / GiB:.0f} GB',
                             level_color(vp, 80, 92), 50)
        else:
            self.text(cr, x + 12, y + 44, 'ComfyUI', 12, DIM, bold=True)
            self.text(cr, x + w - 12, y + 44, 'off', 11, DIM, align='right')
        if g:
            gp = d.get('gpu') or 0
            self.row_bar(cr, x + 12, y + 86, w - 24, 'GPU', gp,
                         ' · '.join(filter(None, [f'{gp:.0f}%', f'{g["watts"]:.0f} W' if g.get('watts') is not None else ''])),
                         level_color(gp, 80, 95), 50)
        s = f.get('softstart') or {}
        if s.get('active') == 'active':
            cap = ' / '.join(filter(None, [f'{s["max_mhz"]} MHz' if s.get('max_mhz') else '', f'{s["watts"]} W' if s.get('watts') else '']))
            self.text(cr, x + 12, y + h - 14, self.fit(cr, f'softstart on · cap {cap or "RP0"}', 11, w - 24), 11, DIM)
        elif s:
            self.text(cr, x + 12, y + h - 14, '⚠ gpu-softstart off: no clock cap (reset risk)', 11, DANGER)

    def draw_builds(self, cr, rect, d):
        b = d.get('builds') or {}
        x, y, w, h = rect
        active = b.get('active', 0)
        tally = f'{active} active' if active else 'idle'
        self.panel(cr, rect, f'builds · {tally}')
        if b.get('error') == 'no token':
            self.panel_error(cr, rect, [('needs a Forgejo token:', WARN),
                                        ('git.ariku.pl → Applications → Token,', DIM),
                                        ('then save it to', DIM),
                                        ('~/.config/piwnica-dashboard/forgejo.token', MUTED)])
            return
        if b.get('error') and not b.get('items'):
            self.panel_error(cr, rect, [(b['error'], WARN if b['error'] != 'connecting…' else DIM)])
            return
        items = b.get('items') or []
        if not items:
            self.panel_error(cr, rect, [('no builds monitored', DIM)])
            return

        now = time.time()
        avail_h = h - 42 - 18
        slot_h = max(38, avail_h // len(items))
        yy = y + 40
        for it in items[:3]:
            name = it.get('name') or it.get('repo') or '?'
            ver = it.get('version') or ''
            status = it.get('status') or 'unknown'

            if status == 'running':
                badge = f'● RUN {fmt_eta(it.get("elapsed"))}'
                col = ACCENT
            elif status == 'waiting':
                badge = f'○ WAIT {fmt_eta(it.get("queued_time"))}' if it.get('queued_time') else '○ QUEUED'
                col = WARN
            elif status == 'success':
                dur = f' {fmt_eta(it["duration"])}' if it.get('duration') else ''
                badge = f'✓ ok{dur}'
                col = ACCENT
            elif status == 'failure':
                dur = f' {fmt_eta(it["duration"])}' if it.get('duration') else ''
                badge = f'✗ fail{dur}'
                col = DANGER
            elif status == 'cancelled':
                badge = '⊘ cancelled'
                col = DIM
            else:
                badge = status
                col = DIM

            self.text(cr, x + 12, yy + 11, name, 11, TEXT, bold=True)
            nw = cr.text_extents(name).x_advance
            if ver:
                v_col = ACCENT if status == 'running' else MUTED
                self.text(cr, x + 12 + nw + 6, yy + 11, self.fit(cr, ver, 10, w - nw - 120), 10, v_col)
            self.text(cr, x + w - 12, yy + 11, badge, 10, col, bold=True, align='right')

            self.bar(cr, x + 12, yy + 18, w - 24, 6, it.get('pct'), col)

            wf = it.get('workflow') or ''
            job = it.get('job') or ''
            details = f'{wf} ({job})' if wf and job else (wf or job or '')
            if it.get('queued_runs'):
                details = f'{details} · +{it["queued_runs"]} queued' if details else f'+{it["queued_runs"]} queued'
            if details:
                self.text(cr, x + 12, yy + 34, self.fit(cr, details, 9, w * 0.6), 9, DIM)

            right_detail = ''
            if status in ('success', 'failure', 'cancelled') and it.get('age') is not None:
                right_detail = fmt_ago(now - it['age'], now)
            elif it.get('ref') and it.get('ref') != ver:
                right_detail = it['ref']
            if right_detail:
                self.text(cr, x + w - 12, yy + 34, self.fit(cr, right_detail, 9, w * 0.35), 9, DIM, align='right')

            yy += slot_h

        foot = []
        if b.get('age') is not None:
            foot.append(f'sync {fmt_ago(now - b["age"], now)}')
        foot.append('git.ariku.pl')
        if b.get('error'):
            foot.append(b['error'])
        self.text(cr, x + 12, y + h - 12, self.fit(cr, ' · '.join(foot), 10, w - 24), 10,
                  WARN if b.get('error') else DIM)
