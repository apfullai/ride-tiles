"""One disposable coarse stage. Parent owns snapshot validation and publication."""
import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))


def write_result(path, value):
    temporary = path.with_suffix('.partial')
    temporary.write_text(json.dumps(value, sort_keys=True)+'\n')
    temporary.replace(path)


def receipt_summary(receipt):
    # Full inventories and tile-coordinate plans remain in the hashed checkpoint.
    # IPC is deliberately bounded independently of region/batch size.
    keys=('schemaVersion','planId','stage','state','batchId','batchCount','candidateTileCount','nonemptyTiles')
    return {key:receipt[key] for key in keys if key in receipt}


def run(request, checkpoint, work, result_path):
    import resumable_region as stages
    config, plan = request['config'], request.get('sourcePlan')
    stage = request['stage']
    kind = stage['kind']
    checkpoint.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    result = {'schemaVersion': 1, 'stage': stage}
    started = time.monotonic()
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
        seconds = max(1, min(240, request.get('stageSeconds', 300)-15-(time.monotonic()-started)))
        result['sourceGroup'] = stages.build_source_group(config, plan, checkpoint, work,
            seconds=seconds, reserve_seconds=min(30, seconds/3))
        result['plannedPause'] = not result['sourceGroup']['completedStates']
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
        result['error'] = {'type': type(error).__name__, 'message': str(error)[:500],
                           'permanent': isinstance(error, (ValueError, RuntimeError))}
        write_result(a.result, result)
        raise


if __name__ == '__main__':
    main()
