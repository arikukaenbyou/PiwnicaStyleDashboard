#!/usr/bin/env python3
"""Status of Proxmox/LXC updates -- read-only.

Updates are installed by one mechanism only: the ariku.pl updater (panel SYSTEM → Updater, afterlife
lib/systemUpdater.ts). It runs each target as a systemd unit that survives a dropped SSH link, repairs an
interrupted dpkg, refuses a Debian major upgrade and keeps a history. This script used to run its own
plan/apply over SSH; two independent apt runners on the same hosts could not see each other's runs, so it now
only shows the ariku.pl updater's status (same as `piwnica-dashboard updater`).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from piwnica_dashboard import config as config_mod  # noqa: E402
from piwnica_dashboard.__main__ import cmd_updater  # noqa: E402

MOVED = ('Installing from here is gone: run the check and the installation in the ariku.pl panel '
         '(SYSTEM → Updater). This script shows their status.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', nargs='?', default='status', choices=('status', 'plan', 'apply'),
                        help='status (default); plan/apply only point to the ariku.pl panel')
    parser.add_argument('--config', help='dashboard TOML config (default: ~/.config/piwnica-dashboard/config.toml)')
    parser.add_argument('--json', action='store_true', help='print the status as JSON')
    args = parser.parse_args(argv)
    if args.mode != 'status':
        print(MOVED, file=sys.stderr)
        return 2
    return cmd_updater(config_mod.load(args.config, create=False), args.json)


if __name__ == '__main__':
    sys.exit(main())
