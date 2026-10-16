"""One durable, coarse Pages-backed build stage per admitted workflow run."""
import argparse
import copy
import datetime as dt
import hashlib
import html
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

from gradual_rollout import (ROOT, FROZEN_REGION, RolloutInterrupted, interrupt_rollout,
                             load_configs, load_state, due_regions, merge_region,
                             utc, write_json)
from rollout_snapshot import (BASE, MAX_REGIONS, _records, inventory, restore_published,
                              seal_snapshot, validate_site, assert_publication_unchanged)

ACTIVE = Path('checkpoints/active')
DESCRIPTOR = 'checkpoint.json'
PROCESS_SECONDS = 500
STAGE_SECONDS = 300
FINALIZE_SECONDS = 90
MIN_STAGE_SECONDS = 45
SCHEMA = 1


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def checkpoint_files(path):
    return {k: v for k, v in _records(path, include_metadata=True).items() if k != DESCRIPTOR}


def empty_progress(config):
    return {'completedStates': [], 'indexReady': False, 'batchIds': [],
            'batchCount': 0, 'completedBatches': []}


def inspect_stage(config, plan, path):
    sys.path.insert(0, str(ROOT/'tools'))
    from resumable_region import validate_checkpoint
    return validate_checkpoint(config, plan, path)


def next_stage(config, progress, plan):
    if plan is None:
        return {'kind': 'start'}
    states = progress['completedStates']
    if not isinstance(states, list) or any(s not in config['sourceStates'] for s in states) or len(set(states)) != len(states):
        raise ValueError('Invalid source progress')
    for state in config['sourceStates']:
        if state not in states:
            return {'kind': 'source', 'state': state}
    if not progress['indexReady']:
        return {'kind': 'index'}
    batches = progress['batchIds']
    if (not isinstance(batches, list) or not batches or batches != list(range(len(batches)))
            or progress['batchCount'] != len(batches)):
        raise ValueError('Invalid indexed batch plan')
    completed = progress['completedBatches']
    if not isinstance(completed, list) or len(set(completed)) != len(completed) or any(b not in batches for b in completed):
        raise ValueError('Invalid completed tile batches')
    for batch in batches:
        if batch not in completed:
            return {'kind': 'tiles', 'batchId': batch}
    return {'kind': 'finalize'}


def validate_transition(config, before, after, stage):
    kind = stage['kind']
    if kind in ('start', 'source'):
        state = config['sourceStates'][0] if kind == 'start' else stage['state']
        completed = after['completedStates']
        pending = [s for s in config['sourceStates'] if s not in before['completedStates']]
        added = [s for s in completed if s not in before['completedStates']]
        expected = before['completedStates'] + added
        if (before['completedStates'] != config['sourceStates'][:len(before['completedStates'])]
                or after != {**before, 'completedStates': expected}
                or added != pending[:len(added)] or not pending or pending[0] != state):
            raise ValueError('Source stage advanced unexpected work')
    elif kind == 'index':
        if before['indexReady'] or not after['indexReady'] or after['completedStates'] != before['completedStates'] or after['completedBatches']:
            raise ValueError('Index stage advanced unexpected work')
    elif kind == 'tiles':
        completed = after['completedBatches']
        added = [b for b in completed if b not in before['completedBatches']]
        pending = [b for b in before['batchIds'] if b not in before['completedBatches']]
        expected = sorted(before['completedBatches']+added)
        if (after != {**before, 'completedBatches': expected} or added != pending[:len(added)]
                or not pending or pending[0] != stage['batchId']):
            raise ValueError('Tile group advanced unexpected work')
    elif kind == 'finalize' and after != before:
        raise ValueError('Finalization changed checkpoint progress')


def load_checkpoint(site, configs, *, expected_fingerprint=None, inspector=inspect_stage):
    root = site/ACTIVE
    if not root.exists():
        return None
    if not root.is_dir() or not (root/DESCRIPTOR).is_file():
        raise ValueError('Incomplete active checkpoint descriptor')
    checkpoint = json.loads((root/DESCRIPTOR).read_text())
    if (checkpoint.get('schemaVersion') != SCHEMA
            or not re.fullmatch(r'[0-9a-f]{64}', checkpoint.get('generationId', ''))
            or checkpoint.get('purpose') not in ('refresh', 'addition')):
        raise ValueError('Invalid active checkpoint')
    config = checkpoint.get('config')
    current = next((c for c in configs if c['id'] == (config or {}).get('id')), None)
    if current is None or config != current or config['id'] == FROZEN_REGION:
        raise ValueError('Active checkpoint configuration changed; explicit restart required')
    if checkpoint.get('configFingerprint') != fingerprint(config):
        raise ValueError('Active checkpoint config fingerprint differs')
    if expected_fingerprint is not None and checkpoint.get('codeFingerprint') != expected_fingerprint:
        raise ValueError('Active checkpoint code changed; explicit restart required')
    if checkpoint.get('files') != checkpoint_files(root):
        raise ValueError('Active checkpoint file hashes or inventory differ')
    for field in ('startedAt', 'updatedAt'):
        utc(checkpoint[field])
    if checkpoint.get('retryAfter') is not None:
        utc(checkpoint['retryAfter'])
    plan = checkpoint.get('sourcePlan')
    progress = inspector(config, plan, root) if plan is not None else empty_progress(config)
    if plan is None and checkpoint['files']:
        raise ValueError('Unpinned checkpoint contains stage output')
    if checkpoint.get('progress') != progress or checkpoint.get('nextStage') != next_stage(config, progress, plan):
        raise ValueError('Active checkpoint progress differs from verified stages')
    return checkpoint


def save_checkpoint(site, checkpoint):
    path = site/ACTIVE
    path.mkdir(parents=True, exist_ok=True)
    checkpoint['files'] = checkpoint_files(path)
    write_json(path/DESCRIPTOR, checkpoint)


def select_region(manifest, state, configs, now, start_region=None):
    by_id = {c['id']: c for c in configs}
    ids = {r['id'] for r in manifest['regions']}
    if start_region:
        if start_region == FROZEN_REGION or start_region not in ids or start_region not in by_id:
            raise ValueError('Explicit start must name a configured existing mutable region')
        return by_id[start_region], 'refresh'
    due = due_regions(manifest, state, now)
    if due:
        if due[0]['id'] not in by_id:
            raise ValueError('Oldest due region lacks configuration; expansion is blocked')
        return by_id[due[0]['id']], 'refresh'
    if len(ids) < MAX_REGIONS:
        for config in configs:
            deferred = state['deferred'].get(config['id'])
            if config['id'] not in ids and (not deferred or utc(deferred['retryAfter']) <= now):
                return config, 'addition'
    return None


class StageFailure(Exception):
    def __init__(self, message, partial_result=None):
        super().__init__(message)
        self.partial_result = partial_result or {}


def subprocess_stage(request, checkpoint, work, seconds):
    work.mkdir(parents=True, exist_ok=True)
    request_path, result_path = work/'request.json', work/'result.json'
    write_json(request_path, request)
    command = [sys.executable, str(ROOT/'publishing/resumable_stage_runner.py'),
               str(request_path), str(checkpoint), str(work/'stage-work'), str(result_path)]
    environment = dict(os.environ, RIDE_DOWNLOAD_LEDGER=str(work.parent/'download-budget.json'))
    process = subprocess.Popen(command, start_new_session=True, env=environment)
    def partial():
        if result_path.exists() and result_path.stat().st_size <= 100_000:
            return json.loads(result_path.read_text())
        return {}
    try:
        code = process.wait(timeout=seconds)
        if code:
            raise StageFailure(f'Stage process exited {code}', partial())
    except BaseException as error:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        if isinstance(error, subprocess.TimeoutExpired):
            raise StageFailure(f'Stage process reached its {seconds:.0f}-second limit', partial()) from error
        raise
    result = partial()
    if not result or result.get('schemaVersion') != SCHEMA:
        raise StageFailure('Stage did not produce a valid completion receipt')
    return result, work/'stage-work'/'region-site'


def verify_code(root, site, timeout):
    from verified_code import verify_code as verify
    from actions_budget import fetch_public_json
    return verify(root, site, timeout=timeout, fetch=fetch_public_json)


def status_for(checkpoint, now, *, last_completed=None, mode=None):
    progress = checkpoint['progress'] if checkpoint else {}
    stage = checkpoint['nextStage'] if checkpoint else None
    error = checkpoint.get('lastError') if checkpoint else None
    return {'schemaVersion': 1, 'updatedAt': now.isoformat(), 'active': checkpoint is not None,
            'city': {'id': checkpoint['config']['id'], 'name': checkpoint['config']['name']} if checkpoint else None,
            'stage': stage['kind'] if stage else None, 'stageDetails': stage,
            'completedCounts': {'sources': len(progress.get('completedStates', [])),
                'sourceTotal': len(checkpoint['config']['sourceStates']) if checkpoint else 0,
                'batches': len(progress.get('completedBatches', [])), 'batchTotal': progress.get('batchCount', 0)},
            'retryAfter': checkpoint.get('retryAfter') if checkpoint else None,
            'needsAttention': bool(checkpoint.get('needsAttention', False)) if checkpoint else False, 'reason': error['message'] if error else None,
            'sourceDate': (checkpoint.get('sourcePlan') or {}).get('sourceDate') if checkpoint else None,
            'sourceTimestamp': (checkpoint.get('sourcePlan') or {}).get('osmReplicationTimestamp') if checkpoint else None,
            'sourceAcquisitionExpiresAt': (checkpoint.get('sourcePlan') or {}).get('expiresAt') if checkpoint else None,
            'failureCount': checkpoint.get('failureCount', 0) if checkpoint else 0,
            'generationId': checkpoint['generationId'] if checkpoint else None,
            'verification': mode, 'lastCompleted': last_completed,
            'checkpointMigration': checkpoint.get('migration') if checkpoint else None}


def write_status(site, status):
    write_json(site/'status.json', status)
    title = 'Tile update in progress' if status['active'] else 'Tile update complete'
    city = (status.get('city') or {}).get('name', (status.get('lastCompleted') or {}).get('name', 'No active city'))
    counts = status['completedCounts']
    fields = [('City', city), ('Next stage', status['stage'] or 'None'),
              ('Sources complete', f"{counts['sources']} / {counts['sourceTotal']}"),
              ('Tile batches complete', f"{counts['batches']} / {counts['batchTotal']}"),
              ('Pinned source date', status['sourceDate'] or 'Pending'),
              ('Source acquisition expires', status.get('sourceAcquisitionExpiresAt') or 'Pending'),
              ('Retry after', status['retryAfter'] or 'Next admitted continuation'),
              ('Attention', status['reason'] or 'No blocker reported'), ('Updated', status['updatedAt'])]
    rows = ''.join(f'<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>' for k,v in fields)
    (site/'status.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>Ride tile update status</title><style>body{font:17px system-ui;max-width:48em;margin:3em auto;padding:0 1em;line-height:1.5}'
        'dt{font-weight:600;margin-top:1em}dd{margin-left:0}</style><h1>'+html.escape(title)+'</h1>'
        '<p>Published map coverage stays available while a complete replacement is prepared.</p><dl>'+rows+'</dl>'
        '<p><a href="manifest.json">Active coverage</a> · <a href="status.json">Machine-readable status</a></p>')


def current_code_fingerprint(root):
    from verified_code import current_fingerprint
    return current_fingerprint(root)


def blocked_publication(site, error, now, deadline, verifier, monotonic):
    """Publish a blocker without changing a byte of an old/incompatible checkpoint."""
    remaining = deadline-monotonic()-FINALIZE_SECONDS
    if remaining < MIN_STAGE_SECONDS:
        raise ValueError('Cannot safely publish checkpoint blocker: '+str(error))
    receipt, mode = verifier(ROOT, site, timeout=min(300, remaining))
    try:
        checkpoint = json.loads((site/ACTIVE/DESCRIPTOR).read_text())
        status = status_for(checkpoint, now, mode=mode)
    except Exception:
        status = status_for(None, now, mode=mode)
    status.update(active=True, needsAttention=True, retryAfter=None, reason=str(error)[:500])
    existing = site/'status.json'
    if existing.exists():
        old = json.loads(existing.read_text())
        if old.get('needsAttention') is True and old.get('reason') == status['reason']:
            return {'changed': False, **old}
    before = inventory(site/ACTIVE) if (site/ACTIVE).exists() else {}
    with tempfile.TemporaryDirectory(dir=site.parent, prefix='blocked-') as temporary:
        temp=Path(temporary); candidate=temp/'site'
        shutil.copytree(site,candidate)
        write_json(candidate/'verification.json',receipt)
        write_status(candidate,status)
        report=seal_snapshot(candidate)
        validate_site(candidate)
        if before != inventory(candidate/ACTIVE):
            raise ValueError('Blocked publication changed checkpoint bytes')
        site.rename(temp/'previous');candidate.rename(site)
    return {'changed':True, 'siteBytes':report['siteBytes'], **status}


def advance(site, configs, *, allow_new=False, start_region=None, now=None, deadline=None,
            execute=subprocess_stage, inspector=inspect_stage, verifier=verify_code,
            fingerprinter=current_code_fingerprint, monotonic=time.monotonic):
    now = now or dt.datetime.now(dt.timezone.utc)
    deadline = deadline if deadline is not None else monotonic()+PROCESS_SECONDS
    manifest = validate_site(site)
    manifest_before = (site/'manifest.json').read_bytes()
    state = load_state(site, manifest)
    try:
        checkpoint = load_checkpoint(site, configs, inspector=inspector)
    except Exception as error:
        return blocked_publication(site, error, now, deadline, verifier, monotonic)
    if checkpoint and checkpoint.get('sourcePlan'):
        old = next((item for item in manifest['regions'] if item['id'] == checkpoint['config']['id']), None)
        timestamp = checkpoint['sourcePlan'].get('osmReplicationTimestamp')
        if old and old.get('sourceTimestamp') and (not timestamp or utc(timestamp) < utc(old['sourceTimestamp'])):
            return blocked_publication(site, ValueError('Pinned source would regress active source freshness'), now, deadline, verifier, monotonic)
    current_fingerprint = fingerprinter(ROOT)
    migration_pending = False
    if checkpoint and checkpoint.get('codeFingerprint') != current_fingerprint:
        from checkpoint_migrations import eligible
        migration_pending = eligible(site/ACTIVE, checkpoint, ROOT)
        if not migration_pending:
            return blocked_publication(site, ValueError('Active checkpoint code changed; explicit restart required'),
                                       now, deadline, verifier, monotonic)
    if start_region and (checkpoint or not allow_new):
        raise ValueError('Explicit start requires new-work permission and no active generation')
    if checkpoint is None:
        if not allow_new:
            return {'changed': False, 'reason': 'Continuation-only run has no active generation', 'active': False}
        selected = select_region(manifest, state, configs, now, start_region)
        if selected is None:
            return {'changed': False, 'reason': 'No eligible due refresh or new city', 'active': False}
    elif checkpoint.get('needsAttention'):
        return {'changed': False, **status_for(checkpoint, now)}
    elif checkpoint.get('retryAfter') and utc(checkpoint['retryAfter']) > now:
        return {'changed': False, 'reason': 'Active stage is waiting for its retry time', **status_for(checkpoint, now)}
    available = deadline-monotonic()-FINALIZE_SECONDS
    if available < MIN_STAGE_SECONDS:
        return {'changed': False, 'reason': 'Insufficient time to verify and checkpoint safely', 'active': bool(checkpoint)}
    receipt, mode = verifier(ROOT, site, timeout=min(300, available))
    code_hash = receipt.get('fingerprint')
    if not isinstance(code_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', code_hash):
        raise ValueError('Verification did not establish a source fingerprint')
    if migration_pending:
        from checkpoint_migrations import migrate
        checkpoint = migrate(site/ACTIVE, checkpoint, ROOT, target_fingerprint=current_fingerprint,
                             verified_fingerprint=code_hash, now=now)
    elif checkpoint:
        checkpoint = load_checkpoint(site, configs, expected_fingerprint=code_hash, inspector=inspector)
    else:
        config, purpose = selected
        checkpoint = {'schemaVersion': SCHEMA, 'generationId': fingerprint({'config': config, 'code': code_hash, 'startedAt': now.isoformat()}),
            'config': config, 'configFingerprint': fingerprint(config), 'codeFingerprint': code_hash,
            'purpose': purpose, 'sourcePlan': None, 'progress': empty_progress(config), 'nextStage': {'kind': 'start'},
            'startedAt': now.isoformat(), 'updatedAt': now.isoformat(), 'retryAfter': None, 'lastError': None, 'stageCount': 0, 'failureCount': 0, 'needsAttention': False, 'files': {}}
    checkpoint = copy.deepcopy(checkpoint)
    if migration_pending:
        # Keep a small durable audit record after this generation is promoted.
        audits = state.setdefault('checkpointMigrations', [])
        if not isinstance(audits, list):
            raise ValueError('Invalid checkpoint migration audit history')
        audits.append({'generationId': checkpoint['generationId'], **checkpoint['migration']})
    stage = checkpoint['nextStage']
    result = None
    last_completed = None
    with tempfile.TemporaryDirectory(dir=site.parent, prefix='resume-') as directory:
        temp = Path(directory)
        candidate = temp/'site'
        shutil.copytree(site, candidate)
        (candidate/ACTIVE).mkdir(parents=True, exist_ok=True)
        write_json(candidate/'verification.json', receipt)
        available = deadline-monotonic()-FINALIZE_SECONDS
        if available >= MIN_STAGE_SECONDS:
            print(json.dumps({'phase': 'checkpoint-stage', 'city': checkpoint['config']['id'], 'stage': stage, 'seconds': min(STAGE_SECONDS, available)}), flush=True)
            request = {'config': checkpoint['config'], 'sourcePlan': checkpoint['sourcePlan'], 'stage': stage, 'batchSize': 128, 'baseUrl': BASE, 'stageSeconds': min(STAGE_SECONDS, available),
                       'minimumSourceTimestamp': next((x.get('sourceTimestamp') for x in manifest['regions'] if x['id']==checkpoint['config']['id']), None)}
            previous_checkpoint = copy.deepcopy(checkpoint)
            try:
                result, generated = execute(request, candidate/ACTIVE, temp/'work', min(STAGE_SECONDS, available))
                plan = result.get('sourcePlan', checkpoint['sourcePlan'])
                if not isinstance(plan, dict):
                    raise ValueError('Stage completion lacks pinned source plan')
                if checkpoint['sourcePlan'] is not None and plan != checkpoint['sourcePlan']:
                    raise ValueError('Stage attempted to change source pins')
                progress = inspector(checkpoint['config'], plan, candidate/ACTIVE)
                if result.get('progress') != progress:
                    raise ValueError('Stage completion differs from verified progress')
                validate_transition(checkpoint['config'], checkpoint['progress'], progress, stage)
                if stage['kind'] not in ('index', 'finalize'):
                    current_files = checkpoint_files(candidate/ACTIVE)
                    if any(current_files.get(path) != record for path,record in checkpoint['files'].items()
                           if path.startswith(('sources/', 'index/', 'batches/'))):
                        raise ValueError('Stage changed a previously completed checkpoint file')
                old_next = stage
                following = next_stage(checkpoint['config'], progress, plan)
                if stage['kind'] not in ('start', 'finalize') and following == old_next:
                    if stage['kind'] not in ('tiles', 'source') or result.get('plannedPause') is not True:
                        raise ValueError('Successful stage did not advance progress')
                if (stage['kind'] in ('start', 'source')
                        and len(progress['completedStates'])-len(checkpoint['progress']['completedStates']) > 1
                        and 'sourceGroup' not in result):
                    raise ValueError('Multiple source advances require an explicit group receipt')
                if stage['kind'] in ('start', 'source') and 'sourceGroup' in result:
                    added = [s for s in progress['completedStates'] if s not in checkpoint['progress']['completedStates']]
                    remaining = [s for s in checkpoint['config']['sourceStates'] if s not in progress['completedStates']]
                    group = result['sourceGroup']
                    if (group.get('completedStates') != added or group.get('remainingStates') != remaining
                            or group.get('plannedPause', bool(remaining)) != bool(remaining)):
                        raise ValueError('Source group receipt disagrees with verified source stages')
                if stage['kind'] == 'tiles' and 'group' in result:
                    added = [b for b in progress['completedBatches'] if b not in checkpoint['progress']['completedBatches']]
                    remaining = [b for b in progress['batchIds'] if b not in progress['completedBatches']]
                    group = result['group']
                    if (group.get('completedBatches') != added or group.get('remainingBatches') != remaining
                            or group.get('plannedPause', bool(remaining)) != bool(remaining)):
                        raise ValueError('Tile group receipt disagrees with verified batches')
                checkpoint.update(sourcePlan=plan, progress=progress, nextStage=following,
                                  updatedAt=now.isoformat(), retryAfter=None, lastError=None, failureCount=0, needsAttention=False,
                                  stageCount=checkpoint['stageCount']+1)
                if stage['kind'] == 'finalize':
                    entry = result.get('region')
                    if not isinstance(entry, dict) or entry.get('id') != checkpoint['config']['id']:
                        raise ValueError('Final region receipt names a different city')
                    previous = next((item for item in manifest['regions'] if item['id'] == entry['id']), None)
                    if previous and previous.get('sourceTimestamp') and (not entry.get('sourceTimestamp') or utc(entry['sourceTimestamp']) < utc(previous['sourceTimestamp'])):
                        raise ValueError('Final region would regress active source freshness')
                    if following['kind'] != 'finalize':
                        raise ValueError('Finalization is missing source/index/tile stages')
                    changed = merge_region(candidate, manifest, entry, generated)
                    if changed:
                        manifest['updatedAt'] = now.isoformat()
                        write_json(candidate/'manifest.json', manifest)
                    state['refreshedAt'][entry['id']] = now.isoformat()
                    state['deferred'].pop(entry['id'], None)
                    write_json(candidate/'rollout-state.json', state)
                    last_completed = {'id': entry['id'], 'name': entry['name'], 'version': entry['version'], 'completedAt': now.isoformat()}
                    shutil.rmtree(candidate/ACTIVE)
                    checkpoint = None
            except Exception as error:
                checkpoint = previous_checkpoint
                # Discard every uncommitted stage byte, keeping the last published stage.
                shutil.rmtree(candidate)
                shutil.copytree(site, candidate)
                (candidate/ACTIVE).mkdir(parents=True, exist_ok=True)
                write_json(candidate/'verification.json', receipt)
                # Retain only completed pin discovery from a failed first source.
                partial_plan = getattr(error, 'partial_result', {}).get('sourcePlan')
                if checkpoint['sourcePlan'] is None and isinstance(partial_plan, dict):
                    progress = inspector(checkpoint['config'], partial_plan, candidate/ACTIVE)
                    if progress != empty_progress(checkpoint['config']):
                        raise ValueError('Initial pin recovery unexpectedly contains completed work')
                    checkpoint.update(sourcePlan=partial_plan, progress=progress,
                                      nextStage=next_stage(checkpoint['config'], progress, partial_plan))
                failures = checkpoint.get('failureCount', 0)+1
                detail = getattr(error, 'partial_result', {}).get('error', {})
                permanent = not isinstance(error, StageFailure) or detail.get('permanent') is True
                attention = permanent or failures >= 3
                checkpoint.update(updatedAt=now.isoformat(),
                    retryAfter=None if attention else (now+dt.timedelta(hours=20)).isoformat(),
                    failureCount=failures, needsAttention=attention,
                    lastError={'at': now.isoformat(), 'message': detail.get('message', str(error))[:500]})
                print(json.dumps({'phase': 'checkpoint-retained', 'reason': str(error)[:500]}), flush=True)
        if migration_pending:
            write_json(candidate/'rollout-state.json', state)
        if checkpoint is not None:
            save_checkpoint(candidate, checkpoint)
            load_checkpoint(candidate, configs, expected_fingerprint=code_hash, inspector=inspector)
            if (candidate/'manifest.json').read_bytes() != manifest_before:
                raise ValueError('Incomplete update changed the active manifest')
        status = status_for(checkpoint, now, last_completed=last_completed, mode=mode)
        write_status(candidate, status)
        report = seal_snapshot(candidate)
        validate_site(candidate)
        # Every prior published region and version remains immutable.
        before_regions = inventory(site/'regions')
        after_regions = inventory(candidate/'regions')
        if any(after_regions.get(path) != digest for path,digest in before_regions.items()):
            raise ValueError('Checkpoint publication changed historical region bytes')
        old = temp/'previous'
        site.rename(old)
        candidate.rename(site)
    return {'changed': True, 'stageAttempted': stage, 'siteBytes': report['siteBytes'], **status}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', default='config/rollout-regions.json')
    p.add_argument('--output', default='site')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--allow-new', action='store_true')
    mode.add_argument('--continue-only', action='store_true')
    p.add_argument('--start-region')
    p.add_argument('--report', default='rollout-report.json')
    a = p.parse_args()
    signal.signal(signal.SIGTERM, interrupt_rollout)
    if os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1':
        raise RuntimeError('Use a fresh workflow run to recheck budgets')
    deadline = time.monotonic()+PROCESS_SECONDS
    site = Path(a.output).resolve()
    configs = load_configs(a.config)
    restored = {}
    restore_published(site, receipt=restored)
    from artifact_budget import check_artifact_budget
    storage = check_artifact_budget(os.environ.get('GITHUB_REPOSITORY', 'apfullai/ride-tiles'), site)
    if not storage['allowed']:
        report = {'changed': False, 'reason': 'Baseline artifact storage cannot safely admit another checkpoint', 'storage': storage}
    else:
        report = advance(site, configs, allow_new=a.allow_new, start_region=a.start_region, deadline=deadline)
    if report['changed']:
        assert_publication_unchanged(restored['publicationRevision'])
    report['restoredPublicationRevision'] = restored['publicationRevision']
    write_json(Path(a.report), report)
    print(json.dumps(report, indent=2))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('changed='+str(report['changed']).lower()+'\n')
            output.write('restored_publication_revision='+restored['publicationRevision']+'\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('## Resumable tile update\n\n```json\n'+json.dumps(report, indent=2)+'\n```\n')


if __name__ == '__main__':
    main()
