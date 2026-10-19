#!/usr/bin/env python3
"""Bounded, Linux-only LOCAL candidates from an existing published generation.

This wrapper never publishes, starts a generation, acquires sources, runs Actions
admission, or creates a CI verification receipt. Production modules stay intact.
A committed candidate is immutable; only an atomic pointer makes a new one current.
"""
import argparse
import contextlib
import copy
import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import tempfile
import time
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'publishing'))
import resumable_rollout as rollout
import rollout_snapshot as snapshot
import verified_code

SCHEMA = 1
MAX_SECONDS = 900
MIN_FREE_BYTES = 250_000_000
MAX_DISK_GROWTH = 2_000_000_000
OWNER = '.vm-workspace.json'
METADATA_LIMIT = 100_000
REVISION = re.compile(r'[0-9a-f]{32}')
ALLOWED_STAGES = {'index', 'tiles', 'finalize'}


class CandidateError(RuntimeError):
    pass


class Interrupted(BaseException):
    pass


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > METADATA_LIMIT:
        raise CandidateError('Missing, unsafe, or oversized local metadata: ' + path.name)
    return snapshot._json(path.read_bytes())


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path, value):
    data = canonical(value)
    if len(data) > METADATA_LIMIT:
        raise CandidateError('Local metadata exceeds its bound')
    fd, name = tempfile.mkstemp(prefix='.atomic-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_tree(root):
    # Child completes all durability work before the supervisor commits its pointer.
    for directory, _, files in os.walk(root, topdown=False):
        for name in files:
            fd = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        sync_directory(directory)


def tree_digest(site):
    # Includes snapshot.json AND all transport shards, unlike inventory().
    return digest(canonical(snapshot._records(site, include_metadata=True)))


def wrapper_fingerprint():
    return digest(Path(__file__).read_bytes())


def runtime_fingerprint():
    sys.path.insert(0, str(ROOT / 'tools'))
    from resumable_region import runtime_fingerprint as identify
    return identify()


def check_environment():
    if sys.platform != 'linux' or not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise CandidateError('Linux with pidfd support is required for safe child cleanup')
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        raise CandidateError('This candidate-only command must run outside GitHub Actions')


def workspace_path(value):
    path = Path(value).expanduser().absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise CandidateError('Workspace path must not contain symlinks')
    path = path.resolve()
    if path == ROOT or path.is_relative_to(ROOT) or ROOT.is_relative_to(path):
        raise CandidateError('Workspace must be outside and separate from the repository')
    return path


@contextlib.contextmanager
def workspace_lock(workspace, create=False):
    if create:
        workspace.mkdir(parents=True, exist_ok=True)
    if not workspace.is_dir():
        raise CandidateError('Workspace does not exist; restore first')
    fd = os.open(workspace / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CandidateError('Workspace lock is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CandidateError('Another command owns this workspace') from exc
        marker = workspace / OWNER
        if not marker.exists():
            if not create or any(p.name != '.lock' for p in workspace.iterdir()):
                raise CandidateError('Workspace must be new or empty; missing ownership marker')
            write_atomic(marker, {'schemaVersion': SCHEMA, 'kind': 'ride-tiles-vm-candidate-workspace',
                                  'workspaceId': uuid.uuid4().hex})
        owner = read_json(marker)
        if (not isinstance(owner, dict) or set(owner) != {'schemaVersion', 'kind', 'workspaceId'}
                or owner['schemaVersion'] != SCHEMA or owner['kind'] != 'ride-tiles-vm-candidate-workspace'
                or not isinstance(owner['workspaceId'], str) or not REVISION.fullmatch(owner['workspaceId'])):
            raise CandidateError('Invalid local workspace ownership marker')
        yield
    finally:
        os.close(fd)


def reference(value):
    if (not isinstance(value, dict) or set(value) != {'revision', 'metadataSha256'}
            or not isinstance(value['revision'], str) or not REVISION.fullmatch(value['revision'])):
        raise CandidateError('Invalid local revision reference')
    snapshot._hash(value['metadataSha256'])
    return value


def read_pointer(workspace):
    value = read_json(workspace / 'current.json')
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'current', 'baseline', 'previous'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != SCHEMA):
        raise CandidateError('Invalid local resume pointer')
    reference(value['current'])
    reference(value['baseline'])
    if value['previous'] is not None:
        reference(value['previous'])
    return value


def read_revision(workspace, ref):
    ref = reference(ref)
    root = workspace / 'revisions' / ref['revision']
    if (workspace / 'revisions').is_symlink() or root.is_symlink() or not root.is_dir():
        raise CandidateError('Unsafe local revision directory')
    path = root / 'metadata.json'
    meta = read_json(path)
    if digest(path.read_bytes()) != ref['metadataSha256']:
        raise CandidateError('Local metadata hash differs')
    if (not isinstance(meta, dict) or set(meta) != {'schemaVersion', 'candidateOnly', 'revision', 'context',
            'complete', 'siteSha256', 'parent', 'createdAt'} or meta['schemaVersion'] != SCHEMA
            or meta['candidateOnly'] is not True or meta['revision'] != ref['revision']
            or type(meta['complete']) is not bool):
        raise CandidateError('Invalid local candidate metadata')
    if meta['parent'] is not None:
        reference(meta['parent'])
    rollout.utc(meta['createdAt'])
    if tree_digest(root / 'site') != meta['siteSha256']:
        raise CandidateError('Local candidate tree hash differs')
    return meta, root / 'site'


def check_code_runtime(context):
    if context['wrapperSha256'] != wrapper_fingerprint():
        raise CandidateError('Continuation wrapper changed; restore into a new workspace')
    if verified_code.current_fingerprint(ROOT) != context['codeFingerprint']:
        raise CandidateError('Production code fingerprint differs from the expected generation')
    if runtime_fingerprint() != context['runtime']:
        raise CandidateError('Pinned generator runtime differs; refusing mixed generation')


def check_checkpoint(site, configs, context, complete=False):
    snapshot.validate_site(site)
    checkpoint = rollout.load_checkpoint(site, configs, expected_fingerprint=context['codeFingerprint'])
    if complete:
        if checkpoint is not None:
            raise CandidateError('Completed local candidate still has an active checkpoint')
        status = snapshot._json((site / 'status.json').read_bytes())
        if (status.get('active') is not False or status.get('needsAttention')
                or (status.get('lastCompleted') or {}).get('id') != context['config']['id']):
            raise CandidateError('Completed local candidate has no matching completion status')
        return None
    if checkpoint is None:
        raise CandidateError('Expected active generation is absent; new work is forbidden')
    if (checkpoint['generationId'] != context['generationId']
            or checkpoint['config'] != context['config']
            or checkpoint['sourcePlan'] != context['sourcePlan']):
        raise CandidateError('Generation, configuration, or source pins differ')
    if (checkpoint.get('needsAttention') or checkpoint.get('retryAfter') is not None
            or checkpoint.get('lastError') is not None or checkpoint.get('failureCount', 0) != 0):
        raise CandidateError('Active checkpoint needs attention or has failure/backoff state')
    if (checkpoint['progress']['completedStates'] != checkpoint['config']['sourceStates']
            or checkpoint['nextStage']['kind'] not in ALLOWED_STAGES):
        raise CandidateError('Every configured source must be acquired; only index/tiles/finalize are allowed')
    return checkpoint


def load_local(workspace, configs):
    pointer = read_pointer(workspace)
    baseline, baseline_site = read_revision(workspace, pointer['baseline'])
    if pointer['current'] == pointer['baseline']:
        current, site = baseline, baseline_site
    else:
        current, site = read_revision(workspace, pointer['current'])
    context = baseline['context']
    if (not isinstance(context, dict) or set(context) != {'generationId', 'codeFingerprint', 'wrapperSha256',
            'publicationRevision', 'config', 'sourcePlan', 'runtime'} or baseline['complete']
            or baseline['parent'] is not None or current['context'] != context):
        raise CandidateError('Immutable live baseline or source pins differ')
    for key in ('generationId', 'codeFingerprint', 'wrapperSha256', 'publicationRevision'):
        snapshot._hash(context[key])
    if (not isinstance(context['sourcePlan'], dict)
            or context['sourcePlan'].get('runtime') != context['runtime']):
        raise CandidateError('Baseline runtime does not match its source plan')
    if current['parent'] != pointer['previous']:
        raise CandidateError('Local previous checkpoint reference differs')
    # The retained previous revision is verified too; a corrupt rollback copy is
    # never quietly ignored while further local work is accepted.
    if pointer['previous'] is not None and pointer['previous'] not in (pointer['current'], pointer['baseline']):
        previous, _ = read_revision(workspace, pointer['previous'])
        if previous['context'] != context:
            raise CandidateError('Previous checkpoint baseline differs')
    check_code_runtime(context)
    check_checkpoint(baseline_site, configs, context)
    checkpoint = check_checkpoint(site, configs, context, complete=current['complete'])
    return pointer, current, site, checkpoint


def describe(meta, site, checkpoint, *, changed=False, reason=None):
    progress = checkpoint['progress'] if checkpoint else {}
    return {'candidateOnly': True, 'changed': changed, 'complete': meta['complete'],
            'site': str(site), 'generationId': meta['context']['generationId'],
            'baselinePublicationRevision': meta['context']['publicationRevision'],
            'nextStage': checkpoint['nextStage'] if checkpoint else None,
            'completedSources': len(progress.get('completedStates', [])),
            'completedBatches': len(progress.get('completedBatches', [])),
            'batchCount': progress.get('batchCount', 0), 'reason': reason}


def make_revision(version, context, complete, parent):
    site = version / 'site'
    meta = {'schemaVersion': SCHEMA, 'candidateOnly': True, 'revision': version.name,
            'context': copy.deepcopy(context), 'complete': complete, 'parent': parent,
            'siteSha256': tree_digest(site), 'createdAt': dt.datetime.now(dt.timezone.utc).isoformat()}
    write_atomic(version / 'metadata.json', meta)
    sync_tree(version)
    return meta


def local_verifier(context):
    def verify(root, site, timeout):
        check_code_runtime(context)
        receipt, mode = rollout.verify_code(root, site, timeout=timeout)
        if receipt.get('fingerprint') != context['codeFingerprint']:
            raise CandidateError('Production verification returned a different fingerprint')
        check_code_runtime(context)
        return receipt, mode
    return verify


def execute_pinned(context):
    def execute(request, checkpoint, work, seconds):
        if (request.get('stage', {}).get('kind') not in ALLOWED_STAGES
                or request.get('config') != context['config']
                or request.get('sourcePlan') != context['sourcePlan']):
            raise CandidateError('Forbidden stage or changed source pins at execution boundary')
        check_code_runtime(context)
        return rollout.subprocess_stage(request, checkpoint, work, seconds)
    return execute


def operation(command, workspace, transaction, *, seconds, expected=None):
    """Runs only in the supervised child; never changes the current pointer."""
    deadline = time.monotonic() + seconds
    configs = rollout.load_configs(ROOT / 'config' / 'rollout-regions.json')
    if command == 'restore':
        if (workspace / 'current.json').exists() or (workspace / 'current.json').is_symlink():
            raise CandidateError('Workspace is already initialized; use continue or a new workspace')
        code = expected['fingerprint']
        generation = expected['generation']
        snapshot._hash(code)
        snapshot._hash(generation)
        if verified_code.current_fingerprint(ROOT) != code:
            raise CandidateError('Production code fingerprint differs before restore')
        config = next((c for c in configs if c['id'] == expected['region']), None)
        if config is None:
            raise CandidateError('Expected region is not configured')
        if shutil.disk_usage(workspace).free < 6 * snapshot.MAX_SITE_BYTES:
            raise CandidateError('Need at least 1.5 GB free temporary workspace space')
        version = transaction / uuid.uuid4().hex
        version.mkdir()
        site = version / 'site'
        receipt = {}
        snapshot.restore_published(site, receipt=receipt)
        if receipt.get('bootstrap') is not False:
            raise CandidateError('A published checkpoint snapshot is required; bootstrap is forbidden')
        if snapshot.publication_revision((site / 'manifest.json').read_bytes(),
                (site / 'snapshot.json').read_bytes()) != receipt['publicationRevision']:
            raise CandidateError('Restored live publication revision differs')
        checkpoint = rollout.load_checkpoint(site, configs, expected_fingerprint=code)
        if checkpoint is None or not isinstance(checkpoint.get('sourcePlan'), dict):
            raise CandidateError('Expected pinned active generation is absent')
        context = {'generationId': generation, 'codeFingerprint': code,
                   'wrapperSha256': wrapper_fingerprint(), 'publicationRevision': receipt['publicationRevision'],
                   'config': config, 'sourcePlan': checkpoint['sourcePlan'], 'runtime': checkpoint['sourcePlan'].get('runtime')}
        check_code_runtime(context)
        checkpoint = check_checkpoint(site, configs, context)
        snapshot.assert_publication_unchanged(context['publicationRevision'])
        meta = make_revision(version, context, False, None)
        snapshot.assert_publication_unchanged(context['publicationRevision'])
        return {'version': version.name, 'previousPointer': None,
                'summary': describe(meta, site, checkpoint, changed=True, reason='Restored local baseline')}

    pointer, current, site, checkpoint = load_local(workspace, configs)
    if command == 'status':
        return {'version': None, 'summary': describe(current, site, checkpoint)}
    if command != 'continue':
        raise CandidateError('Unsupported local command')
    context = current['context']
    snapshot.assert_publication_unchanged(context['publicationRevision'])
    if current['complete']:
        return {'version': None, 'summary': describe(current, site, checkpoint, reason='Local candidate already complete')}
    if shutil.disk_usage(workspace).free < 6 * snapshot.MAX_SITE_BYTES:
        raise CandidateError('Need at least 1.5 GB free temporary workspace space')
    version = transaction / uuid.uuid4().hex
    version.mkdir()
    candidate = version / 'site'
    shutil.copytree(site, candidate, symlinks=True)
    result = rollout.advance(candidate, configs, allow_new=False, start_region=None,
                deadline=deadline, execute=execute_pinned(context), verifier=local_verifier(context))
    snapshot.assert_publication_unchanged(context['publicationRevision'])
    if (result.get('needsAttention') or result.get('retryAfter') is not None
            or result.get('failureCount', 0) != 0):
        raise CandidateError('Stage failed; previous local checkpoint retained: ' + str(result.get('reason', 'unknown error')))
    after = check_checkpoint(candidate, configs, context, complete=result.get('active') is False)
    if not result.get('changed') or (after is not None and after['progress'] == checkpoint['progress']):
        return {'version': None, 'summary': describe(current, site, checkpoint, reason=result.get('reason') or 'No completed stage progress')}
    if after is None and checkpoint['nextStage']['kind'] != 'finalize':
        raise CandidateError('Only the finalize stage may complete a local candidate')
    meta = make_revision(version, context, after is None, pointer['current'])
    check_code_runtime(context)
    snapshot.assert_publication_unchanged(context['publicationRevision'])
    return {'version': version.name, 'previousPointer': pointer,
            'summary': describe(meta, candidate, after, changed=True)}


def proc_table():
    result = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            # comm can contain spaces and parentheses; fields after its last ')'
            # start at field 3 (state), so starttime field 22 is index 19.
            raw = (path / 'stat').read_text()
            fields = raw[raw.rfind(')') + 2:].split()
            result[int(path.name)] = (int(fields[1]), int(fields[19]))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return result


def owned_processes(worker, excluded):
    table = proc_table()
    me = os.getpid()
    # Subreaper adoption survives worker death and separate setsid process groups.
    roots = {pid for pid, (ppid, start) in table.items()
             if ppid == me and (pid, start) not in excluded}
    worker_pid, worker_start = worker
    if worker_pid is not None and worker_start is not None and table.get(worker_pid, (None, None))[1] == worker_start:
        roots.add(worker_pid)
    owned = set(roots)
    while True:
        added = {pid for pid, (ppid, _) in table.items() if ppid in owned} - owned
        if not added:
            break
        owned.update(added)
    return {pid: table[pid][1] for pid in owned}


def kill_owned(worker, excluded):
    # pidfds make signals safe even if a process exits and its PID is reused.
    for pid, start in owned_processes(worker, excluded).items():
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            if proc_table().get(pid, (None, None))[1] == start:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)


def reap_owned(worker, excluded, timeout=10):
    end = time.monotonic() + timeout
    while True:
        owned = owned_processes(worker, excluded)
        if not owned:
            return
        kill_owned(worker, excluded)
        for pid in owned:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if time.monotonic() >= end:
            raise CandidateError('Could not reap all owned stage children after cancellation')
        time.sleep(0.02)


def supervised(callback, transaction, seconds):
    """A global worker watchdog; cleanup gets one additional ten-second bound."""
    if threading.active_count() != 1 or any(ppid == os.getpid() for ppid, _ in proc_table().values()):
        raise CandidateError('Supervisor requires a single-threaded, child-free CLI process')
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    have_previous = False
    handlers = {}
    worker_identity = (None, None)
    disk_budget = None
    parent = os.getpid()
    started = time.monotonic()
    cancelled = None
    def stop(signum, frame):
        nonlocal cancelled
        if os.getpid() != parent:
            raise Interrupted('Local VM worker interrupted')
        # Do not asynchronously raise inside fork setup or descendant reaping.
        cancelled = signum
    try:
        if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
            raise CandidateError('Cannot inspect Linux child subreaper')
        have_previous = True
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise CandidateError('Cannot enable Linux child subreaper')
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, stop)
        worker = os.fork()
        if worker == 0:
            try:
                os.setsid()
                # Best-effort worker cancellation if the supervisor disappears.
                # SIGKILL/VM death needs a cgroup for guaranteed child cleanup.
                if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
                    raise Interrupted('Supervisor disappeared')
                value = callback()
                write_atomic(transaction / 'result.json', value)
            except BaseException as exc:
                try:
                    write_atomic(transaction / 'error.json', {'error': type(exc).__name__, 'message': str(exc)[:2000]})
                finally:
                    os._exit(1)
            os._exit(0)
        # Already under finally coverage: cancellation here still discovers the
        # new direct child even before its PID birth identity has been read.
        worker_identity = (worker, proc_table().get(worker, (None, None))[1])
        while True:
            if cancelled is not None:
                raise Interrupted('Local VM command interrupted')
            pid, status = os.waitpid(worker, os.WNOHANG)
            if pid:
                if status != 0:
                    error = read_json(transaction / 'error.json') if (transaction / 'error.json').exists() else {}
                    raise CandidateError('Local worker failed: ' + error.get('message', f'exit status {status}'))
                break
            admission = transaction / 'disk-admission.json'
            if disk_budget is None and admission.exists():
                disk_budget = read_json(admission)['initialFree']
            if disk_budget is not None:
                free = shutil.disk_usage(transaction).free
                if free < MIN_FREE_BYTES or disk_budget - free > MAX_DISK_GROWTH:
                    raise CandidateError('Local disk watchdog stopped work; reserve or temporary-growth bound exceeded')
            if time.monotonic() - started >= seconds:
                raise CandidateError(f'Global {seconds:g}-second watchdog expired; last committed checkpoint remains recoverable; inspect status')
            time.sleep(min(0.05, seconds / 10))
        return read_json(transaction / 'result.json')
    finally:
        # Exactly one cleanup grace period, on success and every failure boundary.
        # Child-free admission means all newly adopted children belong to us.
        try:
            reap_owned(worker_identity, set())
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            if have_previous:
                libc.prctl(36, previous.value, 0, 0, 0)


def commit(workspace, transaction, result):
    name = result.get('version')
    if name is None:
        return result['summary']
    if not isinstance(name, str) or not REVISION.fullmatch(name):
        raise CandidateError('Invalid completed transaction revision')
    version = transaction / name
    meta = read_json(version / 'metadata.json')
    ref = {'revision': name, 'metadataSha256': digest((version / 'metadata.json').read_bytes())}
    old = result['previousPointer']
    if old is None:
        if (workspace / 'current.json').exists() or (workspace / 'current.json').is_symlink():
            raise CandidateError('Resume pointer appeared during restore')
    elif read_pointer(workspace) != old:
        raise CandidateError('Resume pointer changed during local work')
    revisions = workspace / 'revisions'
    revisions.mkdir(exist_ok=True)
    if revisions.is_symlink():
        raise CandidateError('Unsafe revisions directory')
    version.rename(revisions / name)
    sync_directory(revisions)
    pointer = {'schemaVersion': SCHEMA, 'current': ref, 'baseline': old['baseline'] if old else ref,
               'previous': old['current'] if old else None}
    write_atomic(workspace / 'current.json', pointer)
    result['summary']['site'] = str(revisions / name / 'site')
    return result['summary']


def prune(workspace, exclude=()):
    """Keep baseline, current, previous; never remove a pointed-to revision."""
    pointer = read_pointer(workspace) if (workspace / 'current.json').exists() else None
    keep = {pointer[key]['revision'] for key in ('baseline', 'current', 'previous')
            if pointer and pointer[key] is not None}
    revisions = workspace / 'revisions'
    if revisions.is_symlink():
        raise CandidateError('Unsafe revisions directory')
    if revisions.exists():
        for path in revisions.iterdir():
            if REVISION.fullmatch(path.name) and path.name not in keep:
                if path.is_symlink() or not path.is_dir():
                    raise CandidateError('Unsafe stale revision')
                shutil.rmtree(path)
    for path in workspace.glob('.transaction-*'):
        if re.fullmatch(r'\.transaction-[0-9a-f]{32}', path.name) and path.name not in exclude:
            if path.is_symlink() or not path.is_dir():
                raise CandidateError('Unsafe stale transaction')
            shutil.rmtree(path)


def run(command, workspace, seconds=900, expected=None):
    check_environment()
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= MAX_SECONDS:
        raise CandidateError('Command budget must be between 1 and 900 seconds')
    workspace = workspace_path(workspace)
    with workspace_lock(workspace, create=command == 'restore'):
        transaction = workspace / ('.transaction-' + uuid.uuid4().hex)
        transaction.mkdir()
        def work():
            # Recover abandoned owned work before the next free-space admission.
            # A corrupt pointer or committed tree never authorizes any cleanup.
            if (workspace / 'current.json').exists():
                load_local(workspace, rollout.load_configs(ROOT / 'config' / 'rollout-regions.json'))
            elif command != 'restore':
                raise CandidateError('Workspace has no committed local baseline; restore first')
            if command != 'status':
                prune(workspace, exclude={transaction.name})
            if command != 'status':
                free = shutil.disk_usage(workspace).free
                if free < 6 * snapshot.MAX_SITE_BYTES:
                    raise CandidateError('Need at least 1.5 GB free temporary workspace space')
                write_atomic(transaction / 'disk-admission.json', {'initialFree': free})
            result = operation(command, workspace, transaction, seconds=seconds, expected=expected)
            summary = commit(workspace, transaction, result)
            if command != 'status':
                prune(workspace, exclude={transaction.name})
            # Bulk cleanup and fsync/pointer promotion remain inside the global
            # watchdog. A crash after pointer replacement leaves a complete new
            # revision plus the prior one; a crash before it leaves the old one.
            for path in transaction.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            return summary
        # Failure intentionally retains this owned transaction for bounded
        # cleanup on the next command, rather than unbounded parent rmtree().
        summary = supervised(work, transaction, seconds)
        result_file = transaction / 'result.json'
        if result_file.exists():
            result_file.unlink()
        transaction.rmdir()
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('restore', 'continue', 'status'):
        item = commands.add_parser(name)
        item.add_argument('--workspace', required=True, type=Path)
        item.add_argument('--seconds', type=int, default=900, help='Worker watchdog including commit/cleanup, maximum 900 seconds')
        if name == 'restore':
            item.add_argument('--region', required=True)
            item.add_argument('--generation', required=True)
            item.add_argument('--fingerprint', required=True)
    args = parser.parse_args(argv)
    expected = {'region': args.region, 'generation': args.generation, 'fingerprint': args.fingerprint} if args.command == 'restore' else None
    try:
        print(json.dumps(run(args.command, args.workspace, args.seconds, expected), sort_keys=True))
    except (Exception, Interrupted) as exc:
        print(json.dumps({'candidateOnly': True, 'error': str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
