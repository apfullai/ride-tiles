"""Durable, hash-pinned Pages snapshots using only the Python standard library.

A snapshot is one deterministic gzip/tar stream, split into bounded transport
shards under ``snapshot/``. The descriptor inventories every data file, including
old versions, public source archives and rollout-state.json. Snapshot metadata is
never recursively archived. Remote restores are fail-closed and become visible
only after their entire inventory, live manifest and region content validate.
"""
from functools import lru_cache
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import append_metro
import restore_snapshot
from public_origin import OLD_BASE, relocate_manifest, relocate_region

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from source_package import validate_source_package

BASE = append_metro.BASE
MAX_SITE_BYTES = 250_000_000
MAX_FILE_BYTES = 24_000_000
MAX_MANIFEST_BYTES = 100_000
MAX_REGIONS = 50
MAX_SHARD_BYTES = 8_000_000
MAX_FILES = 150_000
DESCRIPTOR = 'snapshot.json'
SNAPSHOT_DIR = 'snapshot'
FROZEN_PREFIXES = ('regions/chicagoland/19785f3c1ca31233/',
                   'regions/chicago-metro/8d555e92cfa68b53/')
SHA256 = re.compile(r'[0-9a-f]{64}')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid JSON') from exc


def _path(name):
    if (not isinstance(name, str) or not name or len(name.encode('utf-8')) > 1024
            or '\\' in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(p in ('', '.', '..') for p in name.split('/'))
            or PurePosixPath(name).is_absolute()):
        raise ValueError('Unsafe snapshot path')
    return name


def _metadata(name):
    return name == DESCRIPTOR or name == SNAPSHOT_DIR or name.startswith(SNAPSHOT_DIR + '/')


def _integer(value, maximum, label, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid ' + label)
    return value


def _hash(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError('Invalid SHA-256')
    return value


def _records(site, include_metadata=False):
    site = Path(site)
    if site.is_symlink() or not site.is_dir():
        raise ValueError('Site must be a regular directory')
    result = {}
    total = 0
    for directory, dirs, files in os.walk(site, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            kind = path.lstat().st_mode
            if stat.S_ISLNK(kind) or not (stat.S_ISDIR(kind) or stat.S_ISREG(kind)):
                raise ValueError('Snapshot must contain only regular files and directories')
            relative = _path(path.relative_to(site).as_posix())
            if stat.S_ISDIR(kind):
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError('File exceeds Pages size cap: ' + relative)
            if not include_metadata and _metadata(relative):
                continue
            total += size
            if total > MAX_SITE_BYTES:
                raise ValueError('Site exceeds size cap')
            h = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            result[relative] = {'sha256': h.hexdigest(), 'size': size}
            if len(result) > MAX_FILES:
                raise ValueError('Too many snapshot files')
    return dict(sorted(result.items()))


def inventory(site):
    """Hash every data file; transport metadata is deliberately excluded."""
    return {name: item['sha256'] for name, item in _records(site).items()}


def _bootstrap(site):
    restore_snapshot.restore(site)
    append_metro.main(site)
    path = Path(site) / "manifest.json"
    manifest = _json(path.read_bytes())
    # Historical bootstrap stays byte-pinned; only its public manifest moves.
    manifest["regions"] = [relocate_region(r, OLD_BASE, BASE) if r["id"] != "chicago-metro" else r for r in manifest["regions"]]
    path.write_text(json.dumps(manifest, indent=2) + "\n")


@lru_cache(maxsize=1)
def _frozen_reference():
    with tempfile.TemporaryDirectory() as directory:
        site = Path(directory) / 'baseline'
        _bootstrap(site)
        files = {name: value for name, value in inventory(site).items()
                 if name.startswith(FROZEN_PREFIXES)}
        manifest = _json((site / 'manifest.json').read_bytes())
        metro = next(r for r in manifest['regions'] if r['id'] == 'chicago-metro')
        return files, metro


def _validate_frozen(site, manifest, base_url=BASE):
    expected, metro = _frozen_reference()
    if base_url != BASE:
        metro = relocate_region(metro, BASE, base_url)
    actual = {name: value for name, value in inventory(site).items()
              if name.startswith(FROZEN_PREFIXES)}
    if actual != expected:
        raise ValueError('Frozen Chicago version files changed or disappeared')
    if next((r for r in manifest['regions'] if r['id'] == 'chicago-metro'), None) != metro:
        raise ValueError('Frozen metro manifest entry changed or disappeared')
    if not any(r['id'] == 'chicagoland' for r in manifest['regions']):
        raise ValueError('Broad Chicago region disappeared')


def _validate_data(site, base_url=BASE):
    files = _records(site)
    raw = (site / 'manifest.json').read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('Manifest exceeds size cap')
    manifest = _json(raw)
    if (not isinstance(manifest, dict) or type(manifest.get('schemaVersion')) is not int or manifest.get('schemaVersion') != 1
            or not isinstance(manifest.get('regions'), list)
            or not 1 <= len(manifest['regions']) <= MAX_REGIONS):
        raise ValueError('Invalid manifest or region count')
    ids = set()
    for region in manifest['regions']:
        if not isinstance(region, dict):
            raise ValueError('Invalid region')
        rid, version = region.get('id', ''), region.get('version', '')
        if (not isinstance(rid, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', rid)
                or not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{16}', version)
                or rid in ids):
            raise ValueError('Invalid or duplicate region identity')
        ids.add(rid)
        bounds = region.get('bounds', [])
        if (not isinstance(bounds, list) or len(bounds) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds)
                or not -180 <= bounds[0] < bounds[2] <= 180
                or not -90 <= bounds[1] < bounds[3] <= 90):
            raise ValueError('Invalid region bounds')
        relative = f'regions/{rid}/{version}/'
        prefix = base_url.rstrip('/') + '/' + relative
        if (region.get('tiles') != [prefix + 'tiles/{z}/{x}/{y}.pbf']
                or region.get('sha256Manifest') != prefix + 'tile-sha256.json'
                or region.get('sourceUrl') != prefix + 'provenance.json'):
            raise ValueError('Region URLs must address the local version')
        source_url = region.get('sourceDownload', '')
        if not isinstance(source_url, str) or not source_url.startswith(prefix):
            raise ValueError('Source URL must address the local version')
        source_name = source_url[len(base_url.rstrip('/')) + 1:]
        _path(source_name)
        if (urlsplit(source_url).query or urlsplit(source_url).fragment
                or '%' in source_name or source_name not in files):
            raise ValueError('Invalid local source URL')
        root = site / relative
        expected = _json((root / 'tile-sha256.json').read_bytes())
        if not isinstance(expected, dict):
            raise ValueError('Invalid tile inventory')
        normalized = {}
        for key, value in expected.items():
            key = key.removeprefix('tiles/')
            match = re.fullmatch(r'(0|[1-9]\d*)/(0|[1-9]\d*)/(0|[1-9]\d*)\.pbf', key)
            if not match or key in normalized:
                raise ValueError('Invalid or duplicate tile coordinate')
            z, x, y = map(int, match.groups())
            if not 0 <= z <= 24 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
                raise ValueError('Invalid tile coordinate')
            normalized[key] = _hash(value)
        tile_prefix = relative + 'tiles/'
        actual = {name[len(tile_prefix):]: item['sha256']
                  for name, item in files.items() if name.startswith(tile_prefix)}
        content_version = digest(''.join(f'{k} {actual[k]}\n' for k in sorted(actual)).encode())[:16]
        tile_bytes = sum(item['size'] for name, item in files.items() if name.startswith(tile_prefix))
        if (not actual or actual != normalized or content_version != version
                or type(region.get('tileCount')) is not int or region['tileCount'] != len(actual)
                or type(region.get('tileBytes')) is not int or region['tileBytes'] != tile_bytes):
            raise ValueError('Region tile hashes, content version, count or bytes differ')
        provenance = _json((root / 'provenance.json').read_bytes())
        if not isinstance(provenance, dict) or not provenance:
            raise ValueError('Missing provenance')
        if provenance.get('contentVersion', version) != version:
            raise ValueError('Provenance content version differs')
        if provenance.get('bounds', bounds) != bounds:
            raise ValueError('Provenance bounds differ')
        # Exact metro inherits the parent's archiveSha256 in its preserved
        # provenance; its own archive is explicitly pinned in its manifest.
        own_hash = region.get('sourceArchiveSha256')
        provenance_hash = provenance.get('sourceExtractSha256')
        if provenance.get('kind') != 'exact-native-tile-subset':
            provenance_hash = provenance_hash or provenance.get('archiveSha256')
        if region.get('sourceFormat') == 'osm-pbf-chunks-v1':
            if source_url != prefix + 'cycling-source.json' or provenance.get('sourceFormat') != region['sourceFormat']:
                raise ValueError('Invalid chunked source format or descriptor URL')
            descriptor_hash = files[source_name]['sha256']
            if (_hash(own_hash) != descriptor_hash
                    or _hash(provenance.get('sourcePackageSha256')) != descriptor_hash):
                raise ValueError('Source package descriptor hash differs or is absent')
            package = validate_source_package(root, expected_sha256=descriptor_hash)
            if (_hash(provenance_hash) != package['sha256']
                    or type(provenance.get('sourceExtractBytes')) is not int
                    or provenance['sourceExtractBytes'] != package['bytes']):
                raise ValueError('Source package reconstructed identity differs')
        else:
            if region.get('sourceFormat') is not None:
                raise ValueError('Unsupported public source format')
            expected_hashes = [h for h in (own_hash, provenance_hash) if h is not None]
            if not expected_hashes or any(_hash(h) != files[source_name]['sha256'] for h in expected_hashes):
                raise ValueError('Source archive hash differs or is absent')
        metadata = provenance.get('sourceMetadata', {})
        if not isinstance(metadata, dict):
            raise ValueError('Invalid upstream source metadata')
        sources = provenance.get('sources', metadata.get('sources'))
        if not isinstance(sources, list) or not sources:
            raise ValueError('Missing upstream source provenance')
        for source in sources:
            if (not isinstance(source, dict) or not isinstance(source.get('url'), str)
                    or urlsplit(source['url']).scheme != 'https' or not urlsplit(source['url']).netloc):
                raise ValueError('Invalid upstream source provenance')
            _hash(source.get('sha256'))
    _validate_frozen(site, manifest, base_url)
    return manifest


def validate_site(site):
    """Validate current regions, preserved Chicago bytes and the entire envelope."""
    site = Path(site)
    _records(site, include_metadata=True)
    return _validate_data(site)


def _fetch(url, max_bytes):
    request = Request(url, headers={'User-Agent': 'Ride-Pages-Snapshot/1', 'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=60) as response:
        # A redirect to another host must not silently supply recovery data.
        actual, expected = urlsplit(response.geturl()), urlsplit(url)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise ValueError('Cross-origin snapshot redirect')
        length = response.headers.get('Content-Length')
        if length is not None and int(length) > max_bytes:
            raise ValueError('Download exceeds size cap')
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('Download exceeds size cap')
    return data


def _get(fetch, url, limit):
    result = fetch(url, limit)
    if not isinstance(result, bytes) or len(result) > limit:
        raise ValueError('Invalid or oversized download')
    return result


def _descriptor(raw, live_manifest):
    conf = _json(raw)
    if (not isinstance(conf, dict) or type(conf.get('schemaVersion')) is not int or conf.get('schemaVersion') != 1
            or conf.get('format') != 'tar+gzip-split-v1'
            or conf.get('manifestSha256') != digest(live_manifest)):
        raise ValueError('Snapshot descriptor does not match the live manifest')
    files, shards = conf.get('files'), conf.get('shards')
    if (not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES
            or not isinstance(shards, list) or not 1 <= len(shards) <= MAX_SITE_BYTES // MAX_SHARD_BYTES + 1):
        raise ValueError('Invalid snapshot inventory or shards')
    data_size = 0
    for name, item in files.items():
        _path(name)
        if _metadata(name) or not isinstance(item, dict):
            raise ValueError('Recursive or invalid snapshot inventory')
        data_size += _integer(item.get('size'), MAX_FILE_BYTES, 'file size')
        _hash(item.get('sha256'))
    if files.get('manifest.json') != {'sha256': digest(live_manifest), 'size': len(live_manifest)}:
        raise ValueError('Snapshot manifest inventory differs')
    # Reject file/parent collisions before touching disk.
    for name in files:
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ValueError('Snapshot file/directory collision')
    compressed_size = 0
    for index, item in enumerate(shards):
        if not isinstance(item, dict) or item.get('path') != f'snapshot/archive.tar.gz.part{index:05d}':
            raise ValueError('Unsafe or out-of-order snapshot shard')
        compressed_size += _integer(item.get('size'), MAX_SHARD_BYTES, 'shard size', minimum=1)
        _hash(item.get('sha256'))
    if data_size + compressed_size + len(raw) > MAX_SITE_BYTES:
        raise ValueError('Restored site exceeds size cap')
    _hash(conf.get('archiveSha256'))
    return conf


def restore_published(site, base_url=BASE, fetch=None, receipt=None):
    """Restore the current Pages site into a fresh path, or fail without output.

    ``fetch(url, max_bytes) -> bytes`` is injectable for offline tests. It must
    raise urllib.error.HTTPError for HTTP errors. Only a descriptor 404 permits
    bootstrap recovery; every other error propagates without a fallback.
    """
    site = Path(site)
    if site.exists() or site.is_symlink():
        raise ValueError('Snapshot destination must not exist')
    fetch = fetch or _fetch
    base_url = base_url.rstrip('/')
    live = _get(fetch, base_url + '/manifest.json', MAX_MANIFEST_BYTES)
    published = _json(live)
    try:
        raw = _get(fetch, base_url + '/' + DESCRIPTOR, MAX_FILE_BYTES)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        raw = None
    with tempfile.TemporaryDirectory(dir=site.parent) as directory:
        staging = Path(directory) / 'site'
        if raw is None:
            _bootstrap(staging)
            baseline = _json((staging / 'manifest.json').read_bytes())
            def normalized(manifest):
                if not isinstance(manifest, dict) or not isinstance(manifest.get('regions'), list):
                    raise ValueError('Invalid live bootstrap manifest')
                result = {k: v for k, v in manifest.items() if k != 'updatedAt'}
                result['regions'] = sorted(manifest['regions'], key=lambda r: r['id'])
                return result
            if normalized(published) != normalized(baseline):
                raise ValueError('Live regions differ from the frozen bootstrap; refusing rollback')
            (staging / 'manifest.json').write_bytes(live)
        else:
            conf = _descriptor(raw, live)
            staging.mkdir()
            transport = staging / SNAPSHOT_DIR
            transport.mkdir()
            packed = Path(directory) / 'archive.tar.gz'
            combined_hash = hashlib.sha256()
            with packed.open('wb') as stream:
                for shard in conf['shards']:
                    data = _get(fetch, base_url + '/' + shard['path'], shard['size'])
                    if len(data) != shard['size'] or digest(data) != shard['sha256']:
                        raise ValueError('Snapshot shard digest or size mismatch')
                    (staging / shard['path']).write_bytes(data)
                    combined_hash.update(data)
                    stream.write(data)
            if combined_hash.hexdigest() != conf['archiveSha256']:
                raise ValueError('Snapshot archive digest mismatch')
            # Bound decompression before tarfile parses even extended headers.
            tar_path = Path(directory) / 'archive.tar'
            max_tar = min(MAX_SITE_BYTES, sum(f['size'] for f in conf['files'].values())) + len(conf['files']) * 4096 + 10240
            count = 0
            with gzip.open(packed, 'rb') as gz, tar_path.open('wb') as out:
                while True:
                    data = gz.read(min(1024 * 1024, max_tar - count + 1))
                    if not data:
                        break
                    count += len(data)
                    if count > max_tar:
                        raise ValueError('Snapshot decompression exceeds size cap')
                    out.write(data)
            seen = set()
            with tarfile.open(tar_path, mode='r:') as archive:
                for member in archive:
                    name = _path(member.name)
                    expected = conf['files'].get(name)
                    if (not member.isfile() or member.issparse() or member.linkname
                            or name in seen or expected is None or member.size != expected['size']):
                        raise ValueError('Unsafe, duplicate or unexpected snapshot member')
                    seen.add(name)
                    source = archive.extractfile(member)
                    data = source.read(MAX_FILE_BYTES + 1)
                    if len(data) != expected['size'] or digest(data) != expected['sha256']:
                        raise ValueError('Snapshot file digest or size mismatch')
                    target = staging / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            if seen != set(conf['files']) or _records(staging) != conf['files']:
                raise ValueError('Snapshot inventory is incomplete or different')
            if (staging / 'manifest.json').read_bytes() != live:
                raise ValueError('Restored manifest differs from live bytes')
            (staging / DESCRIPTOR).write_bytes(raw)
        _records(staging, include_metadata=True)
        result = _validate_data(staging, base_url)
        if site.exists() or site.is_symlink():
            raise ValueError('Snapshot destination appeared during restore')
        staging.rename(site)
    if receipt is not None:
        receipt['liveManifestSha256'] = digest(live)
        receipt['bootstrap'] = raw is None
        receipt['liveSnapshotSha256'] = digest(raw) if raw is not None else None
        receipt['publicationRevision'] = publication_revision(live, raw)
    return result


def publication_revision(manifest_bytes, descriptor_bytes):
    """Bind checkpoint-only publications as well as client-visible coverage."""
    return digest(json.dumps({'manifestSha256': digest(manifest_bytes),
        'snapshotSha256': digest(descriptor_bytes) if descriptor_bytes is not None else None},
        sort_keys=True, separators=(',', ':')).encode())


def assert_publication_unchanged(expected_revision, base_url=BASE, fetch=None):
    expected_revision = _hash(expected_revision)
    fetch = fetch or _fetch
    live = _get(fetch, base_url.rstrip('/')+'/manifest.json', MAX_MANIFEST_BYTES)
    try:
        snapshot = _get(fetch, base_url.rstrip('/')+'/'+DESCRIPTOR, MAX_FILE_BYTES)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        snapshot = None
    if publication_revision(live, snapshot) != expected_revision:
        raise ValueError('Live publication revision changed since restore; refusing stale checkpoint')
    return expected_revision


def assert_live_unchanged(expected_sha, base_url=BASE, fetch=None):
    """Fail before deployment if another publisher changed the live manifest."""
    expected_sha = _hash(expected_sha)
    current = _get(fetch or _fetch, base_url.rstrip('/') + '/manifest.json', MAX_MANIFEST_BYTES)
    if digest(current) != expected_sha:
        raise ValueError('Live manifest changed since restore; refusing stale publication')
    return expected_sha


def fetch_bytes(url, max_bytes):
    """Public bounded HTTP fetch used by publication race checks."""
    return _fetch(url, max_bytes)


def seal_snapshot(site):
    """Replace recovery metadata with a deterministic self-contained snapshot.

    Invalid or oversized candidates leave existing descriptor/shards unchanged.
    Data files are never changed. Returned sizes include all recovery overhead.
    """
    site = Path(site)
    _validate_data(site)
    files = _records(site)
    with tempfile.TemporaryDirectory(dir=site.parent) as directory:
        temp = Path(directory)
        packed = temp / 'archive.tar.gz'
        with packed.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0, compresslevel=6) as gz:
                with tarfile.open(fileobj=gz, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                    for name, record in files.items():
                        member = tarfile.TarInfo(name)
                        member.size, member.mode, member.mtime = record['size'], 0o644, 0
                        with (site / name).open('rb') as stream:
                            archive.addfile(member, stream)
        transport = temp / SNAPSHOT_DIR
        transport.mkdir()
        shards, archive_hash = [], hashlib.sha256()
        with packed.open('rb') as stream:
            for index, data in enumerate(iter(lambda: stream.read(MAX_SHARD_BYTES), b'')):
                name = f'snapshot/archive.tar.gz.part{index:05d}'
                (temp / name).write_bytes(data)
                archive_hash.update(data)
                shards.append({'path': name, 'sha256': digest(data), 'size': len(data)})
        conf = {'schemaVersion': 1, 'format': 'tar+gzip-split-v1',
                'manifestSha256': files['manifest.json']['sha256'],
                'archiveSha256': archive_hash.hexdigest(), 'files': files, 'shards': shards}
        encoded = (json.dumps(conf, sort_keys=True, separators=(',', ':')) + '\n').encode()
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError('Snapshot descriptor exceeds file size cap')
        data_size = sum(f['size'] for f in files.values())
        snapshot_size = sum(s['size'] for s in shards) + len(encoded)
        if data_size + snapshot_size > MAX_SITE_BYTES:
            raise ValueError('Sealed site exceeds size cap including snapshot overhead')
        _descriptor(encoded, (site / 'manifest.json').read_bytes())
        if _records(site) != files:
            raise ValueError('Site changed while snapshot was being sealed')
        (temp / DESCRIPTOR).write_bytes(encoded)
        # Local build output is never served in-place; publication happens only
        # after this function and the caller's final validation succeed.
        previous = site / SNAPSHOT_DIR
        if previous.exists():
            shutil.rmtree(previous)
        transport.rename(previous)
        (temp / DESCRIPTOR).replace(site / DESCRIPTOR)
    return {'siteBytes': data_size + snapshot_size, 'dataBytes': data_size,
            'snapshotBytes': snapshot_size, 'files': len(files), 'shards': len(shards),
            'manifestSha256': conf['manifestSha256']}
