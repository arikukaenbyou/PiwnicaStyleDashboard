"""packaging/piwnica-dashboard: the optional pre-start hook runs before `run`, never blocks it."""
import os
import subprocess
import tempfile
import unittest

LAUNCHER = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'packaging', 'piwnica-dashboard'))


class TestLauncher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, 'log')
        pkg = os.path.join(self.tmp, 'share', 'piwnica_dashboard')
        os.makedirs(pkg)
        open(os.path.join(pkg, '__init__.py'), 'w').close()
        with open(os.path.join(pkg, '__main__.py'), 'w') as f:
            f.write('import sys\nopen(%r, "a").write(" ".join(["main"] + sys.argv[1:]) + "\\n")\n' % self.log)
        self.conf = os.path.join(self.tmp, 'config')
        os.makedirs(os.path.join(self.conf, 'piwnica-dashboard'))
        self.hook = os.path.join(self.conf, 'piwnica-dashboard', 'pre-start')

    def write_hook(self, body):
        with open(self.hook, 'w') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(self.hook, 0o755)

    def launch(self, *args, **extra):
        env = {'PATH': '/usr/bin:/bin', 'HOME': self.tmp, 'XDG_CONFIG_HOME': self.conf,
               'PIWNICA_HOME': os.path.join(self.tmp, 'share'), **extra}
        return subprocess.run(['/bin/sh', LAUNCHER, *args], env=env, cwd=self.tmp, capture_output=True, text=True, timeout=20)

    def lines(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_hook_runs_before_run_and_default(self):
        self.write_hook('echo hook >> %s\n' % self.log)
        self.assertEqual(self.launch('run').returncode, 0)
        self.assertEqual(self.launch().returncode, 0)
        self.assertEqual(self.lines(), ['hook', 'main run', 'hook', 'main'])

    def test_hook_skipped_for_other_commands_and_on_request(self):
        self.write_hook('echo hook >> %s\n' % self.log)
        self.launch('detect')
        self.launch('run', PIWNICA_NO_PRESTART='1')
        self.assertEqual(self.lines(), ['main detect', 'main run'])

    def test_failing_hook_does_not_block_start(self):
        self.write_hook('exit 3\n')
        r = self.launch('run')
        self.assertEqual(r.returncode, 0)
        self.assertIn('pre-start hook failed (3)', r.stderr)
        self.assertEqual(self.lines(), ['main run'])

    def test_no_hook_is_fine(self):
        self.assertEqual(self.launch('run').returncode, 0)
        self.assertEqual(self.lines(), ['main run'])


if __name__ == '__main__':
    unittest.main()
