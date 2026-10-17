"""Metadata-only storage advisory before tool installation or bulk restore.

Never authorizes publication: the restored baseline and final upload are checked
again. Expiry times are retry hints, never evidence that storage is released.
No source payload, snapshot shard, credential or mutation is needed here.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import PurePosixPath
import re
import time
from urllib.request import Request, HTTPRedirectHandler, build_opener

from actions_budget import BudgetUnavailable, _pages, _timestamp, artifact_repositories, api_read_headers
from artifact_budget import (CAP_BYTES, TAR_BLOCK_BYTES, TAR_RECORD_BYTES,
                             UPLOAD_OVERHEAD_BYTES, _round_up)
from rollout_snapshot import (BASE, DESCRIPTOR, MAX_FILE_BYTES, MAX_MANIFEST_BYTES,
                              _descriptor, publication_revision)

AUDIT_SECONDS = 16
MAX_REQUESTS = 12
API_RESPONSE_BYTES = 4 * 1024 * 1024
SEED_BASE = 'https://apfullai.github.io/rides-tiles'
TARGET_BASE = 'https://apfullai.github.io/ride-tiles'
SEED_OVERHEAD_BYTES = 1_000_000


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BudgetUnavailable('Capacity metadata redirects are not allowed')


def fetch_bytes(url, limit):
    if not (url in tuple(origin + '/' + name for origin in (SEED_BASE, TARGET_BASE)
                             for name in ('manifest.json', 'snapshot.json'))
            or re.fullmatch(r'https://api\.github\.com/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/artifacts\?per_page=100&page=[1-9][0-9]*', url)):
        raise BudgetUnavailable('Unexpected capacity metadata destination')
    headers = {'User-Agent': 'Ride-Early-Capacity/1', 'Cache-Control': 'no-cache'}
    if url.startswith('https://api.github.com/'):
        headers.update(api_read_headers(url))
    request = Request(url, headers=headers)
    with build_opener(NoRedirects()).open(request, timeout=3) as response:
        if response.status != 200 or response.geturl() != url:
            raise BudgetUnavailable('Unexpected capacity metadata response or redirect')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise BudgetUnavailable('Capacity metadata exceeds size limit')
    return data


def metadata_allowance(manifest, raw):
    """Bound exactly the files/parent directories produced by snapshot restore."""
    conf = _descriptor(raw, manifest)
    files = {name: entry['size'] for name, entry in conf['files'].items()}
    files[DESCRIPTOR] = len(raw)
    files.update({shard['path']: shard['size'] for shard in conf['shards']})
    from exact_tar_size import allowance
    return allowance(files)


def retry_hint(artifacts, candidate, now):
    """Earliest potential fit, conditional on the API marking artifacts expired."""
    if candidate > CAP_BYTES:
        return None, 'Baseline alone exceeds artifact allowance; expiry cannot resolve it'
    used = sum(item['size_in_bytes'] for item in artifacts)
    expirations = []
    unknown = False
    for item in artifacts:
        try:
            expiry = _timestamp(item.get('expires_at'), 'artifact expiry')
        except ValueError:
            unknown = True
            continue
        if expiry <= now:
            unknown = True  # Do not pretend a past timestamp freed these bytes.
            continue
        expirations.append((expiry, item['size_in_bytes']))
    for expiry, size in sorted(expirations):
        used -= size
        if used + candidate <= CAP_BYTES:
            return expiry.isoformat(), 'Recheck at or after this time; API-confirmed expiry is required'
    return None, ('Await API-confirmed expiry or valid expiry metadata' if unknown
                  else 'No known expiry can admit this baseline')


def check_capacity(repository, *, fetch=fetch_bytes, now=None, monotonic=time.monotonic,
                   base_url=BASE, seed_migration=False):
    report = {'allowed': False, 'complete': False, 'capBytes': CAP_BYTES,
              'artifactBytes': None, 'candidateBytes': None, 'projectedBytes': None,
              'retryAt': None, 'reason': 'Capacity metadata unavailable',
              'scope': 'baseline-only; full restore and final upload checks remain mandatory'}
    try:
        if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise BudgetUnavailable('Invalid repository')
        if base_url not in (SEED_BASE, TARGET_BASE):
            raise BudgetUnavailable('Unapproved capacity metadata origin')
        if seed_migration and (repository != 'apfullai/ride-tiles' or base_url != SEED_BASE):
            raise BudgetUnavailable('Seed capacity mode requires the approved migration source and target')
        now = now or dt.datetime.now(dt.timezone.utc)
        if now.tzinfo is None:
            raise BudgetUnavailable('Retry clock requires a timezone')
        started = monotonic()
        calls = 0
        def read(url, limit):
            nonlocal calls
            calls += 1
            if calls > MAX_REQUESTS or monotonic() - started >= AUDIT_SECONDS:
                raise BudgetUnavailable('Capacity audit request/time limit reached')
            raw = fetch(url, limit)
            if not isinstance(raw, bytes) or len(raw) > limit:
                raise BudgetUnavailable('Invalid capacity metadata bytes')
            if monotonic() - started >= AUDIT_SECONDS:
                raise BudgetUnavailable('Capacity audit time limit reached')
            return raw
        manifest = read(base_url + '/manifest.json', MAX_MANIFEST_BYTES)
        descriptor = read(base_url + '/snapshot.json', MAX_FILE_BYTES)
        report.update(metadata_allowance(manifest, descriptor))
        report['metadataOrigin'] = base_url
        if seed_migration:
            report['baselineCandidateBytes'] = report['candidateBytes']
            report['seedOverheadBytes'] = SEED_OVERHEAD_BYTES
            report['candidateBytes'] += SEED_OVERHEAD_BYTES
        report['publicationRevision'] = publication_revision(manifest, descriptor)
        repositories = artifact_repositories(repository, now)
        artifacts = []
        for source_repository in repositories:
            artifacts.extend(_pages(lambda url: json.loads(read(url, API_RESPONSE_BYTES)),
                f'https://api.github.com/repos/{source_repository}/actions/artifacts', 'artifacts'))
        report['artifactRepositories'] = repositories
        active = []
        for artifact in artifacts:
            if type(artifact.get('size_in_bytes')) is not int or artifact['size_in_bytes'] < 0 or type(artifact.get('expired')) is not bool:
                raise BudgetUnavailable('Malformed artifact size or expiration state')
            if not artifact['expired']:
                active.append(artifact)
        used = sum(item['size_in_bytes'] for item in active)
        projected = used + report['candidateBytes']
        allowed = projected <= CAP_BYTES
        report.update(allowed=allowed, complete=True, artifactBytes=used,
                      unexpiredArtifactCount=len(active), projectedBytes=projected,
                      reason='Baseline fits; recheck restored and final candidates' if allowed
                      else 'Artifact storage blocks baseline continuation before setup or restore')
        if not allowed:
            report['retryAt'], report['retryNote'] = retry_hint(active, report['candidateBytes'], now)
    except Exception as error:
        report.update(allowed=False, complete=False, reason='Capacity preflight unavailable: '+type(error).__name__+': '+str(error)[:300])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-migration', action='store_true',
                        help='Read the fixed old origin and reserve seed metadata overhead')
    args = parser.parse_args(argv)
    if args.seed_migration:
        report = check_capacity(os.environ.get('GITHUB_REPOSITORY', ''),
                                base_url=SEED_BASE, seed_migration=True)
    else:
        report = check_capacity(os.environ.get('GITHUB_REPOSITORY', ''))
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a') as stream:
            stream.write('allowed='+str(report['allowed']).lower()+'\n')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write('## Early artifact capacity\n\n'+report['reason']+'\n\n')
            if report['retryAt']:
                stream.write('Earliest potential retry: '+report['retryAt']+'. API-confirmed expiry is required.\n\n')
            elif not report['allowed']:
                stream.write('Retry time is unknown; recheck metadata without treating timestamps as released storage.\n\n')
            stream.write('```json\n'+json.dumps(report, indent=2, sort_keys=True)+'\n```\n')
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    main()
