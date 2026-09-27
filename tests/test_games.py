import os
import tempfile
import unittest

from piwnica_dashboard.games import game_running


def fake_proc(*cmdlines):
    root = tempfile.mkdtemp()
    for i, args in enumerate(cmdlines, start=100):
        os.makedirs(f'{root}/{i}')
        with open(f'{root}/{i}/cmdline', 'wb') as f:
            f.write(b'\0'.join(a.encode() for a in args) + b'\0')
    os.makedirs(f'{root}/self')  # non-numeric entries are skipped
    return root


DESKTOP = [['/usr/lib/xorg/Xorg', ':0'], ['xfwm4'], ['/usr/bin/python3', 'ariku-flame-trail.py'],
           ['/home/u/.local/share/Steam/ubuntu12_64/steamwebhelper', '-lang=en'],
           ['/usr/bin/wineserver'], ['C:\\windows\\system32\\services.exe'], ['C:\\windows\\system32\\explorer.exe', '/desktop']]


class TestGames(unittest.TestCase):
    def test_plain_desktop_with_steam_client_and_idle_wine_is_not_a_game(self):
        self.assertFalse(game_running(fake_proc(*DESKTOP)))

    def test_steam_game(self):
        reaper = ['/home/u/.local/share/Steam/ubuntu12_32/reaper', 'SteamLaunch', 'AppId=438100', '--', 'proton', 'waitforexitandrun']
        self.assertTrue(game_running(fake_proc(*DESKTOP, reaper)))

    def test_wine_game_from_heroic(self):
        self.assertTrue(game_running(fake_proc(*DESKTOP, ['Z:\\games\\SomeGame\\Game-Win64-Shipping.exe'])))

    def test_lutris_and_gamescope(self):
        self.assertTrue(game_running(fake_proc(*DESKTOP, ['/usr/bin/python3', '/usr/bin/lutris-wrapper', 'game'])))
        self.assertTrue(game_running(fake_proc(*DESKTOP, ['gamescope', '-f', '--', 'game'])))

    def test_missing_proc(self):
        self.assertFalse(game_running('/nonexistent'))


if __name__ == '__main__':
    unittest.main()
