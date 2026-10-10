"""Forgejo Actions CI builds: monitor build progress, status, and versions
for long-running builds (afterlife, luneta, companion_app).
"""
import os
import re
import time
from datetime import datetime

from .poll import Poller, get_json

DISPLAY_NAMES = {
    'luneta': 'Luneta',
    'afterlife': 'Afterlife',
    'companion_app': 'Companion (Android)',
}


def iso(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
        return t if t > 86400 else None
    except (AttributeError, ValueError):
        return None


def read_token(path):
    try:
        with open(os.path.expanduser(path)) as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = os.environ.get('FORGEJO_API_KEY')
    if tok:
        return tok.strip()
    try:
        with open('/opt/afterlife/.env.local') as f:
            for line in f:
                if line.startswith('FORGEJO_API_KEY='):
                    return line.split('=', 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return None


class BuildsCollector:
    def __init__(self, forgejo_url='https://git.ariku.pl', token_file='~/.config/piwnica-dashboard/forgejo.token',
                 repos=None, interval=30, background=True, fetch=get_json, clock=time.time):
        self.base = forgejo_url.rstrip('/')
        self.token_file = token_file
        self.token = read_token(token_file)
        self.repos = repos if repos is not None else ['afterlife', 'luneta', 'companion_app']
        self.fetch = fetch
        self.clock = clock
        self.poller = Poller(self.fetch_builds, interval, background, clock)

    def _fetch_repo_build(self, repo_key, headers, now):
        full = repo_key if '/' in repo_key else f'ariku/{repo_key}'
        short = repo_key.split('/')[-1]
        display_name = DISPLAY_NAMES.get(short, short.replace('_', ' ').replace('-', ' ').title())

        # 1. Fetch recent workflow runs
        runs_doc = self.fetch(f'{self.base}/api/v1/repos/{full}/actions/runs?limit=8', headers, timeout=10)
        runs = runs_doc.get('workflow_runs') or []
        if not runs:
            return {
                'repo': short, 'full_name': full, 'name': display_name,
                'version': 'unknown', 'ref': '', 'status': 'no runs',
                'workflow': '', 'job': '', 'jobs_total': 0, 'jobs_done': 0,
                'pct': 0, 'elapsed': None, 'duration': None, 'queued_time': None,
                'queued_runs': 0, 'age': None, 'commit_title': '', 'commit_sha': '',
            }

        # 2. Fetch latest tag if available
        tags_doc = []
        try:
            tags_doc = self.fetch(f'{self.base}/api/v1/repos/{full}/tags?limit=1', headers, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        latest_tag = tags_doc[0].get('name') if tags_doc and isinstance(tags_doc, list) else None

        active_running = [r for r in runs if r.get('status') == 'running']
        active_waiting = [r for r in runs if r.get('status') == 'waiting']

        primary = active_running[0] if active_running else (active_waiting[0] if active_waiting else runs[0])
        status = primary.get('status') or 'unknown'

        # Count other runs in queue
        other_queued = len(active_running) + len(active_waiting)
        if primary in active_running or primary in active_waiting:
            other_queued -= 1

        # 3. Fetch jobs for primary run
        jobs = []
        try:
            jobs = self.fetch(f'{self.base}/api/v1/repos/{full}/actions/runs/{primary["id"]}/jobs', headers, timeout=8)
            if not isinstance(jobs, list):
                jobs = []
        except Exception:  # noqa: BLE001
            pass

        total_jobs = len(jobs)
        done_jobs = sum(1 for j in jobs if j.get('status') in ('success', 'failure', 'skipped'))
        running_job = next((j.get('name') for j in jobs if j.get('status') == 'running'), None)
        waiting_job = next((j.get('name') for j in jobs if j.get('status') == 'waiting'), None)
        job_name = running_job or waiting_job or (jobs[0].get('name') if jobs else '')

        # 4. Resolve version
        pref = primary.get('prettyref') or ''
        title = primary.get('title') or ''
        sha = (primary.get('commit_sha') or '')[:8]

        version = None
        if pref.startswith('v'):
            version = pref
        elif latest_tag:
            version = latest_tag
        else:
            m = re.match(r'^(v?\d+\.\d+(?:\.\d+)?):', title)
            if m:
                version = m.group(1)

        # Fallback to inspecting manifest if no tag or title version found
        if not version:
            for manifest_path in ('package.json', 'app/build.gradle.kts'):
                try:
                    ref_arg = f'?ref={pref}' if pref else ''
                    doc = self.fetch(f'{self.base}/api/v1/repos/{full}/contents/{manifest_path}{ref_arg}',
                                     headers, timeout=5)
                    if doc and 'content' in doc:
                        import base64
                        decoded = base64.b64decode(doc['content']).decode('utf-8', errors='replace')
                        if manifest_path.endswith('.json'):
                            import json
                            pv = json.loads(decoded).get('version')
                            if pv:
                                version = f'v{pv}' if not pv.startswith('v') else pv
                                break
                        else:
                            mv = re.search(r'versionName\s*=\s*["\']([^"\']+)["\']', decoded)
                            if mv:
                                pv = mv.group(1)
                                version = f'v{pv}' if not pv.startswith('v') else pv
                                break
                except Exception:  # noqa: BLE001
                    pass

        if not version:
            version = pref or sha or 'main'

        # 5. Timings & progress pct
        started = iso(primary.get('started'))
        stopped = iso(primary.get('stopped'))
        created = iso(primary.get('created'))

        elapsed = None
        duration = None
        queued_time = None
        age = None
        pct = 0

        if status == 'running':
            elapsed = (now - started) if started else ((now - created) if created else 0)
            pct = (100 * done_jobs / total_jobs) if total_jobs > 1 else None
        elif status == 'waiting':
            queued_time = (now - created) if created else 0
            pct = 0
        elif status in ('success', 'failure', 'cancelled', 'skipped'):
            duration = (stopped - started) if (started and stopped) else None
            age = (now - stopped) if stopped else None
            pct = 100

        return {
            'repo': short,
            'full_name': full,
            'name': display_name,
            'version': version,
            'ref': pref,
            'status': status,
            'workflow': primary.get('workflow_id') or '',
            'job': job_name,
            'jobs_total': total_jobs,
            'jobs_done': done_jobs,
            'pct': pct,
            'elapsed': elapsed,
            'duration': duration,
            'queued_time': queued_time,
            'queued_runs': other_queued,
            'age': age,
            'commit_title': title,
            'commit_sha': sha,
        }

    def fetch_builds(self):
        if not self.token:
            raise ValueError('no token')
        headers = {'Authorization': f'token {self.token}'}
        now = self.clock()
        items = []
        for rk in self.repos:
            try:
                items.append(self._fetch_repo_build(rk, headers, now))
            except Exception as e:  # noqa: BLE001
                short = rk.split('/')[-1]
                items.append({
                    'repo': short, 'full_name': rk,
                    'name': DISPLAY_NAMES.get(short, short.title()),
                    'version': '?', 'ref': '', 'status': 'error',
                    'workflow': '', 'job': '', 'jobs_total': 0, 'jobs_done': 0,
                    'pct': 0, 'elapsed': None, 'duration': None, 'queued_time': None,
                    'queued_runs': 0, 'age': None, 'commit_title': f'{type(e).__name__}: {e}',
                    'commit_sha': '',
                })
        active = sum(1 for it in items if it.get('status') in ('running', 'waiting'))
        return {'items': items, 'total': len(items), 'active': active}

    def sample(self):
        if self.token is None:
            self.token = read_token(self.token_file)
        if not self.token:
            return {'error': 'no token'}
        p = self.poller.poll()
        if not p.value:
            return {'error': p.error or 'connecting…'}
        out = dict(p.value)
        out['age'] = self.clock() - p.at if p.at else None
        out['error'] = p.error
        return out
