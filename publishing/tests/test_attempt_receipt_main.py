"""Offline main-loop wiring checks: no HTTP, source transfer, or publication."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'publishing'))
import artifact_budget
import attempt_receipts as receipts
import resumable_rollout as rollout

CODE = 'b' * 64
COMMIT = 'a' * 40
REVISION = 'c' * 64
NOW = datetime(2026, 10, 16, 14, 4, tzinfo=timezone.utc)


class AttemptReceiptMainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.site = self.root/'site'
        self.report = self.root/'report.json'
        self.output = self.root/'github-output'
        self.summary = self.root/'summary.md'
        self.checkpoint = {
            'generationId': 'd' * 64, 'codeFingerprint': CODE,
            'sourcePlan': {'planId': 'e' * 64},
            'nextStage': {'kind': 'source', 'state': 'indiana'},
            'progress': {'completedStates': ['illinois', 'wisconsin']},
            'updatedAt': NOW.isoformat(), 'failureCount': 1,
            'retryAfter': (NOW + timedelta(hours=20)).isoformat(), 'needsAttention': False,
            'attemptHistoryOrigin': {'runId': 100, 'attempt': 1, 'commit': COMMIT},
        }
        self.admission = {'allowed': True, 'complete': True, 'failureCount': 1,
                          'retryAfter': self.checkpoint['retryAfter'], 'needsAttention': False,
                          'reason': 'history permits work'}
        self.addCleanup(patch.stopall)
        started = time.monotonic()
        patch.dict(os.environ, {'GITHUB_ACTIONS': 'true',
                               'RIDE_BUILD_STARTED': str(started),
                               'RIDE_BUILD_DEADLINE': str(started + rollout.BUILD_SECONDS),
                               'GITHUB_REPOSITORY': receipts.REPOSITORY,
                               'GITHUB_RUN_ID': '101', 'GITHUB_RUN_ATTEMPT': '1',
                               'GITHUB_SHA': COMMIT, 'GITHUB_OUTPUT': str(self.output),
                               'GITHUB_STEP_SUMMARY': str(self.summary)}).start()
        patch.object(sys, 'argv', ['resumable_rollout.py', '--continue-only',
                                  '--output', str(self.site), '--report', str(self.report)]).start()
        patch.object(rollout.signal, 'signal').start()
        patch.object(rollout, 'load_configs', return_value=[]).start()
        patch.object(rollout, 'restore_published', side_effect=self.restore).start()
        self.fingerprint = patch.object(rollout, 'current_code_fingerprint', return_value=CODE).start()
        self.audit = patch.object(receipts, 'audit_history', return_value=self.admission).start()
        self.storage = patch.object(artifact_budget, 'check_artifact_budget', return_value={'allowed': True}).start()
        self.advance = patch.object(rollout, 'advance', return_value={'changed': False, 'reason': 'no time'}).start()
        self.unchanged = patch.object(rollout, 'assert_publication_unchanged').start()
        self.no_attempt = patch.object(receipts, 'emit_no_attempt', wraps=receipts.emit_no_attempt).start()
        self.stdout = io.StringIO()

    def restore(self, site, receipt):
        site.mkdir()
        (site/'preserved-snapshot.bin').write_bytes(b'immutable published bytes')
        if self.checkpoint is not None:
            descriptor = site/rollout.ACTIVE/rollout.DESCRIPTOR
            descriptor.parent.mkdir(parents=True)
            descriptor.write_text(json.dumps(self.checkpoint, sort_keys=True))
        receipt['publicationRevision'] = REVISION

    def run_main(self):
        with contextlib.redirect_stdout(self.stdout):
            rollout.main()
        return json.loads(self.report.read_text())

    def records(self):
        return receipts._records(self.stdout.getvalue().encode())

    def test_denied_backoff_skips_storage_and_advance_and_emits_no_attempt(self):
        self.admission.update(allowed=False, reason='retry not due')
        result = self.run_main()
        self.assertFalse(result['changed'])
        self.storage.assert_not_called()
        self.advance.assert_not_called()
        self.unchanged.assert_not_called()
        self.no_attempt.assert_called_once()
        start, terminal = self.records()
        self.assertIsNone(start)
        self.assertEqual(terminal['outcome'], 'not-attempted')
        self.assertTrue(terminal['admissionComplete'])
        self.assertEqual(result['attemptHistory']['failureCount'], 1)
        self.assertIn('changed=false\n', self.output.read_text())

    def test_unavailable_history_emits_no_attempt_without_resetting_count(self):
        self.admission.update(allowed=False, complete=False, failureCount=None,
                              retryAfter=None, needsAttention=True, reason='log unavailable')
        self.run_main()
        self.advance.assert_not_called()
        _, terminal = self.records()
        self.assertFalse(terminal['admissionComplete'])
        self.assertNotIn('failureCount', terminal)

    def test_late_storage_denial_emits_no_attempt(self):
        self.storage.return_value = {'allowed': False, 'reason': 'capacity unavailable'}
        result = self.run_main()
        self.audit.assert_called_once()
        self.advance.assert_not_called()
        self.no_attempt.assert_called_once()
        self.assertFalse(result['changed'])
        self.assertEqual(self.records()[1]['outcome'], 'not-attempted')

    def test_normal_no_source_return_emits_deferred(self):
        result = self.run_main()
        self.assertFalse(result['changed'])
        start, terminal = self.records()
        self.assertIsNone(start)
        self.assertEqual(terminal['outcome'], 'deferred')
        kwargs = self.advance.call_args.kwargs
        self.assertIs(kwargs['attempt_admission'], self.admission)
        self.assertIsInstance(kwargs['attempt_observer'], receipts.AttemptObserver)
        self.assertEqual(kwargs['attempt_origin'], {'runId': 101, 'attempt': 1, 'commit': COMMIT})
        self.assertEqual(self.audit.call_args.kwargs, {'run_id': 101, 'attempt': 1, 'commit': COMMIT})
        self.assertEqual(self.audit.call_args.args[0]['publicationRevision'], REVISION)

    def test_zero_progress_failure_preserves_original_descriptor_and_emits_failure(self):
        def advance(*args, **kwargs):
            checkpoint = copy.deepcopy(self.checkpoint)
            observer = kwargs['attempt_observer']
            observer('started', checkpoint, None)
            checkpoint.update(failureCount=2, needsAttention=False)
            observer('failed', checkpoint, RuntimeError('fixture source timeout'))
            return {'changed': False, 'failureCount': checkpoint['failureCount'],
                    'retryAfter': checkpoint['retryAfter']}
        self.advance.side_effect = advance
        result = self.run_main()
        self.assertFalse(result['changed'])
        self.assertEqual(result['failureCount'], 2)
        self.assertEqual(self.records()[1]['outcome'], 'failed-no-progress')
        self.assertEqual(json.loads((self.site/rollout.ACTIVE/rollout.DESCRIPTOR).read_text()), self.checkpoint)
        self.assertEqual((self.site/'preserved-snapshot.bin').read_bytes(), b'immutable published bytes')
        self.unchanged.assert_not_called()

    def test_partial_prefix_failure_is_progress_and_checks_live_revision(self):
        def advance(*args, **kwargs):
            checkpoint = copy.deepcopy(self.checkpoint)
            observer = kwargs['attempt_observer']
            observer('started', checkpoint, None)
            checkpoint['progress']['completedStates'].append('indiana')
            observer('failed', checkpoint, RuntimeError('fixture next source timeout'))
            return {'changed': True}
        self.advance.side_effect = advance
        result = self.run_main()
        self.assertTrue(result['changed'])
        self.assertEqual(self.records()[1]['outcome'], 'progress')
        self.unchanged.assert_called_once_with(REVISION)

    def test_exception_after_start_is_not_converted_to_deferred(self):
        def advance(*args, **kwargs):
            kwargs['attempt_observer']('started', copy.deepcopy(self.checkpoint), None)
            raise RuntimeError('fixture unexpected termination')
        self.advance.side_effect = advance
        with self.assertRaisesRegex(RuntimeError, 'fixture unexpected termination'):
            self.run_main()
        self.assertIn(receipts.START_MARKER, self.stdout.getvalue())
        self.assertNotIn(receipts.RECEIPT_MARKER, self.stdout.getvalue())
        self.assertFalse(self.report.exists())

    def test_initial_generation_passes_origin_without_synthetic_history(self):
        self.checkpoint = None
        self.advance.return_value = {'changed': True}
        self.run_main()
        self.audit.assert_not_called()
        self.fingerprint.assert_not_called()
        kwargs = self.advance.call_args.kwargs
        self.assertIsNone(kwargs['attempt_observer'])
        self.assertIsNone(kwargs['attempt_admission'])
        self.assertEqual(kwargs['attempt_origin'], {'runId': 101, 'attempt': 1, 'commit': COMMIT})
        self.assertNotIn(receipts.RECEIPT_MARKER, self.stdout.getvalue())

    def test_initial_pin_stage_is_not_given_a_synthetic_history_baseline(self):
        self.checkpoint.update(sourcePlan=None, nextStage={'kind': 'start'})
        self.run_main()
        self.audit.assert_not_called()
        self.assertIsNone(self.advance.call_args.kwargs['attempt_observer'])
        self.assertEqual(self.advance.call_args.kwargs['attempt_origin']['runId'], 101)

    def test_non_source_checkpoint_keeps_existing_advance_path(self):
        self.checkpoint['nextStage'] = {'kind': 'index'}
        self.run_main()
        self.audit.assert_not_called()
        self.advance.assert_called_once()
        self.assertIsNone(self.advance.call_args.kwargs['attempt_observer'])

    def test_untrusted_origin_or_changed_fingerprint_denies_without_rewriting(self):
        self.checkpoint['codeFingerprint'] = 'f' * 64
        result = self.run_main()
        self.assertFalse(result['changed'])
        self.assertIn('explicit migration', result['reason'])
        self.audit.assert_not_called()
        self.storage.assert_not_called()
        self.advance.assert_not_called()
        self.no_attempt.assert_not_called()
        self.assertEqual(json.loads((self.site/rollout.ACTIVE/rollout.DESCRIPTOR).read_text()), self.checkpoint)

    def test_rerun_is_refused_before_restore(self):
        with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
            with self.assertRaisesRegex(RuntimeError, 'fresh workflow'):
                self.run_main()
        self.assertFalse(self.site.exists())


if __name__ == '__main__':
    unittest.main()
