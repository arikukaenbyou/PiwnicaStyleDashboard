"""Collectors on fake /proc and /sys trees: system, GPU backends (Intel i915/xe, AMD, NVIDIA)
and temperature sensors with their limits. No real hardware is read."""
import unittest

from piwnica_dashboard.collect import gpu as gpu_mod
from piwnica_dashboard.collect.system import SystemCollector, default_route_iface, physical_disks
from piwnica_dashboard.collect.temps import find_sensors
from tests.fake import FakeRoot

LSBLK = {'blockdevices': [
    {'name': 'loop0', 'type': 'loop', 'mountpoints': ['/snap/x']},
    {'name': 'sdb', 'type': 'disk', 'rm': True, 'model': 'USB stick', 'mountpoints': ['/run/media/usb']},
    {'name': 'sda', 'model': 'HDD 1TB', 'size': 1000, 'type': 'disk', 'tran': 'sata', 'rm': False, 'mountpoints': [None],
     'children': [{'name': 'sda1', 'type': 'part', 'mountpoints': ['/mnt/hdd']}]},
    {'name': 'nvme0n1', 'model': 'Samsung SSD 980 500GB', 'size': 500, 'type': 'disk', 'tran': 'nvme', 'rm': False,
     'mountpoints': [None], 'children': [{'name': 'nvme0n1p1', 'type': 'part', 'mountpoints': ['/boot']},
                                         {'name': 'nvme0n1p2', 'type': 'part', 'mountpoints': ['/']},
                                         {'name': 'nvme0n1p3', 'type': 'part', 'mountpoints': ['[SWAP]']}]},
]}
VFS = {'/mnt/hdd': (1000e9, 950e9), '/boot': (0.5e9, 0.1e9), '/': (457e9, 259e9)}


class Base(unittest.TestCase):
    def setUp(self):
        self.f = FakeRoot()

    def tearDown(self):
        self.f.close()


class TestSystem(Base):
    def setUp(self):
        super().setUp()
        f = self.f
        f.put('/proc/stat', 'cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 1 0 0 0 0 0 0 0\ncpu1 1 0 0 0 0 0 0 0\n')
        f.put('/proc/meminfo', 'MemTotal: 32000000 kB\nMemAvailable: 20000000 kB\nSwapTotal: 8000000 kB\nSwapFree: 7000000 kB\n')
        f.put('/proc/uptime', '93784.5 100.0')
        f.put('/proc/loadavg', '1.50 1.00 0.50 2/412 999')
        f.put('/proc/sys/kernel/osrelease', '6.18.0-arch1-1')
        f.put('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 4000000)
        f.put('/sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq', 3000000)
        # default route on wlan0 although eth0 is listed first
        f.put('/proc/net/route', 'Iface\tDestination\tGateway\tFlags\n'
                                 'eth0\t0000A8C0\t00000000\t0001\nwlan0\t00000000\t0100A8C0\t0003\n')
        for iface in ('eth0', 'wlan0'):
            f.mkdir(f'/sys/class/net/{iface}/device')
            f.put(f'/sys/class/net/{iface}/statistics/rx_bytes', 0)
            f.put(f'/sys/class/net/{iface}/statistics/tx_bytes', 0)
        f.put('/sys/block/nvme0n1/stat', '0 0 0 0 0 0 0 0')
        f.put('/sys/block/sda/stat', '0 0 0 0 0 0 0 0')

    def test_default_route_interface(self):
        self.assertEqual(default_route_iface(), 'wlan0')

    def test_no_route_falls_back_to_physical_nic(self):
        self.f.put('/proc/net/route', 'Iface\tDestination\tGateway\tFlags\n')
        self.assertEqual(default_route_iface(), 'eth0')

    def test_physical_disks_skip_loop_and_removable_and_swap(self):
        disks = physical_disks(LSBLK)
        self.assertEqual([d['name'] for d in disks], ['sda', 'nvme0n1'])
        self.assertEqual(disks[1]['mounts'], ['/boot', '/'])

    def test_two_samples(self):
        c = SystemCollector(lsblk=lambda: LSBLK, statvfs=lambda m: VFS[m])
        c.sample(100.0)
        f = self.f
        f.put('/proc/stat', 'cpu  1080 0 520 8080 520 0 0 0 0 0\n')
        f.put('/sys/class/net/wlan0/statistics/rx_bytes', 4_000_000)
        f.put('/sys/class/net/wlan0/statistics/tx_bytes', 1_000_000)
        f.put('/sys/block/nvme0n1/stat', f'0 0 {8_000_000 // 512} 0 0 0 0 0')  # 8 MB in 2 s
        d = c.sample(102.0)
        self.assertAlmostEqual(d['cpu'], 50.0)
        self.assertAlmostEqual(d['iowait'], 10.0)
        self.assertAlmostEqual(d['cpu_ghz'], 3.5)
        self.assertEqual(d['mem_used'], 12_000_000 * 1024)
        self.assertAlmostEqual(d['mem'], 37.5)
        self.assertEqual(d['swap_used'], 1_000_000 * 1024)
        self.assertEqual((d['iface'], d['rx'], d['tx']), ('wlan0', 2e6, 0.5e6))
        self.assertEqual((d['uptime'], d['load'], d['procs']), (93784.5, [1.5, 1.0, 0.5], 412))
        self.assertTrue(d['kernel'].startswith('6.18'))
        nv = d['disks'][1]
        self.assertAlmostEqual(nv['used'], 259.1e9, delta=1e6)
        self.assertAlmostEqual(nv['pct'], 56.63, places=2)
        self.assertAlmostEqual(nv['rd'], 4e6, delta=1)
        self.assertAlmostEqual(d['disks'][0]['pct'], 95.0)


def fdinfo(f, pid, fd, text):
    f.link_raw(f'/proc/{pid}/fd/{fd}', '/dev/dri/renderD128')
    f.put(f'/proc/{pid}/fdinfo/{fd}', text)


class TestIntel(Base):
    def card(self, driver='i915'):
        f = self.f
        f.put('/sys/class/drm/card1/device/vendor', '0x8086')
        f.link_dir('/sys/class/drm/card1/device/driver', f'/sys/bus/pci/drivers/{driver}')
        f.put('/sys/class/drm/card1-DP-1/status', 'connected')
        f.put('/sys/class/drm/card1/device/hwmon/hwmon4/energy1_input', 0)
        for n, v in (('act', 2000), ('max', 1600), ('RP0', 2400)):
            f.put(f'/sys/class/drm/card1/gt_{n}_freq_mhz', v)
        f.link_raw('/proc/789/fd/1', '/tmp/not-a-gpu')

    def test_i915_fdinfo_dedup_and_busiest_engine(self):
        self.card()
        f = self.f

        def clients(r8, r9, v9):
            txt = 'drm-driver:\ti915\ndrm-client-id:\t{cid}\ndrm-engine-render:\t{r} ns\ndrm-engine-video:\t{v} ns\ndrm-engine-capacity-video:\t2\n'
            for pid, fd in ((123, 5), (123, 6)):  # same client on two fds -> counted once
                f.put(f'/proc/{pid}/fdinfo/{fd}', txt.format(cid=8, r=r8, v=0))
            f.put('/proc/456/fdinfo/3', txt.format(cid=9, r=r9, v=v9))
        for pid, fd in ((123, 5), (123, 6), (456, 3)):
            f.link_raw(f'/proc/{pid}/fd/{fd}', '/dev/dri/renderD128')
        clients(1_000_000_000, 0, 0)
        g = gpu_mod.detect()
        self.assertEqual((g.vendor, g.name), ('intel', 'Intel Arc'))
        g.sample(100.0)
        clients(2_000_000_000, 500_000_000, 2_000_000_000)
        f.put('/sys/class/drm/card1/device/hwmon/hwmon4/energy1_input', 240_000_000)
        s = g.sample(102.0)
        # render: 1 s + 0.5 s of 2 s = 75 %; video: 2 s over 2 engines of 2 s = 50 %
        self.assertAlmostEqual(s['busy'], 75.0)
        self.assertAlmostEqual(s['watts'], 120.0)
        self.assertEqual((s['mhz'], s['cap_mhz'], s['max_mhz']), (2000, 1600, 2400))

    def test_xe_cycles(self):
        self.card('xe')
        f = self.f
        txt = 'drm-driver:\txe\ndrm-client-id:\t3\ndrm-cycles-rcs:\t{b}\ndrm-total-cycles-rcs:\t{t}\n'
        fdinfo(f, 50, 4, txt.format(b=0, t=1000))
        g = gpu_mod.detect()
        g.sample(10.0)
        f.put('/proc/50/fdinfo/4', txt.format(b=300, t=2000))
        self.assertAlmostEqual(g.sample(11.0)['busy'], 30.0)

    def test_rc6_fallback_without_clients(self):
        self.card()
        self.f.put('/sys/class/drm/card1/gt/gt0/rc6_residency_ms', 10000)
        g = gpu_mod.detect()
        g.sample(100.0)
        self.f.put('/sys/class/drm/card1/gt/gt0/rc6_residency_ms', 11500)
        self.assertAlmostEqual(g.sample(102.0)['busy'], 25.0)


class TestAmd(Base):
    def test_amdgpu_sysfs(self):
        f = self.f
        f.put('/sys/class/drm/card0/device/vendor', '0x1002')
        f.put('/sys/class/drm/card0/device/gpu_busy_percent', 63)
        f.put('/sys/class/drm/card0/device/pp_dpm_sclk', '0: 500Mhz\n1: 1800Mhz *\n2: 2600Mhz\n')
        f.put('/sys/class/drm/card0/device/hwmon/hwmon2/power1_average', 187_000_000)
        s = gpu_mod.detect().sample(1.0)
        self.assertEqual((s['busy'], s['mhz'], s['max_mhz'], s['watts']), (63.0, 1800, 2600, 187.0))

    def test_prefers_card_with_display(self):
        f = self.f
        f.put('/sys/class/drm/card0/device/vendor', '0x8086')   # iGPU, no display
        f.put('/sys/class/drm/card0/gt_act_freq_mhz', 300)
        f.put('/sys/class/drm/card1/device/vendor', '0x1002')   # dGPU driving the monitor
        f.put('/sys/class/drm/card1-HDMI-A-1/status', 'connected')
        self.assertEqual(gpu_mod.detect().vendor, 'amd')


class TestNvidia(Base):
    def test_nvidia_smi(self):
        self.f.put('/sys/class/drm/card0/device/vendor', '0x10de')

        class R:
            def __init__(self, out):
                self.stdout, self.returncode = out, 0
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            if '-q' in cmd:
                return R('    GPU Slowdown Temp                 : 91 C\n')
            return R('47, 1905, 2805, 212.40, 66, NVIDIA GeForce RTX 3070\n')
        g = gpu_mod.detect(runner=runner)
        s = g.sample(1.0)
        self.assertEqual((g.vendor, g.slowdown, g.temp), ('nvidia', 91, 66.0))
        self.assertEqual((s['busy'], s['mhz'], s['max_mhz'], s['watts'], s['name']), (47.0, 1905, 2805, 212.4, 'NVIDIA GeForce RTX 3070'))
        g.sample(2.0)  # cached for 2 s
        self.assertEqual(len([c for c in calls if '-q' not in c]), 1)

    def test_no_nvidia_smi(self):
        self.f.put('/sys/class/drm/card0/device/vendor', '0x10de')

        def runner(cmd, **kw):
            raise FileNotFoundError('nvidia-smi')
        self.assertIsNone(gpu_mod.detect(runner=runner))


class TestTemps(Base):
    def test_amd_desktop(self):
        f = self.f
        f.hwmon(2, 'k10temp', {1: ('Tctl', 69000, None), 3: ('Tccd1', 63000, None)})
        f.hwmon(4, 'i915', {1: (None, 55000, None)})
        f.hwmon(5, 'nct6798', {1: ('SYSTIN', 37000, None), 2: ('CPUTIN', 49000, None)})
        f.hwmon(0, 'nvme', {1: ('Composite', 38000, {'max': 81850, 'crit': 84850})}, device='/sys/devices/pci/nvme/nvme0')
        f.hwmon(1, 'nvme', {1: ('Composite', 45000, {'max': 99850, 'crit': 109850})}, device='/sys/devices/pci/nvme/nvme1')
        s = find_sensors(disk_models={'nvme0n1': 'Samsung SSD 980', 'nvme1n1': 'Lexar SSD NM710'})
        got = [(x.label, x.kind, x.limit, x.read()) for x in s]
        self.assertEqual(got, [('CPU Tctl', 'cpu', 90.0, 69.0), ('GPU Arc', 'gpu', 90.0, 55.0),
                               ('nvme0 Samsung', 'nvme', 70.0, 38.0), ('nvme1 Lexar', 'nvme', 70.0, 45.0),
                               ('Board', 'board', 65.0, 37.0)])

    def test_intel_cpu_uses_reported_tjmax(self):
        self.f.hwmon(1, 'coretemp', {1: ('Package id 0', 52000, {'crit': 100000}), 2: ('Core 0', 50000, None)})
        (cpu,) = find_sensors()
        self.assertEqual((cpu.label, cpu.limit, cpu.yellow, cpu.red), ('CPU Package', 100.0, 80.0, 95.0))

    def test_nvme_with_low_wctemp_and_amdgpu_crit(self):
        f = self.f
        f.hwmon(0, 'nvme', {1: ('Composite', 40000, {'max': 65000})}, device='/sys/devices/pci/nvme/nvme0')
        f.hwmon(3, 'amdgpu', {1: ('edge', 60000, {'crit': 100000}), 2: ('junction', 70000, {'crit': 110000})})
        gpu, nvme = find_sensors()
        self.assertEqual((gpu.label, gpu.limit), ('GPU Radeon', 100.0))
        self.assertEqual((nvme.limit, nvme.yellow, nvme.red), (65.0, 50.0, 63.0))

    def test_overrides(self):
        self.f.hwmon(2, 'k10temp', {1: ('Tctl', 69000, None)})
        (cpu,) = find_sensors({'cpu_limit': 95, 'gpu_limit': 0})
        self.assertEqual((cpu.limit, cpu.yellow, cpu.red), (95.0, 75.0, 90.0))

    def test_zones_at_boundaries(self):
        self.f.hwmon(2, 'k10temp', {1: ('Tctl', 0, None)})
        self.f.hwmon(0, 'nvme', {1: ('Composite', 0, None)}, device='/sys/devices/pci/nvme/nvme0')
        self.f.hwmon(5, 'nct6798', {1: ('SYSTIN', 0, None)})
        cpu, nvme, board = find_sensors()
        cases = [(cpu, 69, 'green'), (cpu, 70, 'yellow'), (cpu, 84, 'yellow'), (cpu, 85, 'red'),
                 (nvme, 54, 'green'), (nvme, 55, 'yellow'), (nvme, 68, 'red'),
                 (board, 44, 'green'), (board, 45, 'yellow'), (board, 55, 'red')]
        self.assertEqual([(s.kind, v, s.zone(v)) for s, v, _ in cases], [(s.kind, v, z) for s, v, z in cases])


if __name__ == '__main__':
    unittest.main()
