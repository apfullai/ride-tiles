"""Tiny offline candidates and real process-tree watchdogs; never a tile build."""
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'development'))
import vm_continue as vm
r, snapshot = vm.rollout, vm.snapshot
sys.path.insert(0, str(ROOT / 'tools'))
import resumable_region

CODE = 'c' * 64
GENERATION = 'a' * 64
RUNTIME = {'python': '3.12.15', 'implementation': 'cpython', 'shapely': '2.1.2',
           'geos': '3.13.1', 'osmium': 'osmium version 1.16.0\nlibosmium version 2.20.0'}
NOW = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)


def read_manifest(site):
    return json.loads((site / 'manifest.json').read_text())


class LocalCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.live = self.root / 'live'
        self.live.mkdir()
        self.config = {'id': 'chicagoland', 'name': 'Chicago', 'bounds': [0, 0, 1, 1],
                       'minzoom': 8, 'maxzoom': 14, 'sourceStates': ['a', 'b', 'c', 'd']}
        self.plan = {'planId': 'd' * 64, 'runtime': RUNTIME, 'sourceDate': '2026-10-13',
                     'expiresAt': '2026-10-20T00:00:00+00:00', 'sources': ['a', 'b', 'c', 'd']}
        self.expected = {'region': self.config['id'], 'generation': GENERATION, 'fingerprint': CODE}
        self.fetches = []
        self.executions = []
        self.addCleanup(patch.stopall)
        patch.object(r, 'load_configs', return_value=[self.config]).start()
        patch.object(snapshot, '_validate_data', side_effect=lambda site, *args: read_manifest(site)).start()
        patch.object(snapshot, 'validate_site', side_effect=read_manifest).start()
        patch.object(r, 'validate_site', side_effect=read_manifest).start()
        patch.object(vm.verified_code, 'current_fingerprint', return_value=CODE).start()
        patch.object(vm, 'runtime_fingerprint', return_value=RUNTIME).start()
        patch.object(resumable_region, 'validate_checkpoint', side_effect=self.inspector).start()
        patch.object(snapshot, '_fetch', side_effect=self.fetch).start()
        patch.object(r, 'subprocess_stage', side_effect=self.execute).start()
        # Any accidental use of Actions admission/full test proof or source HTTP
        # turns a tiny offline test into an immediate failure, not a network call.
        patch.object(r, 'main', side_effect=AssertionError('Actions main is forbidden')).start()
        self.verify = patch.object(r, 'verify_code', return_value=({'schemaVersion': 1, 'fingerprint': CODE, 'runId': None, 'commit': None, 'localFullTestsPassed': True}, 'full-tests-passed')).start()
        patch('urllib.request.urlopen', side_effect=AssertionError('Source/network probe is forbidden')).start()
        patch.dict(os.environ, {}, clear=True).start()
        old = {'id': self.config['id'], 'name': self.config['name'], 'version': 'b' * 16,
               'bounds': self.config['bounds'], 'minzoom': 8, 'maxzoom': 14, 'sourceLayer': 'cycleinfra'}
        self.old_region = old
        r.write_json(self.live / 'manifest.json', {'schemaVersion': 1, 'regions': [old]})
        file = self.live / 'regions' / old['id'] / old['version'] / 'tiles/8/1/1.pbf'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'old tiny fixture tile')
        (self.live / r.ACTIVE).mkdir(parents=True)
        progress = {**r.empty_progress(self.config), 'completedStates': self.config['sourceStates']}
        r.write_json(self.live / r.ACTIVE / 'progress.json', progress)
        cp = {'schemaVersion': 1, 'generationId': GENERATION, 'config': self.config,
              'configFingerprint': r.fingerprint(self.config), 'codeFingerprint': CODE, 'purpose': 'refresh',
              'sourcePlan': self.plan, 'progress': progress, 'nextStage': {'kind': 'index'},
              'startedAt': NOW.isoformat(), 'updatedAt': NOW.isoformat(), 'retryAfter': None,
              'lastError': None, 'stageCount': 4, 'failureCount': 0, 'needsAttention': False}
        r.save_checkpoint(self.live, cp)
        snapshot.seal_snapshot(self.live)
        self.live_before = snapshot._records(self.live, include_metadata=True)

    def fetch(self, url, maximum):
        prefix = snapshot.BASE.rstrip('/') + '/'
        self.assertTrue(url.startswith(prefix))
        relative = url[len(prefix):]
        self.fetches.append(relative)
        data = (self.live / relative).read_bytes()
        self.assertLessEqual(len(data), maximum)
        return data

    def inspector(self, config, plan, path):
        return json.loads((path / 'progress.json').read_text())

    def execute(self, request, checkpoint, work, seconds):
        self.assertIn(request['stage']['kind'], vm.ALLOWED_STAGES)
        self.assertEqual(request['sourcePlan'], self.plan)
        self.assertLessEqual(seconds, r.STAGE_SECONDS)
        self.executions.append(request['stage']['kind'])
        progress = self.inspector(request['config'], request['sourcePlan'], checkpoint)
        kind = request['stage']['kind']
        if kind == 'index':
            progress.update(indexReady=True, batchIds=[0, 1], batchCount=2)
        elif kind == 'tiles':
            progress['completedBatches'].append(request['stage']['batchId'])
        r.write_json(checkpoint / 'progress.json', progress)
        generated = work / 'region-site'
        result = {'schemaVersion': 1, 'sourcePlan': self.plan, 'progress': progress}
        if kind == 'finalize':
            entry = {**self.old_region, 'version': 'e' * 16}
            file = generated / 'regions' / entry['id'] / entry['version'] / 'tiles/8/1/1.pbf'
            file.parent.mkdir(parents=True)
            file.write_bytes(b'new tiny fixture tile')
            result['region'] = entry
        return result, generated

    def run_command(self, command, **kwargs):
        # Most state-machine tests retain mock observations in-process. Dedicated
        # supervisor tests below exercise real fork/setsid/pidfd/reaping behavior.
        with patch.object(vm, 'supervised', side_effect=lambda callback, *_: callback()):
            with contextlib.redirect_stdout(io.StringIO()):
                return vm.run(command, self.workspace, kwargs.pop('seconds', 900),
                              kwargs.pop('expected', self.expected if command == 'restore' else None))

    def current(self):
        pointer = vm.read_pointer(self.workspace)
        return pointer, self.workspace / 'revisions' / pointer['current']['revision'] / 'site'

    def restore(self):
        result = self.run_command('restore')
        self.assertTrue(result['changed'])
        self.assertTrue(result['candidateOnly'])
        return result

    def pointer_bytes(self):
        return (self.workspace / 'current.json').read_bytes()

    def mutate_live_checkpoint(self, change):
        cp = json.loads((self.live / r.ACTIVE / r.DESCRIPTOR).read_text())
        change(cp)
        r.save_checkpoint(self.live, cp)
        snapshot.seal_snapshot(self.live)

    def test_restore_uses_verified_snapshot_and_explicit_identity(self):
        result = self.restore()
        self.assertEqual(result['nextStage'], {'kind': 'index'})
        self.assertEqual(result['completedSources'], 4)
        self.assertIn('snapshot/archive.tar.gz.part00000', self.fetches)
        self.assertEqual(self.executions, [])
        pointer, site = self.current()
        self.assertEqual(pointer['baseline'], pointer['current'])
        self.assertEqual(snapshot._records(site, include_metadata=True), self.live_before)

    def test_continue_uses_local_progress_does_not_restore_over_it(self):
        self.restore()
        self.fetches.clear()
        first = self.run_command('continue')
        second = self.run_command('continue')
        self.assertEqual(first['nextStage'], {'kind': 'tiles', 'batchId': 0})
        self.assertEqual(second['nextStage'], {'kind': 'tiles', 'batchId': 1})
        self.assertEqual(self.executions, ['index', 'tiles'])
        self.assertTrue(all(x in ('manifest.json', 'snapshot.json') for x in self.fetches))
        self.assertEqual(snapshot._records(self.live, include_metadata=True), self.live_before)
        self.assertLessEqual(len(list((self.workspace / 'revisions').iterdir())), 3)

    def test_finalize_remains_local_and_completed_repeat_is_no_work(self):
        self.restore()
        for _ in range(4):
            result = self.run_command('continue')
        self.assertTrue(result['complete'])
        self.assertIsNone(result['nextStage'])
        pointer = self.pointer_bytes()
        result = self.run_command('continue')
        self.assertFalse(result['changed'])
        self.assertEqual(pointer, self.pointer_bytes())
        self.assertEqual(self.executions, ['index', 'tiles', 'tiles', 'finalize'])
        self.assertEqual(snapshot._records(self.live, include_metadata=True), self.live_before)
        self.assertEqual(len(list((self.workspace / 'revisions').iterdir())), 3)
        self.assertTrue(self.run_command('status')['complete'])

    def test_expired_acquisition_window_does_not_block_acquired_sources(self):
        # Production validate_checkpoint uses require_unexpired=False. Wrapper
        # adds no new expiry gate or source request for the acquired generation.
        self.plan['expiresAt'] = '2026-10-18T00:00:00+00:00'
        self.mutate_live_checkpoint(lambda cp: cp.update(sourcePlan=self.plan))
        self.restore()
        self.assertTrue(self.run_command('continue')['changed'])

    def test_insufficient_time_no_work_preserves_pointer(self):
        self.restore()
        before = self.pointer_bytes()
        result = self.run_command('continue', seconds=1)
        self.assertFalse(result['changed'])
        self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(self.executions, [])

    def test_planned_pause_without_progress_keeps_current(self):
        self.restore()
        self.run_command('continue')
        before = self.pointer_bytes()
        def pause(request, cp, work, seconds):
            return {'schemaVersion': 1, 'sourcePlan': self.plan,
                    'progress': self.inspector(request['config'], self.plan, cp), 'plannedPause': True}, work
        with patch.object(r, 'subprocess_stage', side_effect=pause):
            result = self.run_command('continue')
        self.assertFalse(result['changed'])
        self.assertEqual(before, self.pointer_bytes())

    def test_status_is_local_only(self):
        self.restore()
        with patch.object(snapshot, '_fetch', side_effect=AssertionError('Status must be offline')):
            self.assertEqual(self.run_command('status')['nextStage']['kind'], 'index')

    def test_actions_free_and_no_forged_identity(self):
        self.restore()
        self.run_command('continue')
        self.assertFalse(any(k.startswith('GITHUB_') for k in os.environ))
        _, site = self.current()
        receipt = json.loads((site / 'verification.json').read_text())
        self.assertIsNone(receipt['runId'])
        self.assertIsNone(receipt['commit'])
        self.assertTrue(receipt['localFullTestsPassed'])
        self.verify.assert_called()
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            with self.assertRaisesRegex(vm.CandidateError, 'outside GitHub Actions'):
                self.run_command('continue')

    def test_wrong_generation_or_fingerprint_or_region_rejected(self):
        for field, value in [('generation', 'f' * 64), ('fingerprint', 'f' * 64), ('region', 'unknown')]:
            with self.subTest(field=field):
                with self.assertRaises((ValueError, vm.CandidateError)):
                    self.run_command('restore', expected={**self.expected, field: value})
                self.assertFalse((self.workspace / 'current.json').exists())
        self.assertEqual(self.executions, [])

    def test_missing_generation_and_source_stage_rejected(self):
        def source(cp):
            progress = {**cp['progress'], 'completedStates': ['a', 'b', 'c']}
            r.write_json(self.live / r.ACTIVE / 'progress.json', progress)
            cp.update(progress=progress, nextStage={'kind': 'source', 'state': 'd'})
        self.mutate_live_checkpoint(source)
        with self.assertRaisesRegex(vm.CandidateError, 'Every configured source'):
            self.run_command('restore')
        shutil.rmtree(self.live / r.ACTIVE)
        snapshot.seal_snapshot(self.live)
        with self.assertRaisesRegex(vm.CandidateError, 'absent'):
            self.run_command('restore')

    def test_start_stage_and_unpinned_generation_rejected(self):
        def start(cp):
            (self.live / r.ACTIVE / 'progress.json').unlink()
            cp.update(sourcePlan=None, progress=r.empty_progress(self.config), nextStage={'kind': 'start'})
        self.mutate_live_checkpoint(start)
        with self.assertRaisesRegex(vm.CandidateError, 'pinned active generation'):
            self.run_command('restore')
        self.assertEqual(self.executions, [])

    def test_attention_backoff_or_failure_rejected(self):
        for changed in ({'needsAttention': True}, {'retryAfter': (NOW + dt.timedelta(days=1)).isoformat()},
                        {'failureCount': 1}, {'lastError': {'message': 'blocked'}}):
            with self.subTest(changed=changed):
                self.mutate_live_checkpoint(lambda cp: cp.update(needsAttention=False, retryAfter=None,
                                            failureCount=0, lastError=None))
                self.mutate_live_checkpoint(lambda cp: cp.update(changed))
                with self.assertRaisesRegex(vm.CandidateError, 'attention or has failure/backoff'):
                    self.run_command('restore')
        self.assertEqual(self.executions, [])

    def test_runtime_mismatch_and_missing_osmium_fail_closed(self):
        with patch.object(vm, 'runtime_fingerprint', return_value={**RUNTIME, 'python': '3.12.14'}):
            with self.assertRaisesRegex(vm.CandidateError, 'runtime differs'):
                self.run_command('restore')
        with patch.object(vm, 'runtime_fingerprint', side_effect=FileNotFoundError('osmium')):
            with self.assertRaisesRegex(FileNotFoundError, 'osmium'):
                self.run_command('restore')
        self.assertFalse((self.workspace / 'current.json').exists())

    def test_resume_checks_production_and_wrapper_and_runtime(self):
        self.restore()
        before = self.pointer_bytes()
        for target, replacement in ((vm.verified_code, {'current_fingerprint': 'f' * 64}),
                                    (vm, {'wrapper_fingerprint': 'f' * 64}),
                                    (vm, {'runtime_fingerprint': {'wrong': 'runtime'}})):
            name, value = next(iter(replacement.items()))
            with patch.object(target, name, return_value=value):
                with self.assertRaises(vm.CandidateError):
                    self.run_command('continue')
            self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(self.executions, [])

    def test_corrupt_all_candidate_components_fail_closed(self):
        self.restore()
        _, site = self.current()
        for relative in ('manifest.json', 'snapshot.json', 'snapshot/archive.tar.gz.part00000',
                         str(r.ACTIVE / r.DESCRIPTOR), str(r.ACTIVE / 'progress.json')):
            with self.subTest(relative=relative):
                path = site / relative
                old = path.read_bytes()
                path.write_bytes(old + b'corruption')
                with self.assertRaisesRegex(vm.CandidateError, 'tree hash'):
                    self.run_command('continue')
                path.write_bytes(old)
        self.assertEqual(self.executions, [])

    def test_corrupt_pointer_metadata_or_symlink_rejected(self):
        self.restore()
        pointer, site = self.current()
        before = self.pointer_bytes()
        metadata = site.parent / 'metadata.json'
        original = metadata.read_bytes()
        metadata.write_bytes(original + b' ')
        with self.assertRaisesRegex(vm.CandidateError, 'metadata hash'):
            self.run_command('continue')
        metadata.write_bytes(original)
        vm.write_atomic(self.workspace / 'current.json', {**pointer, 'extra': 'invalid'})
        with self.assertRaisesRegex(vm.CandidateError, 'resume pointer'):
            self.run_command('continue')
        (self.workspace / 'current.json').write_bytes(before)
        (site / 'link').symlink_to(self.live / 'manifest.json')
        with self.assertRaisesRegex(ValueError, 'regular files'):
            self.run_command('continue')

    def test_live_revision_change_before_stage_retains_candidate(self):
        self.restore()
        before = self.pointer_bytes()
        (self.live / 'status.html').write_text('a different publication')
        snapshot.seal_snapshot(self.live)
        with self.assertRaisesRegex(ValueError, 'publication revision changed'):
            self.run_command('continue')
        self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(self.executions, [])

    def test_live_revision_change_during_stage_discards_local_progress(self):
        self.restore()
        before = self.pointer_bytes()
        execute = self.execute
        def racing(*args):
            result = execute(*args)
            (self.live / 'status.html').write_text('new publication during index')
            snapshot.seal_snapshot(self.live)
            return result
        with patch.object(r, 'subprocess_stage', side_effect=racing):
            with self.assertRaisesRegex(ValueError, 'publication revision changed'):
                self.run_command('continue')
        self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(self.executions, ['index'])

    def test_source_pin_change_attempt_retains_prior_candidate(self):
        self.restore()
        before = self.pointer_bytes()
        def bad_plan(*args):
            result, generated = self.execute(*args)
            result['sourcePlan'] = {**self.plan, 'planId': 'f' * 64}
            return result, generated
        with patch.object(r, 'subprocess_stage', side_effect=bad_plan):
            with self.assertRaisesRegex(vm.CandidateError, 'Stage failed'):
                self.run_command('continue')
        self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(self.run_command('status')['nextStage']['kind'], 'index')

    def test_partial_stage_exception_and_interrupt_preserve_prior(self):
        self.restore()
        before = self.pointer_bytes()
        for exception in (r.StageFailure('fake stage timeout'), r.RolloutInterrupted('fake global crash')):
            def fail(request, checkpoint, *_):
                (checkpoint / 'partial').write_text('discard this')
                raise exception
            with patch.object(r, 'subprocess_stage', side_effect=fail):
                with self.assertRaises((vm.CandidateError, r.RolloutInterrupted)):
                    self.run_command('continue')
            self.assertEqual(before, self.pointer_bytes())
            _, site = self.current()
            self.assertFalse((site / r.ACTIVE / 'partial').exists())
        self.assertTrue(self.run_command('continue')['changed'])

    def test_atomic_pointer_failure_retains_previous_and_retry_works(self):
        self.restore()
        before = self.pointer_bytes()
        original = os.replace
        def fail(source, destination):
            if Path(destination).name == 'current.json':
                raise OSError('fake crash immediately before pointer replace')
            return original(source, destination)
        with patch.object(vm.os, 'replace', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'fake crash'):
                self.run_command('continue')
        self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(self.run_command('status')['nextStage']['kind'], 'index')
        self.assertTrue(self.run_command('continue')['changed'])
        self.assertLessEqual(len(list((self.workspace / 'revisions').iterdir())), 3)

    def test_crash_after_pointer_replace_leaves_a_complete_resumable_candidate(self):
        self.restore()
        original = vm.write_atomic
        def crash_after(path, value):
            original(path, value)
            if path.name == 'current.json':
                raise OSError('crash after durable pointer')
        with patch.object(vm, 'write_atomic', side_effect=crash_after):
            with self.assertRaisesRegex(OSError, 'crash after'):
                self.run_command('continue')
        self.assertEqual(self.run_command('status')['nextStage'], {'kind': 'tiles', 'batchId': 0})
        self.assertEqual(self.run_command('continue')['nextStage'], {'kind': 'tiles', 'batchId': 1})

    def test_candidate_cap_enforced_without_changing_site_limit(self):
        self.restore()
        before = self.pointer_bytes()
        with patch.object(snapshot, 'MAX_SITE_BYTES', 10):
            with self.assertRaisesRegex(ValueError, 'size cap'):
                self.run_command('continue')
        self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(snapshot.MAX_SITE_BYTES, 250_000_000)

    def test_execute_boundary_rejects_sources_and_changed_pins(self):
        context = {'config': self.config, 'sourcePlan': self.plan}
        execute = vm.execute_pinned(context)
        for request in ({'stage': {'kind': 'source'}, 'config': self.config, 'sourcePlan': self.plan},
                        {'stage': {'kind': 'index'}, 'config': self.config, 'sourcePlan': {}}):
            with self.assertRaisesRegex(vm.CandidateError, 'Forbidden stage or changed source pins'):
                execute(request, self.live, self.root, 30)
        self.assertEqual(self.executions, [])

    def test_nonblocking_workspace_lock(self):
        self.workspace.mkdir()
        with vm.workspace_lock(self.workspace, create=True):
            with self.assertRaisesRegex(vm.CandidateError, 'Another command'):
                self.run_command('restore')

    def test_repository_and_symlink_workspaces_rejected(self):
        for path in (ROOT, ROOT / 'development' / 'outputs', ROOT.parent):
            with self.assertRaisesRegex(vm.CandidateError, 'outside and separate'):
                vm.workspace_path(path)
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(vm.CandidateError, 'symlinks'):
            vm.workspace_path(link / 'workspace')

    def test_restore_never_replaces_initialized_workspace(self):
        self.restore()
        before = self.pointer_bytes()
        with self.assertRaisesRegex(vm.CandidateError, 'already initialized'):
            self.run_command('restore')
        self.assertEqual(before, self.pointer_bytes())

    def test_restore_rejects_unowned_existing_contents(self):
        unrelated = self.workspace / 'revisions' / ('a' * 32) / 'important.txt'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('must survive')
        with self.assertRaisesRegex(vm.CandidateError, 'new or empty'):
            self.run_command('restore')
        self.assertEqual(unrelated.read_text(), 'must survive')
        self.assertFalse((self.workspace / vm.OWNER).exists())

    def test_missing_or_invalid_ownership_marker_rejects_resume(self):
        self.restore()
        marker = self.workspace / vm.OWNER
        old = marker.read_bytes()
        marker.unlink()
        with self.assertRaisesRegex(vm.CandidateError, 'ownership marker'):
            self.run_command('continue')
        marker.write_text('{}')
        with self.assertRaisesRegex(vm.CandidateError, 'ownership marker'):
            self.run_command('continue')
        marker.write_bytes(old)
        self.assertEqual(self.run_command('status')['nextStage']['kind'], 'index')

    def test_status_does_not_prune_owned_orphan_work(self):
        self.restore()
        orphan = self.workspace / ('.transaction-' + 'f' * 32)
        orphan.mkdir()
        (orphan / 'interrupted').write_text('retained for inspection')
        self.run_command('status')
        self.assertTrue(orphan.exists())
        self.run_command('continue')
        self.assertFalse(orphan.exists())

    def test_cleanup_precedes_low_disk_build_admission(self):
        self.restore()
        orphan = self.workspace / ('.transaction-' + 'f' * 32)
        orphan.mkdir()
        (orphan / 'scratch').write_text('bounded cleanup restores space')
        original = shutil.disk_usage
        def usage(path):
            actual = original(path)
            return type(actual)(actual.total, actual.used, 100 if orphan.exists() else actual.free)
        with patch.object(vm.shutil, 'disk_usage', side_effect=usage):
            self.assertTrue(self.run_command('continue')['changed'])
        self.assertFalse(orphan.exists())

    def test_actual_abrupt_worker_death_before_and_after_pointer_swap(self):
        self.restore()
        before = self.pointer_bytes()
        original = os.replace
        def abrupt_before(source, destination):
            if Path(destination).name == 'current.json':
                os._exit(73)
            return original(source, destination)
        with patch.object(vm.os, 'replace', side_effect=abrupt_before):
            with self.assertRaisesRegex(vm.CandidateError, 'worker failed'):
                vm.run('continue', self.workspace, 900)
        self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(self.run_command('status')['nextStage']['kind'], 'index')
        def abrupt_after(source, destination):
            result = original(source, destination)
            if Path(destination).name == 'current.json':
                os._exit(74)
            return result
        with patch.object(vm.os, 'replace', side_effect=abrupt_after):
            with self.assertRaisesRegex(vm.CandidateError, 'worker failed'):
                vm.run('continue', self.workspace, 900)
        self.assertEqual(self.run_command('status')['nextStage'], {'kind': 'tiles', 'batchId': 0})
        self.assertEqual(self.run_command('continue')['nextStage'], {'kind': 'tiles', 'batchId': 1})

    def test_late_timeout_after_commit_recovers_complete_new_pointer(self):
        self.restore()
        commit = vm.commit
        def commit_then_hang(*args):
            result = commit(*args)
            time.sleep(60)
            return result
        with patch.object(vm, 'commit', side_effect=commit_then_hang), \
             patch.object(r, 'FINALIZE_SECONDS', 0), patch.object(r, 'MIN_STAGE_SECONDS', 0):
            with self.assertRaisesRegex(vm.CandidateError, 'remains recoverable; inspect status'):
                vm.run('continue', self.workspace, 2)
        self.assertEqual(self.run_command('status')['nextStage'], {'kind': 'tiles', 'batchId': 0})
        self.assertEqual(self.run_command('continue')['nextStage'], {'kind': 'tiles', 'batchId': 1})


    def test_production_verifier_receipt_must_match_pinned_fingerprint(self):
        self.restore()
        before = self.pointer_bytes()
        with patch.object(r, 'verify_code', return_value=({'fingerprint': 'f' * 64}, 'wrong-proof')):
            with self.assertRaisesRegex(vm.CandidateError, 'different fingerprint'):
                self.run_command('continue')
        self.assertEqual(before, self.pointer_bytes())
        self.assertEqual(self.executions, [])

    def test_whole_command_budget_never_allows_one_hour(self):
        with self.assertRaisesRegex(vm.CandidateError, '900 seconds'):
            self.run_command('restore', seconds=3600)


    def test_real_supervisor_restore_and_one_fake_index(self):
        # All mock stage/HTTP dependencies survive fork; pointer promotion occurs
        # only after the supervised child is reaped successfully.
        with contextlib.redirect_stdout(io.StringIO()):
            result = vm.run('restore', self.workspace, 10, self.expected)
            self.assertEqual(result['nextStage']['kind'], 'index')
            # Production reserve is 135s, so use the normal allowance here; tiny
            # fixture work still completes in substantially less than a second.
            result = vm.run('continue', self.workspace, 900)
        self.assertEqual(result['nextStage'], {'kind': 'tiles', 'batchId': 0})


@unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'pidfd_open'), 'Linux pidfds required')
class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_gone(self, path):
        self.assertTrue(path.exists(), 'fake child did not start')
        for line in path.read_text().splitlines():
            pid = int(line)
            self.assertFalse(Path('/proc', str(pid)).exists(), f'PID {pid} survived or became an unreaped zombie')

    def tree(self, marker, *, worker_crash=False):
        # Worker -> new-session child -> new-session grandchild. Each ignores
        # TERM to exercise watchdog KILL and subreaper adoption, not cooperation.
        code = '''import os,signal,subprocess,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], 'a') as f: f.write(str(os.getpid())+'\\n'); f.flush()
p=subprocess.Popen([sys.executable,'-c',"import os,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); f=open(sys.argv[1],'a'); f.write(str(os.getpid())+chr(10)); f.close(); time.sleep(60)",sys.argv[1]],start_new_session=True)
time.sleep(60)
'''
        process = subprocess.Popen([sys.executable, '-c', code, str(marker)], start_new_session=True)
        limit = time.monotonic() + 5
        while time.monotonic() < limit:
            if marker.exists() and len(marker.read_text().splitlines()) >= 2:
                break
            time.sleep(0.01)
        if worker_crash:
            os.kill(os.getpid(), signal.SIGKILL)
        process.wait()

    def test_global_timeout_kills_reaps_separate_process_groups(self):
        marker = self.root / 'pids'
        started = time.monotonic()
        with self.assertRaisesRegex(vm.CandidateError, 'watchdog expired'):
            vm.supervised(lambda: self.tree(marker), self.root, 0.7)
        self.assertLess(time.monotonic() - started, 4)
        self.assert_gone(marker)

    def test_worker_sigkill_still_kills_reaps_adopted_grandchildren(self):
        marker = self.root / 'pids'
        with self.assertRaisesRegex(vm.CandidateError, 'worker failed'):
            vm.supervised(lambda: self.tree(marker, worker_crash=True), self.root, 10)
        self.assert_gone(marker)

    def test_preexisting_unrelated_process_is_not_signaled(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            with self.assertRaisesRegex(vm.CandidateError, 'child-free CLI'):
                vm.supervised(lambda: time.sleep(60), self.root, 0.1)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_disk_watchdog_kills_and_reaps_stage_tree(self):
        marker = self.root / 'pids'
        def callback():
            vm.write_atomic(self.root / 'disk-admission.json', {'initialFree': 10_000_000_000})
            return self.tree(marker)
        original = shutil.disk_usage
        def usage(path):
            actual = original(path)
            free = 1 if marker.exists() and len(marker.read_text().splitlines()) >= 2 else 10_000_000_000
            return type(actual)(actual.total, actual.used, free)
        with patch.object(vm.shutil, 'disk_usage', side_effect=usage):
            with self.assertRaisesRegex(vm.CandidateError, 'disk watchdog'):
                vm.supervised(callback, self.root, 10)
        self.assert_gone(marker)

    def test_sigterm_cancellation_kills_and_reaps_stage_tree(self):
        marker = self.root / 'pids'
        parent = os.getpid()
        def callback():
            # A worker-owned timer signals the real supervisor after descendants
            # have started. It is itself part of the supervised process tree.
            if os.fork() == 0:
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    if marker.exists() and len(marker.read_text().splitlines()) >= 2:
                        os.kill(parent, signal.SIGTERM)
                        os._exit(0)
                    time.sleep(0.01)
                os._exit(1)
            return self.tree(marker)
        with self.assertRaises(vm.Interrupted):
            vm.supervised(callback, self.root, 10)
        self.assert_gone(marker)


    def test_interrupt_immediately_after_fork_still_reaps_worker(self):
        original_table = vm.proc_table
        original_fork = os.fork
        child = []
        injected = False
        def fork():
            pid = original_fork()
            if pid:
                child.append(pid)
            return pid
        def table():
            nonlocal injected
            if child and not injected:
                injected = True
                raise vm.Interrupted('interrupt before worker birth identity')
            return original_table()
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(vm.os, 'fork', side_effect=fork), patch.object(vm, 'proc_table', side_effect=table):
            with self.assertRaisesRegex(vm.Interrupted, 'before worker birth'):
                vm.supervised(lambda: time.sleep(60), self.root, 10)
        self.assertTrue(child)
        self.assertFalse(Path('/proc', str(child[0])).exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_fork_failure_restores_signal_handlers(self):
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(vm.os, 'fork', side_effect=OSError('simulated fork failure')):
            with self.assertRaisesRegex(OSError, 'fork failure'):
                vm.supervised(lambda: None, self.root, 10)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


    def test_pid_reuse_does_not_reclassify_old_worker(self):
        me = os.getpid()
        # Same PID but a different birth identity and unrelated parent.
        with patch.object(vm, 'proc_table', return_value={99: (1, 222), me: (1, 11)}):
            self.assertEqual(vm.owned_processes((99, 111), set()), {})

    def test_pidfd_signal_requires_matching_birth_identity(self):
        with patch.object(vm, 'owned_processes', return_value={123: 111}), \
             patch.object(vm.os, 'pidfd_open', return_value=456), \
             patch.object(vm, 'proc_table', return_value={123: (1, 222)}), \
             patch.object(vm.signal, 'pidfd_send_signal') as send, patch.object(vm.os, 'close'):
            vm.kill_owned((123, 111), set())
            send.assert_not_called()


class FootprintTests(unittest.TestCase):
    def test_development_files_are_outside_production_fingerprint_selection(self):
        files = [p for p in (ROOT / 'development').rglob('*') if p.is_file()]
        self.assertGreaterEqual(len(files), 2)
        self.assertTrue(all(not vm.verified_code.selected(p.relative_to(ROOT).as_posix()) for p in files))

    def test_atomic_pointer_syncs_file_before_replace_then_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'current.json'
            calls = []
            replace = os.replace
            with patch.object(vm.os, 'fsync', side_effect=lambda fd: calls.append('file-fsync')), \
                 patch.object(vm.os, 'replace', side_effect=lambda src, dst: (calls.append('replace'), replace(src, dst))[-1]), \
                 patch.object(vm, 'sync_directory', side_effect=lambda path: calls.append('directory-fsync')):
                vm.write_atomic(target, {'complete': True})
            self.assertEqual(calls, ['file-fsync', 'replace', 'directory-fsync'])
            self.assertEqual(vm.read_json(target), {'complete': True})


    def test_cli_has_no_publish_upload_or_new_source_controls(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as raised:
                vm.main(['--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('{restore,continue,status}', out.getvalue())
        self.assertNotIn('--allow-new', out.getvalue())


if __name__ == '__main__':
    unittest.main()
