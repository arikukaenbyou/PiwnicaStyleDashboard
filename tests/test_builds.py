"""The builds panel: Forgejo CI build progress, versions, status, and Cairo rendering."""
import base64
import json
import os
import tempfile
import unittest

import cairo

from piwnica_dashboard.collect import builds
from piwnica_dashboard.collector import DemoCollector
from piwnica_dashboard.draw import Dashboard

TOKEN = '9c61a243e70d59fd71994cc016d7d82ce0f973e3'

RUNS_LUNETA = {
    'workflow_runs': [
        {
            'id': 31, 'title': 'v0.13.0: aktualizacje z Forgejo z fallbackiem na ariku.pl',
            'status': 'waiting', 'prettyref': 'v0.13.0', 'workflow_id': 'release.yml',
            'created': '2026-10-10T01:24:37+02:00', 'started': '1970-01-01T01:00:00+01:00',
            'stopped': '1970-01-01T01:00:00+01:00', 'commit_sha': '3a69884b4913f8bf79fc783db179b48ecbf511b9',
        },
        {
            'id': 30, 'title': 'v0.13.0: aktualizacje z Forgejo z fallbackiem na ariku.pl',
            'status': 'running', 'prettyref': 'v0.13.0', 'workflow_id': 'ci.yml',
            'created': '2026-10-10T01:24:37+02:00', 'started': '2026-10-10T02:01:22+02:00',
            'stopped': '1970-01-01T01:00:00+01:00', 'commit_sha': '3a69884b4913f8bf79fc783db179b48ecbf511b9',
        },
    ]
}

JOBS_RUN30 = [
    {'id': 34, 'run_id': 30, 'name': 'test', 'status': 'running'}
]


class TestBuildsCollector(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.token_file = os.path.join(self.dir, 'forgejo.token')

    def test_read_token(self):
        self.assertIsNone(builds.read_token(self.token_file))
        with open(self.token_file, 'w') as f:
            f.write(TOKEN)
        self.assertEqual(builds.read_token(self.token_file), TOKEN)

    def test_no_token_returns_error(self):
        col = builds.BuildsCollector('https://git.ariku.pl', self.token_file, background=False)
        self.assertEqual(col.sample(), {'error': 'no token'})

    def test_fetch_builds_success(self):
        with open(self.token_file, 'w') as f:
            f.write(TOKEN)

        def mock_fetch(url, headers, timeout=5):
            if '/tags' in url:
                if 'luneta' in url:
                    return [{'name': 'v0.13.0'}]
                return []
            if '/runs/30/jobs' in url:
                return JOBS_RUN30
            if '/actions/runs' in url:
                if 'luneta' in url:
                    return RUNS_LUNETA
                if 'afterlife' in url:
                    return {
                        'workflow_runs': [{
                            'id': 37, 'title': 'radar live', 'status': 'waiting',
                            'prettyref': 'main', 'workflow_id': 'ci.yml',
                            'created': '2026-10-10T01:53:58+02:00',
                            'started': '1970-01-01T01:00:00+01:00',
                            'stopped': '1970-01-01T01:00:00+01:00',
                            'commit_sha': '85e103ed112233',
                        }]
                    }
                if 'companion_app' in url:
                    return {
                        'workflow_runs': [{
                            'id': 21, 'title': 'android setup', 'status': 'failure',
                            'prettyref': 'ci/forgejo-android', 'workflow_id': 'android.yml',
                            'created': '2026-10-09T21:44:16+02:00',
                            'started': '2026-10-09T22:22:43+02:00',
                            'stopped': '2026-10-09T23:42:11+02:00',
                            'commit_sha': '651d28bf998877',
                        }]
                    }
            if '/contents/package.json' in url and 'afterlife' in url:
                return {'content': base64.b64encode(json.dumps({'version': '1.0.0'}).encode()).decode()}
            if '/contents/app/build.gradle.kts' in url and 'companion_app' in url:
                gradle = 'versionCode = 7\nversionName = "0.5.0"\n'
                return {'content': base64.b64encode(gradle.encode()).decode()}
            return {}

        col = builds.BuildsCollector('https://git.ariku.pl', self.token_file,
                                     repos=['afterlife', 'luneta', 'companion_app'],
                                     background=False, fetch=mock_fetch, clock=lambda: 1791590500.0)
        res = col.sample()
        self.assertIsNone(res.get('error'))
        self.assertEqual(res['total'], 3)
        self.assertEqual(res['active'], 2)

        items = {it['repo']: it for it in res['items']}
        self.assertEqual(items['luneta']['version'], 'v0.13.0')
        self.assertEqual(items['luneta']['status'], 'running')
        self.assertEqual(items['luneta']['job'], 'test')
        self.assertEqual(items['luneta']['queued_runs'], 1)

        self.assertEqual(items['afterlife']['version'], 'v1.0.0')
        self.assertEqual(items['afterlife']['status'], 'waiting')

        self.assertEqual(items['companion_app']['version'], 'v0.5.0')
        self.assertEqual(items['companion_app']['status'], 'failure')

    def test_api_error_handled_gracefully(self):
        with open(self.token_file, 'w') as f:
            f.write(TOKEN)

        def mock_error(url, headers, timeout=5):
            raise OSError('Connection refused')

        col = builds.BuildsCollector('https://git.ariku.pl', self.token_file,
                                     repos=['luneta'],
                                     background=False, fetch=mock_error, clock=lambda: 1000.0)
        res = col.sample()
        self.assertEqual(len(res['items']), 1)
        self.assertEqual(res['items'][0]['status'], 'error')


class TestBuildsDraw(unittest.TestCase):
    def render(self, data):
        c = DemoCollector()
        c.data = dict(c.data, builds=data)
        dash = Dashboard({'builds': (0, 0, 360, 210)}, c)
        dash.render()
        return dash

    def test_drawing_all_states(self):
        for data in (
            DemoCollector().data['builds'],
            {'error': 'no token'},
            {'error': 'HTTP 500 Server Error'},
            {'items': []},
            {'items': [
                {'name': 'Luneta', 'version': 'v0.13.0', 'status': 'running', 'pct': None,
                 'elapsed': 300, 'workflow': 'ci.yml', 'job': 'test', 'queued_runs': 1},
                {'name': 'Afterlife', 'version': 'v1.0.0', 'status': 'waiting', 'pct': 0,
                 'queued_time': 600, 'workflow': 'ci.yml', 'job': 'build', 'queued_runs': 0},
                {'name': 'Companion (Android)', 'version': 'v0.5.0', 'status': 'success', 'pct': 100,
                 'duration': 1800, 'age': 400, 'workflow': 'android.yml', 'job': 'build'},
            ], 'active': 2, 'total': 3, 'age': 10},
        ):
            dash = self.render(data)
            self.assertIsInstance(dash.cache, cairo.ImageSurface)


if __name__ == '__main__':
    unittest.main()
