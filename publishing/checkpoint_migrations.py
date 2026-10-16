"""Explicit, one-checkpoint migrations for source chunking and public origin.

This is not a general code-fingerprint bypass. Only the independently inventoried
Illinois checkpoint published by run 38619620372 can cross this boundary. Source
acquisition/validation semantics and their dependencies must remain identical.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path

MIGRATION_ID = 'chicago-source-package-v1-20261014'
FROM_COMMIT = '57493447e97a2957c11be6d1cf4d2241841d59c7'
FROM_CODE = '5dfbb66c4899b20816859aeaed9b59f8b6054689331bcc224a9d7962ffa962d9'
FROM_DESCRIPTOR = '48e41324d47dd639b268fa1cb36af97681a219e79a20b9f4ad3a8c7b434e82e3'
SOURCE_SEMANTICS = '48e6b44f1e6f9167801479152264af7c2b62c64ae3d1a138e03574f15aa79005'
PROTECTED_FILES = {'tools/build_region.py': '57e50dadaeb1316e5f2eeb91ae25bb05e7c32501dec7e9c75bf62d50fb662a5b', 'tools/tile_generator.py': '6c50253a8774a8107084be286eb23445a00eede267690f3a3eec8d78b2b1f074', 'config/rollout-regions.json': 'a6aa79a1c69a80e502c9369bf655bfdac8db91fe3504e875575b1cb79e491501', 'requirements.txt': 'a9da53f34af90c2cc5b9ee96f0ef865935582d869aba11fb0eeb105cb01d6e63', 'requirements-dev.txt': '9120e85e6eebf006c20cabff1a1781cf5a3b54aca8b59e0c3398599854fd82fb'}


# This second profile changes only the exact reviewed public URL literals in
# resumable_region.py and build_region.py. Never normalize arbitrary origins,
# source code, checkpoint descriptors, or the preserved source plan.
ORIGIN_MIGRATION_ID = 'chicago-source-package-ride-tiles-origin-v1-20261015'
FROM_PUBLIC_BASE = 'https://apfullai.github.io/rides-tiles'
TO_PUBLIC_BASE = 'https://apfullai.github.io/ride-tiles'
ORIGIN_SOURCE_SEMANTICS = '1e444d29ba9592dec2cebbf3bfdd5870621d5db2cec9637add457ccfe5550308'
ORIGIN_BUILD_REGION_SHA256 = '88b0605ee162910e49271f04ca7172deaa0b980990fa1aac7d3821bac82afe56'


def source_semantics(root):
    tree = ast.parse((Path(root)/'tools/resumable_region.py').read_text())
    tree.body = [node for node in tree.body
                 if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and node.name in ('build_index_stage', 'assemble_region', 'validate_checkpoint',
                                          'build_source_group', 'compact_source_crops'))
                 and not (isinstance(node, ast.ImportFrom) and node.module == 'source_package')]
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def migration_profile(code_root):
    """Match one exact reviewed semantics/dependency profile, without rewriting."""
    code_root = Path(code_root)
    semantics = source_semantics(code_root)
    if semantics == SOURCE_SEMANTICS:
        profile = MIGRATION_ID
        protected = PROTECTED_FILES
    elif semantics == ORIGIN_SOURCE_SEMANTICS:
        profile = ORIGIN_MIGRATION_ID
        protected = {**PROTECTED_FILES, 'tools/build_region.py': ORIGIN_BUILD_REGION_SHA256}
    else:
        return None
    for name, expected in protected.items():
        path = code_root/name
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            return None
    return profile


def eligible(checkpoint_root, checkpoint, code_root):
    """Check exact old identity plus unchanged pre-index execution semantics."""
    checkpoint_root, code_root = Path(checkpoint_root), Path(code_root)
    raw = (checkpoint_root/'checkpoint.json').read_bytes()
    if (json.loads(raw) != checkpoint or checkpoint.get('codeFingerprint') != FROM_CODE
            or hashlib.sha256(raw).hexdigest() != FROM_DESCRIPTOR):
        return False
    if (checkpoint.get('config', {}).get('id') != 'chicagoland'
            or checkpoint.get('purpose') != 'refresh'
            or checkpoint.get('progress') != {'completedStates': ['illinois'], 'indexReady': False,
                'batchIds': [], 'batchCount': 0, 'completedBatches': []}
            or checkpoint.get('nextStage') != {'kind': 'source', 'state': 'wisconsin'}
            or checkpoint.get('migration') is not None
            or checkpoint.get('failureCount') != 0 or checkpoint.get('needsAttention') is not False):
        return False
    from rollout_snapshot import _records
    try:
        actual = {name: record for name, record in _records(checkpoint_root, include_metadata=True).items()
                  if name != 'checkpoint.json'}
    except (ValueError, OSError):
        return False
    if checkpoint.get('files') != actual:
        return False
    if migration_profile(code_root) is None:
        return False
    # Reconstruct the raw crop's identity in a streaming hash, independently of
    # the outer inventory and inner per-part hashes. No source is redownloaded.
    source_root = checkpoint_root/'sources/illinois'
    receipt = json.loads((source_root/'receipt.json').read_text())
    crop = receipt['crop']
    if crop.get('encoding') != 'raw':
        return False
    digest = hashlib.sha256(); size = 0
    parts = crop.get('parts', [])
    if not parts or len(parts) > 32:
        return False
    for index, part in enumerate(parts):
        if part.get('path') != f'crop.part-{index:04d}':
            return False
        path = source_root/part['path']
        if not path.is_file() or path.is_symlink():
            return False
        part_hash = hashlib.sha256(); part_size = 0
        with path.open('rb') as stream:
            for data in iter(lambda: stream.read(1024*1024), b''):
                part_size += len(data); size += len(data)
                if part_size > 8_000_000 or size > 250_000_000:
                    return False
                digest.update(data); part_hash.update(data)
        if part_size != part['bytes'] or part_hash.hexdigest() != part['sha256']:
            return False
    return size == crop['bytes'] and digest.hexdigest() == crop['sha256']


def migrate(checkpoint_root, checkpoint, code_root, *, target_fingerprint, verified_fingerprint, now):
    """Return an audited descriptor after tests; never mutate preserved files."""
    if (target_fingerprint != verified_fingerprint or target_fingerprint == FROM_CODE
            or not eligible(checkpoint_root, checkpoint, code_root)):
        raise ValueError('Checkpoint is not authorized for the source-package migration')
    result = copy.deepcopy(checkpoint)
    result['codeFingerprint'] = target_fingerprint
    result['migration'] = {'id': migration_profile(code_root), 'fromCommit': FROM_COMMIT,
        'fromCodeFingerprint': FROM_CODE, 'toCodeFingerprint': target_fingerprint,
        'fromDescriptorSha256': FROM_DESCRIPTOR, 'at': now.isoformat(),
        'preservedSourcePlanId': checkpoint['sourcePlan']['planId'],
        'preservedFilesSha256': hashlib.sha256(json.dumps(checkpoint['files'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()}
    if result['migration']['id'] == ORIGIN_MIGRATION_ID:
        result['migration'].update(fromPublicBase=FROM_PUBLIC_BASE, toPublicBase=TO_PUBLIC_BASE)
    return result
