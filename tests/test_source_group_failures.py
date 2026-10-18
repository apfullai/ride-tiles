"""Completed-prefix and deadline tests. No network or real source attempts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'publishing'))
from test_resumable_stages import CLOCK, CONFIG, RUNTIME, SOURCE, fake_osmium, plan
import resumable_region as r
import resumable_stage_runner as runner


class SourceGroupFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkpoint = self.root / 'checkpoint'
        self.work = self.root / 'work'
        self.result = self.root / 'result.json'
        self.plan = plan()
        for item in (
            patch.object(r, 'now_utc', side_effect=lambda now=None: now or CLOCK),
            patch.object(r, 'runtime_fingerprint', return_value=RUNTIME),
            patch.object(r, 'source_timestamp', return_value=self.plan['osmReplicationTimestamp']),
            patch.object(r.legacy, 'run', side_effect=fake_osmium),
            patch.object(r.legacy, 'download_source', side_effect=lambda url, path, **kw: Path(path).write_bytes(SOURCE)),
        ):
            item.start()
            self.addCleanup(item.stop)

    def request(self, kind='source', seconds=410):
        return {'config': CONFIG, 'sourcePlan': None if kind == 'start' else self.plan,
                'stage': {'kind': kind, 'state': CONFIG['sourceStates'][0]}, 'stageSeconds': seconds,
                'sourceConcurrency': 1}

    def fail_second(self, error):
        def download(url, path, **kwargs):
            if 'new-jersey-' in url:
                Path(path).write_bytes(b'incomplete raw source')
                raise error
            Path(path).write_bytes(SOURCE)
        return download

    def test_later_timeout_reports_only_validated_committed_prefix(self):
        notices = []
        with patch.object(r.legacy, 'download_source', side_effect=self.fail_second(subprocess.TimeoutExpired('curl', 100))):
            with self.assertRaises(r.SourceGroupFailure) as failure:
                r.build_source_group(CONFIG, self.plan, self.checkpoint, self.work,
                                     on_progress=lambda group, progress: notices.append((group, progress)))
        error = failure.exception
        self.assertEqual(error.group['completedStates'], ['new-york'])
        self.assertEqual(error.group['remainingStates'], ['new-jersey'])
        self.assertEqual(error.group['status'], 'failed')
        self.assertEqual(error.detail['state'], 'new-jersey')
        self.assertFalse(error.detail['permanent'])
        self.assertEqual(error.progress, r.validate_checkpoint(CONFIG, self.plan, self.checkpoint))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0][0]['status'], 'running')
        self.assertFalse((self.checkpoint / 'sources/new-jersey').exists())
        self.assertFalse(list(self.work.rglob('state.osm.pbf')))

    def test_first_failure_does_not_claim_progress(self):
        with patch.object(r.legacy, 'download_source', side_effect=OSError('transfer interrupted')):
            with self.assertRaises(r.SourceGroupFailure) as failure:
                r.build_source_group(CONFIG, self.plan, self.checkpoint, self.work)
        self.assertEqual(failure.exception.group['completedStates'], [])
        self.assertEqual(failure.exception.group['remainingStates'], CONFIG['sourceStates'])
        self.assertFalse(failure.exception.detail['permanent'])

    def test_validation_failure_is_permanent_and_error_message_is_bounded(self):
        with patch.object(r.legacy, 'download_source', side_effect=self.fail_second(RuntimeError('x' * 5000))):
            with self.assertRaises(r.SourceGroupFailure) as failure:
                r.build_source_group(CONFIG, self.plan, self.checkpoint, self.work)
        self.assertTrue(failure.exception.detail['permanent'])
        self.assertEqual(len(failure.exception.detail['message']), 500)
        self.assertEqual(failure.exception.group['completedStates'], ['new-york'])

    def test_corrupt_preceding_crop_cannot_be_claimed_as_completed(self):
        def corrupt(url, path, **kwargs):
            if 'new-jersey-' in url:
                (self.checkpoint / 'sources/new-york/crop.part-0000').write_bytes(b'corrupt')
                raise OSError('transfer interrupted')
            Path(path).write_bytes(SOURCE)
        with patch.object(r.legacy, 'download_source', side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'hash/size mismatch'):
                r.build_source_group(CONFIG, self.plan, self.checkpoint, self.work)

    def test_success_without_commit_is_not_journaled(self):
        with patch.object(r, 'build_source_stage', return_value={}), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'did not commit'):
                r.build_source_group(CONFIG, self.plan, self.checkpoint, self.work)

    def test_runner_journals_before_later_attempt_and_exits_with_failure_receipt(self):
        def download(url, path, **kwargs):
            if 'new-jersey-' in url:
                previous = json.loads(self.result.read_text())
                self.assertEqual(previous['sourceGroup']['completedStates'], ['new-york'])
                self.assertEqual(previous['sourceGroup']['status'], 'running')
                raise subprocess.CalledProcessError(28, 'curl')
            Path(path).write_bytes(SOURCE)
        with patch.object(r.legacy, 'download_source', side_effect=download):
            with self.assertRaises(r.SourceGroupFailure):
                runner.run(self.request(), self.checkpoint, self.work, self.result)
        result = json.loads(self.result.read_text())
        self.assertEqual(result['sourceGroup']['status'], 'failed')
        self.assertEqual(result['sourcePlan'], self.plan)
        self.assertEqual(result['progress'], r.validate_checkpoint(CONFIG, self.plan, self.checkpoint))
        self.assertEqual(result['error']['state'], 'new-jersey')
        self.assertEqual(result['error']['type'], 'CalledProcessError')
        self.assertLess(self.result.stat().st_size, 100_000)

    def test_hard_interrupt_leaves_a_complete_prefix_journal(self):
        with patch.object(r.legacy, 'download_source', side_effect=self.fail_second(KeyboardInterrupt())):
            with self.assertRaises(KeyboardInterrupt):
                runner.run(self.request(), self.checkpoint, self.work, self.result)
        result = json.loads(self.result.read_text())
        self.assertEqual(result['sourceGroup']['completedStates'], ['new-york'])
        self.assertEqual(result['sourceGroup']['status'], 'running')
        self.assertNotIn('error', result)
        self.assertEqual(result['progress'], r.validate_checkpoint(CONFIG, self.plan, self.checkpoint))

    def test_pinning_elapsed_time_does_not_create_extra_source_time(self):
        clock = [0]
        def discover(*args, **kwargs):
            clock[0] = 290
            return self.plan
        with patch.object(r.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(r, 'plan_sources', side_effect=discover):
            result = runner.run(self.request('start', seconds=300), self.checkpoint, self.work, self.result)
        self.assertEqual(result['sourceGroup']['status'], 'paused')
        self.assertEqual(result['sourceGroup']['completedStates'], [])
        r.legacy.download_source.assert_not_called()

    def test_remaining_deadline_reaches_download_and_both_osmium_commands(self):
        clock = [0]
        transfers, commands = [], []
        def download(url, path, **kwargs):
            transfers.append(kwargs['timeout'])
            Path(path).write_bytes(SOURCE)
            clock[0] += 200
        def osmium(*args, **kwargs):
            commands.append(kwargs['timeout'])
            fake_osmium(*args)
            clock[0] += 10
        with patch.object(r.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(r.legacy, 'download_source', side_effect=download), \
                patch.object(r.legacy, 'run', side_effect=osmium):
            result = runner.run(self.request(), self.checkpoint, self.work, self.result)
        self.assertEqual(transfers, [335])
        self.assertEqual(commands, [165, 155])
        self.assertEqual(result['sourceGroup']['completedStates'], ['new-york'])
        self.assertEqual(result['sourceGroup']['status'], 'paused')

    def test_larger_source_cap_preserves_all_finalization_reserves(self):
        clock = [0]
        transfers, commands = [], []
        def download(url, path, **kwargs):
            transfers.append(kwargs['timeout'])
            Path(path).write_bytes(SOURCE)
            clock[0] += 300
        def osmium(*args, **kwargs):
            commands.append(kwargs['timeout'])
            fake_osmium(*args)
            clock[0] += 10
        with patch.object(r.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(r.legacy, 'download_source', side_effect=download), \
                patch.object(r.legacy, 'run', side_effect=osmium):
            result = runner.run(self.request(seconds=600), self.checkpoint, self.work, self.result)
        # 600s is a ceiling, not a promise: 15s result, 30s checkpoint
        # validation, and 30s source processing are reserved before transfer.
        self.assertEqual(transfers, [525])
        self.assertEqual(commands, [255, 245])
        self.assertEqual(result['sourceGroup']['completedStates'], ['new-york'])
        self.assertEqual(result['sourceGroup']['status'], 'paused')

    def test_runner_rejects_unbounded_source_time_without_transfer(self):
        for value in (True, 0, -1, 1201, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'time budget'):
                runner.run(self.request(seconds=value), self.checkpoint, self.work, self.result)
        r.legacy.download_source.assert_not_called()

    def test_largest_source_stage_still_caps_each_transfer_at_twelve_minutes(self):
        clock = [0]
        transfers = []
        def download(url, path, **kwargs):
            transfers.append(kwargs['timeout'])
            Path(path).write_bytes(SOURCE)
            clock[0] += 600
        with patch.object(r.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(r.legacy, 'download_source', side_effect=download):
            result = runner.run(self.request(seconds=1200), self.checkpoint, self.work, self.result)
        self.assertEqual(transfers, [720])
        self.assertEqual(result['sourceGroup']['completedStates'], ['new-york'])
        self.assertEqual(result['sourceGroup']['status'], 'paused')

    def test_invalid_and_exhausted_deadlines_never_download(self):
        with patch.object(r.time, 'monotonic', return_value=100):
            with self.assertRaises(TimeoutError):
                r.build_source_stage(CONFIG, self.plan, 'new-york', self.work, self.checkpoint, deadline=99)
        r.legacy.download_source.assert_not_called()
        for invalid in (True, 0, -1, 1186, float('inf'), float('nan')):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                r.build_source_group(CONFIG, self.plan, self.checkpoint, self.work, seconds=invalid)

    def test_crop_descriptor_cannot_disagree_with_valid_inventory(self):
        r.build_source_stage(CONFIG, self.plan, 'new-york', self.work, self.checkpoint)
        root = self.checkpoint / 'sources/new-york'
        original = json.loads((root / 'receipt.json').read_text())
        for change in ({'sha256': '0' * 64}, {'bytes': 1}, {'encoding': 'gzip'}, {'parts': []}):
            receipt = json.loads(json.dumps(original))
            receipt['crop'].update(change)
            r.write_json(root / 'receipt.json', receipt)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                r.validate_checkpoint(CONFIG, self.plan, self.checkpoint)

    def test_crop_chunk_reordering_and_duplication_fail_closed(self):
        with patch.object(r, 'CHUNK_BYTES', 8):
            r.build_source_stage(CONFIG, self.plan, 'new-york', self.work, self.checkpoint)
            root = self.checkpoint / 'sources/new-york'
            original = json.loads((root / 'receipt.json').read_text())
            for parts in (list(reversed(original['crop']['parts'])), original['crop']['parts'] * 2):
                receipt = json.loads(json.dumps(original))
                receipt['crop']['parts'] = parts
                r.write_json(root / 'receipt.json', receipt)
                with self.assertRaisesRegex(RuntimeError, 'chunk order'):
                    r.validate_checkpoint(CONFIG, self.plan, self.checkpoint)

    def test_result_size_limit_does_not_replace_previous_receipt(self):
        runner.write_result(self.result, {'schemaVersion': 1})
        before = self.result.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'IPC size'):
            runner.write_result(self.result, {'large': 'x' * 100_000})
        self.assertEqual(self.result.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
