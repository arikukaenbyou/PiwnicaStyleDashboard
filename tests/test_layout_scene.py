"""Monitor choice, holdout lookup, config, panel layout around a character and the rendered
scene (pure cairo, no display needed)."""
import os
import tempfile
import unittest

import cairo
import numpy as np

from piwnica_dashboard import config
from piwnica_dashboard.collector import DemoCollector
from piwnica_dashboard.layout import layout
from piwnica_dashboard.scene import Scene, holdout_mask, load_holdout

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, '..', 'examples', 'orin', 'holdout-1200x1920.png')


def mon(name, w, h, primary=False):
    return {'name': name, 'w': w, 'h': h, 'primary': primary}


class TestConfig(unittest.TestCase):
    def test_pick_monitor(self):
        mons = [mon('DP-2', 1920, 1080, True), mon('HDMI-1', 1280, 1024), mon('DP-1', 1200, 1920)]
        self.assertEqual(config.pick_monitor(mons), 2)                      # first portrait
        self.assertEqual(config.pick_monitor(mons, 'HDMI-1'), 1)            # by name
        self.assertEqual(config.pick_monitor(mons, 'nope'), 2)              # unknown name -> auto
        self.assertEqual(config.pick_monitor(mons[:2]), 0)                  # no portrait -> primary
        self.assertEqual(config.pick_monitor([mon('A', 1920, 1080), mon('B', 2560, 1440, True)]), 1)
        self.assertIsNone(config.pick_monitor([]))

    def test_resolve_holdout(self):
        d = tempfile.mkdtemp()
        wall = os.path.join(d, 'wall.png')
        hold = os.path.join(d, 'holdout-1200x1920.png')
        for p in (wall, hold):
            open(p, 'w').close()
        self.assertEqual(config.resolve_holdout('auto', 'DP-1', 1200, 1920, wallpaper_of=lambda c: wall), hold)
        self.assertIsNone(config.resolve_holdout('auto', 'DP-1', 1920, 1080, wallpaper_of=lambda c: wall))
        self.assertIsNone(config.resolve_holdout('auto', 'DP-1', 1200, 1920, wallpaper_of=lambda c: None))
        self.assertEqual(config.resolve_holdout(d, 'DP-1', 1200, 1920), hold)
        self.assertEqual(config.resolve_holdout(hold, 'DP-1', 1, 1), hold)
        self.assertIsNone(config.resolve_holdout('', 'DP-1', 1200, 1920))

    def test_first_start_writes_template_and_user_values_win(self):
        path = os.path.join(tempfile.mkdtemp(), 'sub', 'config.toml')
        cfg = config.load(path)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(cfg['display']['monitor'], 'auto')
        with open(path, 'a') as f:
            f.write('\n[extra]\nx = 1\n')
        with open(path) as f:
            txt = f.read().replace('monitor = "auto"', 'monitor = "DP-1"').replace('cpu_limit = 0', 'cpu_limit = 95')
        with open(path, 'w') as f:
            f.write(txt)
        cfg = config.load(path)
        self.assertEqual((cfg['display']['monitor'], cfg['temps']['cpu_limit'], cfg['display']['tint']), ('DP-1', 95, 0.45))


def example_mask():
    s = load_holdout(EXAMPLE, 1200, 1920)
    assert s is not None, 'examples/orin/holdout-1200x1920.png missing'
    return s, holdout_mask(s, 1200, 1920)


def overlaps(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class TestLayout(unittest.TestCase):
    def test_example_character_keeps_all_panels_off_her(self):
        _, mask = example_mask()
        rects = layout(mask)
        self.assertTrue({'sysmon', 'df', 'sensors', 'pacman', 'updates', 'proxmox'} <= set(rects))
        for x, y, w, h in rects.values():
            self.assertFalse(mask[y:y + h, x:x + w].any())
            self.assertTrue(0 <= x and 0 <= y and x + w <= 1200 and y + h <= 1920)
        vals = list(rects.values())
        self.assertFalse(any(overlaps(a, b) for i, a in enumerate(vals) for b in vals[i + 1:]))

    def test_no_room_means_no_panel(self):
        wide = np.zeros((1920, 1200), bool)
        wide[100:, :] = True
        self.assertEqual(layout(wide), {})

    def test_landscape_monitor_without_holdout(self):
        rects = layout(np.zeros((1080, 1920), bool))
        self.assertEqual(sorted(rects), ['df', 'luneta', 'nfs', 'now', 'pacman', 'proxmox', 'sensors', 'sysmon', 'updates'])
        vals = list(rects.values())
        self.assertFalse(any(overlaps(a, b) for i, a in enumerate(vals) for b in vals[i + 1:]))
        self.assertLessEqual(rects['sysmon'][2], 1160)
        # the pacman panel shares the right column with sensors, one margin under sysmon
        self.assertEqual(rects['pacman'][0], rects['sensors'][0])
        self.assertEqual(rects['pacman'][1], rects['sysmon'][1] + rects['sysmon'][3] + 20)

    def test_example_panels_line_up(self):
        _, mask = example_mask()
        r = layout(mask)
        right = lambda n: r[n][0] + r[n][2]  # noqa: E731
        self.assertEqual((r['sysmon'][0], r['df'][0]), (20, 20))                       # common left edge
        self.assertEqual({right('sysmon'), right('sensors'), right('pacman')}, {1180})  # common right edge
        self.assertEqual(r['pacman'][1], r['sysmon'][1] + r['sysmon'][3] + 20)          # same gap as to the edge
        # extra panels sit one margin under a panel, at the screen edge
        bottoms = {py + ph + 20 for _px, py, _pw, ph in r.values()}
        for name in ('updates', 'proxmox'):
            self.assertIn(r[name][1], bottoms)
            self.assertIn(r[name][0], (20, 1180 - r[name][2]))

    def test_only_wanted_extras(self):
        rects = layout(np.zeros((1080, 1920), bool), extra=('nfs',))
        self.assertEqual(sorted(rects), ['df', 'nfs', 'sensors', 'sysmon'])


class TestScene(unittest.TestCase):
    def render(self, scene, frames=90):
        for _ in range(frames):
            scene.tick()
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32, scene.W, scene.H)
        scene.draw(cairo.Context(out))
        a = np.frombuffer(out.get_data(), np.uint8).reshape(scene.H, out.get_stride())
        return a[:, 3:scene.W * 4:4]

    def test_character_area_stays_transparent_and_panels_are_drawn(self):
        holdout, mask = example_mask()
        scene = Scene(1200, 1920, holdout=holdout, collector=DemoCollector())
        alpha = self.render(scene)
        a = np.frombuffer(holdout.get_data(), np.uint8).reshape(1920, holdout.get_stride())[:, 3:1200 * 4:4]
        # solid part of the character: nothing at all; soft edge: at most what the edge lets through
        self.assertEqual(int(alpha[a == 255].max()), 0, 'something was drawn over the character')
        edge = (a > 0) & (a < 255)
        self.assertTrue(np.all(alpha[edge] <= 255 - a[edge] + 1), 'soft edge lets through too much')
        x, y, w, h = scene.dash.rects['sysmon']
        self.assertGreater(alpha[y + 5:y + h - 5, x + 5:x + w - 5].mean(), 200, 'panel not drawn')

    def test_traces_avoid_panels_and_tint_elsewhere(self):
        scene = Scene(1200, 1920, collector=DemoCollector())
        for x, y, w, h in scene.dash.rects.values():
            for tr in scene.traces:
                for px, py in tr['poly']:
                    self.assertFalse(x <= px <= x + w and y <= py <= y + h)
        alpha = self.render(scene, 1)
        self.assertGreater(alpha[1000, 600], 100)  # tint 0.45 -> ~115

    def test_dirty_rects_cover_moving_pulses(self):
        scene = Scene(800, 600, seed=7)
        rects = []
        for _ in range(60):
            rects += scene.tick()
        self.assertTrue(rects)
        for p in scene.pulses:
            bx, by, bw, bh = scene.pulse_box(p)
            self.assertTrue(any(x <= bx + 1 and y <= by + 1 and x + w >= bx + bw - 1 and y + h >= by + bh - 1
                                for x, y, w, h in rects))


if __name__ == '__main__':
    unittest.main()
