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

    def test_cutouts_are_the_panels_inside_the_window_in_window_pixels(self):
        panels = [(20, 330, 340, 230), (0, 0, 50, 50), (780, 1690, 400, 210), (500, 1800, 10, 10)]
        self.assertEqual(mascot.cutouts([100, 395, 1000, 1450], panels),
                         [[0, 0, 260, 165], [680, 1295, 320, 155], [400, 1405, 10, 10]])
        self.assertEqual(mascot.cutouts([100, 395, 1000, 1450], []), [])

    def test_an_act_request_is_taken_once(self):
        path = os.path.join(tempfile.mkdtemp(), 'rt', 'mascot-act')
        self.assertEqual(mascot.take_act(path), [])
        mascot.request_act(['heart_hands', 'love'], path)
        self.assertEqual(mascot.take_act(path), ['heart_hands', 'love'])
        self.assertFalse(os.path.exists(path))
        with open(path, 'w') as f:
            f.write('{broken')
        self.assertEqual(mascot.take_act(path), [])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(mascot.act_js(['a', 'b"c']),
                         'setTimeout(() => mascot.act("a"), 0); setTimeout(() => mascot.act("b\\"c"), 400);')
        self.assertTrue(mascot.act_path({'XDG_RUNTIME_DIR': '/run/user/7'}).startswith('/run/user/7/piwnica-dashboard/'))

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


def _model(d, catalog=False):
    """A model with motions in and outside Idle and three expressions; optionally its catalogue."""
    import json
    os.makedirs(os.path.join(d, 'motions'), exist_ok=True)
    groups = {'Idle': ['idle'], 'wave': ['wave'], 'dance_happy': ['dance_happy'], 'idle_bored': ['idle_bored'],
              'hair_fix': ['hair_fix'], 'dark_casting': ['dark_casting'], 'Empty': []}
    for names in groups.values():
        for n in names:
            with open(os.path.join(d, 'motions', f'{n}.motion3.json'), 'w') as f:
                json.dump({'Meta': {'Duration': 4.0}}, f)
    open(os.path.join(d, 'w.moc3'), 'w').close()
    model = os.path.join(d, 'w.model3.json')
    with open(model, 'w') as f:
        json.dump({'FileReferences': {
            'Moc': 'w.moc3',
            'Motions': {g: [{'File': f'motions/{n}.motion3.json'} for n in names] for g, names in groups.items()},
            'Expressions': [{'Name': n, 'File': f'{n}.exp3.json'} for n in ('happy', 'smug', 'void')]}}, f)
    sec = {}
    if catalog:
        sec['act_catalog'] = os.path.join(d, 'catalog.json')
        with open(sec['act_catalog'], 'w') as f:
            json.dump({'chibi': {'model3': 'runtime-chibi/c.model3.json', 'motions': {'wave': {'category': 'dance'}}},
                       'main': {'model3': 'runtime/w.model3.json', 'motions': {
                           'wave': {'category': 'gesture', 'tags': ['greeting'], 'face': False, 'duration': 3.2},
                           'dance_happy': {'category': 'dance', 'tags': ['dance'], 'face': True, 'duration': 7.5},
                           'idle_bored': {'category': 'idle', 'tags': ['idle'], 'face': True, 'duration': 10.0},
                           'hair_fix': {'category': 'activity', 'tags': ['cute'], 'face': False, 'duration': 3.0},
                           'dark_casting': {'category': 'gesture', 'tags': ['wow', 'shadow'], 'duration': 5.0}}}}, f)
    return model, sec


@unittest.skipIf(mascot is None, 'python-gobject not installed')
class TestMascotActs(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cache = os.environ.get('XDG_CACHE_HOME')
        os.environ['XDG_CACHE_HOME'] = os.path.join(self.d, 'cache')  # never the machine's own measurement

    def tearDown(self):
        if self.cache is None:
            del os.environ['XDG_CACHE_HOME']
        else:
            os.environ['XDG_CACHE_HOME'] = self.cache

    def test_kinds_come_from_the_group_names_without_a_catalogue(self):
        model, sec = _model(self.d)
        a = mascot.acts(model, sec)
        self.assertEqual(a['gesture'], ['wave', 'hair_fix', 'dark_casting'])  # Idle and the empty group are out
        self.assertEqual(a['dance'], ['dance_happy'])
        self.assertEqual(a['idle'], ['idle_bored'])
        self.assertEqual(a['emote'], ['happy', 'smug', 'void'])
        self.assertEqual(a['plain'], [])  # nothing known about the faces
        self.assertEqual(a['every'], [20.0, 60.0])
        self.assertFalse(a['log'])

    def test_the_catalogue_gives_kind_face_and_length(self):
        model, sec = _model(self.d, catalog=True)
        a = mascot.acts(model, sec)
        self.assertEqual(a['gesture'], ['wave', 'hair_fix', 'dark_casting'])  # the part for this model, not chibi
        self.assertEqual(a['plain'], ['wave', 'hair_fix'])
        secs = {m['name']: m['seconds'] for m in mascot.act_motions(model, sec)}
        self.assertEqual(secs['wave'], 3.2)
        self.assertEqual(secs['dance_happy'], 7.5)
        self.assertEqual({m['name']: m['seconds'] for m in mascot.act_motions(model, {})}['wave'], 4.0)  # the file

    def test_skip_by_name_tag_and_idle_skip(self):
        model, sec = _model(self.d, catalog=True)
        sec.update(act_skip=['wow', 'smug', 'idle_bored'], idle_skip=['hair_fix'])
        a = mascot.acts(model, sec)
        self.assertEqual(a['gesture'], ['wave'])
        self.assertEqual(a['idle'], [])
        self.assertEqual(a['emote'], ['happy', 'void'])

    def test_only_the_kinds_asked_for_and_none_at_all(self):
        model, sec = _model(self.d)
        a = mascot.acts(model, dict(sec, acts=['dance', 'bogus']))
        self.assertEqual((a['gesture'], a['dance'], a['idle'], a['emote']), ([], ['dance_happy'], [], []))
        a = mascot.acts(model, dict(sec, acts=[]))
        self.assertFalse(any(a[k] for k in mascot.ACT_KINDS))
        self.assertFalse(any(mascot.acts(os.path.join(self.d, 'none.model3.json'), {})[k] for k in mascot.ACT_KINDS))

    def test_pause_between_acts_is_clamped(self):
        self.assertEqual(mascot.act_every({'act_every': [1, 2]}), [5.0, 5.0])
        self.assertEqual(mascot.act_every({'act_every': [30, 10]}), [30.0, 30.0])
        self.assertEqual(mascot.act_every({'act_every': 'often'}), [20.0, 60.0])
        self.assertEqual(mascot.act_every({}), [20.0, 60.0])

    def test_measured_cut_off_motions_are_left_out_until_the_model_or_box_changes(self):
        import json
        model, sec = _model(self.d)
        path = mascot.acts_cache_path()
        self.assertTrue(path.startswith(self.d))
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f:
            json.dump({**mascot._acts_key(model, sec, (430, 1250)), 'clipped': ['wave']}, f)
        self.assertEqual(mascot.acts(model, sec, (430, 1250))['gesture'], ['hair_fix', 'dark_casting'])
        self.assertEqual(mascot.acts(model, sec, (600, 1250))['gesture'][0], 'wave')  # another box: not measured
        self.assertEqual(mascot.acts(model, dict(sec, zoom=1.2), (430, 1250))['gesture'][0], 'wave')
        os.utime(os.path.join(self.d, 'w.moc3'), (1, 1))  # a new export
        self.assertEqual(mascot.acts(model, sec, (430, 1250))['gesture'][0], 'wave')

    def test_a_motion_is_cut_off_when_it_reaches_an_edge_the_rest_pose_does_not(self):
        base = np.zeros((200, 100), np.uint8)
        base[50:200, 30:70] = 255  # stands on the bottom edge, clear of the sides and the top
        self.assertFalse(mascot.is_clipped(base, base))
        arm = base.copy()
        arm[80:130, 0:30] = 255  # 50 px of the left edge
        self.assertTrue(mascot.is_clipped(arm, base))
        jump = base.copy()
        jump[0:50, 0:60] = 255  # the top edge
        self.assertTrue(mascot.is_clipped(jump, base))
        inside = base.copy()
        inside[80:130, 5:30] = 255  # reaches far but stays inside
        self.assertFalse(mascot.is_clipped(inside, base))
        tail = base.copy()
        tail[120:140, 70:100] = 255  # the rest pose already touches the right edge here...
        swing = base.copy()
        swing[135:160, 70:100] = 255  # ...and the motion touches it a little lower
        self.assertFalse(mascot.is_clipped(swing, tail))
        brush = base.copy()
        brush[80:100, 0:30] = 255  # a strand of hair brushing the edge: too little to count
        self.assertFalse(mascot.is_clipped(brush, base))

    def test_page_gets_the_acts_and_a_measuring_run_gets_none(self):
        model, sec = _model(self.d)
        paths = {'model': model, 'core': '/c.js', 'pixi': '/p.js', 'cubism4': '/x.js'}
        html = mascot.page_html(paths, 300, 900, dict(sec, act_every=[30, 90], act_log=True))
        self.assertIn('"dance": ["dance_happy"]', html)
        self.assertIn('"every": [30.0, 90.0]', html)
        self.assertIn('"log": true', html)
        for name in ('core', 'pixi', 'cubism4'):
            open(os.path.join(self.d, name), 'w').close()
        cfg = {'mascot': dict(sec, model=model, core=os.path.join(self.d, 'core'), libs=self.d)}
        os.rename(os.path.join(self.d, 'pixi'), os.path.join(self.d, 'pixi.min.js'))
        os.rename(os.path.join(self.d, 'cubism4'), os.path.join(self.d, 'cubism4.min.js'))
        still, paths = mascot._still(cfg, log=lambda s: None)
        self.assertEqual(still['acts'], [])
        self.assertIn('"dance": []', mascot.page_html(paths, 300, 900, still))
        self.assertEqual(mascot._still({'mascot': {}}, log=lambda s: None), (None, None))


class TestMascotConfig(unittest.TestCase):
    def test_off_by_default_and_template_parses(self):
        import tomllib
        self.assertFalse(config_mod.DEFAULTS['mascot']['enabled'])
        tpl = tomllib.loads(config_mod.TEMPLATE)['mascot']
        self.assertEqual(set(tpl), set(config_mod.DEFAULTS['mascot']))
        self.assertFalse(tpl['enabled'])


if __name__ == '__main__':
    unittest.main()
