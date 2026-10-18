"""Only the explicitly approved, independently hashed source stage can migrate."""
import copy
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'publishing'))
import checkpoint_migrations as migration


class SourcePackageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cp=self.root/'checkpoint';self.source=self.cp/'sources/illinois'
        self.source.mkdir(parents=True)
        raw=b'unchanged raw source crop';digest=hashlib.sha256(raw).hexdigest()
        (self.source/'crop.part-0000').write_bytes(raw)
        receipt={'crop':{'encoding':'raw','bytes':len(raw),'sha256':digest,
            'parts':[{'path':'crop.part-0000','bytes':len(raw),'sha256':digest}]}}
        (self.source/'receipt.json').write_text(json.dumps(receipt))
        self.checkpoint={'codeFingerprint':migration.FROM_CODE,'config':{'id':'chicagoland'},
            'purpose':'refresh','sourcePlan':{'planId':'pinned-plan','runtime':{'python':'pinned'}},
            'generationId':'same-generation','files':{'source':'same-inventory'},
            'progress':{'completedStates':['illinois'],'indexReady':False,'batchIds':[],
                'batchCount':0,'completedBatches':[]},'nextStage':{'kind':'source','state':'wisconsin'},
            'failureCount':0,'needsAttention':False}
        from rollout_snapshot import _records
        self.checkpoint['files']=_records(self.cp,include_metadata=True)
        (self.cp/'checkpoint.json').write_text(json.dumps(self.checkpoint))
        self.pin=patch.object(migration,'FROM_DESCRIPTOR',hashlib.sha256((self.cp/'checkpoint.json').read_bytes()).hexdigest())
        self.pin.start();self.addCleanup(self.pin.stop)
        self.code = self.code_copy()

    def eligible(self):return migration.eligible(self.cp,self.checkpoint,self.code)

    def test_exact_source_only_descriptor_migrates_without_touching_any_file(self):
        before={p.relative_to(self.cp).as_posix():p.read_bytes() for p in self.cp.rglob('*') if p.is_file()}
        self.assertTrue(self.eligible())
        result=migration.migrate(self.cp,self.checkpoint,self.code,target_fingerprint='a'*64,
            verified_fingerprint='a'*64,now=dt.datetime(2026,10,14,tzinfo=dt.timezone.utc))
        self.assertEqual({k:v for k,v in result.items() if k not in ('codeFingerprint','migration')},
                         {k:v for k,v in self.checkpoint.items() if k!='codeFingerprint'})
        self.assertEqual(result['migration']['fromCodeFingerprint'],migration.FROM_CODE)
        self.assertEqual(result['migration']['preservedSourcePlanId'],'pinned-plan')
        self.assertEqual(before,{p.relative_to(self.cp).as_posix():p.read_bytes() for p in self.cp.rglob('*') if p.is_file()})

    def test_old_identity_or_progress_changes_are_not_eligible(self):
        for mutate in (lambda c:c.update(codeFingerprint='0'*64),
                       lambda c:c.update(generationId='different'),
                       lambda c:c['progress'].update(indexReady=True),
                       lambda c:c['sourcePlan'].update(runtime={'python':'changed'})):
            with self.subTest(mutate=mutate):
                before=copy.deepcopy(self.checkpoint);mutate(self.checkpoint)
                self.assertFalse(self.eligible());self.checkpoint=before
        (self.cp/'checkpoint.json').write_text(json.dumps(self.checkpoint)+'\n')
        self.assertFalse(self.eligible())

    def test_test_receipt_mismatch_cannot_authorize_migration(self):
        with self.assertRaisesRegex(ValueError,'not authorized'):
            migration.migrate(self.cp,self.checkpoint,self.code,target_fingerprint='a'*64,
                              verified_fingerprint='b'*64,now=dt.datetime.now(dt.timezone.utc))

    def test_corrupt_crop_or_inconsistent_whole_hash_is_refused(self):
        (self.source/'crop.part-0000').write_bytes(b'corrupt')
        self.assertFalse(self.eligible())
        raw=b'unchanged raw source crop'
        (self.source/'crop.part-0000').write_bytes(raw)
        receipt=json.loads((self.source/'receipt.json').read_text());receipt['crop']['sha256']='f'*64
        (self.source/'receipt.json').write_text(json.dumps(receipt))
        self.assertFalse(self.eligible())

    def test_changed_source_semantics_or_dependency_is_refused(self):
        with patch.object(migration,'ORIGIN_SOURCE_SEMANTICS','0'*64):self.assertFalse(self.eligible())
        with patch.object(migration,'ORIGIN_BUILD_REGION_SHA256','0'*64):self.assertFalse(self.eligible())

    def test_source_semantics_fingerprint_ignores_only_new_packaging_boundary(self):
        path=self.root/'tools/resumable_region.py';path.parent.mkdir()
        original=(self.code/'tools/resumable_region.py').read_text();path.write_text(original)
        self.assertEqual(migration.source_semantics(self.root),migration.ORIGIN_SOURCE_SEMANTICS)
        path.write_text(original+'\n# comment only\n')
        self.assertEqual(migration.source_semantics(self.root),migration.ORIGIN_SOURCE_SEMANTICS)
        path.write_text(original.replace("'Pinned source checksum mismatch; refusing replacement'","'changed acquisition'"))
        self.assertNotEqual(migration.source_semantics(self.root),migration.ORIGIN_SOURCE_SEMANTICS)


    def code_copy(self):
        root = Path(tempfile.mkdtemp(prefix='historical-code-', dir=self.root))
        for name in (*migration.PROTECTED_FILES, 'tools/resumable_region.py'):
            target = root/name
            target.parent.mkdir(parents=True, exist_ok=True)
            original = (ROOT/'publishing/tests/fixtures/source_package_migration'/(Path(name).name+'.txt')
                        if name in ('tools/resumable_region.py', 'tools/build_region.py') else ROOT/name)
            shutil.copyfile(original, target)
        return root

    def test_origin_migration_has_distinct_audit_and_preserves_all_other_fields(self):
        self.assertEqual(migration.migration_profile(self.code), migration.ORIGIN_MIGRATION_ID)
        result = migration.migrate(self.cp, self.checkpoint, self.code,
            target_fingerprint='a'*64, verified_fingerprint='a'*64,
            now=dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc))
        self.assertEqual(result['migration']['id'], migration.ORIGIN_MIGRATION_ID)
        self.assertEqual(result['migration']['fromPublicBase'], migration.FROM_PUBLIC_BASE)
        self.assertEqual(result['migration']['toPublicBase'], migration.TO_PUBLIC_BASE)
        self.assertEqual(result['files'], self.checkpoint['files'])
        self.assertEqual(result['sourcePlan'], self.checkpoint['sourcePlan'])
        self.assertEqual(result['generationId'], self.checkpoint['generationId'])

    def test_only_exact_approved_origin_and_matching_dependency_pair_are_eligible(self):
        root = self.code_copy()
        source = root/'tools/resumable_region.py'
        original = source.read_text()
        for wrong in ('https://apfullai.github.io/other', migration.TO_PUBLIC_BASE+'/',
                      'http://apfullai.github.io/ride-tiles', migration.FROM_PUBLIC_BASE):
            source.write_text(original.replace(migration.TO_PUBLIC_BASE, wrong))
            self.assertFalse(migration.eligible(self.cp, self.checkpoint, root))
        source.write_text(original)
        dependency = root/'tools/build_region.py'
        dependency.write_text(dependency.read_text().replace(migration.TO_PUBLIC_BASE, migration.FROM_PUBLIC_BASE))
        self.assertFalse(migration.eligible(self.cp, self.checkpoint, root))

    def test_original_origin_profile_remains_exact_and_separate(self):
        root = self.code_copy()
        for name in ('tools/resumable_region.py', 'tools/build_region.py'):
            path = root/name
            path.write_text(path.read_text().replace(migration.TO_PUBLIC_BASE, migration.FROM_PUBLIC_BASE))
        self.assertEqual(migration.migration_profile(root), migration.MIGRATION_ID)
        self.assertTrue(migration.eligible(self.cp, self.checkpoint, root))
        result = migration.migrate(self.cp, self.checkpoint, root,
            target_fingerprint='a'*64, verified_fingerprint='a'*64,
            now=dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc))
        self.assertEqual(result['migration']['id'], migration.MIGRATION_ID)
        self.assertNotIn('toPublicBase', result['migration'])

    def test_other_protected_files_and_acquisition_semantics_stay_pinned(self):
        root = self.code_copy()
        for name in migration.PROTECTED_FILES:
            path = root/name
            original = path.read_bytes()
            path.write_bytes(original+b'\n')
            self.assertFalse(migration.eligible(self.cp, self.checkpoint, root), name)
            path.write_bytes(original)
        source = root/'tools/resumable_region.py'
        source.write_text(source.read_text().replace("'Pinned source checksum mismatch; refusing replacement'", "'altered'"))
        self.assertFalse(migration.eligible(self.cp, self.checkpoint, root))

    def test_retry_code_cannot_use_old_source_package_profile(self):
        self.assertIsNone(migration.migration_profile(ROOT))
        self.assertFalse(migration.eligible(self.cp, self.checkpoint, ROOT))


if __name__=='__main__':unittest.main()
