"""Temperature sensors with automatic limits and colour zones.

Zones: green = idle / light work, yellow = normal under load, red = close to the limit,
not recommended for long periods. Limits come from the hardware when it reports them,
otherwise from vendor specs; every limit can be overridden in config.toml ([temps]).

    CPU  Intel coretemp: TjMax from temp*_crit (reported by the CPU).
         AMD k10temp/zenpower: no TjMax exposed -> 90 C by default (e.g. Ryzen 5 5600 per AMD);
         set cpu_limit if your model differs (AMD product page, "Max. Operating Temperature").
    GPU  amdgpu: temp*_crit of the edge sensor; NVIDIA: "GPU Slowdown Temp" from nvidia-smi;
         Intel Arc: 90 C (default thermal throttle point).
    NVMe min(70 C, WCTEMP from temp1_max): consumer drives are specified for 0-70 C operation;
         firmware warning thresholds are often higher (throttling starts there, not the spec).
    Board SYSTIN-style sensors measure case air: 45 C yellow, 55 C red.
"""
from .. import sysfs

DEFAULT_LIMIT = {'cpu': 90.0, 'gpu': 90.0, 'nvme': 70.0, 'board': 65.0}


def zones(kind, limit):
    """(yellow_from, red_from, limit)"""
    if kind == 'nvme':
        return limit - 15, limit - 2, limit
    if kind == 'board':
        return 45.0, 55.0, limit
    return limit - 20, limit - 5, limit


def _labels(h):
    out = {}
    for lab in sysfs.glob(h + '/temp*_label'):
        out[sysfs.read(lab, '')] = lab[:-len('_label')]
    return out


def _milli(path):
    v = sysfs.read_int(path)
    return v / 1000 if v is not None else None


class Sensor:
    def __init__(self, key, label, kind, path, limit):
        self.key, self.label, self.kind, self.path = key, label, kind, path
        self.limit = limit
        self.yellow, self.red, _ = zones(kind, limit)

    def read(self):
        return _milli(self.path + '_input')

    def zone(self, value):
        return 'red' if value >= self.red else 'yellow' if value >= self.yellow else 'green'

    def __repr__(self):
        return f'Sensor({self.key!r}, {self.label!r}, {self.kind}, limit={self.limit:g})'


def find_sensors(overrides=None, disk_models=None):
    """Scan hwmon once. overrides: {'cpu_limit': 95, ...} (0/None = auto)."""
    ov = {k: v for k, v in (overrides or {}).items() if v}
    models = disk_models or {}
    found = []
    for h in sysfs.glob('/sys/class/hwmon/hwmon*'):
        name = sysfs.read(h + '/name', '')
        labels = _labels(h)
        if name in ('k10temp', 'zenpower'):
            src = labels.get('Tctl') or labels.get('Tdie') or (h + '/temp1')
            found.append(Sensor('cpu', 'CPU Tctl', 'cpu', src, float(ov.get('cpu_limit', DEFAULT_LIMIT['cpu']))))
        elif name == 'coretemp':
            src = next((v for k, v in labels.items() if k.startswith('Package id')), h + '/temp1')
            crit = _milli(src + '_crit')
            found.append(Sensor('cpu', 'CPU Package', 'cpu', src, float(ov.get('cpu_limit', crit or 100.0))))
        elif name == 'amdgpu':
            src = labels.get('edge') or (h + '/temp1')
            crit = _milli(src + '_crit')
            found.append(Sensor('gpu', 'GPU Radeon', 'gpu', src, float(ov.get('gpu_limit', crit or DEFAULT_LIMIT['gpu']))))
        elif name in ('i915', 'xe') and sysfs.exists(h + '/temp1_input'):
            found.append(Sensor('gpu', 'GPU Arc', 'gpu', h + '/temp1', float(ov.get('gpu_limit', DEFAULT_LIMIT['gpu']))))
        elif name == 'nvme':
            dev = sysfs.realpath(h + '/device')
            ctrl = next((part for part in dev.split('/') if part.startswith('nvme') and part[4:].isdigit()), None)
            src = labels.get('Composite') or (h + '/temp1')
            wctemp = _milli(src + '_max')
            limit = DEFAULT_LIMIT['nvme'] if not wctemp or wctemp < 40 or wctemp > 150 else min(DEFAULT_LIMIT['nvme'], wctemp)
            model = (models.get(f'{ctrl}n1') or '').split()
            label = f'{ctrl} {model[0]}' if ctrl and model else (ctrl or 'NVMe')
            found.append(Sensor(ctrl or 'nvme', label, 'nvme', src, float(ov.get('nvme_limit', limit))))
        elif name.startswith(('nct', 'it87', 'it86', 'asus', 'w83')):
            src = labels.get('SYSTIN') or labels.get('Motherboard') or labels.get('System')
            if src and not any(s.kind == 'board' for s in found):
                found.append(Sensor('board', 'Board', 'board', src, DEFAULT_LIMIT['board']))
    # stable order: CPU, GPU, NVMe..., board
    order = {'cpu': 0, 'gpu': 1, 'nvme': 2, 'board': 3}
    found.sort(key=lambda s: (order[s.kind], s.key))
    # one CPU / one GPU sensor is enough
    out, seen = [], set()
    for s in found:
        if s.kind in ('cpu', 'gpu', 'board') and s.kind in seen:
            continue
        seen.add(s.kind)
        out.append(s)
    return out


class VirtualSensor(Sensor):
    """Sensor fed from elsewhere (NVIDIA temperature comes from nvidia-smi, not hwmon)."""

    def __init__(self, key, label, kind, limit, getter):
        super().__init__(key, label, kind, '', limit)
        self.getter = getter

    def read(self):
        return self.getter()
