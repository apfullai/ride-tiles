"""Cross-repository budget continuity and explicit audited cutover evidence."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import actions_budget as actions
import artifact_budget as storage
import early_capacity as early
from test_actions_budget import PublicHistory, NOW, run, job
from test_early_capacity import fixture

SOURCE = 'apfullai/rides-tiles'
TARGET = 'apfullai/ride-tiles'


def pending():
    return {'schemaVersion': 1, 'sourceRepository': SOURCE, 'targetRepository': TARGET,
            'month': '2026-10', 'state': 'pending', 'oldPublisherPaused': False,
            'publisherPausedAt': None, 'runtime': None, 'artifacts': None}


def finalized(minutes=12, closeout=False):
    ledger = pending()
    ledger.update(state='finalized', oldPublisherPaused=True,
                  publisherPausedAt='2026-10-14T09:00:00Z',
                  runtime={'complete': True, 'usedMinutes': minutes,
                           'observedAt': '2026-10-14T10:00:00Z', 'evidenceRunIds': [123]})
    if closeout:
        ledger['artifacts'] = {'complete': True, 'unexpiredArtifactBytes': 0,
                              'unexpiredArtifactCount': 0, 'observedAt': '2026-10-14T11:00:00Z'}
    return ledger


class MigrationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'ledger.json'
        self.save(pending())
        patcher = patch.object(actions, 'MIGRATION_LEDGER_PATH', self.path)
        patcher.start(); self.addCleanup(patcher.stop)
        self.site = self.root/'site'; self.site.mkdir()
        (self.site/'manifest.json').write_bytes(b'{}')
        self.calls = []

    def save(self, ledger):
        self.path.write_text(json.dumps(ledger))

    def artifact_fetch(self, url):
        self.calls.append(url)
        if f'/repos/{SOURCE}/' in url:
            size = 100_000_000
        elif f'/repos/{TARGET}/' in url:
            size = 20_000_000
        else:
            raise AssertionError(url)
        # The same numeric ID in different repositories is not a duplicate page.
        return {'total_count': 1, 'artifacts': [{'id': 1, 'size_in_bytes': size,
            'expired': False, 'expires_at': '2026-10-15T19:00:00Z'}]}

    def early_fetch(self, url, limit):
        manifest, descriptor, _ = fixture()
        if url in (early.SEED_BASE+'/manifest.json', early.TARGET_BASE+'/manifest.json'):
            return manifest
        if url in (early.SEED_BASE+'/snapshot.json', early.TARGET_BASE+'/snapshot.json'):
            return descriptor
        return json.dumps(self.artifact_fetch(url)).encode()

    def runtime_check(self, history=None, now=NOW):
        history = history or PublicHistory()
        return actions.check_budget(TARGET, 999, 1, now=now,
            fetch=lambda url: history(url.replace('/repos/'+TARGET+'/', '/repos/example/tiles/')))

    def test_both_storage_guards_sum_old_and_new_live_artifacts(self):
        full = storage.check_artifact_budget(TARGET, self.site, fetch=self.artifact_fetch)
        first = early.check_capacity(TARGET, fetch=self.early_fetch, now=NOW)
        for result in (full, first):
            self.assertTrue(result['allowed'], result)
            self.assertEqual(result['artifactBytes'], 120_000_000)
            self.assertEqual(result['unexpiredArtifactCount'], 2)
            self.assertEqual(result['artifactRepositories'], [TARGET, SOURCE])

    def test_old_storage_api_failure_never_resets_storage(self):
        def unavailable(url):
            if f'/repos/{SOURCE}/' in url:
                raise HTTPError(url, 404, 'missing', {}, None)
            return self.artifact_fetch(url)
        result = storage.check_artifact_budget(TARGET, self.site, fetch=unavailable)
        self.assertFalse(result['allowed']); self.assertFalse(result['complete'])
        def early_unavailable(url, limit):
            if 'api.github.com' in url:
                return json.dumps(unavailable(url)).encode()
            return self.early_fetch(url, limit)
        self.assertFalse(early.check_capacity(TARGET, fetch=early_unavailable, now=NOW)['allowed'])

    def test_finalized_runtime_without_artifact_closeout_keeps_old_api_read(self):
        self.save(finalized())
        self.assertEqual(actions.artifact_repositories(TARGET, NOW), [TARGET, SOURCE])

    def test_only_verified_zero_closeout_removes_old_storage_dependency(self):
        self.save(finalized(closeout=True))
        result = storage.check_artifact_budget(TARGET, self.site, fetch=self.artifact_fetch)
        self.assertTrue(result['allowed'], result)
        self.assertEqual(result['artifactBytes'], 20_000_000)
        self.assertEqual(result['artifactRepositories'], [TARGET])
        self.assertFalse(any('/repos/'+SOURCE+'/' in url for url in self.calls))
        result = early.check_capacity(TARGET, fetch=self.early_fetch, now=NOW)
        self.assertEqual(result['artifactRepositories'], [TARGET])

    def test_pending_missing_and_malformed_ledger_block_runtime(self):
        self.assertFalse(self.runtime_check()['allowed'])
        self.path.unlink()
        self.assertFalse(self.runtime_check()['allowed'])
        self.path.write_text('{}')
        self.assertFalse(self.runtime_check()['allowed'])

    def test_missing_ledger_blocks_both_storage_guards(self):
        self.path.unlink()
        self.assertFalse(storage.check_artifact_budget(TARGET, self.site, fetch=self.artifact_fetch)['allowed'])
        self.assertFalse(early.check_capacity(TARGET, fetch=self.early_fetch, now=NOW)['allowed'])

    def test_finalized_minutes_carry_forward_and_keep_full_reservation(self):
        self.save(finalized(76))
        history = PublicHistory()
        history.add(run(1), [[job(10, 1, seconds=61)]])
        result = self.runtime_check(history)
        self.assertTrue(result['allowed'], result)
        self.assertEqual(result['carriedMinutes'], 76)
        self.assertEqual(result['usedMinutes'], 78)
        self.assertEqual(result['reservedMinutes'], 22)
        self.save(finalized(77))
        result = self.runtime_check(history)
        self.assertFalse(result['allowed']); self.assertTrue(result['complete'])
        self.assertEqual(result['usedMinutes'], 79)

    def test_october_carry_ends_next_month_but_ledger_is_still_required(self):
        self.save(finalized(91))
        november = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(actions.migration_runtime_minutes(TARGET, november), 0)
        self.assertEqual(actions.migration_runtime_minutes(TARGET, NOW), 91)
        self.save(pending())
        with self.assertRaises(ValueError): actions.migration_runtime_minutes(TARGET, november)

    def test_ledger_cannot_claim_false_pause_unknown_usage_or_unreleased_artifacts(self):
        mutations = [lambda d:d.update(oldPublisherPaused=False),
                     lambda d:d.update(month='2026-11'),
                     lambda d:d.update(sourceRepository='someone/other'),
                     lambda d:d['runtime'].update(usedMinutes=None),
                     lambda d:d['runtime'].update(usedMinutes=True),
                     lambda d:d['runtime'].update(usedMinutes=-1),
                     lambda d:d['runtime'].update(complete=False),
                     lambda d:d['runtime'].update(evidenceRunIds=[1,1]),
                     lambda d:d['runtime'].update(observedAt='2026-10-14T08:00:00Z'),
                     lambda d:d['artifacts'].update(unexpiredArtifactBytes=1),
                     lambda d:d['artifacts'].update(unexpiredArtifactCount=1),
                     lambda d:d['artifacts'].update(unexpiredArtifactBytes=False),
                     lambda d:d['artifacts'].update(observedAt='2026-10-14T08:00:00Z'),
                     lambda d:d['artifacts'].update(observedAt='2026-10-16T08:00:00Z'),
                     lambda d:d.pop('artifacts')]
        for mutate in mutations:
            ledger = finalized(closeout=True); mutate(ledger); self.save(ledger)
            with self.subTest(ledger=ledger), self.assertRaises(ValueError):
                actions.read_migration_ledger(NOW)
        ledger = pending(); ledger['artifacts'] = finalized(closeout=True)['artifacts']; self.save(ledger)
        with self.assertRaises(ValueError): actions.read_migration_ledger(NOW)
        self.path.write_text(json.dumps(pending()).replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'))
        with self.assertRaises(ValueError): actions.read_migration_ledger(NOW)

    def test_original_and_unrelated_repositories_are_unaffected(self):
        self.path.unlink()
        for repository in (SOURCE, 'example/tiles'):
            self.assertEqual(actions.artifact_repositories(repository, NOW), [repository])
            self.assertEqual(actions.migration_runtime_minutes(repository, NOW), 0)

    def test_seed_reads_only_old_metadata_and_reserves_extra_megabyte(self):
        calls = []
        def fetch(url, limit):
            calls.append(url)
            return self.early_fetch(url, limit)
        report = early.check_capacity(TARGET, fetch=fetch, now=NOW,
                                     base_url=early.SEED_BASE, seed_migration=True)
        self.assertTrue(report['allowed'], report)
        self.assertEqual(report['seedOverheadBytes'], 1_000_000)
        self.assertEqual(report['candidateBytes'], report['baselineCandidateBytes']+1_000_000)
        self.assertEqual(calls[:2], [early.SEED_BASE+'/manifest.json', early.SEED_BASE+'/snapshot.json'])
        self.assertFalse(any(early.TARGET_BASE in url for url in calls))
        self.assertFalse(early.check_capacity(TARGET, fetch=fetch, now=NOW,
                            base_url='https://other.example', seed_migration=True)['allowed'])
        self.assertFalse(early.check_capacity(SOURCE, fetch=fetch, now=NOW,
                            base_url=early.SEED_BASE, seed_migration=True)['allowed'])


if __name__ == '__main__': unittest.main()
