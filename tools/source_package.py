"""Public, byte-exact OSM PBF packages with bounded content-hashed chunks.

This module intentionally uses only the standard library so snapshot recovery
can validate a source package without installing geometry or OSM tooling.
"""
import hashlib
import json
from pathlib import Path
import re
import stat

FORMAT = 'osm-pbf-chunks-v1'
CHUNK_BYTES = 8_000_000
MAX_SOURCE_BYTES = 250_000_000
DESCRIPTOR_NAME = 'cycling-source.json'
CHUNK_DIRECTORY = 'cycling-source'
SHA256 = re.compile(r'[a-f0-9]{64}')
REASSEMBLY = ('Download each relative chunk path from this directory. Concatenate the chunks '
              'in listed order as cycling-source.osm.pbf, then verify the declared byte count and SHA-256.')


def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Source package requires regular files')


def _hash_file(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate source descriptor key')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite source descriptor')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid source package JSON') from exc


def write_source_package(source, root):
    """Write ordered chunks and a descriptor without changing source bytes."""
    source, root = Path(source), Path(root)
    _regular(source)
    size = source.stat().st_size
    if not 0 < size <= MAX_SOURCE_BYTES:
        raise ValueError('Source package size exceeds site envelope')
    if root.is_symlink():
        raise ValueError('Source package root cannot be a symlink')
    root.mkdir(parents=True, exist_ok=True)
    target = root / CHUNK_DIRECTORY
    descriptor_path = root / DESCRIPTOR_NAME
    if target.exists() or descriptor_path.exists():
        raise ValueError('Refusing to overwrite an existing source package')
    target.mkdir()
    whole = hashlib.sha256()
    chunks = []
    with source.open('rb') as stream:
        while block := stream.read(CHUNK_BYTES):
            whole.update(block)
            checksum = hashlib.sha256(block).hexdigest()
            name = f'{CHUNK_DIRECTORY}/{len(chunks):05d}-{checksum}.pbf.part'
            (root / name).write_bytes(block)
            chunks.append({'path': name, 'bytes': len(block), 'sha256': checksum})
    descriptor = {'schemaVersion': 1, 'format': FORMAT, 'filename': 'cycling-source.osm.pbf',
                  'bytes': size, 'sha256': whole.hexdigest(), 'chunks': chunks, 'reassembly': REASSEMBLY}
    descriptor_path.write_text(json.dumps(descriptor, sort_keys=True, indent=2) + '\n')
    descriptor_sha = _hash_file(descriptor_path)
    validate_source_package(root, expected_sha256=descriptor_sha)
    return {'descriptor': descriptor, 'descriptorSha256': descriptor_sha}


def validate_source_package(root, descriptor_name=DESCRIPTOR_NAME, expected_sha256=None):
    """Verify descriptor, exact inventory and reconstructed hash by streaming.

    expected_sha256, when provided, is the descriptor's SHA-256. The returned
    descriptor's sha256 is the reconstructed PBF SHA-256. Validation never
    writes a concatenated file or reads a whole source into memory.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or descriptor_name != DESCRIPTOR_NAME:
        raise ValueError('Invalid source package root or descriptor path')
    descriptor_path = root / descriptor_name
    _regular(descriptor_path)
    if not 0 < descriptor_path.stat().st_size <= 100_000:
        raise ValueError('Source descriptor size exceeds bound')
    raw = descriptor_path.read_bytes()
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
            or not SHA256.fullmatch(expected_sha256) or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError('Source descriptor SHA256 mismatch')
    descriptor = _json(raw)
    if (not isinstance(descriptor, dict)
            or set(descriptor) != {'schemaVersion', 'format', 'filename', 'bytes', 'sha256', 'chunks', 'reassembly'}
            or type(descriptor['schemaVersion']) is not int or descriptor['schemaVersion'] != 1
            or descriptor['format'] != FORMAT or descriptor['filename'] != 'cycling-source.osm.pbf'
            or descriptor['reassembly'] != REASSEMBLY
            or type(descriptor['bytes']) is not int or not 0 < descriptor['bytes'] <= MAX_SOURCE_BYTES
            or not isinstance(descriptor['sha256'], str) or not SHA256.fullmatch(descriptor['sha256'])):
        raise ValueError('Invalid source package descriptor')
    if descriptor['bytes'] + len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('Source package total exceeds site envelope')
    chunks = descriptor['chunks']
    expected_count = (descriptor['bytes'] + CHUNK_BYTES - 1) // CHUNK_BYTES
    if not isinstance(chunks, list) or len(chunks) != expected_count:
        raise ValueError('Missing or duplicate source chunks')
    directory = root / CHUNK_DIRECTORY
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Invalid source chunk directory')
    expected_paths = set()
    total = 0
    whole = hashlib.sha256()
    for index, chunk in enumerate(chunks):
        if (not isinstance(chunk, dict) or set(chunk) != {'path', 'bytes', 'sha256'}
                or not isinstance(chunk['sha256'], str) or not SHA256.fullmatch(chunk['sha256'])):
            raise ValueError('Invalid source chunk metadata')
        expected_name = f'{CHUNK_DIRECTORY}/{index:05d}-{chunk["sha256"]}.pbf.part'
        expected_size = min(CHUNK_BYTES, descriptor['bytes'] - index * CHUNK_BYTES)
        if (chunk['path'] != expected_name or type(chunk['bytes']) is not int
                or chunk['bytes'] != expected_size or expected_name in expected_paths):
            raise ValueError('Invalid source chunk path, order or size')
        expected_paths.add(expected_name)
        path = root / expected_name
        _regular(path)
        if path.stat().st_size != expected_size:
            raise ValueError('Source chunk size mismatch')
        chunk_hash = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                chunk_hash.update(block)
                whole.update(block)
                total += len(block)
        if chunk_hash.hexdigest() != chunk['sha256']:
            raise ValueError('Source chunk SHA256 mismatch')
    actual_paths = set()
    for path in directory.iterdir():
        _regular(path)
        actual_paths.add(path.relative_to(root).as_posix())
    if actual_paths != expected_paths:
        raise ValueError('Unexpected or missing source chunk files')
    if total != descriptor['bytes'] or whole.hexdigest() != descriptor['sha256']:
        raise ValueError('Reconstructed source hash or size differs')
    return descriptor
