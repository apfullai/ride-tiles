"""Bounded, preservation-first refresh and one-city-at-a-time publication.

All writes are to a fresh staging directory. No deployment is performed here.
"""
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from rollout_snapshot import (BASE, MAX_MANIFEST_BYTES, MAX_REGIONS,
                              MAX_SITE_BYTES, inventory, restore_published,
                              seal_snapshot, validate_site, assert_live_unchanged)

ROOT = Path(__file__).resolve().parents[1]
REFRESH_DAYS = 90
PROCESS_SECONDS = 480
REGION_SECONDS = 240
FINALIZE_SECONDS = 90
MIN_BUILD_SECONDS = 60
FROZEN_REGION = 'chicago-metro'


class RolloutInterrupted(BaseException):
    """Global cancellation must escape candidate-failure recovery."""


def interrupt_rollout(signum, frame):
    raise RolloutInterrupted('Rollout process watchdog interrupted the build')


def utc(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Refresh timestamp must include timezone')
    return parsed.astimezone(dt.timezone.utc)


def load_configs(path):
    configs = json.loads(Path(path).read_text())
    if not isinstance(configs, list) or not configs:
        raise ValueError('Expected a nonempty ordered region queue')
    seen = set()
    for c in configs:
        if (not isinstance(c, dict) or not re.fullmatch(r'[a-z0-9-]{1,48}', c.get('id', ''))
                or c['id'] in seen or c['id'] == FROZEN_REGION):
            raise ValueError('Invalid, duplicate or frozen queue identity')
        seen.add(c['id'])
        bounds = c.get('bounds', [])
        if (len(bounds) != 4 or any(type(x) not in (int, float) for x in bounds)
                or not -180 <= bounds[0] < bounds[2] <= 180
                or not -85 <= bounds[1] < bounds[3] <= 85
                or not 0 < (bounds[2]-bounds[0])*(bounds[3]-bounds[1]) <= 6
                or (c.get('minzoom'), c.get('maxzoom')) != (8, 14)
                or not isinstance(c.get('name'), str)
                or not isinstance(c.get('sourceStates'), list)
                or not 1 <= len(c.get('sourceStates', [])) <= (4 if c['id'] == 'chicagoland' and bounds == [-89, 41.05, -86.65, 43.25] else 3)
                or any(not isinstance(x, str) or not re.fullmatch(r'[a-z-]+', x) for x in c['sourceStates'])
                or len(set(c['sourceStates'])) != len(c['sourceStates'])):
            raise ValueError('Unbounded or unsupported region configuration')
    return configs


def load_state(site, manifest):
    path = site / 'rollout-state.json'
    state = json.loads(path.read_text()) if path.exists() else {'schemaVersion': 1, 'refreshedAt': {}}
    if state.get('schemaVersion') != 1 or not isinstance(state.get('refreshedAt'), dict):
        raise ValueError('Invalid refresh state')
    ids = {r['id'] for r in manifest['regions']}
    for rid, stamp in state['refreshedAt'].items():
        if rid not in ids:
            raise ValueError('Refresh state names missing region')
        utc(stamp)
    state.setdefault('deferred', {})
    if not isinstance(state['deferred'], dict):
        raise ValueError('Invalid deferred state')
    for rid, item in state['deferred'].items():
        if (not re.fullmatch(r'[a-z0-9-]{1,48}', rid) or not isinstance(item, dict)
                or not isinstance(item.get('reason'), str)):
            raise ValueError('Invalid deferred region')
        if utc(item['retryAfter']) <= utc(item['lastAttemptAt']):
            raise ValueError('Invalid retry interval')
    return state


def due_regions(manifest, state, now, include_deferred=False):
    """No state means existing mutable coverage gets an initial refresh first."""
    def last(region):
        stamp = state['refreshedAt'].get(region['id'])
        return utc(stamp) if stamp else dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    regions = [r for r in manifest['regions'] if r['id'] != FROZEN_REGION]
    if any(last(r) > now for r in regions):
        raise ValueError('Refresh state is in the future')
    return sorted((r for r in regions if (now-last(r)).days >= REFRESH_DAYS
                   and (include_deferred or r['id'] not in state.get('deferred', {})
                        or utc(state['deferred'][r['id']]['retryAfter']) <= now)),
                  key=lambda r: (last(r), r['id']))


def subprocess_builder(config, root, seconds):
    """Kill the entire per-region process group, including curl and osmium."""
    request = root / 'request.json'
    request.write_text(json.dumps(config))
    command = [sys.executable, str(ROOT / 'publishing/build_one.py'), str(request), str(root)]
    environment = dict(os.environ, RIDE_DOWNLOAD_LEDGER=str(root.parent/'download-budget.json'))
    process = subprocess.Popen(command, start_new_session=True, env=environment)
    try:
        code = process.wait(timeout=seconds)
        if code:
            raise RuntimeError(f'Region process exited {code}')
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    return json.loads((root / 'region.json').read_text()), root / 'site'


def merge_region(site, manifest, entry, generated):
    rid, version = entry['id'], entry['version']
    old = next((r for r in manifest['regions'] if r['id'] == rid), None)
    if rid == FROZEN_REGION:
        raise ValueError('Cannot regenerate frozen metro')
    if old:
        for field in ('bounds', 'minzoom', 'maxzoom', 'sourceLayer'):
            if old[field] != entry[field]:
                raise ValueError('Refresh must not change coverage contract')
    destination = site / 'regions' / rid / version
    source = generated / 'regions' / rid / version
    if not source.is_dir():
        raise ValueError('Region output is missing')
    if destination.exists():
        # The tile content version is immutable. Keep original metadata/source too.
        if inventory(destination / 'tiles') != inventory(source / 'tiles'):
            raise ValueError('Existing version has different content')
        if old and old['version'] == version:
            return False
        raise ValueError('Refusing to move current region back to a retained old version')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    if old:
        manifest['regions'][manifest['regions'].index(old)] = entry
    else:
        manifest['regions'].append(entry)
    return True


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def prepare(site, configs, *, now=None, builder=subprocess_builder,
            seconds=PROCESS_SECONDS, monotonic=time.monotonic):
    """Refresh every due region that fits; expansion waits until all are current.

    A failed/oversized candidate is discarded. Valid completed refreshes can still
    be published. The caller only uploads when changed=True after final validation.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    end = monotonic() + seconds
    manifest = validate_site(site)
    state = load_state(site, manifest)
    original = inventory(site)
    initial_state = copy.deepcopy(state)
    by_id = {c['id']: c for c in configs}
    due = due_regions(manifest, state, now)
    report = {'changed': False, 'refreshed': [], 'added': None, 'skipped': [],
              'dueBefore': [r['id'] for r in due], 'refreshTargetDays': REFRESH_DAYS}
    pending = []
    for region in due:
        config = by_id.get(region['id'])
        if config is None:
            pending.append((region['id'], 'Missing refresh configuration'))
        else:
            pending.append((region['id'], config))
    # Even when there is no due work, try no more than one queued city.
    existing_ids = {r['id'] for r in manifest['regions']}
    next_city = next((c for c in configs if c['id'] not in existing_ids
                      and (c['id'] not in state['deferred']
                           or utc(state['deferred'][c['id']]['retryAfter']) <= now)), None)
    if next_city and len(existing_ids) >= MAX_REGIONS:
        report['skipped'].append({'id': next_city['id'], 'reason': 'Client manifest region cap reached'})
        next_city = None
    attempts = [(rid, conf, False) for rid, conf in pending]
    if next_city:
        attempts.append((next_city['id'], next_city, True))
    blocked = False
    for rid, config, addition in attempts:
        if addition and (blocked or due_regions(manifest, state, now)):
            report['skipped'].append({'id': rid, 'reason': 'Existing refresh work remains due'})
            break
        if isinstance(config, str):
            report['skipped'].append({'id': rid, 'reason': config})
            blocked = True
            continue
        available = end - monotonic() - FINALIZE_SECONDS
        if available < MIN_BUILD_SECONDS:
            report['skipped'].append({'id': rid, 'reason': 'Process runtime reserve exhausted'})
            break
        # Each attempt is disposable. Never mutate live or accept partial output.
        with tempfile.TemporaryDirectory(dir=site.parent, prefix='region-') as tmp:
            work = Path(tmp)
            try:
                print(json.dumps({'phase': 'build', 'region': rid, 'seconds': min(REGION_SECONDS, available)}), flush=True)
                entry, generated = builder(config, work, min(REGION_SECONDS, available))
                if entry.get('id') != rid:
                    raise ValueError('Builder returned wrong region')
                candidate = work / 'candidate'
                shutil.copytree(site, candidate)
                candidate_manifest = copy.deepcopy(manifest)
                candidate_state = copy.deepcopy(state)
                data_changed = merge_region(candidate, candidate_manifest, entry, generated)
                candidate_state['refreshedAt'][rid] = now.isoformat()
                candidate_state['deferred'].pop(rid, None)
                if data_changed:
                    candidate_manifest['updatedAt'] = now.isoformat()
                write_json(candidate / 'manifest.json', candidate_manifest)
                write_json(candidate / 'rollout-state.json', candidate_state)
                # Includes retained historical files, ODbL sources and new recovery shards.
                seal_snapshot(candidate)
                validate_site(candidate)
            except (Exception,) as error:
                reason = str(error)[:300]
                report['skipped'].append({'id': rid, 'reason': reason})
                state['deferred'][rid] = {'lastAttemptAt': now.isoformat(),
                    'retryAfter': (now + dt.timedelta(days=21)).isoformat(), 'reason': reason}
                print(json.dumps({'phase': 'deferred', 'region': rid, 'reason': reason}), flush=True)
                blocked = True
                # A failed refresh does not authorize trying a new city this run.
                continue
            # Atomic replacement of local staging; Pages remains unchanged until deploy.
            old_stage = work / 'previous'
            site.rename(old_stage)
            candidate.rename(site)
            manifest, state = candidate_manifest, candidate_state
            if addition:
                report['added'] = rid
            else:
                report['refreshed'].append(rid)
    if state != initial_state:
        write_json(site/'rollout-state.json', state)
        print(json.dumps({'phase': 'seal', 'refreshed': report['refreshed'], 'added': report['added']}), flush=True)
        seal_snapshot(site)
    report['dueRemaining'] = [r['id'] for r in due_regions(manifest, state, now, include_deferred=True)]
    report['eligibleDueRemaining'] = [r['id'] for r in due_regions(manifest, state, now)]
    report['deferred'] = state['deferred']
    report['changed'] = inventory(site) != original
    if report['changed']:
        validate_site(site)
        report['siteBytes'] = sum(p.stat().st_size for p in site.rglob('*') if p.is_file())
        if report['siteBytes'] > MAX_SITE_BYTES or (site/'manifest.json').stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError('Final publication budget exceeded')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config/rollout-regions.json')
    parser.add_argument('--output', default='site')
    parser.add_argument('--report', default='rollout-report.json')
    args = parser.parse_args()
    # Never let a process-level watchdog strand a separate region process group.
    signal.signal(signal.SIGTERM, interrupt_rollout)
    if os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1':
        raise RuntimeError('Retry using a fresh workflow dispatch so budget is rechecked')
    site = Path(args.output).resolve()
    if site.exists():
        raise ValueError('Use a fresh output directory')
    configs = load_configs(args.config)
    receipt = {}
    restore_published(site, receipt=receipt)
    report = prepare(site, configs)
    if report['changed']:
        assert_live_unchanged(receipt['liveManifestSha256'])
    report['restoredManifestSha256'] = receipt['liveManifestSha256']
    write_json(Path(args.report), report)
    print(json.dumps(report, indent=2))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('changed=' + str(report['changed']).lower() + '\n')
            output.write('restored_manifest_sha256=' + receipt['liveManifestSha256'] + '\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('## Bounded tile rollout\n\n```json\n' + json.dumps(report, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()
