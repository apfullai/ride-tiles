"""One disposable coarse stage. Parent owns snapshot validation and publication."""
import argparse
import datetime as dt
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))


def write_result(path, value):
    data = json.dumps(value, sort_keys=True, allow_nan=False)+'\n'
    if len(data.encode('utf-8')) > 100_000:
        raise RuntimeError('Stage result exceeds IPC size bound')
    temporary = path.with_suffix('.partial')
    temporary.write_text(data)
    temporary.replace(path)


def receipt_summary(receipt):
    # Full inventories and tile-coordinate plans remain in the hashed checkpoint.
    # IPC is deliberately bounded independently of region/batch size.
    keys=('schemaVersion','planId','stage','state','batchId','batchCount','candidateTileCount','nonemptyTiles')
    return {key:receipt[key] for key in keys if key in receipt}


def run(request, checkpoint, work, result_path):
    started = time.monotonic()
    import resumable_region as stages
    config, plan = request['config'], request.get('sourcePlan')
    stage = request['stage']
    kind = stage['kind']
    checkpoint.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    result = {'schemaVersion': 1, 'stage': stage}
    if kind in ('start', 'plan'):
        plan = stages.plan_sources(config, now=dt.datetime.now(dt.timezone.utc))
        result['sourcePlan'] = plan
        minimum = request.get('minimumSourceTimestamp')
        if minimum:
            timestamp = plan.get('osmReplicationTimestamp')
            parse = lambda value: dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if not timestamp or parse(timestamp) < parse(minimum):
                raise RuntimeError('Pinned source would regress active source freshness')
        # Pin discovery is complete before the expensive first source attempt.
        write_result(result_path, result)
        if kind == 'plan':
            return result
        stage = {'kind': 'source', 'state': config['sourceStates'][0]}
        kind = 'source'
        result['stage'] = stage
    if kind == 'source':
        budget = request.get('stageSeconds', 300)
        if (isinstance(budget, bool) or not isinstance(budget, (int, float))
                or not math.isfinite(budget) or not 0 < budget <= 1200):
            raise ValueError('Invalid source-stage time budget')
        # The parent already subtracts consumed setup/recovery time and its
        # publication reserves from the shared build deadline. Never replace
        # that remaining allowance with this runner's larger admission ceiling.
        seconds = budget - 15 - (time.monotonic()-started)
        def save_source_progress(group, progress):
            result.update(sourcePlan=plan, sourceGroup=group, progress=progress,
                          plannedPause=group['plannedPause'])
            write_result(result_path, result)
        if seconds <= 0:
            progress = stages.validate_checkpoint(config, plan, checkpoint)
            remaining = [state for state in config['sourceStates'] if state not in progress['completedStates']]
            result['sourceGroup'] = {'schemaVersion': 1, 'planId': plan['planId'], 'completedStates': [],
                                     'remainingStates': remaining, 'plannedPause': bool(remaining),
                                     'status': 'paused' if remaining else 'complete'}
        else:
            try:
                result['sourceGroup'] = stages.build_source_group(config, plan, checkpoint, work,
                    seconds=seconds, reserve_seconds=min(30, seconds/3), on_progress=save_source_progress,
                    concurrent_downloads=request.get('sourceConcurrency', 2))
            except stages.SourceGroupFailure as error:
                result['error'] = error.detail
                save_source_progress(error.group, error.progress)
                raise
        result['plannedPause'] = result['sourceGroup']['plannedPause']
    elif kind == 'index':
        result['receipt'] = receipt_summary(stages.build_index_stage(config, plan, checkpoint, work, batch_size=request.get('batchSize', 128)))
    elif kind == 'tiles':
        seconds = max(1, min(240, request.get('stageSeconds', 300)-15))
        result['group'] = stages.build_tile_group(checkpoint, work, seconds=seconds, reserve_seconds=min(30, seconds/3))
        result['plannedPause'] = not result['group']['completedBatches']
    elif kind == 'finalize':
        target = work/'region-site'
        target.mkdir()
        result['region'] = stages.assemble_region(config, plan, checkpoint, work, target, request['baseUrl'])
    else:
        raise ValueError('Unsupported checkpoint stage')
    result['sourcePlan'] = plan
    result['progress'] = stages.validate_checkpoint(config, plan, checkpoint)
    write_result(result_path, result)
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('request', type=Path)
    p.add_argument('checkpoint', type=Path)
    p.add_argument('work', type=Path)
    p.add_argument('result', type=Path)
    a = p.parse_args()
    try:
        run(json.loads(a.request.read_text()), a.checkpoint, a.work, a.result)
    except Exception as error:
        result = json.loads(a.result.read_text()) if a.result.exists() else {'schemaVersion':1}
        result['error'] = getattr(error, 'detail', {'type': type(error).__name__, 'message': str(error)[:500],
                                                  'permanent': isinstance(error, (ValueError, RuntimeError))})
        write_result(a.result, result)
        raise


if __name__ == '__main__':
    main()
