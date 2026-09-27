"""Cairo drawing of the three panels: sysmon.sh (tiles), df -h (disks), sensors (temperatures)."""
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


def level_color(v, warn, crit):
    return DANGER if v >= crit else WARN if v >= warn else ACCENT


class Dashboard:
    def __init__(self, rects, collector):
        self.rects = rects
        self.c = collector

    def tick(self):
        self.c.sample()

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

    @staticmethod
    def bar(cr, x, y, w, h, pct, color):
        cr.set_source_rgba(*BORDER, 0.9)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgba(*color, 0.9)
        cr.rectangle(x, y, w * max(0, min(100, pct)) / 100, h)
        cr.fill()

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
    def draw(self, cr):
        d = self.c.data
        if not d:
            return
        for name, fn in (('sysmon', self.draw_sysmon), ('df', self.draw_df), ('sensors', self.draw_sensors)):
            if name in self.rects:
                fn(cr, self.rects[name], d)

    def draw_sysmon(self, cr, rect, d):
        self.panel(cr, rect, 'sysmon.sh')
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
