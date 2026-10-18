"""Bounded, fail-closed source-attempt history in existing Actions job logs.

No artifact, cache, write API, token grant, or source download is used. The
caller supplies a verified live checkpoint and the exact successful publisher
run that established it. Job summaries are a human-readable mirror only; the
public plain-text job-log API is the machine-readable source of truth.

A deletion, expired log, incomplete attempt, changed code identity, or audit
limit never resets a failure count. It prevents further source work.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = 'apfullai/ride-tiles'
WORKFLOW = '.github/workflows/rollout.yml'
BUILD_JOB = 'build'
STAGE_STEP = 'Verify code and advance one durable city checkpoint'
START_MARKER = 'RIDE_TILES_ATTEMPT_START_V1 '
RECEIPT_MARKER = 'RIDE_TILES_ATTEMPT_RECEIPT_V1 '
BACKOFF = timedelta(hours=20)
FAILURE_LIMIT = 3
MAX_REQUESTS = 30
MAX_SECONDS = 25
MAX_ITEMS = 1000
MAX_ATTEMPTS = 4
MAX_JSON_BYTES = 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 8192
PER_PAGE = 100
UTC = timezone.utc
HEX64 = re.compile(r'[0-9a-f]{64}')
HEX40 = re.compile(r'[0-9a-f]{40}')
LOG_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ')
IDENTITY_FIELDS = ('repository', 'workflow', 'publicationRevision', 'generationId',
                   'sourcePlanSha256', 'codeFingerprint', 'stage')


class HistoryUnavailable(ValueError):
    """Evidence is insufficient to permit another source attempt."""


def _require(condition, reason):
    if not condition:
        raise HistoryUnavailable(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def source_plan_hash(plan):
    """Bind the full already-validated plan, including pins and expiry."""
    return hashlib.sha256(canonical(plan).encode()).hexdigest()


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, 'duplicate receipt JSON field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          HistoryUnavailable('non-finite JSON value')))


def _stamp(value):
    _require(isinstance(value, str) and len(value) <= 40, 'invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(parsed.tzinfo is not None, 'timestamp lacks timezone')
    return parsed.astimezone(UTC)


def _positive(value):
    _require(type(value) is int and value > 0, 'invalid run or attempt id')
    return value


def _hash(value, expression=HEX64):
    _require(isinstance(value, str) and expression.fullmatch(value) is not None,
             'invalid identity hash')
    return value


def _stage(value):
    _require(isinstance(value, dict), 'invalid stage')
    kind = value.get('kind')
    expected = {'kind'}
    if kind == 'source':
        expected.add('state')
        _require(isinstance(value.get('state'), str) and
                 re.fullmatch('[a-z0-9-]{1,64}', value['state']), 'invalid source state')
    elif kind == 'tiles':
        expected.add('batchId')
        _require(type(value.get('batchId')) is int and 0 <= value['batchId'] < 100000,
                 'invalid tile batch')
    else:
        _require(kind in ('start', 'index', 'finalize'), 'unknown stage')
    _require(set(value) == expected, 'unexpected stage field')


def identity(baseline):
    value = {key: baseline[key] for key in IDENTITY_FIELDS}
    _require(value['repository'] == REPOSITORY and value['workflow'] == WORKFLOW,
             'unexpected repository or workflow')
    for key in ('publicationRevision', 'generationId', 'sourcePlanSha256', 'codeFingerprint'):
        _hash(value[key])
    _stage(value['stage'])
    return value


def validate_baseline(baseline, now):
    """No bootstrap reset: carry the publisher's persisted failure controls."""
    identity(baseline)
    _positive(baseline['baselineRunId'])
    _positive(baseline['baselineRunAttempt'])
    _hash(baseline['baselineCommit'], HEX40)
    updated = _stamp(baseline['checkpointUpdatedAt'])
    _require(updated <= now, 'checkpoint timestamp is in the future')
    count = baseline['failureCount']
    attention = baseline['needsAttention']
    _require(type(count) is int and 0 <= count <= FAILURE_LIMIT, 'invalid baseline failure count')
    _require(type(attention) is bool, 'invalid baseline attention flag')
    retry = baseline['retryAfter']
    _require(count < FAILURE_LIMIT or attention, 'baseline lost failure-limit attention')
    if retry is not None:
        _require(count > 0 and not attention and _stamp(retry) >= updated,
                 'invalid baseline retry time')
    elif count > 0:
        _require(attention, 'baseline failure lost its retry time')
    return updated


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BoundedReader:
    """Same existing read token for metadata; log redirects are anonymous.

    Redirect URLs are accepted only from the exact GitHub job-log endpoint,
    only to known GitHub Actions log hosts, and are never printed or persisted.
    Every network read and injected test response shares the audit limits.
    """
    def __init__(self, fetch_json=None, fetch_log=None, monotonic=time.monotonic):
        self.fetch_json = fetch_json
        self.fetch_log = fetch_log
        self.monotonic = monotonic
        self.deadline = monotonic() + MAX_SECONDS
        self.requests = 0
        self.bytes = 0

    def check(self):
        _require(self.monotonic() < self.deadline, 'attempt history time limit reached')

    def reserve(self):
        self.check()
        self.requests += 1
        _require(self.requests <= MAX_REQUESTS, 'attempt history request limit reached')

    def consume(self, raw, maximum):
        self.check()
        _require(isinstance(raw, bytes) and len(raw) <= maximum, 'attempt history response too large')
        self.bytes += len(raw)
        _require(self.bytes <= MAX_TOTAL_BYTES, 'attempt history byte limit reached')
        return raw

    def _body(self, response, maximum):
        _require(response.headers.get('Content-Encoding', 'identity') == 'identity',
                 'encoded log or API response is unsupported')
        chunks = []
        size = 0
        while True:
            self.check()
            chunk = response.read1(min(65536, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            _require(size <= maximum, 'attempt history response too large')
            chunks.append(chunk)
        return self.consume(b''.join(chunks), maximum)

    def _open(self, url, headers):
        self.reserve()
        return build_opener(_NoRedirects()).open(
            Request(url, headers={**headers, 'Accept-Encoding': 'identity'}),
            timeout=min(3, max(.05, self.deadline - self.monotonic())))

    def json(self, url):
        if self.fetch_json is not None:
            self.reserve()
            value = self.fetch_json(url)
            self.consume(canonical(value).encode(), MAX_JSON_BYTES)
            return value
        from actions_budget import api_read_headers
        with self._open(url, api_read_headers(url)) as response:
            _require(response.status == 200 and response.geturl() == url, 'unexpected metadata response')
            return _json(self._body(response, MAX_JSON_BYTES))

    def log(self, job_id):
        _positive(job_id)
        if self.fetch_log is not None:
            self.reserve()
            self.reserve()  # Conservatively budget the download redirect too.
            return self.consume(self.fetch_log(job_id), MAX_LOG_BYTES)
        url = f'https://api.github.com/repos/{REPOSITORY}/actions/jobs/{job_id}/logs'
        # Do not call api_read_headers here: the existing contents-only token
        # neither grants nor needs actions:read for public anonymous job logs.
        headers = {'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'ride-tiles-attempt-history'}
        try:
            response = self._open(url, headers)
        except HTTPError as error:
            try:
                _require(error.code == 302, 'public job log is unavailable')
                location = error.headers.get('Location', '')
            finally:
                error.close()
        else:
            response.close()
            raise HistoryUnavailable('job-log endpoint did not return its expected redirect')
        parsed = urlsplit(location)
        _require(parsed.scheme == 'https' and parsed.port in (None, 443) and
                 not parsed.username and not parsed.password and not parsed.fragment and
                 (re.fullmatch(r'productionresultssa[0-9]+\.blob\.core\.windows\.net', parsed.hostname or '')
                  or parsed.hostname == 'pipelines.actions.githubusercontent.com'),
                 'unexpected job-log download host')
        # One redirect only. Never send a token, cookie, or API authorization.
        with self._open(location, {'User-Agent': headers['User-Agent']}) as response:
            _require(response.status == 200 and response.geturl() == location,
                     'unexpected log download response or redirect')
            return self._body(response, MAX_LOG_BYTES)


def _pages(reader, endpoint, key):
    values, seen, total = [], set(), None
    page = 1
    while True:
        data = reader.json(endpoint + '?' + urlencode({'per_page': PER_PAGE, 'page': page}))
        _require(isinstance(data, dict), 'invalid history response')
        count, items = data.get('total_count'), data.get(key)
        _require(type(count) is int and 0 <= count < MAX_ITEMS and isinstance(items, list),
                 'history list is incomplete or exceeds its audit limit')
        _require(total in (None, count), 'history total changed during audit')
        total = count
        _require(len(items) == min(PER_PAGE, total - len(values)), 'incomplete history page')
        for item in items:
            _require(isinstance(item, dict), 'invalid history item')
            identifier = _positive(item.get('id'))
            _require(identifier not in seen, 'duplicate history item')
            seen.add(identifier)
            values.append(item)
        if len(values) == total:
            return values
        page += 1


def _run_identity(run):
    _require(run.get('repository', {}).get('full_name') == REPOSITORY and
             run.get('head_repository', {}).get('full_name') == REPOSITORY and
             run.get('head_branch') == 'main' and run.get('event') in ('push', 'schedule', 'workflow_dispatch') and
             run.get('path') in (WORKFLOW, WORKFLOW + '@main', WORKFLOW + '@refs/heads/main'),
             'untrusted rollout run identity')
    _hash(run.get('head_sha'), HEX40)


def _records(log):
    records = {'start': [], 'receipt': []}
    for line in log.decode('utf-8', errors='strict').splitlines():
        line = LOG_TIMESTAMP.sub('', line, count=1)
        for kind, marker in (('start', START_MARKER), ('receipt', RECEIPT_MARKER)):
            if line.startswith(marker):
                raw = line[len(marker):]
                _require(len(raw.encode()) <= MAX_RECEIPT_BYTES, 'receipt is too large')
                record = _json(raw)
                _require(isinstance(record, dict), 'receipt is not an object')
                records[kind].append(record)
    _require(len(records['receipt']) == 1 and len(records['start']) <= 1,
             'missing or ambiguous terminal attempt receipt')
    return (records['start'][0] if records['start'] else None), records['receipt'][0]


def _common_record(record, baseline, run, attempt, job):
    _require(record.get('schemaVersion') == 1 and type(record.get('schemaVersion')) is int and
             record.get('identity') == identity(baseline) and
             record.get('runId') == run['id'] and type(record.get('runId')) is int and
             record.get('runAttempt') == attempt and type(record.get('runAttempt')) is int and
             record.get('commit') == run['head_sha'] and record.get('job') == BUILD_JOB,
             'receipt does not match verified code, run, or checkpoint identity')
    stamp = _stamp(record.get('at'))
    _require(_stamp(job['started_at']) <= stamp <= _stamp(job['completed_at']),
             'receipt timestamp is outside its job')
    return stamp


def audit_history(baseline, *, run_id, attempt, commit, now=None, fetch_json=None,
                  fetch_log=None, monotonic=time.monotonic):
    """Return allow/deny plus durable failure state; never throw on bad history.

    ``baseline`` must come from independently validated live metadata, not from
    another log. Its baseline run must be pinned by a reviewed migration or by
    a published checkpoint receipt. The caller MUST NOT derive that exemption
    by selecting whichever recent run happens to match or by using today's date.
    """
    report = {'allowed': False, 'complete': False, 'failureCount': None,
              'needsAttention': True, 'retryAfter': None, 'reason': 'attempt history unavailable'}
    reader = BoundedReader(fetch_json, fetch_log, monotonic)
    try:
        now = now or datetime.now(UTC)
        _require(isinstance(now, datetime) and now.tzinfo is not None, 'invalid audit clock')
        now = now.astimezone(UTC)
        boundary = validate_baseline(baseline, now)
        _positive(run_id)
        _require(_positive(attempt) == 1, 'a fresh workflow run is required')
        _hash(commit, HEX40)
        base = f'https://api.github.com/repos/{REPOSITORY}/actions/runs'
        runs = _pages(reader, base, 'workflow_runs')
        by_id = {run['id']: run for run in runs}
        _require(run_id in by_id and baseline['baselineRunId'] in by_id,
                 'current or baseline run missing from complete history')
        current, origin = by_id[run_id], by_id[baseline['baselineRunId']]
        _run_identity(current)
        _run_identity(origin)
        _require(current.get('head_sha') == commit and current.get('run_attempt') == attempt and
                 current.get('status') == 'in_progress', 'current run identity changed')
        _require(origin['head_sha'] == baseline['baselineCommit'] and
                 origin.get('run_attempt') == baseline['baselineRunAttempt'] and
                 origin.get('status') == 'completed' and origin.get('conclusion') == 'success' and
                 _stamp(origin['created_at']) <= boundary <= _stamp(origin['updated_at']) <= now,
                 'baseline publisher is not the exact successful pinned run')
        # Complete totals alone cannot reveal deleted runs. Workflow run numbers
        # are monotonic across all branches/events; require every number between
        # the pinned publisher and this run, including otherwise irrelevant runs.
        workflow_id = _positive(origin.get('workflow_id'))
        _require(current.get('workflow_id') == workflow_id, 'rollout workflow identity changed')
        first_number = _positive(origin.get('run_number'))
        current_number = _positive(current.get('run_number'))
        _require(first_number < current_number and current_number - first_number < MAX_ITEMS,
                 'invalid or excessive baseline run-number span')
        numbers = []
        for run in runs:
            if run.get('workflow_id') == workflow_id:
                number = _positive(run.get('run_number'))
                if first_number <= number <= current_number:
                    numbers.append(number)
        _require(sorted(numbers) == list(range(first_number, current_number + 1)),
                 'deleted, duplicate, or missing rollout run history')
        count, attention, retry = baseline['failureCount'], baseline['needsAttention'], baseline['retryAfter']
        terminal = []
        seen_jobs = set()
        for run in runs:
            path = run.get('path')
            _require(isinstance(path, str) and bool(path), 'history workflow path missing')
            if path.split('@', 1)[0] != WORKFLOW or run.get('head_branch') != 'main':
                continue
            _run_identity(run)
            created, updated = _stamp(run.get('created_at')), _stamp(run.get('updated_at'))
            _require(created <= updated <= now, 'inconsistent rollout timestamps')
            if run['id'] == run_id:
                continue
            if run['id'] == baseline['baselineRunId'] or updated < boundary:
                continue
            _require(run.get('status') == 'completed', 'another relevant rollout is incomplete')
            last_attempt = _positive(run.get('run_attempt'))
            _require(last_attempt <= MAX_ATTEMPTS, 'attempt scan limit reached')
            for number in range(1, last_attempt + 1):
                jobs = _pages(reader, f'{base}/{run["id"]}/attempts/{number}/jobs', 'jobs')
                _require(bool(jobs), 'relevant attempt job history is empty')
                builds = []
                for job in jobs:
                    _require(job['id'] not in seen_jobs and job.get('run_id') == run['id'] and
                             job.get('run_attempt', number) == number and job.get('status') == 'completed',
                             'incomplete or mismatched attempt job history')
                    seen_jobs.add(job['id'])
                    if job.get('name') == BUILD_JOB:
                        builds.append(job)
                _require(len(builds) <= 1, 'duplicate build jobs')
                if not builds:
                    continue  # Complete API inventory proves no build was run.
                job = builds[0]
                if job.get('conclusion') == 'skipped' and job.get('steps') == []:
                    continue
                _require(job.get('head_sha') == run['head_sha'], 'build commit differs from run')
                steps = job.get('steps')
                _require(isinstance(steps, list), 'build steps missing')
                stages = [step for step in steps if isinstance(step, dict) and step.get('name') == STAGE_STEP]
                _require(len(stages) == 1, 'rollout step history missing or ambiguous')
                if stages[0].get('conclusion') == 'skipped' and stages[0].get('status') == 'completed':
                    continue
                _require(stages[0].get('status') == 'completed', 'source attempt step is incomplete')
                start, record = _records(reader.log(job['id']))
                finished = _common_record(record, baseline, run, number, job)
                if start is not None:
                    began = _common_record(start, baseline, run, number, job)
                    _require(began <= finished, 'attempt receipt predates its start')
                else:
                    began = finished
                terminal.append((began, finished, run['id'], number, start, record))
        terminal.sort(key=lambda item: (item[0], item[2], item[3]))
        last_finished = boundary
        for began, finished, _, _, start, record in terminal:
            _require(began >= last_finished, 'overlapping or out-of-order relevant attempts')
            last_finished = finished
            outcome = record.get('outcome')
            _require(outcome in ('deferred', 'not-attempted', 'failed-no-progress', 'paused-no-progress', 'progress'), 'unknown receipt outcome')
            if outcome == 'progress':
                raise HistoryUnavailable('unpublished progress requires reconciliation with the live baseline')
            if outcome == 'not-attempted':
                _require(start is None and type(record.get('admissionComplete')) is bool,
                         'not-attempted receipt contains a started or ambiguous source attempt')
                _require(not any(key in record for key in ('failureCount', 'needsAttention', 'retryAfter')),
                         'not-attempted receipt must not redefine failure state')
                continue
            if outcome == 'deferred':
                _require(start is None, 'deferred receipt contains a started source attempt')
            else:
                _require(start is not None, 'source failure has no start receipt')
                _require(not attention and (retry is None or began >= _stamp(retry)),
                         'source attempt violated persisted backoff or attention')
                if outcome == 'failed-no-progress':
                    _require(type(record.get('permanent')) is bool, 'failure permanence is missing')
                    count += 1
                    attention = record['permanent'] or count >= FAILURE_LIMIT
                    retry = None if attention else (finished + BACKOFF).isoformat()
            _require(record.get('failureCount') == count and type(record.get('failureCount')) is int and
                     record.get('needsAttention') is attention and record.get('retryAfter') == retry,
                     'receipt lost or changed cumulative failure controls')
        reader.check()
        blocked = attention or (retry is not None and _stamp(retry) > now)
        report.update(allowed=not blocked, complete=True, failureCount=count,
                      needsAttention=attention, retryAfter=retry,
                      reason='source attempt needs attention' if attention else
                             'source attempt retry is not due' if blocked else 'source attempt history permits work')
    except Exception as error:
        # Do not include exception URLs: signed log locations must not leak into
        # summaries. The exception class plus controlled reason is sufficient.
        reason = str(error) if isinstance(error, HistoryUnavailable) else type(error).__name__
        report['reason'] = 'Attempt history unavailable: ' + reason[:240]
    report.update(auditRequests=reader.requests, auditBytes=reader.bytes)
    return report


def make_record(baseline, *, run_id, attempt, commit, at):
    """Build shared receipt identity after code/checkpoint verification."""
    _positive(run_id)
    _positive(attempt)
    _hash(commit, HEX40)
    _stamp(at)
    return {'schemaVersion': 1, 'identity': identity(baseline), 'runId': run_id,
            'runAttempt': attempt, 'commit': commit, 'job': BUILD_JOB, 'at': at}


def emit_record(record, *, start=False, stream=None, summary_path=None):
    """Flush one escaped line; summaries are explanatory, never machine inputs."""
    raw = canonical(record)
    _require(len(raw.encode()) <= MAX_RECEIPT_BYTES, 'receipt is too large')
    marker = START_MARKER if start else RECEIPT_MARKER
    print(marker + raw, file=stream or sys.stdout, flush=True)
    path = summary_path if summary_path is not None else os.environ.get('GITHUB_STEP_SUMMARY')
    if path and not start:
        with open(path, 'a', encoding='utf-8') as summary:
            summary.write('\n### Durable source-attempt receipt\n\n```json\n' + raw + '\n```\n')


BOOTSTRAP_DESCRIPTOR_SHA256 = '79336c510d58f660178ff7e33dd0c638aec6be41f6f6c070d5edf1f95d75efe5'
BOOTSTRAP_RUN_ID = 39134231112
BOOTSTRAP_COMMIT = 'ae037d0d4e37e82314a679006ebe4d86bc0497f5'


def baseline_from_checkpoint(raw, publication_revision, reviewed_fingerprint):
    """Use a published origin, with one exact legacy-descriptor exception.

    The caller must already have verified ``raw`` against the live snapshot
    descriptor. This helper does not download or validate the full publication.
    ``reviewed_fingerprint`` is the freshly computed/verified current code hash;
    the migration must independently authorize changing the legacy code hash.
    """
    _require(isinstance(raw, bytes) and len(raw) <= 65536, 'invalid checkpoint descriptor bytes')
    checkpoint = _json(raw)
    origin = checkpoint.get('attemptHistoryOrigin')
    if hashlib.sha256(raw).hexdigest() == BOOTSTRAP_DESCRIPTOR_SHA256:
        _require(origin is None, 'bootstrap unexpectedly contains a history origin')
        origin = {'runId': BOOTSTRAP_RUN_ID, 'attempt': 1, 'commit': BOOTSTRAP_COMMIT}
    else:
        _require(isinstance(origin, dict) and set(origin) == {'runId', 'attempt', 'commit'},
                 'published checkpoint has no trusted source-attempt origin')
        _require(checkpoint.get('codeFingerprint') == reviewed_fingerprint,
                 'published attempt code changed; explicit migration is required')
    return {**{key: checkpoint[key] for key in ('generationId', 'failureCount', 'needsAttention', 'retryAfter')},
            'repository': REPOSITORY, 'workflow': WORKFLOW,
            'publicationRevision': _hash(publication_revision),
            'sourcePlanSha256': source_plan_hash(checkpoint.get('sourcePlan')),
            'codeFingerprint': _hash(reviewed_fingerprint), 'stage': checkpoint['nextStage'],
            'checkpointUpdatedAt': checkpoint['updatedAt'],
            'baselineRunId': origin['runId'], 'baselineRunAttempt': origin['attempt'],
            'baselineCommit': origin['commit']}


class AttemptObserver:
    """Callback for advance(event, checkpoint, error); owns no durable storage.

    Construct only after an allowed complete audit. Call finish_if_unstarted()
    after a normal return from advance, so a verified no-op is distinguishable
    from a killed/missing attempt. Exceptions must leave the receipt incomplete.
    The effective in-memory checkpoint failure controls are synchronized with
    the terminal receipt; the restored baseline bytes must remain untouched.
    """
    def __init__(self, baseline, admission, *, run_id, attempt, commit,
                 clock=lambda: datetime.now(UTC), stream=None, summary_path=None):
        _require(admission.get('allowed') is True and admission.get('complete') is True,
                 'an admitted complete history is required before observing source work')
        self.baseline = _json(canonical(baseline))
        self.state = {key: admission[key] for key in ('failureCount', 'needsAttention', 'retryAfter')}
        self.run_id, self.attempt, self.commit = run_id, attempt, commit
        self.clock, self.stream, self.summary_path = clock, stream, summary_path
        self.started = False
        self.terminal = False
        self.progress_before = None

    def _record(self):
        return make_record(self.baseline, run_id=self.run_id, attempt=self.attempt,
                           commit=self.commit, at=self.clock().astimezone(UTC).isoformat())

    def _emit(self, record, start=False):
        emit_record(record, start=start, stream=self.stream, summary_path=self.summary_path)

    def __call__(self, event, checkpoint, error):
        _require(not self.terminal, 'attempt already has a terminal receipt')
        if event == 'started':
            _require(not self.started and isinstance(checkpoint, dict), 'duplicate or invalid attempt start')
            _require(checkpoint.get('generationId') == self.baseline['generationId'] and
                     checkpoint.get('codeFingerprint') == self.baseline['codeFingerprint'] and
                     source_plan_hash(checkpoint.get('sourcePlan')) == self.baseline['sourcePlanSha256'] and
                     checkpoint.get('nextStage') == self.baseline['stage'],
                     'started checkpoint differs from admitted source identity')
            self.progress_before = canonical(checkpoint['progress'])
            record = self._record()
            self._emit(record, start=True)
            self.started = True
            return
        _require(self.started and event in ('failed', 'progress', 'paused') and isinstance(checkpoint, dict),
                 'invalid terminal attempt event')
        record = self._record()
        progressed = canonical(checkpoint['progress']) != self.progress_before
        if progressed:
            # Even a partial prefix is unresolved until this run's new baseline
            # is actually observed live. Do not claim an upload was a deployment.
            record.update(outcome='progress', **self.state)
        elif event == 'failed':
            self.state['failureCount'] += 1
            permanent = checkpoint.get('needsAttention') is True and self.state['failureCount'] < FAILURE_LIMIT
            attention = permanent or self.state['failureCount'] >= FAILURE_LIMIT
            self.state.update(needsAttention=attention,
                              retryAfter=None if attention else (_stamp(record['at']) + BACKOFF).isoformat())
            checkpoint.update(self.state)
            record.update(outcome='failed-no-progress', permanent=permanent, **self.state)
        else:
            # A planned deadline pause did not complete a source and must not
            # erase earlier failures merely because its subprocess returned 0.
            checkpoint.update(self.state)
            record.update(outcome='paused-no-progress', **self.state)
        self._emit(record)
        self.terminal = True

    def finish_if_unstarted(self):
        if not self.started and not self.terminal:
            record = self._record()
            record.update(outcome='deferred', **self.state)
            self._emit(record)
            self.terminal = True


def emit_no_attempt(baseline, admission, *, run_id, attempt, commit, now=None,
                    stream=None, summary_path=None):
    """Record a denied audit ONLY when the caller never invoked source work.

    This marker does not redefine failure controls, including when history was
    unavailable. Any older unresolved attempt still independently blocks work.
    """
    _require(admission.get('allowed') is False and type(admission.get('complete')) is bool,
             'not-attempted receipt requires an explicit denied audit')
    now = now or datetime.now(UTC)
    record = make_record(baseline, run_id=run_id, attempt=attempt, commit=commit,
                         at=now.astimezone(UTC).isoformat())
    record.update(outcome='not-attempted', admissionComplete=admission['complete'])
    emit_record(record, stream=stream, summary_path=summary_path)
    return record
