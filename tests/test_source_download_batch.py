"""Managed whole-source concurrency with fake processes; never uses network."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_resumable_stages import CLOCK, CONFIG, RUNTIME, SOURCE, fake_osmium, plan
import build_region as b
import resumable_region as r
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'publishing'))
import resumable_stage_runner as runner


class FakeProcess:
    def __init__(self, args, *, data=None, code=None, status=b'200', stuck=False, kill_error=False):
        self.args = args
        self.path = Path(args[args.index('-o')+1])
        self.returncode = code
        self.status = status
        self.stuck = stuck
        self.kill_error = kill_error
        self.stdout = io.BytesIO()
        self.killed = False
        self.reaped = threading.Event()
        self.wait_timeouts = []
        if data is not None:
            self.path.write_bytes(data)

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if self.kill_error:
            raise OSError('fake kill failure')
        if not self.stuck:
            self.returncode = -9

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.stuck or self.returncode is None:
            raise subprocess.TimeoutExpired('fake curl', timeout)
        self.reaped.set()
        return self.returncode

    def communicate(self, timeout=None):
        return self.status, None


class SourceDownloadBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root/'ledger.json'
        self.processes = []
        self.source_plan = plan()
        self.requests = [{'state': item['state'], 'url': item['url'], 'path': self.root/(item['state']+'.pbf')}
                         for item in self.source_plan['sources']]
        environment = patch.dict(b.os.environ, {'RIDE_DOWNLOAD_LEDGER': str(self.ledger)})
        environment.start(); self.addCleanup(environment.stop)

    def spawn(self, outcomes, reserved=None):
        def start(args, **kwargs):
            if reserved is not None:
                self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], reserved)
            self.assertEqual(kwargs, {'stdout': subprocess.PIPE})
            self.assertEqual(args[:2], ['curl', '--disable'])
            self.assertNotIn('--location', args)
            self.assertNotIn('--range', args)
            self.assertNotIn('--header', args)
            self.assertEqual(args[args.index('--retry')+1], '0')
            self.assertLessEqual(float(args[args.index('--max-time')+1]), 720)
            self.assertEqual(args[args.index('--max-filesize')+1], '1500000000')
            process = FakeProcess(args, **outcomes[len(self.processes)])
            self.processes.append(process)
            return process
        return patch.object(b.subprocess, 'Popen', side_effect=start)

    def batch(self, seconds=30):
        return b.SourceDownloadBatch(self.requests, deadline=time.monotonic()+seconds)

    def stage_patches(self):
        for item in (
            patch.object(r, 'now_utc', side_effect=lambda now=None: now or CLOCK),
            patch.object(r, 'runtime_fingerprint', return_value=RUNTIME),
            patch.object(r, 'source_timestamp', return_value=self.source_plan['osmReplicationTimestamp']),
            patch.object(r.legacy, 'download_source', side_effect=AssertionError('must use the admitted whole-source transfer')),
            patch.object(r.legacy, 'run', side_effect=fake_osmium),
        ):
            item.start(); self.addCleanup(item.stop)

    def test_both_full_reservations_precede_either_process_and_refund_once(self):
        with self.spawn([{'data': b'first', 'code': 0}, {'data': b'second', 'code': 0}], reserved=3_000_000_000):
            with self.batch() as batch:
                self.assertEqual(batch.wait_for('new-york').read_bytes(), b'first')
                self.assertEqual(batch.wait_for('new-jersey').read_bytes(), b'second')
                self.assertEqual(batch.summary['admittedDownloads'], 2)
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], 11)
        self.assertEqual(len(self.processes), 2)
        self.assertTrue(all(process.reaped.is_set() for process in self.processes))
        self.assertTrue(all(all(0 < value <= 5 for value in process.wait_timeouts) for process in self.processes))

    def test_prior_metadata_spend_admits_one_full_reservation_explicitly(self):
        self.ledger.write_text(json.dumps({'reservedBytes': 17}))
        with self.spawn([{'data': b'whole', 'code': 0}], reserved=1_500_000_017):
            with self.batch() as batch:
                batch.wait_for('new-york')
                self.assertEqual(batch.summary, {'requestedDownloads': 2, 'admittedDownloads': 1,
                    'reservedBytes': 1_500_000_000, 'singleSourceFallback': True})
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], 22)

    def test_insufficient_full_allowance_never_starts_or_changes_ledger(self):
        before=json.dumps({'reservedBytes': 1_500_000_001})
        self.ledger.write_text(before)
        with patch.object(b.subprocess, 'Popen') as spawn, self.assertRaisesRegex(RuntimeError, 'full source'):
            with self.batch():
                self.fail('must not enter')
        spawn.assert_not_called()
        self.assertEqual(self.ledger.read_text(), before)

    def test_http_redirect_denial_and_partial_response_are_not_retried(self):
        for status, code in ((b'302', 0), (b'403', 22), (b'429', 22), (b'206', 0)):
            with self.subTest(status=status):
                self.processes=[]
                self.ledger.unlink(missing_ok=True)
                self.requests[0]['path'].unlink(missing_ok=True)
                with self.spawn([{'data': b'rejected', 'code': code, 'status': status}]):
                    with self.assertRaises(b.SourceDownloadFailure):
                        with b.SourceDownloadBatch(self.requests[:1], deadline=time.monotonic()+10) as batch:
                            batch.wait_for('new-york')
                self.assertEqual(len(self.processes), 1)
                self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], 1_500_000_000)

    def test_transfer_error_kills_and_reaps_running_sibling_without_refund(self):
        with self.spawn([{'code': None}, {'code': 28}]):
            with self.assertRaises(b.SourceDownloadFailure):
                with self.batch() as batch:
                    batch.wait_for('new-york')
        self.assertTrue(self.processes[0].killed)
        self.assertTrue(all(process.reaped.is_set() for process in self.processes))
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], 3_000_000_000)

    def test_shared_deadline_kills_and_reaps_both_pending_processes(self):
        with self.spawn([{'code': None}, {'code': None}]):
            with self.assertRaises(b.SourceDownloadFailure) as error:
                with self.batch(seconds=0.05) as batch:
                    batch.wait_for('new-york')
        self.assertIsInstance(error.exception.cause, TimeoutError)
        self.assertTrue(all(process.killed and process.reaped.is_set() for process in self.processes))
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], 3_000_000_000)

    def test_720_second_transfer_cap_does_not_expand_shorter_shared_deadline(self):
        with self.spawn([{'data': b'a', 'code': 0}, {'data': b'b', 'code': 0}]):
            with self.batch(seconds=1100) as batch:
                for request in self.requests:
                    batch.wait_for(request['state'])
        values=[float(process.args[process.args.index('--max-time')+1]) for process in self.processes]
        self.assertTrue(all(719 < value <= 720 for value in values))
        self.assertGreaterEqual(values[0], values[1])

    def test_partial_startup_failure_reaps_first_and_keeps_both_reservations(self):
        def spawn(args, **kwargs):
            if self.processes:
                raise OSError('second process could not start')
            process=FakeProcess(args)
            self.processes.append(process)
            return process
        with patch.object(b.subprocess, 'Popen', side_effect=spawn), self.assertRaises(OSError):
            with self.batch():
                self.fail('must not enter')
        self.assertTrue(self.processes[0].killed and self.processes[0].reaped.is_set())
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], 3_000_000_000)

    def test_caller_exception_cancels_both_without_waiting_for_transfer_cap(self):
        with self.spawn([{'code': None}, {'code': None}]):
            with self.assertRaisesRegex(ValueError, 'crop failed'):
                with self.batch():
                    raise ValueError('crop failed')
        self.assertTrue(all(process.killed and process.reaped.is_set() for process in self.processes))

    def test_reap_timeout_is_bounded_and_not_silently_successful(self):
        with self.spawn([{'code': None, 'stuck': True}, {'code': 28}]):
            with self.assertRaises(b.SourceDownloadFailure):
                with self.batch() as batch:
                    batch.wait_for('new-york')
        self.assertTrue(self.processes[0].killed)
        self.assertTrue(all(0 < value <= 5 for value in self.processes[0].wait_timeouts))

    def test_one_kill_failure_does_not_skip_sibling_cancellation(self):
        with self.spawn([{'code': None, 'kill_error': True}, {'code': None}]):
            with self.assertRaisesRegex(ValueError, 'stop'):
                with self.batch():
                    raise ValueError('stop')
        self.assertTrue(all(process.killed for process in self.processes))
        self.assertTrue(self.processes[1].reaped.is_set())
        self.assertTrue(all(0 < value <= 5 for value in self.processes[0].wait_timeouts))

    def test_unapproved_or_mismatched_urls_fail_before_reservation(self):
        for url in ('https://other.example/new-york-261001.osm.pbf',
                    self.requests[1]['url'], self.requests[0]['url'].replace('261001','latest')):
            with self.subTest(url=url), self.assertRaises(ValueError):
                b.SourceDownloadBatch([{**self.requests[0], 'url': url}], deadline=time.monotonic()+10)
        self.assertFalse(self.ledger.exists())

    def test_second_failure_during_first_validation_keeps_only_first_crop(self):
        self.stage_patches()
        def osmium(*args, **kwargs):
            if not self.processes[1].reaped.is_set():
                self.processes[1].returncode=28
                self.assertTrue(self.processes[1].reaped.wait(timeout=2))
            fake_osmium(*args)
        with self.spawn([{'data': SOURCE, 'code': 0}, {'code': None}]), patch.object(r.legacy,'run',side_effect=osmium):
            with self.assertRaises(r.SourceGroupFailure) as failure:
                r.build_source_group(CONFIG,self.source_plan,self.root/'checkpoint',self.root/'work',
                                     seconds=1185,concurrent_downloads=2)
        self.assertEqual(failure.exception.group['completedStates'],['new-york'])
        self.assertEqual(failure.exception.detail['state'],'new-jersey')
        self.assertFalse(failure.exception.detail['permanent'])
        self.assertEqual(r.validate_checkpoint(CONFIG,self.source_plan,self.root/'checkpoint')['completedStates'],['new-york'])
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'],1_500_000_000+len(SOURCE))
        self.assertFalse(list((self.root/'work').rglob('*.osm.pbf')))

    def test_first_failure_cannot_commit_later_completed_download(self):
        self.stage_patches()
        with self.spawn([{'code': 28}, {'data': SOURCE, 'code': 0}]):
            with self.assertRaises(r.SourceGroupFailure) as failure:
                r.build_source_group(CONFIG,self.source_plan,self.root/'checkpoint',self.root/'work',concurrent_downloads=2)
        self.assertEqual(failure.exception.group['completedStates'],[])
        self.assertFalse((self.root/'checkpoint'/'sources').exists())
        r.legacy.run.assert_not_called()

    def test_corrupt_first_source_cancels_pending_second_and_commits_neither(self):
        self.stage_patches()
        with self.spawn([{'data': b'x'*len(SOURCE), 'code': 0}, {'code': None}]):
            with self.assertRaises(r.SourceGroupFailure) as failure:
                r.build_source_group(CONFIG,self.source_plan,self.root/'checkpoint',self.root/'work',concurrent_downloads=2)
        self.assertIn('checksum mismatch',failure.exception.detail['message'])
        self.assertEqual(failure.exception.group['completedStates'],[])
        self.assertTrue(self.processes[1].killed and self.processes[1].reaped.is_set())

    def test_pins_expiry_and_runtime_are_checked_before_any_raw_request(self):
        self.stage_patches()
        bad=plan(expires=True)
        with patch.object(b.subprocess,'Popen') as spawn, self.assertRaisesRegex(RuntimeError,'expired'):
            r.build_source_group(CONFIG,bad,self.root/'checkpoint',self.root/'work',concurrent_downloads=2)
        spawn.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_successful_batch_validates_and_journals_sources_in_configured_order(self):
        self.stage_patches()
        notices=[]
        with self.spawn([{'data': SOURCE, 'code': 0}, {'data': SOURCE, 'code': 0}]):
            result=r.build_source_group(CONFIG,self.source_plan,self.root/'checkpoint',self.root/'work',
                concurrent_downloads=2,on_progress=lambda group, progress:notices.append(group['completedStates']))
        self.assertEqual(notices,[['new-york'],['new-york','new-jersey']])
        self.assertEqual(result['completedStates'],CONFIG['sourceStates'])
        self.assertEqual(result['status'],'complete')
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'],2*len(SOURCE))

    def test_runner_defaults_to_two_managed_downloads_and_bounds_receipt(self):
        self.stage_patches()
        with self.spawn([{'data': SOURCE, 'code': 0}, {'data': SOURCE, 'code': 0}]):
            result=runner.run({'config':CONFIG,'sourcePlan':self.source_plan,
                'stage':{'kind':'source','state':'new-york'},'stageSeconds':1200},
                self.root/'checkpoint',self.root/'work',self.root/'result.json')
        self.assertEqual(len(self.processes),2)
        self.assertEqual(result['sourceGroup']['acquisition']['admittedDownloads'],2)
        self.assertEqual(result['sourceGroup']['completedStates'],CONFIG['sourceStates'])
        self.assertLess((self.root/'result.json').stat().st_size,100_000)
        self.assertNotIn(str(self.root), (self.root/'result.json').read_text())


if __name__ == '__main__':
    unittest.main()
