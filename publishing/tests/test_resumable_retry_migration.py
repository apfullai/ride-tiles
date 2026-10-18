"""Synthetic crops exercise migration guards; these tests do not restore Pages."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'publishing'))
import checkpoint_migrations as dispatch
import retry_prefix_migration as migration
from verified_code import blob_sha, fingerprint, source_records


class RetryMigration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cp = self.root / 'checkpoint'
        for state in ('illinois', 'wisconsin'):
            source = self.cp / 'sources' / state
            source.mkdir(parents=True)
            raw = ('synthetic, unchanged ' + state + ' crop').encode()
            sha = hashlib.sha256(raw).hexdigest()
            (source / 'crop.part-0000').write_bytes(raw)
            (source / 'receipt.json').write_text(json.dumps({'crop': {
                'encoding': 'raw', 'bytes': len(raw), 'sha256': sha,
                'parts': [{'path': 'crop.part-0000', 'bytes': len(raw), 'sha256': sha}]}}))
        self.checkpoint = {
            'schemaVersion': 1, 'codeFingerprint': migration.FROM_CODE,
            'generationId': migration.FROM_GENERATION,
            'config': {'id': 'chicagoland'}, 'purpose': 'refresh',
            'progress': {'completedStates': ['illinois', 'wisconsin'],
                'indexReady': False, 'batchIds': [], 'batchCount': 0, 'completedBatches': []},
            'nextStage': {'kind': 'source', 'state': 'indiana'},
            'failureCount': 1, 'needsAttention': False,
            'retryAfter': migration.FROM_RETRY_AFTER,
            'lastError': {'at': '2026-10-18T14:04:14.579685+00:00', 'message': 'curl exit 28'},
            'sourcePlan': {'planId': 'same-pinned-plan', 'sourceDate': '2026-10-13',
                'expiresAt': '2026-10-20T00:00:00+00:00',
                'runtime': {'python': '3.12.15'},
                'sources': [{'state': 'indiana', 'md5': 'f36ca58217780d769f700363cead9e0d',
                    'url': 'https://download.geofabrik.de/north-america/us/indiana-261013.osm.pbf'}]},
            'stageCount': 2, 'startedAt': '2026-10-14T18:58:47.231395+00:00',
            'updatedAt': '2026-10-18T14:04:14.579685+00:00',
            'migration': {'id': 'previous-exact-pack', 'previousMigration': {'id': 'origin'}},
        }
        self.descriptor_patch = patch.object(migration, 'FROM_DESCRIPTOR', '')
        self.descriptor_patch.start()
        self.addCleanup(self.descriptor_patch.stop)
        self.seal_fixture()

        # This is an explicit complete source-inventory fixture, not a claim that
        # the local code-only recovery contains the bootstrap binary payloads.
        pins = json.loads((ROOT / migration.PINS_PATH).read_bytes())
        self.records = {**pins, **{name: blob_sha(('synthetic source ' + name).encode())
                                   for name in migration.ADDED}, **source_records(ROOT)}
        self.source_patch = patch('verified_code.source_records', side_effect=lambda root: self.records)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)

    def seal_fixture(self):
        from rollout_snapshot import _records
        self.checkpoint['files'] = {name: value for name, value in _records(self.cp, include_metadata=True).items()
                                    if name != 'checkpoint.json'}
        self.raw = (json.dumps(self.checkpoint, sort_keys=True, indent=2) + '\n').encode()
        (self.cp / 'checkpoint.json').write_bytes(self.raw)
        migration.FROM_DESCRIPTOR = hashlib.sha256(self.raw).hexdigest()

    def migrate(self, target=None, verified=None):
        fp = fingerprint(self.records)
        return migration.migrate(self.cp, self.checkpoint, ROOT,
            target_fingerprint=target or fp, verified_fingerprint=verified or fp,
            now=dt.datetime(2026, 10, 18, 15, tzinfo=dt.timezone.utc))

    def test_preserves_every_field_and_all_source_bytes_with_previous_audit_chain(self):
        before = {p.relative_to(self.cp).as_posix(): p.read_bytes()
                  for p in self.cp.rglob('*') if p.is_file()}
        original = copy.deepcopy(self.checkpoint)
        self.assertTrue(migration.eligible(self.cp, self.checkpoint, ROOT))
        result = self.migrate()
        self.assertEqual({k: v for k, v in result.items() if k not in ('codeFingerprint', 'migration')},
                         {k: v for k, v in original.items() if k not in ('codeFingerprint', 'migration')})
        self.assertEqual(result['migration']['previousMigration'], original['migration'])
        self.assertEqual(result['migration']['fromCommit'], migration.FROM_COMMIT)
        self.assertEqual(result['migration']['preservedSourcePlanId'], original['sourcePlan']['planId'])
        self.assertEqual(result['migration']['preservedFilesSha256'], hashlib.sha256(
            json.dumps(original['files'], sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.assertEqual(before, {p.relative_to(self.cp).as_posix(): p.read_bytes()
                                  for p in self.cp.rglob('*') if p.is_file()})
        result['sourcePlan']['sources'][0]['md5'] = 'changed-copy'
        result['migration']['previousMigration']['id'] = 'changed-copy'
        self.assertEqual(self.checkpoint, original)

    def test_dispatches_only_the_explicit_retry_profile(self):
        self.assertTrue(dispatch.eligible(self.cp, self.checkpoint, ROOT))
        fp = fingerprint(self.records)
        result = dispatch.migrate(self.cp, self.checkpoint, ROOT,
            target_fingerprint=fp, verified_fingerprint=fp,
            now=dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc))
        self.assertEqual(result['migration']['id'], migration.ID)
        self.assertFalse(migration.eligible(self.cp, result, ROOT))

    def test_changed_descriptor_values_or_bytes_are_rejected(self):
        for key, value in (('codeFingerprint', '0' * 64), ('generationId', 'other'),
                           ('failureCount', 0), ('retryAfter', None), ('needsAttention', True),
                           ('nextStage', {'kind': 'source', 'state': 'michigan'})):
            with self.subTest(key=key):
                altered = copy.deepcopy(self.checkpoint)
                altered[key] = value
                self.assertFalse(migration.eligible(self.cp, altered, ROOT))
        (self.cp / 'checkpoint.json').write_bytes(self.raw + b'\n')
        self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))

    def test_explicit_failure_and_retry_guards_remain_with_fixture_descriptor(self):
        original = copy.deepcopy(self.checkpoint)
        for key, value in (('failureCount', 0), ('failureCount', True), ('retryAfter', None), ('needsAttention', True),
                           ('generationId', 'other'), ('purpose', 'seed')):
            with self.subTest(key=key):
                self.checkpoint = copy.deepcopy(original)
                self.checkpoint[key] = value
                self.seal_fixture()
                self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))

    def test_requires_verified_current_target_fingerprint(self):
        fp = fingerprint(self.records)
        for target, verified in ((fp, '0' * 64), ('a' * 64, 'a' * 64),
                                 (migration.FROM_CODE, migration.FROM_CODE)):
            with self.subTest(target=target, verified=verified):
                with self.assertRaisesRegex(ValueError, 'not verified'):
                    self.migrate(target, verified)

    def test_missing_added_removed_or_changed_protected_source_is_rejected(self):
        original = dict(self.records)
        for name, value in (('tools/tile_generator.py', '0' * 40),
                            ('requirements.txt', '0' * 40),
                            ('config/rollout-regions.json', '0' * 40),
                            ('tools/unreviewed.py', '0' * 40),
                            ('bootstrap/chicagoland.tar.gz.part000', None),
                            ('publishing/attempt_receipts.py', None),
                            ('tools/build_region.py', None)):
            with self.subTest(name=name, value=value):
                self.records = dict(original)
                if value is None:
                    self.records.pop(name)
                else:
                    self.records[name] = value
                self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))

    def test_pin_file_hash_is_required(self):
        with patch.object(migration, 'PINS_SHA256', '0' * 64):
            self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))

    def test_corrupt_missing_extra_or_symlinked_crop_fails(self):
        path = self.cp / 'sources/illinois/crop.part-0000'
        original = path.read_bytes()
        path.write_bytes(b'corrupt')
        self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))
        path.unlink()
        self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))
        path.write_bytes(original)
        extra = path.with_name('unlisted.part')
        extra.write_bytes(b'extra')
        self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))
        extra.unlink()
        path.unlink()
        path.symlink_to(self.cp / 'sources/wisconsin/crop.part-0000')
        self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))

    def test_reconstructed_crop_and_part_metadata_are_independently_checked(self):
        path = self.cp / 'sources/illinois/receipt.json'
        original = json.loads(path.read_bytes())
        for mutate in (lambda crop: crop.update(sha256='f' * 64),
                       lambda crop: crop.update(encoding='gzip'),
                       lambda crop: crop.update(parts=[]),
                       lambda crop: crop.update(bytes=True),
                       lambda crop: crop['parts'][0].update(path='../crop.part-0000'),
                       lambda crop: crop['parts'][0].update(bytes=8_000_001)):
            receipt = copy.deepcopy(original)
            mutate(receipt['crop'])
            path.write_text(json.dumps(receipt))
            # Authorize this synthetic descriptor so the test reaches crop
            # verification rather than stopping at the outer inventory hash.
            self.seal_fixture()
            self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))


class RetryMigrationSourcePins(unittest.TestCase):
    def test_original_complete_source_inventory_matches_published_fingerprint(self):
        raw = (ROOT / migration.PINS_PATH).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), migration.PINS_SHA256)
        pins = json.loads(raw)
        self.assertEqual(fingerprint(pins), migration.FROM_CODE)
        self.assertEqual(len(pins), 99)
        self.assertTrue(migration.CHANGED <= set(pins))
        self.assertFalse(set(pins) & migration.ADDED)

    def test_historical_source_fixtures_match_the_original_blob_records(self):
        pins = json.loads((ROOT / migration.PINS_PATH).read_bytes())
        for name in ('build_region.py', 'resumable_region.py'):
            fixture = ROOT / 'publishing/tests/fixtures/source_package_migration' / (name + '.txt')
            self.assertEqual(blob_sha(fixture.read_bytes()), pins['tools/' + name])

    def test_complete_checkout_profile_when_payloads_are_available(self):
        pins = json.loads((ROOT / migration.PINS_PATH).read_bytes())
        records = source_records(ROOT)
        missing = set(pins) - set(records)
        if missing:
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                self.fail('CI must verify the complete original selected-file inventory: '
                          + ', '.join(sorted(missing)))
            self.skipTest('Code-only local recovery lacks full bootstrap/research payload checkout')
        self.assertTrue(migration._code_profile(ROOT))


if __name__ == '__main__':
    unittest.main()
