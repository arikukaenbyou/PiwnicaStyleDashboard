"""Heartbeat file for Luneta's monitor (health.py); data from fakes, nothing from the machine."""
import json
import os
import stat
import tempfile
import unittest
from types import SimpleNamespace

from piwnica_dashboard import health


class TestHealth(unittest.TestCase):
    def test_alerts_only_real_trouble(self):
        data = {'proxmox': {'error': 'no token'}, 'builds': {'error': 'connecting…'}, 'updates': {'error': None, 'total': 3},
                'now': {'error': 'HTTPError: 502'}, 'cpu': 12.5, 'temps': [], 'pacman': None}
        self.assertEqual(health.alerts(data), [{'source': 'now', 'error': 'HTTPError: 502'},
                                               {'source': 'proxmox', 'error': 'no token'}])
        self.assertEqual(health.alerts(None), [])

    def test_mascot_states(self):
        self.assertEqual(health.mascot_state(None), 'off')
        m = SimpleNamespace(ready=False, paused=False, error=None)
        self.assertEqual(health.mascot_state(m), 'loading')
        m.ready = True
        self.assertEqual(health.mascot_state(m), 'ready')
        m.paused = True
        self.assertEqual(health.mascot_state(m), 'hidden')
        m.error = 'model: 404'
        self.assertEqual(health.mascot_state(m), 'error: model: 404')

    def test_snapshot_and_atomic_write(self):
        doc = health.snapshot(1000.04, 900.0, '0.1.0', True, 'hidden', {'proxmox': {'error': 'no token'}}, pid=42)
        self.assertEqual(doc, {'schema': 1, 'pid': 42, 'version': '0.1.0', 'started': 900.0, 'at': 1000.0,
                               'interval': health.INTERVAL, 'paused': True, 'mascot': 'hidden',
                               'alerts': [{'source': 'proxmox', 'error': 'no token'}]})
        d = tempfile.mkdtemp()
        path = os.path.join(d, 'piwnica-dashboard', 'health.json')
        health.write(path, doc)
        health.write(path, {**doc, 'at': 1010.0})  # replaced in place, no temp file left behind
        with open(path, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['at'], 1010.0)
        self.assertEqual(os.listdir(os.path.dirname(path)), ['health.json'])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        health.remove(path)
        self.assertFalse(os.path.exists(path))
        health.remove(path)  # already gone: no error

    def test_default_path(self):
        self.assertEqual(health.default_path({'XDG_RUNTIME_DIR': '/run/user/1000'}),
                         '/run/user/1000/piwnica-dashboard/health.json')
        self.assertTrue(health.default_path({}).endswith('/piwnica-dashboard/health.json'))


if __name__ == '__main__':
    unittest.main()
