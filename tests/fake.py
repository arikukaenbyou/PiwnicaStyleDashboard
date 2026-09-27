"""Build fake /proc and /sys trees for tests and point piwnica_dashboard.sysfs at them."""
import os
import shutil
import tempfile

from piwnica_dashboard import sysfs


class FakeRoot:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix='piwnica-test-')
        self._old = sysfs.ROOT
        sysfs.ROOT = self.root

    def close(self):
        sysfs.ROOT = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, path, value):
        full = self.root + path
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(str(value))

    def mkdir(self, path):
        os.makedirs(self.root + path, exist_ok=True)

    def link_dir(self, path, target):
        """Symlink path -> fake directory target (like hwmon/device)."""
        self.mkdir(target)
        os.makedirs(os.path.dirname(self.root + path), exist_ok=True)
        os.symlink(self.root + target, self.root + path)

    def link_raw(self, path, target):
        """Symlink with a literal target (like /proc/<pid>/fd/N -> /dev/dri/renderD128)."""
        os.makedirs(os.path.dirname(self.root + path), exist_ok=True)
        os.symlink(target, self.root + path)

    def hwmon(self, n, name, temps, device=None):
        """temps: {index: (label or None, millidegrees, {'crit': m, 'max': m})}"""
        h = f'/sys/class/hwmon/hwmon{n}'
        self.put(h + '/name', name)
        for i, (label, value, extra) in temps.items():
            self.put(f'{h}/temp{i}_input', value)
            if label:
                self.put(f'{h}/temp{i}_label', label)
            for k, v in (extra or {}).items():
                self.put(f'{h}/temp{i}_{k}', v)
        if device:
            self.link_dir(h + '/device', device)
        return h
