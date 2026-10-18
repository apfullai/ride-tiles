"""Fail-closed, restartable region construction from one pinned OSM release.

Only complete directories are committed to a checkpoint. Raw state downloads,
intermediate exports, and incomplete batches live in the disposable work tree.
The caller owns runtime/download admission and publishing the checkpoint.
"""
import datetime as dt
from contextlib import ExitStack
import gzip
import hashlib
import json
import math
import os
import platform
import sys
import time
import zlib
import shapely
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.request

import build_region as legacy
import tile_generator as generator
from source_package import FORMAT as SOURCE_FORMAT, write_source_package, validate_source_package

SCHEMA = 1
CHUNK_BYTES = 8_000_000
MAX_FILE_BYTES = 24_000_000
MAX_CHECKPOINT_BYTES = 250_000_000
MAX_PLAN_AGE_DAYS = 90
DAILY_RETENTION_DAYS = 7
DEFAULT_BATCH_SIZE = 128
# The publisher derives the actual allowance from its shared build deadline.
# This ceiling is only the child maximum, with 15s left to write its result.
MAX_SOURCE_GROUP_SECONDS = 1185
SOURCE_FINISH_SECONDS = 30
BASE = 'https://download.geofabrik.de/north-america/us/'
PUBLIC_BASE = 'https://apfullai.github.io/ride-tiles'
SHA = re.compile(r'[a-f0-9]{64}')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256(path):
    return legacy.sha256(path)


def now_utc(now=None):
    value = now or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        raise RuntimeError('Clock must have an explicit timezone')
    return value.astimezone(dt.timezone.utc)


def parse_time(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError('Invalid timestamp') from exc
    if parsed.tzinfo is None:
        raise RuntimeError('Timestamp lacks timezone')
    return parsed.astimezone(dt.timezone.utc)


def config_hash(config):
    legacy.validate_config(config)
    if len(set(config['sourceStates'])) != len(config['sourceStates']):
        raise RuntimeError('Duplicate configured source state')
    return digest(config)


def runtime_fingerprint():
    """Geometry and export runtimes must not drift during one generation."""
    osmium = subprocess.check_output(['osmium', '--version'], text=True, timeout=10).strip()
    if not osmium or len(osmium) > 4096:
        raise RuntimeError('Cannot identify osmium runtime')
    return {'python': platform.python_version(), 'implementation': sys.implementation.name,
            'shapely': shapely.__version__, 'geos': shapely.geos_version_string, 'osmium': osmium}


def fetch_metadata(url, maximum, headers=None, prefix=False):
    """Bound metadata bytes and charge the shared download ledger on failure."""
    if not url.startswith(BASE):
        raise RuntimeError('Unapproved source metadata URL')
    allowance = maximum if prefix else maximum + 1
    ledger_path = os.environ.get('RIDE_DOWNLOAD_LEDGER')
    ledger = Path(ledger_path) if ledger_path else None
    if ledger:
        state = json.loads(ledger.read_text()) if ledger.exists() else {'reservedBytes': 0}
        spent = state.get('reservedBytes')
        if type(spent) is not int or not 0 <= spent <= 3_000_000_000 - allowance:
            raise RuntimeError('Cumulative download budget exhausted by metadata')
        ledger.write_text(json.dumps({'reservedBytes': spent + allowance}))
    request = urllib.request.Request(url, headers={'User-Agent': 'rides-tiles/1.0', **(headers or {})})
    with urllib.request.urlopen(request, timeout=20) as response:
        if not response.geturl().startswith(BASE):
            raise RuntimeError('Source metadata redirected outside the provider')
        data = response.read(allowance)
    if len(data) > maximum:
        raise RuntimeError('Source metadata exceeds bound')
    if ledger:
        ledger.write_text(json.dumps({'reservedBytes': spent + len(data)}))
    return data


def fetch_small(url, maximum=2_000_000):
    return fetch_metadata(url, maximum).decode('utf-8')


def protobuf_fields(data):
    """Minimal bounded wire reader for the official OSM PBF header schema.

    Reference: openstreetmap/OSM-binary osmpbf/fileformat.proto and
    osmpbf/osmformat.proto. No OSM object/geometry data is parsed here.
    """
    position = 0
    def varint():
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise RuntimeError('Truncated PBF header varint')
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise RuntimeError('Oversized PBF header varint')
    result = {}
    while position < len(data):
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not field:
            raise RuntimeError('Invalid PBF header field')
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else 8 if wire == 1 else 4
            if length > len(data) - position:
                raise RuntimeError('Truncated PBF header field')
            value = data[position:position + length]
            position += length
        else:
            raise RuntimeError('Unsupported PBF header wire type')
        result.setdefault(field, []).append(value)
    return result


def pbf_header_timestamp(data):
    if len(data) < 4:
        raise RuntimeError('Truncated PBF file header')
    length = int.from_bytes(data[:4], 'big')
    if not 0 < length <= 65536 or len(data) < 4 + length:
        raise RuntimeError('Invalid PBF blob header size')
    header = protobuf_fields(data[4:4 + length])
    if header.get(1) != [b'OSMHeader'] or len(header.get(3, [])) != 1:
        raise RuntimeError('Missing PBF OSMHeader')
    size = header[3][0]
    if type(size) is not int or not 0 < size <= 65536 or len(data) < 4 + length + size:
        raise RuntimeError('PBF header exceeds bounded prefix')
    blob = protobuf_fields(data[4 + length:4 + length + size])
    if set(blob).intersection({4, 5, 6, 7}):
        raise RuntimeError('Unsupported PBF header compression')
    if 1 in blob and 3 not in blob and len(blob[1]) == 1:
        decoded = blob[1][0]
    elif 3 in blob and 1 not in blob and len(blob[3]) == 1 and len(blob.get(2, [])) == 1:
        raw_size = blob[2][0]
        if type(raw_size) is not int or not 0 < raw_size <= 1_000_000:
            raise RuntimeError('PBF header decompression exceeds bound')
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(blob[3][0], raw_size + 1)
        if len(decoded) != raw_size or not decoder.eof or decoder.unused_data:
            raise RuntimeError('Invalid compressed PBF header')
    else:
        raise RuntimeError('Missing PBF header blob data')
    timestamp = protobuf_fields(decoded).get(32)
    if not timestamp or len(timestamp) != 1 or type(timestamp[0]) is not int:
        raise RuntimeError('Missing OSM replication timestamp in PBF header')
    try:
        return dt.datetime.fromtimestamp(timestamp[0], dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError, OSError) as exc:
        raise RuntimeError('Invalid OSM replication timestamp') from exc


def read_pbf_timestamp(url):
    prefix = fetch_metadata(url, 131072, headers={'Range': 'bytes=0-131071'}, prefix=True)
    return pbf_header_timestamp(prefix)


def plan_sources(config, now=None):
    """Pin the newest common dated Geofabrik release, headers and checksums.

    Daily files have a conservative seven-day acquisition window. Retained
    monthly releases have a ninety-day window. Availability is not guaranteed:
    missing/changed pins fail, never substitute latest. Completed crops remain
    usable after acquisition expiry. The publisher forbids freshness rollback.
    """
    clock = now_utc(now)
    config_sha = config_hash(config)
    dates = []
    for state in config['sourceStates']:
        page = fetch_small(BASE + state + '.html')
        found = set(re.findall(r'\b' + re.escape(state) + r'-(\d{6})\.osm\.pbf(?:["<\s]|\.md5)', page))
        valid = set()
        for value in found:
            try:
                date = dt.datetime.strptime(value, '%y%m%d').date()
            except ValueError:
                continue
            retention = MAX_PLAN_AGE_DAYS if date.day == 1 else DAILY_RETENTION_DAYS
            if 0 <= (clock.date() - date).days < retention:
                valid.add(value)
        dates.append(valid)
    common = set.intersection(*dates)
    if not common:
        raise RuntimeError('No recent common dated source release')
    release = max(common)
    date = dt.datetime.strptime(release, '%y%m%d').replace(tzinfo=dt.timezone.utc)
    sources = []
    timestamps = set()
    for state in config['sourceStates']:
        filename = state + '-' + release + '.osm.pbf'
        url = BASE + filename
        checksum = fetch_small(url + '.md5', maximum=512).strip()
        match = re.fullmatch(r'([a-fA-F0-9]{32})\s+\*?' + re.escape(filename), checksum)
        if not match:
            raise RuntimeError('Missing or invalid pinned source checksum: ' + state)
        timestamp = read_pbf_timestamp(url)
        if parse_time(timestamp).date() != date.date():
            raise RuntimeError('Pinned source header date differs from dated release')
        timestamps.add(timestamp)
        sources.append({'state': state, 'url': url, 'md5': match.group(1).lower(), 'osmReplicationTimestamp': timestamp})
    if len(timestamps) != 1:
        raise RuntimeError('Dated sources have mixed OSM replication timestamps')
    plan = {'schemaVersion': SCHEMA, 'configSha256': config_sha,
            'sourceDate': date.date().isoformat(), 'createdAt': clock.isoformat(),
            'osmReplicationTimestamp': next(iter(timestamps)), 'runtime': runtime_fingerprint(),
            'expiresAt': (date + dt.timedelta(days=MAX_PLAN_AGE_DAYS if date.day == 1 else DAILY_RETENTION_DAYS)).isoformat(),
            'sources': sources}
    plan['planId'] = digest(plan)
    validate_source_plan(config, plan, now=clock)
    return plan


def validate_source_plan(config, plan, now=None, require_unexpired=True):
    if not isinstance(plan, dict) or plan.get('schemaVersion') != SCHEMA:
        raise RuntimeError('Invalid source plan schema')
    unsigned = {k: v for k, v in plan.items() if k != 'planId'}
    if plan.get('planId') != digest(unsigned) or plan.get('configSha256') != config_hash(config):
        raise RuntimeError('Source plan identity/config mismatch')
    try:
        date = dt.date.fromisoformat(plan['sourceDate'])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError('Invalid source release date') from exc
    created = parse_time(plan['createdAt'])
    expiry = parse_time(plan['expiresAt'])
    expected_expiry = dt.datetime.combine(date, dt.time(), dt.timezone.utc) + dt.timedelta(days=MAX_PLAN_AGE_DAYS if date.day == 1 else DAILY_RETENTION_DAYS)
    if expiry != expected_expiry or not dt.datetime.combine(date, dt.time(), dt.timezone.utc) <= created < expiry:
        raise RuntimeError('Invalid source release validity window')
    if require_unexpired and now_utc(now) >= expiry:
        raise RuntimeError('Pinned source release expired; refusing to substitute latest')
    if plan.get('runtime') != runtime_fingerprint():
        raise RuntimeError('Generator runtime changed; refusing mixed generation')
    if parse_time(plan.get('osmReplicationTimestamp')).date() != date:
        raise RuntimeError('Source plan replication timestamp/date mismatch')
    sources = plan.get('sources')
    if not isinstance(sources, list) or [s.get('state') for s in sources] != config['sourceStates']:
        raise RuntimeError('Source plan states/order mismatch')
    for source in sources:
        expected = BASE + source['state'] + '-' + date.strftime('%y%m%d') + '.osm.pbf'
        if source.get('url') != expected or not re.fullmatch(r'[a-f0-9]{32}', source.get('md5', '')) or source.get('osmReplicationTimestamp') != plan['osmReplicationTimestamp']:
            raise RuntimeError('Invalid pinned source identity')
    return plan


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise RuntimeError('Invalid checkpoint path')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != value:
        raise RuntimeError('Unsafe checkpoint path')
    return path


def file_map(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Checkpoint symlinks are forbidden')
        if path.relative_to(root).as_posix() == 'receipt.json':
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError('Unexpected checkpoint filesystem object')
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise RuntimeError('Checkpoint file exceeds 24 MB')
        result[path.relative_to(root).as_posix()] = {'sha256': sha256(path), 'bytes': size}
    if sum(v['bytes'] for v in result.values()) > MAX_CHECKPOINT_BYTES:
        raise RuntimeError('Checkpoint stage exceeds site budget')
    return result


def make_receipt(root, plan_id, stage, **details):
    receipt = {'schemaVersion': SCHEMA, 'planId': plan_id, 'stage': stage,
               'files': file_map(root), **details}
    write_json(Path(root) / 'receipt.json', receipt)
    return receipt


def verify_receipt(root, plan_id=None, stage=None):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or (root / 'receipt.json').is_symlink():
        raise RuntimeError('Invalid checkpoint stage directory')
    try:
        if (root / 'receipt.json').stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError('Checkpoint receipt exceeds file size bound')
        receipt = json.loads((root / 'receipt.json').read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('Incomplete checkpoint stage: missing receipt') from exc
    if receipt.get('schemaVersion') != SCHEMA or not SHA.fullmatch(receipt.get('planId', '')):
        raise RuntimeError('Invalid checkpoint receipt')
    if plan_id is not None and receipt['planId'] != plan_id:
        raise RuntimeError('Mixed source plans in checkpoint')
    if stage is not None and receipt.get('stage') != stage:
        raise RuntimeError('Wrong checkpoint stage')
    if receipt.get('files') != file_map(root):
        raise RuntimeError('Checkpoint file hash/size mismatch or unexpected files')
    return receipt


def pack_file(source, root, name, compress=False):
    """Persist bounded transport chunks; record both encoded and raw identity."""
    source, root = Path(source), Path(root)
    root.mkdir(parents=True, exist_ok=True)
    raw_size = source.stat().st_size
    if not 0 < raw_size <= MAX_CHECKPOINT_BYTES:
        raise RuntimeError('Invalid checkpoint payload size')
    encoded = source
    if compress:
        encoded = root / (name + '.tmp-gzip')
        with source.open('rb') as src, encoded.open('wb') as target:
            with gzip.GzipFile(fileobj=target, mode='wb', mtime=0, filename='', compresslevel=6) as gz:
                shutil.copyfileobj(src, gz)
    parts = []
    try:
        with encoded.open('rb') as src:
            while data := src.read(CHUNK_BYTES):
                path = name + '.part-' + format(len(parts), '04d')
                (root / path).write_bytes(data)
                parts.append({'path': path, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    finally:
        if compress:
            encoded.unlink(missing_ok=True)
    return {'bytes': raw_size, 'sha256': sha256(source), 'encoding': 'gzip' if compress else 'raw', 'parts': parts}


def unpack_file(descriptor, root, destination):
    root, destination = Path(root), Path(destination)
    if type(descriptor.get('bytes')) is not int or not 0 < descriptor['bytes'] <= MAX_CHECKPOINT_BYTES:
        raise RuntimeError('Invalid unpacked checkpoint size')
    if not SHA.fullmatch(descriptor.get('sha256', '')) or descriptor.get('encoding') not in ('raw', 'gzip'):
        raise RuntimeError('Invalid checkpoint payload identity')
    parts = descriptor.get('parts')
    if not isinstance(parts, list) or not parts or len(parts) > MAX_CHECKPOINT_BYTES // CHUNK_BYTES + 1:
        raise RuntimeError('Invalid checkpoint chunks')
    seen = set()
    packed = destination.with_name(destination.name + '.packed')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with packed.open('wb') as out:
            for part in parts:
                name = safe_relative(part['path'])
                if name.as_posix() in seen:
                    raise RuntimeError('Duplicate checkpoint chunk')
                seen.add(name.as_posix())
                path = root / name
                if path.is_symlink() or type(part.get('bytes')) is not int or not 0 < part['bytes'] <= CHUNK_BYTES:
                    raise RuntimeError('Invalid checkpoint chunk size')
                if path.stat().st_size != part['bytes'] or sha256(path) != part['sha256']:
                    raise RuntimeError('Corrupt checkpoint chunk')
                with path.open('rb') as src:
                    shutil.copyfileobj(src, out)
        opener = gzip.open if descriptor['encoding'] == 'gzip' else open
        count = 0
        with opener(packed, 'rb') as src, destination.open('wb') as out:
            while data := src.read(1024 * 1024):
                count += len(data)
                if count > descriptor['bytes']:
                    raise RuntimeError('Checkpoint decompression exceeds declared size')
                out.write(data)
        if count != descriptor['bytes'] or sha256(destination) != descriptor['sha256']:
            raise RuntimeError('Checkpoint payload SHA256 mismatch')
    finally:
        packed.unlink(missing_ok=True)
    return destination


def workspace(work):
    Path(work).mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='stage-', dir=work)


def commit_stage(staged, destination):
    destination = Path(destination)
    if destination.exists():
        raise RuntimeError('Refusing to overwrite completed checkpoint stage')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Both trees are on the runner filesystem. A cross-device rename is refused.
    Path(staged).rename(destination)


def remaining_seconds(deadline, reserve=0):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic() - reserve
    if remaining <= 0:
        raise TimeoutError('Source stage reached its time budget')
    return remaining


def source_timestamp(path, *, deadline=None):
    value = subprocess.check_output(['osmium', 'fileinfo', '-g', 'header.option.osmosis_replication_timestamp', str(path)],
                                    text=True, timeout=remaining_seconds(deadline)).strip()
    parse_time(value)
    return value


def check_source_receipt(config, plan, state, receipt):
    pin = next(s for s in plan['sources'] if s['state'] == state)
    if receipt.get('state') != state or receipt.get('source', {}).get('url') != pin['url']:
        raise RuntimeError('Wrong state source receipt')
    source = receipt['source']
    if source.get('md5') != pin['md5'] or not SHA.fullmatch(source.get('sha256', '')):
        raise RuntimeError('Source checksum receipt mismatch')
    timestamp = parse_time(source['osmReplicationTimestamp'])
    if timestamp.date().isoformat() != plan['sourceDate']:
        raise RuntimeError('Source timestamp does not match pinned release date')
    if timestamp != parse_time(plan['osmReplicationTimestamp']):
        raise RuntimeError('Mixed OSM replication timestamps: downloaded source differs from pinned header')
    if type(source.get('bytes')) is not int or not 1_000_000 < source['bytes'] <= legacy.MAX_SOURCE_BYTES:
        raise RuntimeError('Invalid source byte receipt')
    return timestamp


def validate_source_crop(root, receipt, *, deadline=None):
    """Validate the exact raw crop described by a non-compacted source receipt.

    Inventory hashing alone does not prove that the crop descriptor names those
    files, in the right order, with the declared reconstructed identity.
    """
    crop = receipt.get('crop')
    if (not isinstance(crop, dict) or crop.get('encoding') != 'raw'
            or type(crop.get('bytes')) is not int or not 0 < crop['bytes'] <= MAX_CHECKPOINT_BYTES
            or not isinstance(crop.get('sha256'), str) or not SHA.fullmatch(crop['sha256'])):
        raise RuntimeError('Invalid source crop descriptor')
    parts = crop.get('parts')
    if not isinstance(parts, list) or not parts or len(parts) > MAX_CHECKPOINT_BYTES // CHUNK_BYTES + 1:
        raise RuntimeError('Invalid source crop chunks')
    expected = {}
    total = 0
    checksum = hashlib.sha256()
    for number, part in enumerate(parts):
        if (not isinstance(part, dict) or part.get('path') != 'crop.part-' + format(number, '04d')
                or type(part.get('bytes')) is not int or not 0 < part['bytes'] <= CHUNK_BYTES
                or not isinstance(part.get('sha256'), str) or not SHA.fullmatch(part['sha256'])):
            raise RuntimeError('Invalid source crop chunk order or identity')
        name = part['path']
        expected[name] = {'bytes': part['bytes'], 'sha256': part['sha256']}
        path = Path(root) / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != part['bytes']:
            raise RuntimeError('Invalid source crop chunk file')
        chunk_hash = hashlib.sha256()
        with path.open('rb') as source:
            for data in iter(lambda: source.read(1024 * 1024), b''):
                remaining_seconds(deadline)
                total += len(data)
                if total > crop['bytes']:
                    raise RuntimeError('Source crop exceeds declared size')
                checksum.update(data)
                chunk_hash.update(data)
        if chunk_hash.hexdigest() != part['sha256']:
            raise RuntimeError('Source crop chunk SHA256 mismatch')
    if receipt.get('files') != expected or total != crop['bytes'] or checksum.hexdigest() != crop['sha256']:
        raise RuntimeError('Source crop descriptor differs from committed payload')


def build_source_stage(config, plan, state, work, checkpoint, *, deadline=None, source_path=None):
    remaining_seconds(deadline)
    validate_source_plan(config, plan, require_unexpired=False)
    if state not in config['sourceStates']:
        raise RuntimeError('Unplanned source state')
    checkpoint = Path(checkpoint)
    existing = validate_checkpoint(config, plan, checkpoint)
    dest = checkpoint / 'sources' / state
    if state in existing['completedStates']:
        return verify_receipt(dest, plan['planId'], 'source')
    validate_source_plan(config, plan)
    pin = next(s for s in plan['sources'] if s['state'] == state)
    with workspace(work) as temporary:
        temp = Path(temporary)
        source, filtered, crop = (temp / name for name in ('state.osm.pbf', 'filtered.osm.pbf', 'crop.osm.pbf'))
        if source_path is None:
            download_options = {'timeout': min(legacy.MAX_TRANSFER_SECONDS, remaining_seconds(deadline, SOURCE_FINISH_SECONDS))} if deadline is not None else {}
            legacy.download_source(pin['url'], source, **download_options)
        else:
            source = Path(source_path)
            if source.is_symlink() or not source.is_file():
                raise RuntimeError('Invalid prefetched source file')
        remaining_seconds(deadline)
        size = source.stat().st_size
        if not 1_000_000 < size <= legacy.MAX_SOURCE_BYTES:
            raise RuntimeError('Source size outside safety bounds')
        md5 = hashlib.md5(usedforsecurity=False)
        source_sha = hashlib.sha256()
        with source.open('rb') as src:
            for data in iter(lambda: src.read(1024 * 1024), b''):
                remaining_seconds(deadline)
                md5.update(data)
                source_sha.update(data)
        if md5.hexdigest() != pin['md5']:
            raise RuntimeError('Pinned source checksum mismatch; refusing replacement')
        timestamp_options = {'deadline': deadline} if deadline is not None else {}
        timestamp = source_timestamp(source, **timestamp_options)
        metadata = {**pin, 'sha256': source_sha.hexdigest(), 'bytes': size,
                    'osmReplicationTimestamp': timestamp, 'downloadedAt': now_utc().isoformat()}
        check_source_receipt(config, plan, state, {'state': state, 'source': metadata})
        for previous in existing['completedStates']:
            receipt = verify_receipt(checkpoint / 'sources' / previous, plan['planId'], 'source')
            if parse_time(receipt['source']['osmReplicationTimestamp']) != parse_time(timestamp):
                raise RuntimeError('Mixed OSM replication timestamps; refusing source stage')
        def run_source(*args):
            options = {'timeout': remaining_seconds(deadline)} if deadline is not None else {}
            legacy.run(*args, **options)
        run_source('osmium', 'tags-filter', str(source), 'w/highway=cycleway,path,footway,track,pedestrian',
                   'w/cycleway', 'w/cycleway:left', 'w/cycleway:right', 'w/cycleway:both',
                   'w/bicycle=designated,yes,official,permissive', '-o', str(filtered))
        run_source('osmium', 'extract', '--bbox', ','.join(map(str, config['bounds'])), '--strategy', 'complete_ways', str(filtered), '-o', str(crop))
        remaining_seconds(deadline)
        staged = temp / 'commit'
        staged.mkdir()
        descriptor = pack_file(crop, staged, 'crop')
        receipt = make_receipt(staged, plan['planId'], 'source', state=state, source=metadata, crop=descriptor)
        verify_receipt(staged, plan['planId'], 'source')
        validate_source_crop(staged, receipt, deadline=deadline)
        remaining_seconds(deadline)
        commit_stage(staged, dest)
    return receipt


class SourceGroupFailure(Exception):
    def __init__(self, group, progress, state, error):
        super().__init__(str(error)[:500])
        self.group = group
        self.progress = progress
        self.detail = {'state': state, 'type': type(error).__name__, 'message': str(error)[:500],
                       'permanent': isinstance(error, (ValueError, RuntimeError))}


def build_source_group(config, plan, checkpoint, work, seconds=240, reserve_seconds=30, on_progress=None, concurrent_downloads=1):
    """Acquire a validated prefix and journal it before attempting another source.

    The callback is only an IPC claim; the publisher must independently verify
    the listed checkpoint directories before retaining them after child failure.
    """
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= MAX_SOURCE_GROUP_SECONDS:
        raise RuntimeError('Invalid source-group time budget')
    if not isinstance(reserve_seconds, (int, float)) or isinstance(reserve_seconds, bool) or not math.isfinite(reserve_seconds) or not 0 <= reserve_seconds < seconds:
        raise RuntimeError('Invalid source-group safety reserve')
    if type(concurrent_downloads) is not int or concurrent_downloads not in (1, 2):
        raise ValueError('Source acquisition concurrency must be one or two')
    deadline = time.monotonic() + seconds
    status = validate_checkpoint(config, plan, checkpoint)
    missing = [state for state in config['sourceStates'] if state not in status['completedStates']]
    completed = []
    acquisition = None
    def group_receipt(state):
        remaining = [item for item in missing if item not in completed]
        receipt = {'schemaVersion': SCHEMA, 'planId': plan['planId'], 'completedStates': list(completed),
                   'remainingStates': remaining, 'plannedPause': bool(remaining), 'status': state}
        if acquisition is not None:
            receipt['acquisition'] = acquisition
        return receipt

    def inspect_prefix(attempted):
        progress = validate_checkpoint(config, plan, checkpoint)
        added = [item for item in progress['completedStates'] if item not in status['completedStates']]
        if (added != missing[:len(added)] or len(added) > attempted
                or any(item not in progress['completedStates'] for item in status['completedStates'])
                or any(progress[key] != status[key] for key in ('indexReady', 'batchIds', 'batchCount', 'completedBatches'))):
            raise RuntimeError('Source group changed work outside its configured prefix')
        return progress, added

    slowest = 30.0
    with ExitStack() as resources:
        downloads = None
        selected = missing
        if concurrent_downloads == 2 and missing:
            if deadline-time.monotonic() <= reserve_seconds+2*slowest:
                return group_receipt('paused')
            # Every pin/runtime/expiry check precedes raw acquisition. The batch
            # reserves both full allowances before starting either curl.
            validate_source_plan(config, plan)
            raw = Path(resources.enter_context(workspace(work)))
            pins = {item['state']: item for item in plan['sources']}
            requests = [{'state': state, 'url': pins[state]['url'], 'path': raw/(state+'.osm.pbf')}
                        for state in missing[:2]]
            downloads = resources.enter_context(legacy.SourceDownloadBatch(requests,
                deadline=deadline-reserve_seconds-SOURCE_FINISH_SECONDS))
            acquisition = downloads.summary
            selected = [item['state'] for item in downloads.sources]
        for number, state in enumerate(selected):
            if downloads is None and deadline-time.monotonic() <= reserve_seconds+2*slowest:
                break
            started = time.monotonic()
            try:
                source_options = {'source_path': downloads.wait_for(state)} if downloads else {}
                build_source_stage(config, plan, state, work, checkpoint,
                                   deadline=deadline-reserve_seconds, **source_options)
            except Exception as error:
                progress, completed = inspect_prefix(number+1)
                failed_state = error.state if isinstance(error, legacy.SourceDownloadFailure) else state
                cause = error.cause if isinstance(error, legacy.SourceDownloadFailure) else error
                raise SourceGroupFailure(group_receipt('failed'), progress, failed_state, cause) from error
            progress, completed = inspect_prefix(number+1)
            if completed != missing[:number+1]:
                raise RuntimeError('Source stage did not commit its complete crop')
            if on_progress is not None:
                on_progress(group_receipt('running'), progress)
            slowest = max(slowest, time.monotonic()-started)
    return group_receipt('paused' if len(completed) < len(missing) else 'complete')


def compact_source_crops(config, plan, checkpoint, index):
    """Discard only redundant active-work payloads in a disposable candidate.

    The complete canonical source and index have already been independently
    reconstructed and verified. The publisher must commit the candidate only
    after this whole operation validates; interruption cannot affect the prior
    published checkpoint. Historical region data is never touched here.
    """
    checkpoint = Path(checkpoint)
    for state in config['sourceStates']:
        root = checkpoint / 'sources' / state
        receipt = verify_receipt(root, plan['planId'], 'source')
        original_hash = sha256(root / 'receipt.json')
        if original_hash != index['sourceReceiptHashes'][state] or receipt['crop']['sha256'] != index['sourceCropHashes'][state]:
            raise RuntimeError('Source changed before compaction')
        expected_files = {part['path']: {'bytes': part['bytes'], 'sha256': part['sha256']} for part in receipt['crop']['parts']}
        if receipt['files'] != expected_files:
            raise RuntimeError('Unexpected source files before compaction')
        for name in expected_files:
            (root / safe_relative(name)).unlink()
        receipt['files'] = {}
        receipt['compactedInto'] = {'mergedSourceSha256': index['mergedSource']['sha256'],
                                    'indexSha256': index['index']['sha256'],
                                    'originalReceiptSha256': original_hash}
        write_json(root / 'receipt.json', receipt)


def build_index_stage(config, plan, checkpoint, work, batch_size=DEFAULT_BATCH_SIZE):
    validate_source_plan(config, plan, require_unexpired=False)
    if type(batch_size) is not int or not 1 <= batch_size <= 512:
        raise RuntimeError('Invalid tile batch size')
    checkpoint = Path(checkpoint)
    current = validate_checkpoint(config, plan, checkpoint)
    if current['indexReady']:
        receipt = verify_receipt(checkpoint / 'index', plan['planId'], 'index')
        if receipt['batchSize'] != batch_size:
            raise RuntimeError('Existing tile batch size cannot change')
        return receipt
    if current['completedStates'] != config['sourceStates']:
        raise RuntimeError('Cannot index before every pinned state crop is complete')
    with workspace(work) as temporary:
        temp = Path(temporary)
        crops = []
        for state in config['sourceStates']:
            directory = checkpoint / 'sources' / state
            receipt = verify_receipt(directory, plan['planId'], 'source')
            crops.append(unpack_file(receipt['crop'], directory, temp / (state + '.osm.pbf')))
        merged = temp / 'region.osm.pbf'
        if len(crops) == 1:
            shutil.copyfile(crops[0], merged)
        else:
            legacy.run('osmium', 'merge', *map(str, crops), '-o', str(merged))
        sequence = temp / 'cycling.geojsonseq'
        legacy.run('osmium', 'export', '--attributes=id,type', '-f', 'geojsonseq', '-x', 'print_record_separator=false', str(merged), '-o', str(sequence))
        database = temp / 'features.sqlite'
        summary = generator.build_feature_index(sequence, database, config['bounds'], range(config['minzoom'], config['maxzoom'] + 1), batch_size)
        staged = temp / 'commit'
        staged.mkdir()
        index = pack_file(database, staged, 'features', compress=True)
        merged_descriptor = pack_file(merged, staged, 'merged-source')
        receipt = make_receipt(staged, plan['planId'], 'index', index=index, mergedSource=merged_descriptor, runtime=plan['runtime'],
                               sourceCropHashes={state: verify_receipt(checkpoint / 'sources' / state)['crop']['sha256'] for state in config['sourceStates']},
                               sourceReceiptHashes={state: sha256(checkpoint / 'sources' / state / 'receipt.json') for state in config['sourceStates']},
                               compactedSourceCrops=True, **summary)
        verify_receipt(staged, plan['planId'], 'index')
        unpack_file(receipt['index'], staged, temp / 'verified-index.sqlite')
        unpack_file(receipt['mergedSource'], staged, temp / 'verified-source.osm.pbf')
        commit_stage(staged, checkpoint / 'index')
        compact_source_crops(config, plan, checkpoint, receipt)
        validate_checkpoint(config, plan, checkpoint)
    return receipt


def commit_tile_batch(checkpoint, batch_id, temp, index, database):
    dest = checkpoint / 'batches' / format(batch_id, '06d')
    if dest.exists():
        return validate_batch(dest, index, batch_id)
    staged = temp / ('batch-' + format(batch_id, '06d'))
    staged.mkdir()
    summary = generator.generate_tile_batch(database, staged, batch_id)
    batch = make_receipt(staged, index['planId'], 'tiles', indexSha256=index['index']['sha256'], **summary)
    validate_batch(staged, index, batch_id)
    commit_stage(staged, dest)
    return batch


def build_tile_stage(checkpoint, batch_id, work):
    checkpoint = Path(checkpoint)
    index_dir = checkpoint / 'index'
    receipt = verify_receipt(index_dir, stage='index')
    if receipt.get('runtime') != runtime_fingerprint():
        raise RuntimeError('Generator runtime changed; refusing mixed tile generation')
    if type(batch_id) is not int or batch_id not in receipt['batchIds']:
        raise RuntimeError('Unplanned tile batch')
    dest = checkpoint / 'batches' / format(batch_id, '06d')
    if dest.exists():
        return validate_batch(dest, receipt, batch_id)
    with workspace(work) as temporary:
        temp = Path(temporary)
        database = unpack_file(receipt['index'], index_dir, temp / 'features.sqlite')
        return commit_tile_batch(checkpoint, batch_id, temp, receipt, database)


def build_tile_group(checkpoint, work, seconds=240, reserve_seconds=30):
    """Reuse one index for bounded batches, yielding before the process limit.

    Individual complete batches are atomic within the disposable candidate.
    The caller publishes their group only after its independent integrity and
    budget checks. A hard process kill may lose this unpublished group, never
    change the previously published checkpoint or live region.
    """
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= 285:
        raise RuntimeError('Invalid tile-group time budget')
    if not isinstance(reserve_seconds, (int, float)) or isinstance(reserve_seconds, bool) or not 0 <= reserve_seconds < seconds:
        raise RuntimeError('Invalid tile-group safety reserve')
    deadline = time.monotonic() + seconds
    checkpoint = Path(checkpoint)
    index_dir = checkpoint / 'index'
    index = verify_receipt(index_dir, stage='index')
    if index.get('runtime') != runtime_fingerprint():
        raise RuntimeError('Generator runtime changed; refusing mixed tile generation')
    existing = set()
    batches_dir = checkpoint / 'batches'
    if batches_dir.exists():
        if batches_dir.is_symlink():
            raise RuntimeError('Tile batch directory cannot be a symlink')
        for root in sorted(batches_dir.iterdir()):
            if not root.is_dir() or not re.fullmatch(r'\d{6}', root.name):
                raise RuntimeError('Unexpected tile batch directory')
            batch_id = int(root.name)
            validate_batch(root, index, batch_id)
            existing.add(batch_id)
    missing = [batch_id for batch_id in index['batchIds'] if batch_id not in existing]
    completed = []
    # Require room for a conservative first batch. Later batches use twice the
    # slowest observed duration and at least this floor, plus the final reserve.
    slowest = 15.0
    if missing and deadline - time.monotonic() > reserve_seconds + 2 * slowest:
        with workspace(work) as temporary:
            temp = Path(temporary)
            database = unpack_file(index['index'], index_dir, temp / 'features.sqlite')
            for batch_id in missing:
                if deadline - time.monotonic() <= reserve_seconds + 2 * slowest:
                    break
                started = time.monotonic()
                commit_tile_batch(checkpoint, batch_id, temp, index, database)
                slowest = max(slowest, time.monotonic() - started)
                completed.append(batch_id)
    remaining = [batch_id for batch_id in missing if batch_id not in completed]
    return {'completedBatches': completed, 'remainingBatches': remaining, 'plannedPause': bool(remaining)}


def validate_batch(directory, index, expected_id=None):
    receipt = verify_receipt(directory, index['planId'], 'tiles')
    batch_id = receipt.get('batchId')
    if type(batch_id) is not int or batch_id not in index['batchIds'] or expected_id is not None and batch_id != expected_id:
        raise RuntimeError('Unexpected tile batch ID')
    if receipt.get('indexSha256') != index['index']['sha256']:
        raise RuntimeError('Tile batch belongs to a different canonical feature index')
    expected = index['batches'][batch_id]
    if receipt.get('candidateTiles') != expected['tiles'] or receipt.get('candidateDigest') != expected['digest']:
        raise RuntimeError('Missing, duplicate, or overlapping candidate tiles')
    tiles = receipt.get('tileHashes')
    if not isinstance(tiles, dict) or set(tiles) - set(expected['tiles']):
        raise RuntimeError('Unexpected generated tile')
    if set(receipt['files']) != set(tiles):
        raise RuntimeError('Tile batch file coverage mismatch')
    for path, checksum in tiles.items():
        if receipt['files'][path]['sha256'] != checksum or not 0 < receipt['files'][path]['bytes'] <= 2_000_000:
            raise RuntimeError('Invalid tile batch hash or size')
    if receipt.get('nonemptyTiles') != len(tiles):
        raise RuntimeError('Tile batch count mismatch')
    return receipt


def validate_checkpoint(config, plan, checkpoint):
    validate_source_plan(config, plan, require_unexpired=False)
    checkpoint = Path(checkpoint)
    if checkpoint.is_symlink():
        raise RuntimeError('Checkpoint root cannot be a symlink')
    result = {'completedStates': [], 'indexReady': False, 'batchIds': [], 'batchCount': 0, 'completedBatches': []}
    allowed_states = set(config['sourceStates'])
    sources_dir = checkpoint / 'sources'
    timestamps = set()
    compacted = {}
    if sources_dir.exists():
        if sources_dir.is_symlink() or any(not p.is_dir() or p.name not in allowed_states for p in sources_dir.iterdir()):
            raise RuntimeError('Unexpected source checkpoint directory')
        for state in config['sourceStates']:
            root = sources_dir / state
            if not root.exists():
                continue
            receipt = verify_receipt(root, plan['planId'], 'source')
            timestamps.add(check_source_receipt(config, plan, state, receipt))
            if 'compactedInto' in receipt:
                if receipt['files']:
                    raise RuntimeError('Compacted source still contains payload files')
                compacted[state] = receipt
            else:
                validate_source_crop(root, receipt)
            result['completedStates'].append(state)
    if len(timestamps) > 1:
        raise RuntimeError('Mixed source replication timestamps in checkpoint')
    index_dir = checkpoint / 'index'
    if index_dir.exists():
        if result['completedStates'] != config['sourceStates']:
            raise RuntimeError('Feature index is missing a pinned source')
        index = verify_receipt(index_dir, plan['planId'], 'index')
        expected_hashes = {state: verify_receipt(sources_dir / state)['crop']['sha256'] for state in config['sourceStates']}
        if index.get('sourceCropHashes') != expected_hashes:
            raise RuntimeError('Canonical index source crop mismatch')
        if index.get('compactedSourceCrops') is True:
            if set(compacted) != allowed_states or set(index.get('sourceReceiptHashes', {})) != allowed_states:
                raise RuntimeError('Incomplete source compaction')
            for state, source_receipt in compacted.items():
                expected_marker = {'mergedSourceSha256': index['mergedSource']['sha256'],
                                   'indexSha256': index['index']['sha256'],
                                   'originalReceiptSha256': index['sourceReceiptHashes'][state]}
                if source_receipt['compactedInto'] != expected_marker:
                    raise RuntimeError('Compacted source does not match canonical index')
                original = {k: v for k, v in source_receipt.items() if k != 'compactedInto'}
                original['files'] = {part['path']: {'bytes': part['bytes'], 'sha256': part['sha256']} for part in original['crop']['parts']}
                original_bytes = (json.dumps(original, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
                if hashlib.sha256(original_bytes).hexdigest() != expected_marker['originalReceiptSha256']:
                    raise RuntimeError('Compacted source provenance differs from original receipt')
        elif compacted:
            raise RuntimeError('Compacted source has no compatible canonical index')
        if index.get('runtime') != plan['runtime']:
            raise RuntimeError('Canonical index runtime mismatch')
        if index.get('bounds') != config['bounds'] or index.get('zooms') != list(range(config['minzoom'], config['maxzoom'] + 1)):
            raise RuntimeError('Canonical index region mismatch')
        if type(index.get('batchSize')) is not int or not 1 <= index['batchSize'] <= 512:
            raise RuntimeError('Invalid canonical tile batch size')
        counts = index.get('featuresByCategory')
        if not isinstance(counts, dict) or not counts or any(k not in legacy.KINDS or type(v) is not int or v <= 0 for k, v in counts.items()):
            raise RuntimeError('Invalid canonical feature counts')
        if index.get('featureCount') != sum(counts.values()):
            raise RuntimeError('Canonical feature count mismatch')
        ids = index.get('batchIds')
        if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids) or ids != list(range(len(ids))) or index.get('batchCount') != len(ids):
            raise RuntimeError('Invalid canonical tile batch IDs')
        batches = index.get('batches')
        if not isinstance(batches, list) or len(batches) != len(ids):
            raise RuntimeError('Invalid canonical tile batch plan')
        all_tiles = []
        for batch_id, batch in enumerate(batches):
            tiles = batch.get('tiles')
            if not isinstance(tiles, list) or not tiles or len(tiles) > index['batchSize'] or batch.get('digest') != digest(tiles):
                raise RuntimeError('Invalid canonical tile batch coverage')
            for tile in tiles:
                if not isinstance(tile, str) or not re.fullmatch(r'tiles/\d+/\d+/\d+\.pbf', tile):
                    raise RuntimeError('Invalid canonical tile path')
                _, z, x, y = tile.removesuffix('.pbf').split('/')
                z, x, y = int(z), int(x), int(y)
                if not config['minzoom'] <= z <= config['maxzoom'] or not 0 <= x < 2**z or not 0 <= y < 2**z:
                    raise RuntimeError('Canonical tile coordinates outside region zoom bounds')
            all_tiles.extend(tiles)
        if len(set(all_tiles)) != len(all_tiles) or len(all_tiles) != index.get('candidateTileCount'):
            raise RuntimeError('Duplicate or missing canonical candidate tiles')
        result.update(indexReady=True, batchIds=ids, batchCount=len(ids))
    if compacted and not result['indexReady']:
        raise RuntimeError('Compacted source is missing its canonical index')
    batches_dir = checkpoint / 'batches'
    if batches_dir.exists():
        if batches_dir.is_symlink():
            raise RuntimeError('Tile batch directory cannot be a symlink')
        if not result['indexReady']:
            raise RuntimeError('Tile batches have no canonical index')
        for root in sorted(batches_dir.iterdir()):
            if not root.is_dir() or not re.fullmatch(r'\d{6}', root.name):
                raise RuntimeError('Unexpected tile batch directory')
            batch_id = int(root.name)
            validate_batch(root, index, batch_id)
            result['completedBatches'].append(batch_id)
    return result


def assemble_region(config, plan, checkpoint, work, site, base):
    if base != PUBLIC_BASE:
        raise RuntimeError('Unapproved public tiles base URL')
    checkpoint, site = Path(checkpoint), Path(site)
    status = validate_checkpoint(config, plan, checkpoint)
    if not status['indexReady'] or status['completedBatches'] != status['batchIds']:
        raise RuntimeError('Cannot finalize: missing tile batches')
    index = verify_receipt(checkpoint / 'index', plan['planId'], 'index')
    tile_hashes = {}
    tile_bytes = 0
    for batch_id in status['batchIds']:
        batch = validate_batch(checkpoint / 'batches' / format(batch_id, '06d'), index, batch_id)
        if set(tile_hashes).intersection(batch['tileHashes']):
            raise RuntimeError('Duplicate generated tiles across batches')
        tile_hashes.update(batch['tileHashes'])
        tile_bytes += sum(item['bytes'] for item in batch['files'].values())
    if not tile_hashes or len(tile_hashes) > legacy.MAX_TILE_COUNT or tile_bytes > MAX_CHECKPOINT_BYTES:
        raise RuntimeError('Region exceeds publication bounds')
    content_hashes = {key.removeprefix('tiles/'): value for key, value in tile_hashes.items()}
    version = hashlib.sha256(''.join(f'{key} {content_hashes[key]}\n' for key in sorted(content_hashes)).encode()).hexdigest()[:16]
    final = site / 'regions' / config['id'] / version
    if final.exists():
        raise RuntimeError('Refusing to overwrite an existing published version')
    with workspace(work) as temporary:
        temp = Path(temporary)
        staged = temp / 'commit'
        staged.mkdir()
        for batch_id in status['batchIds']:
            root = checkpoint / 'batches' / format(batch_id, '06d')
            batch = validate_batch(root, index, batch_id)
            for name in batch['tileHashes']:
                path = staged / safe_relative(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, path)
        merged = unpack_file(index['mergedSource'], checkpoint / 'index', temp / 'cycling-source.osm.pbf')
        source_package = write_source_package(merged, staged)
        sources = [verify_receipt(checkpoint / 'sources' / state, plan['planId'], 'source')['source'] for state in config['sourceStates']]
        stamp = now_utc().strftime('%Y%m%dT%H%M%SZ')
        provenance = {'sources': sources, 'sourcePlanId': plan['planId'], 'sourceDate': plan['sourceDate'],
                      'generatorCommit': os.environ.get('GITHUB_SHA', 'local'), 'generatorRunId': os.environ.get('GITHUB_RUN_ID'),
                      'bounds': config['bounds'], 'categories': legacy.KINDS, 'generatedAt': stamp,
                      'sourceLicense': 'https://opendatacommons.org/licenses/odbl/1-0/', 'sourceExtractSha256': sha256(merged),
                      'sourceExtractBytes': merged.stat().st_size, 'sourceFormat': SOURCE_FORMAT,
                      'sourcePackageSha256': source_package['descriptorSha256']}
        write_json(staged / 'provenance.json', provenance)
        write_json(staged / 'tile-sha256.json', tile_hashes)
        report = {'generated_utc': now_utc().isoformat(), 'bounds': config['bounds'],
                  'features_by_category': index['featuresByCategory'], 'nonempty_tiles': len(tile_hashes),
                  'tile_bytes': tile_bytes, 'tile_batches': len(status['batchIds']), 'source_plan': plan['planId']}
        write_json(staged / 'report.json', report)
        prefix = f'{base}/regions/{config["id"]}/{version}'
        entry = {key: config[key] for key in ['id', 'name', 'bounds', 'minzoom', 'maxzoom']}
        entry.update({'sourceLayer': 'cycleinfra', 'tiles': [prefix + '/tiles/{z}/{x}/{y}.pbf'], 'version': version,
                      'generatedAt': stamp, 'sourceUrl': prefix + '/provenance.json', 'sourceTimestamp': sources[0]['osmReplicationTimestamp'],
                      'categories': legacy.KINDS, 'tileCount': len(tile_hashes), 'tileBytes': tile_bytes,
                      'sha256Manifest': prefix + '/tile-sha256.json', 'sourceDownload': prefix + '/cycling-source.json',
                      'sourceFormat': SOURCE_FORMAT, 'sourceArchiveSha256': source_package['descriptorSha256']})
        write_json(staged / 'tilejson.json', {'tilejson': '3.0.0', 'scheme': 'xyz', 'name': config['name'],
                   'bounds': config['bounds'], 'minzoom': config['minzoom'], 'maxzoom': config['maxzoom'],
                   'tiles': entry['tiles'], 'attribution': legacy.ATTRIBUTION,
                   'vector_layers': [{'id': 'cycleinfra', 'fields': {'kind': 'String', 'name': 'String', 'surface': 'String'}}]})
        file_map(staged)
        commit_stage(staged, final)
    return entry
