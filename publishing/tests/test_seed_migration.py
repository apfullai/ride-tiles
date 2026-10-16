import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import seed_migration as seed
from public_origin import BASE, OLD_BASE, relocate_manifest, relocate_region

class OriginTests(unittest.TestCase):
    def region(self):
        prefix=OLD_BASE+'/regions/example/0123456789abcdef/'
        return {'id':'example','version':'0123456789abcdef','tiles':[prefix+'tiles/{z}/{x}/{y}.pbf'],
            'sourceUrl':prefix+'provenance.json','sourceDownload':prefix+'source.pbf',
            'sha256Manifest':prefix+'tile-sha256.json','sourceArchiveSha256':'a'*64,
            'bounds':[-1,1,2,3],'tileCount':1,'tileBytes':123}
    def test_only_public_links_change_and_roundtrip_exact(self):
        r=self.region();before=copy.deepcopy(r)
        changed=relocate_region(r,OLD_BASE,BASE)
        self.assertEqual(r,before)
        self.assertEqual(relocate_region(changed,BASE,OLD_BASE),r)
        for field in r.keys()-{'tiles','sourceUrl','sourceDownload','sha256Manifest'}:
            self.assertEqual(changed[field],r[field])
        m={'schemaVersion':1,'updatedAt':'preserve','regions':[r]}
        self.assertEqual(relocate_manifest(m,OLD_BASE,BASE)['updatedAt'],'preserve')
    def test_wrong_origin_or_version_fails(self):
        for value in ('https://evil.example',BASE+'/other',OLD_BASE+'evil'):
            with self.assertRaises(ValueError):relocate_region(self.region(),value,BASE)
        r=self.region();r['sourceUrl']=OLD_BASE+'/regions/other/0123456789abcdef/provenance.json'
        with self.assertRaises(ValueError):relocate_region(r,OLD_BASE,BASE)
    def test_seed_rejects_source_race_before_restore(self):
        with patch.object(seed,'assert_destination_absent'),patch.object(seed,'assert_publication_unchanged',side_effect=ValueError('changed')),patch.object(seed,'restore_published') as restore:
            with self.assertRaises(ValueError):seed.seed('unused')
            restore.assert_not_called()
    def test_seed_requires_exact_checkpoint_before_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            site=Path(d)/'site'
            def restore(path,origin,fetch,receipt):
                (path/'checkpoints/active').mkdir(parents=True)
                (path/'checkpoints/active/checkpoint.json').write_text('{}')
                receipt.update(publicationRevision=seed.SOURCE_REVISION,bootstrap=False)
            with patch.object(seed,'assert_destination_absent'),patch.object(seed,'assert_publication_unchanged'),patch.object(seed,'restore_published',side_effect=restore):
                with self.assertRaisesRegex(ValueError,'checkpoint differs'):seed.seed(site)
                self.assertFalse((site/'manifest.json').exists())
    def test_destination_requires_exact_absence(self):
        def absent(url,limit):raise HTTPError(url,404,'missing',{},None)
        seed.assert_destination_absent(absent)
        with self.assertRaisesRegex(ValueError,'replay refused'):
            seed.assert_destination_absent(lambda url,limit:b'{}')
        def redirected(url,limit):raise HTTPError('https://other.example/missing',404,'missing',{},None)
        with self.assertRaises(HTTPError):seed.assert_destination_absent(redirected)
        for status in (403,429,500):
            def failed(url,limit):raise HTTPError(url,status,'failed',{},None)
            with self.assertRaises(HTTPError):seed.assert_destination_absent(failed)
    def test_seed_requires_code_proof_and_keeps_deploy_race_guard(self):
        root=Path(__file__).resolve().parents[2]
        self.assertIn('find_proof(None,source_records(root),fetch_public_json)',(root/'publishing/seed_migration.py').read_text())
        wf=(root/'.github/workflows/rollout.yml').read_text()
        self.assertIn('OLD_BASE if os.environ',wf)
        self.assertIn('seed_migration.py',wf)
        self.assertNotIn('  schedule:',wf)
if __name__=='__main__':unittest.main()
