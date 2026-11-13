import os
import signal
import tempfile
import unittest

import numpy as np

try:
    from piwnica_dashboard import mascot
except ImportError:  # no python-gobject
    mascot = None
from piwnica_dashboard import config as config_mod


@unittest.skipIf(mascot is None, 'python-gobject not installed')
class TestMascot(unittest.TestCase):
    def test_webkit_gc_keeps_off_the_apps_sigusr1(self):
        # JavaScriptCore's GC signal on SIGUSR1 (app.py's hide/show cycle) hung the dashboard
        self.assertEqual(os.environ.get('JSC_SIGNAL_FOR_GC'), str(int(signal.SIGUSR2)))

    def test_auto_place_is_bottom_centre_half_wide_2_to_3(self):
        self.assertEqual(mascot.place({'rect': 'auto'}, 1200, 1920), [300, 1020, 600, 900])

    def test_rect_is_clamped_to_the_monitor(self):
        self.assertEqual(mascot.place({'rect': [1000, 1800, 500, 500]}, 1200, 1920), [1000, 1800, 200, 120])
        self.assertEqual(mascot.place({'rect': [350, 860, 500, 750]}, 1200, 1920), [350, 860, 500, 750])

    def test_missing_files_are_named(self):
        d = tempfile.mkdtemp()
        for name in ('m.model3.json', 'core.js', 'pixi.min.js'):
            open(os.path.join(d, name), 'w').close()
        paths, missing = mascot.files({'model': os.path.join(d, 'm.model3.json'), 'core': os.path.join(d, 'core.js'), 'libs': d})
        self.assertIsNone(paths)
        self.assertEqual(missing, ['cubism4'])
        open(os.path.join(d, 'cubism4.min.js'), 'w').close()
        paths, missing = mascot.files({'model': os.path.join(d, 'm.model3.json'), 'core': os.path.join(d, 'core.js'), 'libs': d})
        self.assertEqual(missing, [])
        self.assertEqual(paths['cubism4'], os.path.join(d, 'cubism4.min.js'))
        self.assertEqual(mascot.files({})[1], ['model', 'core', 'pixi', 'cubism4'])

    def test_holdout_covers_the_outline_plus_margin_in_monitor_coordinates(self):
        alpha = np.zeros((100, 50), np.uint8)
        alpha[40:60, 20:30] = 255  # the model: a 10 x 20 block inside a 50 x 100 box
        h = mascot.holdout(alpha, [200, 300, 50, 100], 400, 600, margin=10)
        self.assertEqual(h.shape, (600, 400))
        ys, xs = np.nonzero(h)
        # block at x 220..229, y 340..359, grown by 10 px and a soft edge of ~3 px
        self.assertTrue(205 <= xs.min() <= 210 and 239 <= xs.max() <= 244, (xs.min(), xs.max()))
        self.assertTrue(325 <= ys.min() <= 330 and 369 <= ys.max() <= 374, (ys.min(), ys.max()))
        self.assertEqual(h[350, 225], 255)
        self.assertEqual(h[0, 0], 0)

    def test_faint_pixels_do_not_count(self):
        alpha = np.full((10, 10), 10, np.uint8)
        self.assertFalse(mascot.holdout(alpha, [0, 0, 10, 10], 20, 20, margin=2).any())

    def test_holdout_png_loads_as_a_scene_mask(self):
        from piwnica_dashboard.scene import load_holdout
        a = np.zeros((40, 30), np.uint8)
        a[10:20, 5:15] = 200
        path = os.path.join(tempfile.mkdtemp(), 'holdout-30x40.png')
        mascot.write_alpha_png(a, path)
        surf = load_holdout(path, 30, 40)
        self.assertIsNotNone(surf)
        self.assertEqual((mascot.surface_alpha(surf) > 0).sum(), 100)

    def test_page_gets_the_config_and_file_urls(self):
        html = mascot.page_html({'model': '/m/w.model3.json', 'core': '/c.js', 'pixi': '/p.js', 'cubism4': '/x.js'},
                                500, 750, {'fps': 99, 'fade': 0.18})
        self.assertIn('"model": "file:///m/w.model3.json"', html)
        self.assertIn('"fps": 60', html)
        self.assertIn('"fade": 0.18', html)
        self.assertNotIn('/*CONFIG*/', html)

    def test_idle_plan_lists_every_rest_pose_with_its_length(self):
        import json
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, 'motions'))
        for name, secs in (('idle', 8.0), ('think', 40.0)):
            with open(os.path.join(d, 'motions', f'{name}.motion3.json'), 'w') as f:
                json.dump({'Meta': {'Duration': secs}}, f)
        model = os.path.join(d, 'w.model3.json')
        with open(model, 'w') as f:
            json.dump({'FileReferences': {'Motions': {
                'Idle': [{'File': 'motions/idle.motion3.json'}, {'File': 'motions/think.motion3.json'},
                         {'File': 'motions/gone.motion3.json'}],
                'Wave': [{'File': 'motions/idle.motion3.json'}]}}}, f)
        # long motions capped, a missing file gets a default length, other groups ignored
        self.assertEqual(mascot.idle_plan(model, cap=15.0), [(0, 8.0), (1, 15.0), (2, 5.0)])
        self.assertEqual(mascot.idle_plan(os.path.join(d, 'none.model3.json')), [])
        # [mascot] idle_skip by file name: left out of the holdout and handed to the page as indexes
        sec = {'idle_skip': ['think']}
        self.assertEqual(mascot.idle_skip(model, sec), [1])
        self.assertEqual(mascot.idle_plan(model, skip=mascot.idle_skip(model, sec)), [(0, 8.0), (2, 5.0)])
        html = mascot.page_html({'model': model, 'core': '/c.js', 'pixi': '/p.js', 'cubism4': '/x.js'}, 300, 900, sec)
        self.assertIn('"idle_skip": [1]', html)
        with open(model, 'w') as f:
            json.dump({'FileReferences': {}}, f)
        self.assertEqual(mascot.idle_plan(model), [])


class TestMascotConfig(unittest.TestCase):
    def test_off_by_default_and_template_parses(self):
        import tomllib
        self.assertFalse(config_mod.DEFAULTS['mascot']['enabled'])
        tpl = tomllib.loads(config_mod.TEMPLATE)['mascot']
        self.assertEqual(set(tpl), set(config_mod.DEFAULTS['mascot']))
        self.assertFalse(tpl['enabled'])


if __name__ == '__main__':
    unittest.main()
