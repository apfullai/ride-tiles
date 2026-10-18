import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import attempt_receipts as ar

UTC = timezone.utc
BASE_TIME = datetime(2026, 10, 18, 14, 4, 14, 579685, tzinfo=UTC)
SHA = 'a' * 40
CODE = 'b' * 64


def timestamp(time):
    return time.isoformat()


def baseline():
    return {'repository': ar.REPOSITORY, 'workflow': ar.WORKFLOW,
            'publicationRevision': 'c' * 64, 'generationId': 'd' * 64,
            'sourcePlanSha256': ar.source_plan_hash({'planId': 'e' * 64}),
            'codeFingerprint': CODE, 'stage': {'kind': 'source', 'state': 'indiana'},
            'baselineRunId': 100, 'baselineRunAttempt': 1, 'baselineCommit': SHA,
            'checkpointUpdatedAt': timestamp(BASE_TIME), 'failureCount': 1,
            'needsAttention': False, 'retryAfter': timestamp(BASE_TIME + ar.BACKOFF)}


def run(identifier, time, *, current=False, attempt=1):
    return {'id': identifier, 'run_number': identifier, 'workflow_id': 10,
            'repository': {'full_name': ar.REPOSITORY},
            'head_repository': {'full_name': ar.REPOSITORY},
            'path': ar.WORKFLOW, 'head_branch': 'main', 'event': 'schedule',
            'head_sha': SHA, 'run_attempt': attempt,
            'created_at': timestamp(time - timedelta(minutes=2)),
            'updated_at': timestamp(time + timedelta(minutes=5)),
            'status': 'in_progress' if current else 'completed',
            'conclusion': None if current else 'success'}


class History:
    def __init__(self):
        self.baseline = baseline()
        self.now = BASE_TIME + timedelta(hours=21)
        self.runs = [run(100, BASE_TIME), run(101, self.now - timedelta(minutes=5), current=True)]
        self.jobs = {}
        self.logs = {}
        self.calls = []

    def add(self, *, outcome='failed-no-progress', at=None, count=2, permanent=False):
        at = at or BASE_TIME + timedelta(hours=20, minutes=1)
        identifier = self.runs[-1]['id']
        self.runs[-1] = run(identifier, at)
        self.runs.append(run(identifier + 1, self.now - timedelta(minutes=5), current=True))
        job_id = identifier * 100
        job = {'id': job_id, 'run_id': identifier, 'run_attempt': 1, 'head_sha': SHA,
               'name': 'build', 'status': 'completed', 'conclusion': 'success',
               'started_at': timestamp(at - timedelta(minutes=1)),
               'completed_at': timestamp(at + timedelta(minutes=2)),
               'steps': [{'name': ar.STAGE_STEP, 'status': 'completed', 'conclusion': 'success'}]}
        self.jobs[(identifier, 1)] = [job]
        start = ar.make_record(self.baseline, run_id=identifier, attempt=1, commit=SHA,
                               at=timestamp(at))
        receipt = ar.make_record(self.baseline, run_id=identifier, attempt=1, commit=SHA,
                                 at=timestamp(at + timedelta(minutes=1)))
        attention = permanent or count >= 3
        retry = None if attention else timestamp(at + timedelta(minutes=1) + ar.BACKOFF)
        if outcome == 'deferred':
            count, attention, retry = (self.baseline[k] for k in ('failureCount', 'needsAttention', 'retryAfter'))
        receipt.update(outcome=outcome, failureCount=count, needsAttention=attention,
                       retryAfter=retry, permanent=permanent)
        lines = ['ordinary tool output']
        if outcome != 'deferred':
            lines.append(ar.START_MARKER + ar.canonical(start))
        lines.append(ar.RECEIPT_MARKER + ar.canonical(receipt))
        self.logs[job_id] = ('\n'.join('2026-10-19T10:06:00.0000000Z ' + line for line in lines) + '\n').encode()
        return job, receipt

    def fetch(self, url):
        self.calls.append(url)
        path = url.split('?', 1)[0]
        if path.endswith('/actions/runs'):
            values, key = self.runs, 'workflow_runs'
        else:
            parts = path.split('/')
            values, key = self.jobs[(int(parts[-4]), int(parts[-2]))], 'jobs'
        return {'total_count': len(values), key: copy.deepcopy(values)}

    def audit(self):
        return ar.audit_history(self.baseline, run_id=self.runs[-1]['id'], attempt=1,
                                commit=SHA, now=self.now, fetch_json=self.fetch,
                                fetch_log=lambda identifier: self.logs[identifier])


class AttemptReceiptTests(unittest.TestCase):
    def test_bootstrap_preserves_one_failure_without_reading_legacy_log(self):
        h = History()
        h.now = BASE_TIME + timedelta(hours=1)
        h.runs[-1] = run(101, h.now - timedelta(minutes=5), current=True)
        report = h.audit()
        self.assertTrue(report['complete'], report)
        self.assertFalse(report['allowed'])
        self.assertEqual(report['failureCount'], 1)
        self.assertEqual(report['retryAfter'], h.baseline['retryAfter'])
        self.assertEqual(len(h.calls), 1)

    def test_bootstrap_due_does_not_reset_count(self):
        h = History()
        report = h.audit()
        self.assertTrue(report['allowed'], report)
        self.assertEqual(report['failureCount'], 1)

    def test_failed_no_progress_starts_twenty_hour_backoff(self):
        h = History()
        _, receipt = h.add()
        report = h.audit()
        self.assertTrue(report['complete'], report)
        self.assertFalse(report['allowed'])
        self.assertEqual(report['failureCount'], 2)
        self.assertEqual(report['retryAfter'], receipt['retryAfter'])

    def test_three_failures_need_attention(self):
        h = History()
        h.now = BASE_TIME + timedelta(hours=42)
        h.add()
        h.add(at=BASE_TIME + timedelta(hours=41), count=3)
        report = h.audit()
        self.assertTrue(report['complete'], report)
        self.assertFalse(report['allowed'])
        self.assertTrue(report['needsAttention'])
        self.assertEqual(report['failureCount'], 3)
        self.assertIsNone(report['retryAfter'])

    def test_permanent_failure_needs_attention_immediately(self):
        h = History()
        h.add(permanent=True)
        report = h.audit()
        self.assertTrue(report['complete'], report)
        self.assertTrue(report['needsAttention'])
        self.assertEqual(report['failureCount'], 2)

    def test_missing_terminal_receipt_is_not_success(self):
        h = History()
        job, _ = h.add()
        h.logs[job['id']] = b'process timed out\n'
        self.assertFalse(h.audit()['complete'])

    def test_duplicate_terminal_receipt_denies(self):
        h = History()
        job, receipt = h.add()
        h.logs[job['id']] += (ar.RECEIPT_MARKER + ar.canonical(receipt) + '\n').encode()
        self.assertIn('ambiguous', h.audit()['reason'])

    def test_deleted_run_is_detected_by_workflow_number_gap(self):
        h = History()
        h.add()
        h.runs.pop(1)
        self.assertIn('missing rollout run', h.audit()['reason'])

    def test_different_identity_does_not_reset_history(self):
        for field in ('codeFingerprint', 'publicationRevision', 'generationId', 'sourcePlanSha256'):
            with self.subTest(field=field):
                h = History()
                job, _ = h.add()
                h.baseline[field] = 'f' * 64
                self.assertIn('identity', h.audit()['reason'])

    def test_timestamp_outside_job_denies(self):
        h = History()
        job, _ = h.add()
        job['completed_at'] = timestamp(BASE_TIME)
        self.assertIn('outside its job', h.audit()['reason'])

    def test_pre_due_attempt_denies(self):
        h = History()
        h.add(at=BASE_TIME + timedelta(hours=19))
        self.assertIn('violated persisted backoff', h.audit()['reason'])

    def test_unpublished_progress_does_not_reset_failures(self):
        h = History()
        h.add(outcome='progress')
        self.assertIn('reconciliation', h.audit()['reason'])

    def test_deferred_receipt_keeps_failure_controls(self):
        h = History()
        h.add(outcome='deferred')
        report = h.audit()
        self.assertTrue(report['allowed'], report)
        self.assertEqual(report['failureCount'], 1)

    def test_baseline_missing_or_rerun_denies(self):
        h = History()
        h.runs[0]['run_attempt'] = 2
        self.assertFalse(h.audit()['complete'])
        h.runs.pop(0)
        self.assertFalse(h.audit()['complete'])

    def test_oversized_log_denies(self):
        h = History()
        job, _ = h.add()
        h.logs[job['id']] = b'x' * (ar.MAX_LOG_BYTES + 1)
        self.assertIn('too large', h.audit()['reason'])

    def test_log_fetch_error_hides_signed_url(self):
        h = History()
        h.add()
        report = ar.audit_history(h.baseline, run_id=h.runs[-1]['id'], attempt=1,
                                 commit=SHA, now=h.now, fetch_json=h.fetch,
                                 fetch_log=lambda _: (_ for _ in ()).throw(OSError('https://private?secret')))
        self.assertFalse(report['complete'])
        self.assertNotIn('secret', report['reason'])

    def test_incomplete_page_denies(self):
        h = History()
        original = h.fetch
        def incomplete(url):
            result = original(url)
            result['total_count'] += 1
            return result
        h.fetch = incomplete
        self.assertIn('incomplete history page', h.audit()['reason'])

    def test_unrelated_branch_preserves_run_sequence_without_reading_log(self):
        h = History()
        h.add()
        h.runs[1]['head_branch'] = 'feature'
        h.logs.clear()
        self.assertTrue(h.audit()['allowed'])

    def test_incomplete_relevant_run_denies(self):
        h = History()
        h.add()
        h.runs[1]['status'] = 'in_progress'
        self.assertIn('incomplete', h.audit()['reason'])

    def test_skipped_build_needs_no_log(self):
        h = History()
        job, _ = h.add()
        job.update(conclusion='skipped', steps=[])
        h.logs.clear()
        self.assertTrue(h.audit()['allowed'])

    def test_cumulative_count_cannot_reset(self):
        h = History()
        job, receipt = h.add()
        receipt['failureCount'] = 1
        start = ar.make_record(h.baseline, run_id=101, attempt=1, commit=SHA,
                               at=timestamp(BASE_TIME + timedelta(hours=20, minutes=1)))
        h.logs[job['id']] = (ar.START_MARKER + ar.canonical(start) + '\n' +
                             ar.RECEIPT_MARKER + ar.canonical(receipt)).encode()
        self.assertIn('cumulative failure controls', h.audit()['reason'])

    def test_duplicate_json_keys_denied(self):
        with self.assertRaises(ar.HistoryUnavailable):
            ar._records((ar.RECEIPT_MARKER + '{"schemaVersion":1,"schemaVersion":1}').encode())

    def test_request_and_clock_limits(self):
        reader = ar.BoundedReader(fetch_json=lambda _: {}, monotonic=lambda: 0)
        for _ in range(ar.MAX_REQUESTS):
            reader.json('fixture')
        with self.assertRaises(ar.HistoryUnavailable):
            reader.json('fixture')
        times = iter([0, ar.MAX_SECONDS + 1])
        reader = ar.BoundedReader(monotonic=lambda: next(times))
        with self.assertRaises(ar.HistoryUnavailable):
            reader.reserve()

    def test_total_byte_limit(self):
        reader = ar.BoundedReader(monotonic=lambda: 0)
        reader.bytes = ar.MAX_TOTAL_BYTES
        with self.assertRaises(ar.HistoryUnavailable):
            reader.consume(b'x', 2)

    def test_emit_escapes_lines_and_mirrors_summary(self):
        h = History()
        record = ar.make_record(h.baseline, run_id=101, attempt=1, commit=SHA, at=timestamp(h.now))
        record['reason'] = 'line one\nline two'
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.md'
            ar.emit_record(record, stream=output, summary_path=path)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertIn(ar.canonical(record), path.read_text())


class ObserverTests(unittest.TestCase):
    def setup_observer(self):
        h = History()
        checkpoint = {'generationId': h.baseline['generationId'], 'codeFingerprint': CODE,
                      'sourcePlan': {'planId': 'e' * 64}, 'nextStage': h.baseline['stage'],
                      'progress': {'completedStates': ['illinois', 'wisconsin']},
                      'failureCount': 1, 'needsAttention': False, 'retryAfter': h.baseline['retryAfter']}
        output = io.StringIO()
        observer = ar.AttemptObserver(h.baseline, h.audit(), run_id=101, attempt=1,
                                      commit=SHA, clock=lambda: h.now, stream=output, summary_path='')
        return h, checkpoint, output, observer

    def test_failure_observer_preserves_and_increments_prior_count(self):
        h, checkpoint, output, observer = self.setup_observer()
        observer('started', checkpoint, None)
        checkpoint.update(failureCount=1, needsAttention=False, retryAfter=None)
        observer('failed', checkpoint, RuntimeError('timeout'))
        start, terminal = ar._records(output.getvalue().encode())
        self.assertIsNotNone(start)
        self.assertEqual(terminal['failureCount'], 2)
        self.assertEqual(checkpoint['failureCount'], 2)
        self.assertEqual(checkpoint['retryAfter'], timestamp(h.now + ar.BACKOFF))

    def test_pause_observer_does_not_erase_failures(self):
        h, checkpoint, output, observer = self.setup_observer()
        observer('started', checkpoint, None)
        checkpoint.update(failureCount=0, needsAttention=False, retryAfter=None)
        observer('paused', checkpoint, None)
        _, terminal = ar._records(output.getvalue().encode())
        self.assertEqual(terminal['outcome'], 'paused-no-progress')
        self.assertEqual(checkpoint['failureCount'], 1)
        self.assertEqual(checkpoint['retryAfter'], h.baseline['retryAfter'])

    def test_partial_progress_with_error_is_unresolved_progress(self):
        _, checkpoint, output, observer = self.setup_observer()
        observer('started', checkpoint, None)
        checkpoint['progress']['completedStates'].append('indiana')
        observer('failed', checkpoint, RuntimeError('michigan timeout'))
        _, terminal = ar._records(output.getvalue().encode())
        self.assertEqual(terminal['outcome'], 'progress')

    def test_deferred_when_advance_returns_without_source_work(self):
        _, _, output, observer = self.setup_observer()
        observer.finish_if_unstarted()
        observer.finish_if_unstarted()
        start, terminal = ar._records(output.getvalue().encode())
        self.assertIsNone(start)
        self.assertEqual(terminal['outcome'], 'deferred')

    def test_history_denial_does_not_claim_a_new_failure(self):
        h = History()
        job, _ = h.add()
        output = io.StringIO()
        ar.emit_no_attempt(h.baseline, {'allowed': False, 'complete': False},
                           run_id=101, attempt=1, commit=SHA,
                           now=BASE_TIME + timedelta(hours=20, minutes=1),
                           stream=output, summary_path='')
        h.logs[job['id']] = output.getvalue().encode()
        report = h.audit()
        self.assertTrue(report['allowed'], report)
        self.assertEqual(report['failureCount'], 1)

    def test_baseline_rejects_unpinned_legacy_descriptor(self):
        with self.assertRaises(ar.HistoryUnavailable):
            ar.baseline_from_checkpoint(b'{"failureCount":0}', 'c' * 64, CODE)

    def test_future_baseline_uses_published_origin(self):
        h = History()
        raw = {'attemptHistoryOrigin': {'runId': 100, 'attempt': 1, 'commit': SHA},
               'generationId': h.baseline['generationId'], 'failureCount': 2,
               'needsAttention': False, 'retryAfter': h.baseline['retryAfter'],
               'sourcePlan': {'planId': 'e' * 64}, 'codeFingerprint': CODE,
               'nextStage': h.baseline['stage'], 'updatedAt': h.baseline['checkpointUpdatedAt']}
        result = ar.baseline_from_checkpoint(ar.canonical(raw).encode(), 'c' * 64, CODE)
        self.assertEqual(result['baselineRunId'], 100)
        self.assertEqual(result['failureCount'], 2)
        with self.assertRaises(ar.HistoryUnavailable):
            ar.baseline_from_checkpoint(ar.canonical(raw).encode(), 'c' * 64, 'f' * 64)


class FakeResponse(io.BytesIO):
    def __init__(self, data, url, status=200):
        super().__init__(data)
        self.url, self.status, self.headers = url, status, {}

    def geturl(self):
        return self.url


class TransportTests(unittest.TestCase):
    def test_public_log_redirect_is_anonymous_and_single_use(self):
        from urllib.error import HTTPError
        location = 'https://productionresultssa7.blob.core.windows.net/actions-results/job.txt?signature=opaque'
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                if len(calls) == 1:
                    raise HTTPError(request.full_url, 302, 'Found', {'Location': location}, io.BytesIO())
                return FakeResponse(b'job logs', location)
        with patch.object(ar, 'build_opener', return_value=Opener()), patch.dict('os.environ', {'TILE_GITHUB_READ_TOKEN': 'sentinel-token'}):
            raw = ar.BoundedReader().log(111)
        self.assertEqual(raw, b'job logs')
        self.assertEqual(len(calls), 2)
        self.assertTrue(all('Authorization' not in dict(request.header_items()) for request in calls))
        self.assertEqual(calls[1].full_url, location)

    def test_unexpected_log_redirect_host_is_rejected(self):
        from urllib.error import HTTPError
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 302, 'Found', {'Location': 'https://attacker.invalid/log'}, io.BytesIO())
        with patch.object(ar, 'build_opener', return_value=Opener()):
            with self.assertRaises(ar.HistoryUnavailable):
                ar.BoundedReader().log(111)

    def test_wrong_status_does_not_follow_or_leak_redirect(self):
        from urllib.error import HTTPError
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 403, 'Forbidden', {'Location': 'https://private?secret'}, io.BytesIO())
        with patch.object(ar, 'build_opener', return_value=Opener()):
            with self.assertRaises(ar.HistoryUnavailable):
                ar.BoundedReader().log(111)


if __name__ == '__main__':
    unittest.main()
