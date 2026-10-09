"""The updates panel: the ariku.pl inventory view, the token, errors and the show_ok switch."""
import os
import tempfile
import unittest

import cairo

from piwnica_dashboard import config
from piwnica_dashboard.collect import updates
from piwnica_dashboard.collector import DemoCollector
from piwnica_dashboard.draw import Dashboard

TOKEN = 'lnt_' + 'a' * 43
DOC = {'generatedAt': '2026-10-09T12:00:00Z', 'items': [
    {'id': 'svc:immich', 'name': 'Immich', 'kind': 'service', 'status': 'update', 'reason': 'dostępna v3.3.1'},
    {'id': 'lxc:128', 'name': 'afterlife', 'kind': 'lxc', 'status': 'ok', 'reason': None},
    {'id': 'router:mikrotik', 'name': 'MikroTik', 'kind': 'router', 'status': 'critical', 'reason': 'CVE'},
    {'id': 'ha:tasmota', 'name': 'tasmota', 'kind': 'device', 'status': 'stale', 'reason': None},
    {'id': 'docker:3/n8n', 'name': 'n8n', 'kind': 'container', 'status': 'attention', 'reason': 'restarting'},
]}


class TestView(unittest.TestCase):
    def test_most_urgent_first_and_ok_hidden(self):
        v = updates.view(DOC, 1000.0, show_ok=False, fetched_at=900.0)
        self.assertEqual([i['name'] for i in v['items']], ['MikroTik', 'n8n', 'Immich', 'tasmota'])
        self.assertEqual((v['hidden_ok'], v['total'], v['age']), (1, 5, 100.0))
        self.assertEqual(v['counts']['critical'], 1)

    def test_show_ok(self):
        v = updates.view(DOC, 1000.0, show_ok=True)
        self.assertEqual(v['items'][-1]['name'], 'afterlife')
        self.assertEqual(v['hidden_ok'], 0)


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.token = os.path.join(self.dir, 'luneta.token')
        self.cfg = os.path.join(self.dir, 'config.toml')

    def collector(self, fetch, show_ok=False):
        return updates.UpdatesCollector('https://ariku.pl/', self.token, show_ok, self.cfg,
                                        background=False, fetch=fetch, clock=lambda: 1000.0)

    def test_no_token_then_token_appears(self):
        calls = []
        c = self.collector(lambda url, h, **kw: calls.append((url, h)) or DOC)
        self.assertEqual(c.sample(), {'error': 'no token'})
        with open(self.token, 'w') as f:
            f.write(TOKEN)
        v = c.sample()
        self.assertEqual(calls, [('https://ariku.pl/api/infra/inventory', {'Authorization': f'Bearer {TOKEN}'})])
        self.assertEqual(len(v['items']), 4)
        self.assertIsNone(v['error'])

    def test_server_error_is_shown(self):
        with open(self.token, 'w') as f:
            f.write(TOKEN)

        def fail(url, h, **kw):
            raise OSError('HTTP 403 Inwentarz jest tylko dla właściciela.')
        self.assertIn('HTTP 403', self.collector(fail).sample()['error'])

    def test_show_ok_switch_without_restart(self):
        with open(self.token, 'w') as f:
            f.write(TOKEN)
        with open(self.cfg, 'w') as f:
            f.write('[updates]\nshow_ok = false\n')
        c = self.collector(lambda url, h, **kw: DOC)
        self.assertEqual(c.sample()['hidden_ok'], 1)
        with open(self.cfg, 'w') as f:
            f.write('[updates]\nshow_ok = true\n')
        os.utime(self.cfg, (1, 1))  # a different mtime, however fast the test runs
        self.assertEqual(c.sample()['hidden_ok'], 0)

    def test_read_value_survives_a_broken_file(self):
        with open(self.cfg, 'w') as f:
            f.write('[updates\nshow_ok = ')
        self.assertTrue(config.read_value(self.cfg, 'updates', 'show_ok', True))


class TestDraw(unittest.TestCase):
    def render(self, data):
        c = DemoCollector()
        c.data = dict(c.data, updates=data)
        dash = Dashboard({'updates': (0, 0, 360, 230)}, c)
        dash.render()
        return dash

    def test_states_render(self):
        for data in (DemoCollector().data['updates'], {'error': 'no token'}, {'error': 'HTTP 501'},
                     updates.view({'items': []}, 0), updates.view({'items': DOC['items'][1:2]}, 0),
                     updates.view({'items': DOC['items'] * 4}, 0)):
            self.assertIsInstance(self.render(data).cache, cairo.ImageSurface)


if __name__ == '__main__':
    unittest.main()
