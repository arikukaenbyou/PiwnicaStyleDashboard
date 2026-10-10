"""The Claude limits panel: the /api/oauth/usage view, the credentials file and the token never leaking."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import cairo

from piwnica_dashboard.collect import claude
from piwnica_dashboard.collector import DemoCollector
from piwnica_dashboard.draw import Dashboard

TOKEN = 'sk-ant-oat01-' + 'x' * 60
# the shape api.anthropic.com returned on 2026-10-10, trimmed
DOC = {
    'five_hour': {'utilization': 51.0, 'resets_at': '2026-10-10T04:40:00.200152+00:00'},
    'seven_day': {'utilization': 52.0, 'resets_at': '2026-10-15T21:00:00.200173+00:00'},
    'seven_day_opus': None,
    'extra_usage': {'is_enabled': False, 'monthly_limit': None, 'used_credits': None, 'utilization': None},
    'limits': [
        {'kind': 'session', 'group': 'session', 'percent': 51, 'severity': 'normal',
         'resets_at': '2026-10-10T04:40:00.200152+00:00'},
        {'kind': 'weekly_all', 'group': 'weekly', 'percent': 52, 'severity': 'normal',
         'resets_at': '2026-10-15T21:00:00.200173+00:00'},
    ],
    'seven_day_breakdown': {'rows': [{'key': 'chat', 'display_name': 'Chats', 'percent': 0},
                                     {'key': 'cowork', 'display_name': 'Cowork', 'percent': 1},
                                     {'key': 'claude_code', 'display_name': 'Claude Code', 'percent': 99}]},
}


class TestView(unittest.TestCase):
    def test_limits(self):
        v = claude.view(DOC, 1000.0, 990.0)
        self.assertEqual([(l['label'], l['pct']) for l in v['limits']], [('5h session', 51.0), ('week', 52.0)])
        self.assertEqual(v['limits'][0]['resets_at'], datetime(2026, 10, 10, 4, 40, 0, 200152, timezone.utc).timestamp())
        self.assertEqual(v['breakdown'], [{'name': 'Claude Code', 'pct': 99.0}, {'name': 'Cowork', 'pct': 1.0}])
        self.assertEqual((v['extra'], v['age']), (None, 10.0))

    def test_older_response_without_limits(self):
        doc = dict(DOC, limits=None, seven_day_sonnet={'utilization': 7, 'resets_at': None})
        v = claude.view(doc, 0)
        self.assertEqual([l['label'] for l in v['limits']], ['5h session', 'week', 'week sonnet'])

    def test_extra_usage_and_unknown_kind(self):
        doc = {'limits': [{'kind': 'weekly_scoped', 'percent': 95, 'severity': 'warning'}],
               'extra_usage': {'is_enabled': True, 'utilization': 40, 'used_credits': 20.0, 'monthly_limit': 50,
                               'currency': 'USD'}}
        v = claude.view(doc, 0)
        self.assertEqual(v['limits'][0]['label'], 'weekly scoped')
        self.assertEqual(v['extra'], {'pct': 40.0, 'used': 20.0, 'limit': 50.0, 'currency': 'USD'})


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), '.credentials.json')
        self.now = 1000.0

    def write(self, expires_at=2000.0):
        with open(self.path, 'w') as f:
            json.dump({'claudeAiOauth': {'accessToken': TOKEN, 'refreshToken': 'r', 'expiresAt': expires_at * 1000,
                                         'subscriptionType': 'max'}}, f)

    def collector(self, fetch):
        return claude.ClaudeCollector(self.path, background=False, fetch=fetch, clock=lambda: self.now)

    def test_no_credentials(self):
        self.assertEqual(self.collector(lambda *a, **k: DOC).sample(), {'error': 'no credentials'})

    def test_fetch_with_the_token(self):
        self.write()
        calls = []
        v = self.collector(lambda url, h, **kw: calls.append((url, h)) or DOC).sample()
        self.assertEqual(calls[0][0], 'https://api.anthropic.com/api/oauth/usage')
        self.assertEqual(calls[0][1]['Authorization'], f'Bearer {TOKEN}')
        self.assertEqual((v['plan'], v['error'], len(v['limits'])), ('max', None, 2))
        with open(self.path) as f:
            self.assertIn('"refreshToken": "r"', f.read())  # the file is never rewritten

    def test_expired_token_is_not_sent(self):
        self.write(expires_at=500.0)
        calls = []
        v = self.collector(lambda *a, **k: calls.append(a) or DOC).sample()
        self.assertEqual((calls, v['error']), ([], claude.EXPIRED))

    def test_401_and_errors_never_show_the_token(self):
        self.write()
        def fail(url, h, **kw):
            raise OSError(f'HTTP 500 bad {h["Authorization"]}')
        v = self.collector(fail).sample()
        self.assertNotIn(TOKEN, v['error'])
        def unauthorized(url, h, **kw):
            raise OSError('HTTP 401 token expired')
        self.assertEqual(self.collector(unauthorized).sample()['error'], claude.EXPIRED)

    def test_failed_refresh_keeps_the_numbers(self):
        self.write()
        docs = [DOC]
        def fetch(url, h, **kw):
            if not docs:
                raise OSError('HTTP 429')
            return docs.pop()
        c = self.collector(fetch)
        c.sample()
        self.now += 120
        v = c.sample()
        self.assertEqual((len(v['limits']), v['error']), (2, 'HTTP 429'))


class TestDraw(unittest.TestCase):
    def test_states_render(self):
        for data in (DemoCollector().data['claude'], {'error': 'no credentials'}, {'error': claude.EXPIRED},
                     dict(claude.view(DOC, 0), error='HTTP 429'), claude.view({}, 0)):
            c = DemoCollector()
            c.data = dict(c.data, claude=data)
            dash = Dashboard({'claude': (0, 0, 320, 110)}, c)
            dash.render()
            self.assertIsInstance(dash.cache, cairo.ImageSurface)


if __name__ == '__main__':
    unittest.main()
