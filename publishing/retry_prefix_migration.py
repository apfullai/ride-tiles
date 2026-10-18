"""Migrate one inventoried failed source checkpoint across the retry/prefix fix.

The descriptor pins the generation, source plan, acquisition deadline, failures,
retry time and previous audit chain. This is not a general fingerprint bypass.
All original selected source blobs outside the reviewed change list stay pinned.
"""
import copy
import hashlib
import json
from pathlib import Path

FROM_CODE = '126d3437c002a1a9cdd9c14f8d9ccbf8851a489d9caa187d509c00353d8e24ba'
FROM_DESCRIPTOR = '79336c510d58f660178ff7e33dd0c638aec6be41f6f6c070d5edf1f95d75efe5'
FROM_COMMIT = 'ae037d0d4e37e82314a679006ebe4d86bc0497f5'
FROM_GENERATION = '2726f5fdcf1b830f7f5c29eff8b058d83a78278770c7cfc3acb5c25d6f2e8c6e'
FROM_RETRY_AFTER = '2026-10-19T10:04:14.579685+00:00'
ID = 'chicago-indiana-retry-prefix-v1-20261018'
PINS_PATH = 'publishing/retry_prefix_source_pins.json'
PINS_SHA256 = 'f9348702f007e9727f754132def3349a1ad8d9d06ab89ba45f461bffea868966'

# Names are deliberately explicit. No glob, directory or dependency exception is
# allowed, and any added or removed selected source file outside this set fails.
CHANGED = {
    '.github/workflows/rollout.yml',
    'publishing/actions_budget.py',
    'publishing/checkpoint_migrations.py',
    'publishing/resumable_rollout.py',
    'publishing/resumable_stage_runner.py',
    'publishing/tests/test_exact_pack.py',
    'publishing/tests/test_actions_budget.py',
    'publishing/tests/test_migration_budget.py',
    'publishing/tests/test_resumable_integration.py',
    'publishing/tests/test_resumable_migrations.py',
    'publishing/tests/test_resumable_rollout.py',
    'publishing/tests/test_resumable_workflow.py',
    'tests/test_download_budget.py',
    'tests/test_resumable_stages.py',
    'tools/build_region.py',
    'tools/resumable_region.py',
}
ADDED = {
    'publishing/ATTEMPT_RECEIPT_INTEGRATION.md',
    'publishing/attempt_receipts.py',
    'publishing/retry_prefix_migration.py',
    PINS_PATH,
    'publishing/tests/test_attempt_receipts.py',
    'publishing/tests/test_attempt_receipt_main.py',
    'publishing/tests/test_failed_source_publication.py',
    'publishing/tests/test_resumable_retry_migration.py',
    'publishing/tests/fixtures/source_package_migration/build_region.py.txt',
    'publishing/tests/fixtures/source_package_migration/resumable_region.py.txt',
    'tests/test_source_group_failures.py',
    'tests/test_source_download_batch.py',
}


def _code_profile(code_root):
    from verified_code import fingerprint, source_records
    path = code_root / PINS_PATH
    if path.is_symlink() or not path.is_file():
        return False
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINS_SHA256:
        return False
    pins = json.loads(raw)
    if (fingerprint(pins) != FROM_CODE or not CHANGED <= set(pins)
            or set(pins) & ADDED):
        return False
    actual = source_records(code_root)
    return (set(actual) == set(pins) | ADDED
            and all(actual.get(name) == blob
                    for name, blob in pins.items() if name not in CHANGED))


def _crop_matches(source):
    """Rehash each preserved part and the reconstructed raw crop locally."""
    receipt = json.loads((source / 'receipt.json').read_bytes())
    crop = receipt['crop']
    parts = crop['parts']
    if (crop['encoding'] != 'raw' or not isinstance(parts, list)
            or not 1 <= len(parts) <= 32 or type(crop['bytes']) is not int
            or not 1 <= crop['bytes'] <= 250_000_000):
        return False
    digest = hashlib.sha256()
    size = 0
    for index, part in enumerate(parts):
        if (part['path'] != f'crop.part-{index:04d}'
                or type(part['bytes']) is not int
                or not 1 <= part['bytes'] <= 8_000_000):
            return False
        path = source / part['path']
        if path.is_symlink() or not path.is_file():
            return False
        part_hash = hashlib.sha256()
        part_size = 0
        with path.open('rb') as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b''):
                part_size += len(data)
                size += len(data)
                if part_size > part['bytes'] or size > crop['bytes']:
                    return False
                part_hash.update(data)
                digest.update(data)
        if part_size != part['bytes'] or part_hash.hexdigest() != part['sha256']:
            return False
    return size == crop['bytes'] and digest.hexdigest() == crop['sha256']


def eligible(root, checkpoint, code_root):
    """Require the exact old descriptor, complete source tree and crop bytes."""
    root, code_root = Path(root), Path(code_root)
    try:
        raw = (root / 'checkpoint.json').read_bytes()
        if (hashlib.sha256(raw).hexdigest() != FROM_DESCRIPTOR
                or json.loads(raw) != checkpoint
                or checkpoint['codeFingerprint'] != FROM_CODE
                or checkpoint['generationId'] != FROM_GENERATION
                or checkpoint['config']['id'] != 'chicagoland'
                or checkpoint['purpose'] != 'refresh'
                or type(checkpoint['failureCount']) is not int
                or checkpoint['failureCount'] != 1
                or checkpoint['needsAttention'] is not False
                or checkpoint['retryAfter'] != FROM_RETRY_AFTER
                or checkpoint['progress'] != {
                    'completedStates': ['illinois', 'wisconsin'],
                    'indexReady': False, 'batchIds': [], 'batchCount': 0,
                    'completedBatches': []}
                or checkpoint['nextStage'] != {'kind': 'source', 'state': 'indiana'}
                or not _code_profile(code_root)):
            return False
        from rollout_snapshot import _records
        files = {name: record for name, record in _records(root, include_metadata=True).items()
                 if name != 'checkpoint.json'}
        if files != checkpoint['files']:
            return False
        return all(_crop_matches(root / 'sources' / state)
                   for state in ('illinois', 'wisconsin'))
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        return False


def migrate(root, checkpoint, code_root, *, target_fingerprint, verified_fingerprint, now):
    """Return an audited copy; do not reset failures, deadlines, or source bytes."""
    from verified_code import current_fingerprint
    if (target_fingerprint == FROM_CODE
            or target_fingerprint != verified_fingerprint
            or target_fingerprint != current_fingerprint(code_root)
            or not eligible(root, checkpoint, code_root)):
        raise ValueError('Exact retry/prefix checkpoint migration not verified')
    result = copy.deepcopy(checkpoint)
    result['codeFingerprint'] = target_fingerprint
    result['migration'] = {
        'id': ID, 'fromCommit': FROM_COMMIT, 'fromCodeFingerprint': FROM_CODE,
        'toCodeFingerprint': target_fingerprint, 'fromDescriptorSha256': FROM_DESCRIPTOR,
        'at': now.isoformat(), 'preservedSourcePlanId': checkpoint['sourcePlan']['planId'],
        'preservedFilesSha256': hashlib.sha256(json.dumps(checkpoint['files'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest(),
        'previousMigration': copy.deepcopy(checkpoint.get('migration')),
    }
    return result
