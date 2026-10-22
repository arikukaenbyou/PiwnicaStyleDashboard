"""The ariku.pl updater's status: the view, the token, transport errors, missing/old/incomplete data, drawing."""
import os
import socket
import tempfile
import unittest

import cairo

from piwnica_dashboard.collect import updater_status as us
from piwnica_dashboard.collect import updates
from piwnica_dashboard.collector import DemoCollector
from piwnica_dashboard.draw import Dashboard

TOKEN = 'f' * 64
RUNNING = {
    'generatedAt': '2026-10-22T18:00:00.000Z', 'configured': True, 'storageConfigured': True,
    'current': {'id': 'r1', 'kind': 'apply', 'state': 'running', 'startedAt': '2026-10-22T17:55:00.000Z',
                'finishedAt': None, 'durationSeconds': 300, 'targets': ['host', 'lxc:105'],
                'stage': {'target': 'lxc:105', 'message': 'Setting up openssl'}, 'error': None, 'exitCode': None,
                'logTail': [f'[lxc:105] line {i}' for i in range(40)]},
    'lastCheck': {'id': 'c1', 'state': 'succeeded', 'at': '2026-10-22T17:00:00.000Z', 'pendingTargets': 2,
                  'securityUpdates': 3},
    'targets': [{'key': 'host', 'name': 'Proxmox host', 'state': 'running', 'runId': 'r1', 'at': '2026-10-22T17:55:00.000Z', 'error': None},
                {'key': 'lxc:105', 'name': 'n8n', 'state': 'weird', 'runId': 'r1', 'at': None, 'error': None}],
    'recent': [],
}
FAILED = dict(RUNNING, current=dict(RUNNING['current'], state='failed', finishedAt='2026-10-22T17:58:00.000Z',
                                    durationSeconds=180, stage=None, error='SSH zakończyło się kodem 255.'),
              targets=[{'key': 'host', 'name': 'Proxmox host', 'state': 'failed', 'error': 'SSH zakończyło się kodem 255.'}])
T_1800 = us.iso('2026-10-22T18:00:00Z')  # "now" in the tests: 5 minutes after the run started


class TestView(unittest.TestCase):
    def test_running_counts_on_and_trims(self):
        v = us.view(RUNNING, T_1800 + 30, T_1800, 300)
        run = v['run']
        self.assertEqual((run['state'], run['kind'], run['duration']), ('running', 'apply', 330))
        self.assertEqual(run['stage'], 'lxc:105 Setting up openssl')
        self.assertEqual(len(run['log']), 6)
        self.assertEqual([(t['key'], t['state']) for t in v['targets']], [('host', 'running'), ('lxc:105', 'unknown')])
        self.assertEqual(v['last_check']['pending'], 2)
        self.assertEqual((v['age'], v['stale']), (30, False))

    def test_failed_keeps_server_duration_and_error(self):
        run = us.view(FAILED, T_1800 + 999, T_1800, 300)['run']
        self.assertEqual((run['state'], run['duration'], run['stage']), ('failed', 180, None))
        self.assertIn('255', run['error'])

    def test_old_data_is_stale(self):
        self.assertTrue(us.view(RUNNING, T_1800 + 901, T_1800, 300)['stale'])

    def test_incomplete_or_malformed_documents_never_raise(self):
        for doc in (None, [], {}, {'current': 'x', 'targets': 'y', 'lastCheck': 5},
                    {'current': {'state': 'exploded'}}, {'targets': [None, {'name': 'no key'}]},
                    {'current': {'state': 'running', 'startedAt': None, 'logTail': [1, None, 'ok']}}):
            v = us.view(doc, 100.0, 90.0, 300)
            self.assertIn('run', v)
            self.assertIsInstance(v['targets'], list)
        v = us.view({'current': {'state': 'running', 'logTail': [1, None, 'ok']}}, 100.0, 90.0, 300)
        self.assertEqual(v['run']['log'], ['ok'])
        self.assertIsNone(us.view({}, 100.0, 90.0, 300)['run'])
        self.assertFalse(us.view({'configured': False}, 100.0, 90.0, 300)['configured'])


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.token = os.path.join(self.dir, 'updater-status.token')
        self.now = [T_1800]

    def collector(self, fetch, interval=300):
        return us.UpdaterStatusCollector('https://ariku.pl/', self.token, interval, background=False, fetch=fetch,
                                         clock=lambda: self.now[0])

    def write_token(self, value=TOKEN):
        with open(self.token, 'w') as f:
            f.write(value + '\n')

    def test_no_token_then_token_appears(self):
        calls = []
        c = self.collector(lambda url, h, **kw: calls.append((url, h, kw)) or RUNNING)
        self.assertEqual(c.sample(), {'error': 'no token'})
        self.write_token()
        v = c.sample()
        self.assertEqual(calls, [('https://ariku.pl/api/infra/system-updater/status',
                                  {'Authorization': f'Bearer {TOKEN}'}, {'timeout': 10})])
        self.assertEqual(v['run']['state'], 'running')
        self.assertIsNone(v['error'])

    def test_a_luneta_token_or_junk_is_not_a_status_token(self):
        for bad in ('lnt_short', 'x' * 10, 'has spaces ' * 5, ''):
            self.write_token(bad)
            self.assertEqual(self.collector(lambda *a, **kw: RUNNING).sample(), {'error': 'no token'})

    def test_transport_and_http_errors(self):
        self.write_token()
        for exc in (OSError('HTTP 401 Wymagany nagłówek'), OSError('HTTP 501 not configured'),
                    socket.timeout('timed out'), ConnectionRefusedError(111, 'refused'), ValueError('bad JSON')):
            def fail(url, h, exc=exc, **kw):
                raise exc
            out = self.collector(fail).sample()
            self.assertEqual(set(out), {'error'})
            self.assertIn(type(exc).__name__, out['error'])

    def test_failed_refresh_keeps_last_status_with_the_error(self):
        self.write_token()
        answers = [RUNNING, OSError('HTTP 503 Lemuria niedostępna')]

        def fetch(url, h, **kw):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        c = self.collector(fetch)
        self.assertIsNone(c.sample()['error'])
        self.now[0] += us.BUSY_INTERVAL
        out = c.sample()
        self.assertEqual(out['run']['state'], 'running')
        self.assertIn('HTTP 503', out['error'])

    def test_refreshes_faster_while_an_update_runs(self):
        self.write_token()
        docs = [RUNNING, FAILED]
        c = self.collector(lambda url, h, **kw: docs[0] if len(docs) == 2 else docs[-1])
        c.sample()
        self.assertEqual(c.poller.interval, us.BUSY_INTERVAL)
        docs.pop(0)
        self.now[0] += us.BUSY_INTERVAL
        c.sample()
        self.assertEqual(c.poller.interval, 300)

    def test_updates_panel_carries_the_status_even_without_the_luneta_token(self):
        self.write_token()
        status = self.collector(lambda url, h, **kw: RUNNING)
        inv = updates.UpdatesCollector('https://ariku.pl', os.path.join(self.dir, 'missing.token'),
                                       background=False, fetch=lambda *a, **kw: {}, status=status)
        out = inv.sample()
        self.assertEqual(out['error'], 'no token')
        self.assertEqual(out['updater']['run']['state'], 'running')


class TestDraw(unittest.TestCase):
    def render(self, updater, inventory=None):
        c = DemoCollector()
        data = dict(inventory or c.data['updates'], updater=updater)
        c.data = dict(c.data, updates=data)
        dash = Dashboard({'updates': (0, 0, 360, 260)}, c)
        dash.render()
        return dash

    def test_states_render(self):
        for st in (DemoCollector().data['updates']['updater'], {'error': 'no token'}, {'error': 'OSError: HTTP 401'},
                   us.view(RUNNING, T_1800, T_1800, 300), us.view(FAILED, T_1800, T_1800, 300),
                   us.view(RUNNING, T_1800 + 5000, T_1800, 300), us.view({}, 0, 0, 300), None):
            self.assertIsInstance(self.render(st).cache, cairo.ImageSurface)
        self.assertIsInstance(self.render(us.view(FAILED, T_1800, T_1800, 300), {'error': 'no token'}).cache,
                              cairo.ImageSurface)


if __name__ == '__main__':
    unittest.main()
