"""scripts/update-homelab.py: read-only status of the ariku.pl updater; plan/apply only point to the panel."""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    'update_homelab', os.path.join(os.path.dirname(__file__), '..', 'scripts', 'update-homelab.py'))
uh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uh)

from piwnica_dashboard import __main__ as cli  # noqa: E402
from piwnica_dashboard.collect import updater_status as us  # noqa: E402


class TestScript(unittest.TestCase):
    def test_plan_and_apply_install_nothing(self):
        for mode in ('plan', 'apply'):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch.object(uh, 'cmd_updater') as status:
                self.assertEqual(uh.main([mode]), 2)
            status.assert_not_called()
            self.assertIn('SYSTEM → Updater', err.getvalue())

    def test_no_remote_apt_runner_left(self):
        self.assertFalse(hasattr(uh, 'REMOTE_SCRIPT'))
        self.assertFalse(hasattr(uh, 'ssh_call'))

    def test_status_is_the_default(self):
        with mock.patch.object(uh, 'cmd_updater', return_value=0) as status, \
                mock.patch.object(uh.config_mod, 'load', return_value={}):
            self.assertEqual(uh.main([]), 0)
        status.assert_called_once_with({}, False)


class TestCmdUpdater(unittest.TestCase):
    def run_cli(self, sample):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(us.UpdaterStatusCollector, 'sample', return_value=sample):
            rc = cli.cmd_updater({'updates': {'status_token_file': os.path.join(tempfile.mkdtemp(), 't')}})
        return rc, out.getvalue()

    def test_missing_token_explains_where_it_goes(self):
        rc, out = self.run_cli({'error': 'no token'})
        self.assertEqual(rc, 1)
        self.assertIn('SYSTEM_UPDATER_STATUS_TOKEN', out)

    def test_transport_error(self):
        rc, out = self.run_cli({'error': 'OSError: HTTP 503 Lemuria niedostępna'})
        self.assertEqual(rc, 1)
        self.assertIn('HTTP 503', out)

    def test_failed_run_exits_nonzero_and_shows_the_error(self):
        v = us.view({'current': {'id': 'r', 'kind': 'apply', 'state': 'failed', 'startedAt': '2026-10-22T17:00:00Z',
                                 'durationSeconds': 61, 'error': 'dpkg przerwany', 'logTail': ['[host] E: dpkg']},
                     'targets': [{'key': 'host', 'name': 'Proxmox host', 'state': 'failed', 'error': 'dpkg przerwany'}]},
                    100.0, 90.0, 300)
        v['error'] = None
        rc, out = self.run_cli(v)
        self.assertEqual(rc, 1)
        self.assertIn('install: failed', out)
        self.assertIn('dpkg przerwany', out)
        self.assertIn('1m01s', out)

    def test_no_runs(self):
        rc, out = self.run_cli(dict(us.view({}, 100.0, 90.0, 300), error=None))
        self.assertEqual(rc, 0)
        self.assertIn('no updater runs yet', out)


if __name__ == '__main__':
    unittest.main()
