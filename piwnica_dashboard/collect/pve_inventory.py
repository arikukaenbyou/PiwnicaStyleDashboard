"""Read-only application and package inventory via SSH on the Proxmox node.

The remote script uses pct for LXC containers and QEMU Guest Agent for VMs. It is
sent to `python3 -` over SSH and is never installed on the node or in a guest.
"""
import json
import re
import subprocess


REMOTE_SCRIPT = r'''
import json
import subprocess

def run(args, timeout=15):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, '', f'{type(e).__name__}: {e}'

guest_script = r"""
set +e
. /etc/os-release 2>/dev/null
printf '__OS__%s\n' "${PRETTY_NAME:-unknown}"
if command -v dpkg-query >/dev/null 2>&1; then
  printf '__PACKAGES__\n'
  dpkg-query -W -f='${binary:Package}\t${Version}\n' $(apt-mark showmanual 2>/dev/null) 2>/dev/null
  printf '__UPDATES__\n'
  apt list --upgradable 2>/dev/null | sed '1d'
elif command -v apk >/dev/null 2>&1; then
  printf '__PACKAGES__\n'
  apk info -v 2>/dev/null
  printf '__UPDATES__\n'
  apk version -l '<' 2>/dev/null
fi
app_version() {
  name=$1
  shift
  if command -v "$1" >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
    version=$(timeout 5 "$@" 2>&1 | head -n 1 | tr '\t' ' ')
    if [ -n "$version" ]; then printf '__APP__%s=%s\n' "$name" "$version"; fi
  fi
}
app_version AdGuardHome AdGuardHome --version
app_version n8n n8n --version
app_version traefik traefik version
app_version immich immich-server --version
app_version Jackett Jackett --version
app_version jellyfin jellyfin --version
app_version navidrome navidrome --version
app_version transmission transmission-daemon --version
app_version postgres postgres --version
app_version mariadb mariadb --version
app_version mosquitto mosquitto -h
app_version Radarr Radarr --version
app_version Sonarr Sonarr --version
app_version Bazarr bazarr --version
app_version Readarr Readarr --version
app_version Lidarr Lidarr --version
app_version mediamtx mediamtx --version
app_version redis redis-server --version
app_version forgejo forgejo --version
app_version Hermes hermes --version
app_version wordpress wp core version --allow-root
if command -v docker >/dev/null 2>&1; then
  printf '__DOCKER__\n'
  docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>&1
fi
"""

def parse_guest_text(text):
    result = {'os': None, 'packages': [], 'updates': [], 'app_versions': [], 'docker': []}
    section = None
    for line in text.splitlines():
        if line.startswith('__OS__'):
            result['os'] = line[6:].strip()
            section = None
        elif line.startswith('__APP__'):
            result['app_versions'].append(line[7:].strip())
        elif line == '__PACKAGES__':
            section = 'packages'
        elif line == '__UPDATES__':
            section = 'updates'
        elif line == '__DOCKER__':
            section = 'docker'
        elif section and line.strip():
            if section == 'packages':
                result[section].append(line.strip())
            elif section == 'updates':
                if not line.startswith('Listing') and not line.startswith('WARNING'):
                    result[section].append(line.strip())
            else:
                fields = line.split('|', 2)
                if len(fields) == 3:
                    result[section].append({'name': fields[0], 'image': fields[1], 'status': fields[2]})
                elif 'not found' in line.lower() or 'permission denied' in line.lower():
                    result['docker_error'] = line[:180]
    return result

def guest_exec(vmid):
    rc, out, err = run(['qm', 'guest', 'exec', str(vmid), '--', '/bin/sh', '-c', guest_script], 30)
    if rc:
        return None, err or out or f'guest exec exited {rc}'
    try:
        response = json.loads(out)
    except ValueError:
        return None, 'invalid guest-agent response'
    if response.get('exitcode', 0):
        return None, response.get('err-data') or f'guest exec exited {response["exitcode"]}'
    return parse_guest_text(response.get('out-data', '')), None

def os_info(vmid):
    rc, out, err = run(['qm', 'guest', 'cmd', str(vmid), 'get-osinfo'], 15)
    if rc:
        return None
    try:
        info = json.loads(out)
        return info.get('pretty-name') or info.get('name') or info.get('version')
    except ValueError:
        return None

rc, out, err = run(['pvesh', 'get', '/cluster/resources', '--output-format', 'json'])
if rc:
    raise SystemExit(err or 'could not list Proxmox guests')
resources = json.loads(out)
guests = []
for item in resources:
    kind = item.get('type')
    if kind not in ('lxc', 'qemu') or item.get('template'):
        continue
    guest = {'id': item.get('vmid'), 'name': item.get('name') or str(item.get('vmid')),
             'type': kind, 'status': item.get('status'), 'os': None, 'packages': [],
             'updates': [], 'app_versions': [], 'docker': [], 'error': None}
    if item.get('status') != 'running':
        guest['error'] = 'stopped'
    elif kind == 'lxc':
        rc, text, err = run(['pct', 'exec', str(item['vmid']), '--', 'sh', '-lc', guest_script], 30)
        if rc:
            guest['error'] = err or text or f'pct exec exited {rc}'
        else:
            guest.update(parse_guest_text(text))
    else:
        guest['os'] = os_info(item['vmid'])
        details, error = guest_exec(item['vmid'])
        if details:
            guest.update(details)
        elif error:
            guest['error'] = error
    guests.append(guest)
print(json.dumps({'guests': sorted(guests, key=lambda x: x['id'])}))
'''


def parse_smart(data):
    """Extract stable SMART facts from PVE's smartctl response."""
    attrs = {a.get('name'): a for a in data.get('attributes', []) if a.get('name')}
    text = data.get('text') or ''

    def text_number(pattern):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            return None
        value = match.group(1).replace(',', '')
        return int(value, 16) if value.lower().startswith('0x') else int(value)

    def attr_number(name, raw=True):
        item = attrs.get(name) or {}
        value = item.get('raw') if raw else item.get('value')
        match = re.search(r'\d+', str(value)) if value is not None else None
        return int(match.group()) if match else None

    nvme = data.get('type') == 'text'
    out = {
        'health': data.get('health'),
        'temperature': text_number(r'Temperature:\s*(\d+)\s+Celsius') if nvme else attr_number('Temperature_Celsius'),
        'hours': text_number(r'Power On Hours:\s*([\d,]+)') if nvme else attr_number('Power_On_Hours'),
        'cycles': text_number(r'Power Cycles:\s*(\d+)') if nvme else attr_number('Power_Cycle_Count'),
        'wear': text_number(r'Percentage Used:\s*(\d+)%') if nvme else data.get('wearout'),
        'reallocated': attr_number('Reallocated_Sector_Ct') if not nvme else None,
        'pending': attr_number('Current_Pending_Sector') if not nvme else None,
        'uncorrectable': attr_number('Offline_Uncorrectable') if not nvme else None,
        'critical_warning': text_number(r'Critical Warning:\s*(0x[0-9a-f]+)') if nvme else None,
        'media_errors': text_number(r'Media and Data Integrity Errors:\s*([\d,]+)') if nvme else None,
        'error_log_entries': text_number(r'Error Information Log Entries:\s*([\d,]+)') if nvme else None,
    }
    return out


def scan_guest_inventory(host, ssh_user='root', timeout=180, runner=subprocess.run):
    """Run the transient read-only scan on a Proxmox node and return guest inventories."""
    if not host or not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise ValueError('invalid Proxmox host')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', ssh_user):
        raise ValueError('invalid SSH user')
    command = [
        'ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
        '-o', 'StrictHostKeyChecking=yes', f'{ssh_user}@{host}', 'python3', '-',
    ]
    proc = runner(command, input=REMOTE_SCRIPT, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or f'ssh exited {proc.returncode}').strip()
        raise OSError(detail[:200])
    try:
        result = json.loads(proc.stdout)
    except ValueError as e:
        raise ValueError('Proxmox guest scanner returned invalid JSON') from e
    if not isinstance(result.get('guests'), list):
        raise ValueError('Proxmox guest scanner response has no guest list')
    return result['guests']


def parse_guest_text(text):
    """Parse the line-oriented payload emitted by the transient guest shell scan."""
    result = {'os': None, 'packages': [], 'updates': [], 'app_versions': [], 'docker': []}
    section = None
    for line in text.splitlines():
        if line.startswith('__OS__'):
            result['os'] = line[6:].strip()
            section = None
        elif line.startswith('__APP__'):
            result['app_versions'].append(line[7:].strip())
        elif line == '__PACKAGES__':
            section = 'packages'
        elif line == '__UPDATES__':
            section = 'updates'
        elif line == '__DOCKER__':
            section = 'docker'
        elif section and line.strip():
            if section == 'packages':
                result[section].append(line.strip())
            elif section == 'updates':
                if not line.startswith(('Listing', 'WARNING')):
                    result[section].append(line.strip())
            else:
                fields = line.split('|', 2)
                if len(fields) == 3:
                    result[section].append({'name': fields[0], 'image': fields[1], 'status': fields[2]})
                elif 'not found' in line.lower() or 'permission denied' in line.lower():
                    result['docker_error'] = line[:180]
    return result


def inventory_summary(guests, disks, smart):
    """Short status suitable for the existing PVE panel without displacing its meters."""
    issues = sum(
        1 for d in disks
        if (s := smart.get(d['name'])) and (
            s.get('health') not in ('PASSED', 'OK') or s.get('critical_warning') or s.get('pending') or
            s.get('reallocated') or s.get('uncorrectable') or s.get('media_errors') or s.get('error_log_entries')
        )
    )
    known = sum(1 for d in disks if d['name'] in smart)
    updates = sum(len(g.get('updates') or []) for g in guests)
    docker = sum(len(g.get('docker') or []) for g in guests)
    return {'disk_count': len(disks), 'smart_known': known, 'smart_issues': issues,
            'guest_count': len(guests), 'updates': updates, 'docker_count': docker}
