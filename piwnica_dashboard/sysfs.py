"""Tiny helpers for reading /proc and /sys.

Every path goes through ``p()``, so tests can point the whole program at a fake
tree with ``PIWNICA_ROOT=/tmp/fake`` (or by setting ``sysfs.ROOT``).
"""
import glob as _glob
import os

ROOT = os.environ.get('PIWNICA_ROOT', '')


def p(path):
    return ROOT + path


def strip(path):
    """Real (prefixed) path -> logical path."""
    return path[len(ROOT):] if ROOT and path.startswith(ROOT) else path


def glob(pattern):
    """Logical paths matching a logical pattern."""
    return sorted(strip(x) for x in _glob.glob(p(pattern)))


def exists(path):
    return os.path.exists(p(path))


def read(path, default=None):
    try:
        with open(p(path)) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=None):
    try:
        return int(read(path))
    except (TypeError, ValueError):
        return default


def realpath(path):
    return strip(os.path.realpath(p(path)))


def readlink(path):
    return os.readlink(p(path))
