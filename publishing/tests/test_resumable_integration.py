"""Actual stage/codec/snapshot integration with tiny synthetic public source data."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'publishing'))
import build_region
import resumable_region as stages
import resumable_rollout as rollout
import resumable_stage_runner as runner
import rollout_snapshot as snapshot

spec=importlib.util.spec_from_file_location('source_fixtures',ROOT/'tests/test_resumable_stages.py')
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


class ResumableIntegrationTests(unittest.TestCase):
    def test_large_index_inventory_stays_in_checkpoint_not_ipc_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);checkpoint=root/'checkpoint';work=root/'work';result_path=root/'result.json'
            paths=[f'tiles/14/{index}/123.pbf' for index in range(10_000)]
            full={'schemaVersion':1,'planId':'a'*64,'stage':'index','batchCount':79,'candidateTileCount':10_000,
                  'batches':[{'tiles':paths}], 'files':{p:{'sha256':'b'*64,'bytes':100} for p in paths}}
            self.assertGreater(len(json.dumps(full)),100_000)
            progress={'completedStates':fixtures.CONFIG['sourceStates'],'indexReady':True,
                      'batchIds':list(range(79)),'batchCount':79,'completedBatches':[]}
            def index(*args,**kwargs):
                (checkpoint/'index').mkdir()
                (checkpoint/'index/receipt.json').write_text(json.dumps(full))
                return full
            with patch.object(stages,'build_index_stage',side_effect=index), \
                 patch.object(stages,'validate_checkpoint',return_value=progress):
                result=runner.run({'config':fixtures.CONFIG,'sourcePlan':fixtures.plan(),
                    'stage':{'kind':'index'}},checkpoint,work,result_path)
            self.assertLess(result_path.stat().st_size,10_000)
            self.assertEqual(result['receipt']['candidateTileCount'],10_000)
            self.assertNotIn('files',result['receipt']);self.assertNotIn('batches',result['receipt'])
            self.assertEqual(json.loads((checkpoint/'index/receipt.json').read_text()),full)

    def test_source_index_group_restore_and_promotion_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);site=root/'site';site.mkdir()
            config=dict(fixtures.CONFIG)
            plan=fixtures.plan(config)
            def old_osmium(*args):
                output=Path(args[args.index('-o')+1])
                if args[1]=='export':output.write_text(json.dumps(fixtures.features()[0])+'\n')
                else:output.write_bytes(b'old compact public source')
            with patch.object(build_region,'download_source',side_effect=lambda url,path:Path(path).write_bytes(fixtures.SOURCE)), \
                 patch.object(build_region,'run',side_effect=old_osmium), \
                 patch.object(build_region.subprocess,'check_output',return_value='2026-09-30T20:21:31Z'):
                old=build_region.build({**config,'sourceStates':['new-york']},root/'initial',site,snapshot.BASE)
            rollout.write_json(site/'manifest.json',{'schemaVersion':1,'regions':[old]})
            initial_manifest=(site/'manifest.json').read_bytes()
            prior=snapshot.inventory(site/'regions')
            def execute(request,checkpoint,work,seconds):
                work.mkdir()
                result_path=work/'result.json'
                result=runner.run(request,checkpoint,work/'stage-work',result_path)
                return result,work/'stage-work/region-site'
            now=dt.datetime(2026,10,14,tzinfo=dt.timezone.utc)
            with patch.object(stages,'now_utc',side_effect=lambda now=None:now or fixtures.CLOCK), \
                 patch.object(stages,'runtime_fingerprint',return_value=fixtures.RUNTIME), \
                 patch.object(stages,'plan_sources',return_value=plan), \
                 patch.object(stages.legacy,'download_source',side_effect=lambda url,path:Path(path).write_bytes(fixtures.SOURCE)), \
                 patch.object(stages.legacy,'run',side_effect=fixtures.fake_osmium), \
                 patch.object(stages,'source_timestamp',return_value='2026-10-01T20:21:31Z'), \
                 patch.object(snapshot,'_validate_frozen',return_value=None):
                for number in range(10):
                    report=rollout.advance(site,[config],allow_new=number==0,now=now,
                        execute=execute,verifier=lambda *a,**k:({'fingerprint':'c'*64},'fixture'),
                        fingerprinter=lambda root:'c'*64)
                    self.assertTrue(report['changed'])
                    self.assertFalse(report['needsAttention'],report)
                    if report['active']:
                        self.assertEqual((site/'manifest.json').read_bytes(),initial_manifest)
                    # Each next run restores only published self-contained bytes.
                    restored=root/f'restored-{number}'
                    def fetch(url,limit):return (site/url.removeprefix(snapshot.BASE+'/')).read_bytes()
                    receipt={}
                    snapshot.restore_published(restored,fetch=fetch,receipt=receipt)
                    self.assertEqual(snapshot.inventory(restored),snapshot.inventory(site))
                    self.assertEqual(receipt['publicationRevision'],snapshot.publication_revision(
                        (site/'manifest.json').read_bytes(),(site/'snapshot.json').read_bytes()))
                    shutil.rmtree(site);restored.rename(site)
                    if not report['active']:break
                else:self.fail('Build failed to reach promotion')
                self.assertEqual(number,3)
                active=json.loads((site/'manifest.json').read_text())['regions'][0]
                self.assertNotEqual(active['version'],old['version'])
                self.assertEqual(active['sourceTimestamp'],'2026-10-01T20:21:31Z')
                self.assertEqual(active['sourceFormat'],'osm-pbf-chunks-v1')
                self.assertTrue(active['sourceDownload'].endswith('/cycling-source.json'))
                self.assertFalse((site/rollout.ACTIVE).exists())
                self.assertTrue(all(snapshot.inventory(site/'regions')[k]==v for k,v in prior.items()))
                snapshot.validate_site(site)


if __name__=='__main__':unittest.main()
