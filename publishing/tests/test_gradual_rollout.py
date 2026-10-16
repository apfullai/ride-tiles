import copy
import datetime as dt
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'publishing'))
import gradual_rollout as rollout

NOW = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)


def config(rid):
    return {'id': rid, 'name': rid, 'bounds': [0, 0, 1, 1],
            'minzoom': 8, 'maxzoom': 14, 'sourceStates': ['new-york']}


def region(rid, version='a'*16):
    return {**config(rid), 'version': version, 'sourceLayer': 'cycleinfra'}


def manifest_at(site):
    return json.loads((site/'manifest.json').read_text())


def fake_seal(site):
    # Unit-test seam; full real budget and cryptographic checks have separate tests.
    if sum(p.stat().st_size for p in site.rglob('*') if p.is_file()) > 20_000:
        raise ValueError('Published bytes exceed cumulative budget')
    return {}


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name)/'site'
        self.site.mkdir()
        self.old = [region('chicago-metro'), region('chicagoland'), region('nyc-pilot')]
        for r in self.old:
            target = self.site/'regions'/r['id']/r['version']/'tiles/8/1/1.pbf'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'original')
        rollout.write_json(self.site/'manifest.json', {'schemaVersion': 1, 'regions': self.old})
        self.configs = [config('nyc-pilot'), config('chicagoland'), config('los-angeles'), config('houston')]
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(rollout, 'validate_site', side_effect=manifest_at).start()
        patch.object(rollout, 'seal_snapshot', side_effect=fake_seal).start()

    def builder(self, c, root, seconds):
        self.calls.append(c['id'])
        entry = region(c['id'], 'b'*16)
        output = root/'site'
        tile = output/'regions'/entry['id']/entry['version']/'tiles/8/1/1.pbf'
        tile.parent.mkdir(parents=True)
        tile.write_bytes(b'new')
        return entry, output

    def current(self, ids=None):
        rollout.write_json(self.site/'rollout-state.json', {'schemaVersion': 1,
            'refreshedAt': {rid: NOW.isoformat() for rid in (ids or ['chicagoland', 'nyc-pilot'])}})

    def test_initial_refreshes_existing_then_at_most_one_city(self):
        before = rollout.inventory(self.site)
        result = rollout.prepare(self.site, self.configs, now=NOW, builder=self.builder)
        self.assertEqual(self.calls, ['chicagoland', 'nyc-pilot', 'los-angeles'])
        self.assertEqual(result['added'], 'los-angeles')
        self.assertEqual(result['dueRemaining'], [])
        after = rollout.inventory(self.site)
        self.assertTrue(all(after[k] == v for k, v in before.items() if k != 'manifest.json'))
        self.assertEqual(manifest_at(self.site)['regions'][0], self.old[0])
        self.assertTrue(result['changed'])

    def test_failed_refresh_preserves_it_and_prevents_expansion(self):
        def build(c, root, seconds):
            if c['id'] == 'chicagoland':
                self.calls.append(c['id'])
                raise RuntimeError('bounded download failure')
            return self.builder(c, root, seconds)
        result = rollout.prepare(self.site, self.configs, now=NOW, builder=build)
        self.assertEqual(self.calls, ['chicagoland', 'nyc-pilot'])
        self.assertEqual(result['dueRemaining'], ['chicagoland'])
        self.assertIsNone(result['added'])
        self.assertEqual(next(r for r in manifest_at(self.site)['regions'] if r['id']=='chicagoland'), self.old[1])

    def test_failed_addition_preserves_coverage_and_records_backoff(self):
        self.current()
        before = rollout.inventory(self.site)
        def fail(*args):
            raise subprocess.TimeoutExpired('build_one', 60)
        result = rollout.prepare(self.site, self.configs, now=NOW, builder=fail)
        self.assertTrue(result['changed'])
        self.assertTrue(all(rollout.inventory(self.site)[k] == v for k,v in before.items() if k != 'rollout-state.json'))
        self.assertEqual(len(result['skipped']), 1)
        self.assertEqual(result['deferred']['los-angeles']['retryAfter'], '2026-11-04T00:00:00+00:00')

    def test_no_due_and_no_new_is_no_change_no_deploy(self):
        self.current()
        before = rollout.inventory(self.site)
        result = rollout.prepare(self.site, self.configs[:2], now=NOW, builder=self.builder)
        self.assertFalse(result['changed'])
        self.assertFalse(self.calls)
        self.assertEqual(before, rollout.inventory(self.site))

    def test_missing_existing_config_blocks_expansion_without_dropping_region(self):
        self.current(['chicagoland'])
        before = rollout.inventory(self.site)
        result = rollout.prepare(self.site, [config('chicagoland'), config('los-angeles')], now=NOW, builder=self.builder)
        self.assertFalse(result['changed'])
        self.assertEqual(result['dueRemaining'], ['nyc-pilot'])
        self.assertFalse(self.calls)
        self.assertEqual(before, rollout.inventory(self.site))

    def test_cumulative_budget_rejects_candidate_without_losing_existing(self):
        self.current()
        before = rollout.inventory(self.site)
        def huge(c, root, seconds):
            entry, output = self.builder(c, root, seconds)
            (output/'regions'/entry['id']/entry['version']/'source.pbf').write_bytes(b'x'*21_000)
            return entry, output
        result = rollout.prepare(self.site, self.configs, now=NOW, builder=huge)
        self.assertTrue(result['changed'])  # Only the bounded retry/backoff state changes.
        self.assertIn('cumulative budget', result['skipped'][0]['reason'])
        self.assertTrue(all(rollout.inventory(self.site)[k] == v for k,v in before.items() if k != 'rollout-state.json'))

    def test_process_reserve_blocks_work(self):
        result = rollout.prepare(self.site, self.configs, now=NOW, builder=self.builder, seconds=100)
        self.assertFalse(result['changed'])
        self.assertFalse(self.calls)
        self.assertEqual(result['dueRemaining'], ['chicagoland', 'nyc-pilot'])

    def test_oldest_due_order_and_frozen_exclusion(self):
        self.current()
        state = {'refreshedAt': {'chicagoland': '2026-07-01T00:00:00Z', 'nyc-pilot': '2026-06-01T00:00:00Z'}}
        due = rollout.due_regions(manifest_at(self.site), state, NOW)
        self.assertEqual([r['id'] for r in due], ['nyc-pilot', 'chicagoland'])

    def test_identical_version_preserves_immutable_metadata_and_records_check(self):
        before = rollout.inventory(self.site)
        def same(c, root, seconds):
            self.calls.append(c['id'])
            r = region(c['id'])
            out = root/'site'
            target = out/'regions'/r['id']/r['version']/'tiles/8/1/1.pbf'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'original')
            return r, out
        result = rollout.prepare(self.site, self.configs[:2], now=NOW, builder=same)
        self.assertTrue(result['changed'])  # Real freshness check metadata is durable.
        self.assertEqual(manifest_at(self.site)['regions'], self.old)
        self.assertTrue(all(rollout.inventory(self.site)[k] == v for k,v in before.items() if k!='manifest.json'))
        again = rollout.prepare(self.site, self.configs[:2], now=NOW, builder=same)
        self.assertFalse(again['changed'])

    def test_client_region_cap_stops_expansion(self):
        self.current()
        with patch.object(rollout, 'MAX_REGIONS', 3):
            result = rollout.prepare(self.site, self.configs, now=NOW, builder=self.builder)
        self.assertFalse(result['changed'])
        self.assertFalse(self.calls)
        self.assertIn('cap', result['skipped'][0]['reason'])

    def test_deferred_refresh_does_not_silently_claim_freshness(self):
        self.current(['nyc-pilot'])
        state = json.loads((self.site/'rollout-state.json').read_text())
        state['deferred'] = {'chicagoland': {'lastAttemptAt': '2026-10-07T00:00:00Z',
            'retryAfter': '2026-10-28T00:00:00Z', 'reason': 'download timeout'}}
        rollout.write_json(self.site/'rollout-state.json', state)
        result = rollout.prepare(self.site, self.configs, now=NOW, builder=self.builder)
        self.assertEqual(self.calls, ['los-angeles'])
        self.assertEqual(result['dueRemaining'], ['chicagoland'])
        self.assertEqual(result['eligibleDueRemaining'], [])
        self.assertNotIn('chicagoland', result['refreshed'])

    def test_deferred_city_yields_to_next_candidate(self):
        self.current()
        state = json.loads((self.site/'rollout-state.json').read_text())
        state['deferred'] = {'los-angeles': {'lastAttemptAt': '2026-10-07T00:00:00Z',
            'retryAfter': '2026-10-28T00:00:00Z', 'reason': 'oversize'}}
        rollout.write_json(self.site/'rollout-state.json', state)
        result = rollout.prepare(self.site, self.configs, now=NOW, builder=self.builder)
        self.assertEqual(self.calls, ['houston'])
        self.assertEqual(result['added'], 'houston')

    def test_config_requires_client_zoom_contract_and_scoped_four_sources(self):
        path = Path(self.temp.name)/'queue.json'
        good = config('test-city')
        for field,value in [('minzoom',9),('maxzoom',13),('sourceStates',['a','b','c','d'])]:
            bad = dict(good, **{field:value})
            path.write_text(json.dumps([bad]))
            with self.assertRaises(ValueError):
                rollout.load_configs(path)
        chicago = config('chicagoland')
        chicago.update(bounds=[-89,41.05,-86.65,43.25], sourceStates=['illinois','wisconsin','indiana','michigan'])
        path.write_text(json.dumps([chicago]))
        self.assertEqual(rollout.load_configs(path), [chicago])

    def test_global_watchdog_propagates_without_starting_another_region(self):
        def interrupted(c, root, seconds):
            self.calls.append(c['id'])
            raise rollout.RolloutInterrupted('global deadline')
        before = rollout.inventory(self.site)
        with self.assertRaises(rollout.RolloutInterrupted):
            rollout.prepare(self.site, self.configs, now=NOW, builder=interrupted)
        self.assertEqual(self.calls, ['chicagoland'])
        self.assertEqual(rollout.inventory(self.site), before)

    @unittest.skipUnless(os.name == 'posix' and Path('/proc').exists(), 'Linux process-group regression')
    def test_actual_sigterm_kills_child_group_and_cannot_recover(self):
        # Exercise the real handler and subprocess_group path, including a
        # grandchild that ignores SIGTERM. Global interruption is unrecoverable.
        fake = Path(self.temp.name)/'fake'
        (fake/'publishing').mkdir(parents=True)
        child_script = """import json, os, signal, subprocess, sys, time
from pathlib import Path
root=Path(sys.argv[2])
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
(root.parent/'pids.json').write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(60)
"""
        (fake/'publishing/build_one.py').write_text(child_script)
        work = Path(self.temp.name)/'worker'
        work.mkdir()
        wrapper = Path(self.temp.name)/'parent.py'
        wrapper.write_text("import sys,signal\nfrom pathlib import Path\n"
            + f"sys.path.insert(0,{str(ROOT/'publishing')!r})\n"
            + "import gradual_rollout as r\n"
            + f"r.ROOT=Path({str(fake)!r})\n"
            + "signal.signal(signal.SIGTERM,r.interrupt_rollout)\n"
            + "try:\n"
            + f" r.subprocess_builder({config('test-city')!r},Path({str(work)!r}),60)\n"
            + "except Exception:\n"
            + f" Path({str(Path(self.temp.name)/'incorrect-recovery')!r}).touch()\n")
        parent = subprocess.Popen([sys.executable, str(wrapper)], start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children = []
        try:
            deadline = time.monotonic()+5
            pidfile = Path(self.temp.name)/'pids.json'
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(pidfile.exists(), 'child process did not start')
            children = json.loads(pidfile.read_text())
            parent.send_signal(signal.SIGTERM)
            self.assertNotEqual(parent.wait(timeout=5), 0)
            self.assertFalse((Path(self.temp.name)/'incorrect-recovery').exists())
            for pid in children:
                status = Path(f'/proc/{pid}/status')
                if status.exists():
                    self.assertIn('State:\tZ', status.read_text(), 'descendant is still running')
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
                parent.wait()
            if children:
                try:
                    os.killpg(children[0], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_process_timeout_kills_descendants(self):
        process = Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired('build', 1), 0]
        process.poll.return_value = None
        with patch.object(rollout.subprocess, 'Popen', return_value=process), patch.object(rollout.os, 'killpg') as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                rollout.subprocess_builder(config('nyc-pilot'), Path(self.temp.name), 1)
        kill.assert_called_once_with(123, rollout.signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
