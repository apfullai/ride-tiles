import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'publishing'))
import resumable_rollout as r
import rollout_snapshot as snapshot

NOW = dt.datetime(2026,10,14,tzinfo=dt.timezone.utc)
CODE = 'c'*64


def config(rid):
    return {'id':rid,'name':rid,'bounds':[0,0,1,1],'minzoom':8,'maxzoom':14,'sourceStates':['state-a','state-b']}


def region(rid, version='a'*16):
    return {**config(rid),'version':version,'sourceLayer':'cycleinfra'}


def manifest(site):
    return json.loads((site/'manifest.json').read_text())


class ResumableRolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name)/'site'
        self.site.mkdir()
        self.configs = [config('chicagoland'), config('nyc-pilot'), config('los-angeles')]
        self.old = [region('chicago-metro'), region('chicagoland'), region('nyc-pilot')]
        r.write_json(self.site/'manifest.json', {'schemaVersion':1,'regions':self.old})
        for item in self.old:
            target=self.site/'regions'/item['id']/item['version']/'tiles/8/1/1.pbf'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'old tile')
        self.calls=[]
        self.plan={'planId':'d'*64,'sourceDate':'2026-10-01','sources':['state-a','state-b']}
        self.addCleanup(patch.stopall)
        patch.object(r,'validate_site',side_effect=manifest).start()
        patch.object(r,'seal_snapshot',side_effect=lambda site:{'siteBytes':sum(p.stat().st_size for p in site.rglob('*') if p.is_file())}).start()

    def inspector(self,config,plan,path):
        if plan != self.plan:
            raise ValueError('bad pins')
        path=path/'progress.json'
        return json.loads(path.read_text()) if path.exists() else r.empty_progress(config)

    def verifier(self,*args,**kwargs):
        return {'fingerprint':CODE},'verified-fixture'

    def execute(self,request,checkpoint,work,seconds):
        stage=request['stage'];kind=stage['kind'];self.calls.append(copy.deepcopy(stage))
        plan=request['sourcePlan'] or self.plan
        progress=self.inspector(request['config'],plan,checkpoint)
        if kind=='start':
            progress['completedStates']=[request['config']['sourceStates'][0]]
        elif kind=='source':
            progress['completedStates'].append(stage['state'])
        elif kind=='index':
            progress.update(indexReady=True,batchIds=[0,1],batchCount=2)
        elif kind=='tiles':
            progress['completedBatches'].append(stage['batchId'])
        r.write_json(checkpoint/'progress.json',progress)
        output=work/'generated'
        result={'schemaVersion':1,'sourcePlan':plan,'progress':progress,'stage':stage}
        if kind=='finalize':
            entry=region(request['config']['id'],'b'*16)
            path=output/'regions'/entry['id']/entry['version']/'tiles/8/1/1.pbf'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'new tile')
            result['region']=entry
        return result,output

    def advance(self,**kwargs):
        return r.advance(self.site,self.configs,now=kwargs.pop('now',NOW),
            execute=kwargs.pop('execute',self.execute),inspector=self.inspector,
            verifier=kwargs.pop('verifier',self.verifier),fingerprinter=kwargs.pop('fingerprinter',lambda root:CODE),**kwargs)

    def test_each_stage_preserves_live_manifest_until_complete_promotion(self):
        initial=(self.site/'manifest.json').read_bytes()
        prior=r.inventory(self.site/'regions')
        for number in range(5):
            result=self.advance(allow_new=number==0)
            self.assertTrue(result['active'])
            self.assertEqual((self.site/'manifest.json').read_bytes(),initial)
            self.assertEqual(r.inventory(self.site/'regions'),prior)
        final=self.advance()
        self.assertFalse(final['active'])
        self.assertFalse((self.site/r.ACTIVE).exists())
        self.assertEqual([s['kind'] for s in self.calls],['start','source','index','tiles','tiles','finalize'])
        self.assertEqual(next(x['version'] for x in manifest(self.site)['regions'] if x['id']=='chicagoland'),'b'*16)
        self.assertTrue(all(r.inventory(self.site/'regions')[k]==v for k,v in prior.items()))
        self.assertEqual(manifest(self.site)['regions'][0],self.old[0])

    def test_continuation_only_cannot_start_city(self):
        before=r.inventory(self.site)
        result=self.advance(verifier=lambda *a,**k: self.fail('should not test inactive continuation'))
        self.assertFalse(result['changed']);self.assertFalse(self.calls)
        self.assertEqual(before,r.inventory(self.site))

    def test_one_active_city_rejects_replacement(self):
        self.advance(allow_new=True)
        before=r.inventory(self.site)
        with self.assertRaisesRegex(ValueError,'no active'):
            self.advance(allow_new=True,start_region='nyc-pilot')
        self.assertEqual(before,r.inventory(self.site))

    def test_explicit_existing_refresh_overrides_legacy_backoff(self):
        r.write_json(self.site/'rollout-state.json',{'schemaVersion':1,'refreshedAt':{},'deferred':{
            'chicagoland':{'lastAttemptAt':NOW.isoformat(),'retryAfter':(NOW+dt.timedelta(days=21)).isoformat(),'reason':'old timeout'}}})
        result=self.advance(allow_new=True,start_region='chicagoland')
        self.assertEqual(result['city']['id'],'chicagoland')
        self.assertEqual(result['completedCounts']['sources'],1)
        for invalid in ('chicago-metro','los-angeles','not-configured'):
            with self.assertRaises(ValueError):
                r.select_region(manifest(self.site),r.load_state(self.site,manifest(self.site)),self.configs,NOW,invalid)

    def test_corruption_and_changed_fingerprint_reject_resume(self):
        self.advance(allow_new=True)
        before=r.inventory(self.site)
        checkpoint_before=r.inventory(self.site/r.ACTIVE)
        result=self.advance(verifier=lambda *a,**k:({'fingerprint':'e'*64},'new-code'),fingerprinter=lambda root:'e'*64)
        self.assertTrue(result['needsAttention']);self.assertIn('code changed',result['reason'])
        self.assertEqual(checkpoint_before,r.inventory(self.site/r.ACTIVE))
        (self.site/r.ACTIVE/'progress.json').write_text('{}')
        result=self.advance()
        self.assertTrue(result['needsAttention']);self.assertIn('hashes',result['reason'])

    def test_failed_stage_discards_partial_bytes_and_retains_prior_stage(self):
        self.advance(allow_new=True)
        original=(self.site/'manifest.json').read_bytes()
        def fail(req,cp,work,seconds):
            (cp/'partial-uncommitted').write_bytes(b'incomplete')
            raise r.StageFailure('bounded source timeout')
        result=self.advance(execute=fail)
        self.assertFalse(result['needsAttention'])
        self.assertEqual(result['completedCounts']['sources'],1)
        self.assertFalse((self.site/r.ACTIVE/'partial-uncommitted').exists())
        self.assertEqual((self.site/'manifest.json').read_bytes(),original)
        self.assertFalse(self.advance()['changed'])
        resumed=self.advance(now=NOW+dt.timedelta(days=1,seconds=1))
        self.assertEqual(resumed['completedCounts']['sources'],2)

    def test_failed_first_source_can_preserve_immutable_plan(self):
        def fail(req,cp,work,seconds):
            raise r.StageFailure('first source timeout',{'sourcePlan':self.plan})
        result=self.advance(allow_new=True,execute=fail)
        self.assertEqual(result['stage'],'source')
        self.assertEqual(result['completedCounts']['sources'],0)
        checkpoint=r.load_checkpoint(self.site,self.configs,inspector=self.inspector)
        self.assertEqual(checkpoint['sourcePlan'],self.plan)
        self.assertEqual(checkpoint['files'],{})

    def test_global_interrupt_is_never_published_as_failure(self):
        self.advance(allow_new=True)
        before=r.inventory(self.site)
        def stop(*args):raise r.RolloutInterrupted('global kill')
        with self.assertRaises(r.RolloutInterrupted):self.advance(execute=stop)
        self.assertEqual(before,r.inventory(self.site))

    def test_storage_failure_leaves_previous_checkpoint_unchanged(self):
        self.advance(allow_new=True)
        before=r.inventory(self.site)
        with patch.object(r,'seal_snapshot',side_effect=ValueError('250 MB exhausted')):
            with self.assertRaisesRegex(ValueError,'250 MB'):self.advance()
        self.assertEqual(before,r.inventory(self.site))

    def test_unexpected_multi_stage_progress_is_rejected(self):
        def extra(req,cp,work,seconds):
            result,out=self.execute(req,cp,work,seconds)
            result['progress']['completedStates']=['state-a','state-b']
            r.write_json(cp/'progress.json',result['progress'])
            return result,out
        result=self.advance(allow_new=True,execute=extra)
        self.assertTrue(result['needsAttention'])
        self.assertEqual(result['completedCounts']['sources'],0)

    def test_source_group_can_publish_a_contiguous_verified_prefix(self):
        def group(req,cp,work,seconds):
            result,out=self.execute(req,cp,work,seconds)
            result['progress']['completedStates']=['state-a','state-b']
            result['sourceGroup']={'completedStates':['state-a','state-b'],'remainingStates':[], 'plannedPause':False}
            r.write_json(cp/'progress.json',result['progress'])
            return result,out
        result=self.advance(allow_new=True,execute=group)
        self.assertFalse(result['needsAttention']);self.assertEqual(result['completedCounts']['sources'],2)
        self.assertEqual(result['stage'],'index')

    def test_source_group_cannot_skip_a_state_or_alter_prior_source_files(self):
        self.advance(allow_new=True)
        cp=self.site/r.ACTIVE
        (cp/'sources/state-a').mkdir(parents=True)
        (cp/'sources/state-a/receipt.json').write_text('immutable')
        descriptor=json.loads((cp/r.DESCRIPTOR).read_text());r.save_checkpoint(self.site,descriptor)
        def alter(req,checkpoint,work,seconds):
            result,out=self.execute(req,checkpoint,work,seconds)
            (checkpoint/'sources/state-a/receipt.json').write_text('altered')
            return result,out
        result=self.advance(execute=alter)
        self.assertTrue(result['needsAttention']);self.assertIn('previously completed',result['reason'])
        self.assertEqual((cp/'sources/state-a/receipt.json').read_text(),'immutable')
        before=r.empty_progress(self.configs[0]);after={**before,'completedStates':['state-b']}
        with self.assertRaisesRegex(ValueError,'unexpected'):
            r.validate_transition(self.configs[0],before,after,{'kind':'start'})

    def test_source_group_cooperative_pause_is_not_a_failure(self):
        self.advance(allow_new=True)
        def pause(req,cp,work,seconds):
            progress=self.inspector(req['config'],req['sourcePlan'],cp)
            return {'schemaVersion':1,'sourcePlan':req['sourcePlan'],'progress':progress,'plannedPause':True,
                    'sourceGroup':{'completedStates':[],'remainingStates':['state-b'],'plannedPause':True}},work/'unused'
        result=self.advance(execute=pause)
        self.assertFalse(result['needsAttention']);self.assertIsNone(result['retryAfter'])
        self.assertEqual(result['completedCounts']['sources'],1)

    def test_authorized_migration_preserves_generation_and_source_then_resumes(self):
        import checkpoint_migrations
        self.configs[0]['sourceStates']=['illinois','wisconsin']
        self.advance(allow_new=True)
        cp=self.site/r.ACTIVE;source=cp/'sources/illinois';source.mkdir(parents=True)
        raw=b'unchanged crop';digest=hashlib.sha256(raw).hexdigest()
        (source/'crop.part-0000').write_bytes(raw)
        r.write_json(source/'receipt.json',{'crop':{'encoding':'raw','bytes':len(raw),'sha256':digest,
            'parts':[{'path':'crop.part-0000','bytes':len(raw),'sha256':digest}]}})
        descriptor=json.loads((cp/r.DESCRIPTOR).read_text());r.save_checkpoint(self.site,descriptor)
        before=r.checkpoint_files(cp);manifest_before=(self.site/'manifest.json').read_bytes()
        pin=hashlib.sha256((cp/r.DESCRIPTOR).read_bytes()).hexdigest();clock=[0]
        def verify(*a,**k):clock[0]=160;return {'fingerprint':'a'*64},'full-tests'
        with patch.object(checkpoint_migrations,'FROM_DESCRIPTOR',pin), \
             patch.object(checkpoint_migrations,'FROM_CODE',CODE):
            result=self.advance(fingerprinter=lambda root:'a'*64,verifier=verify,deadline=200,monotonic=lambda:clock[0])
        self.assertTrue(result['changed']);self.assertEqual(before,r.checkpoint_files(cp))
        current=json.loads((cp/r.DESCRIPTOR).read_text())
        for field in ('generationId','sourcePlan','config','progress','files'):
            self.assertEqual(current[field],descriptor[field])
        self.assertEqual(current['codeFingerprint'],'a'*64)
        self.assertEqual((self.site/'manifest.json').read_bytes(),manifest_before)
        audit=json.loads((self.site/'rollout-state.json').read_text())['checkpointMigrations']
        self.assertEqual(len(audit),1);self.assertEqual(audit[0]['generationId'],descriptor['generationId'])
        for number in range(5):
            result=self.advance(fingerprinter=lambda root:'a'*64,verifier=lambda *a,**k:({'fingerprint':'a'*64},'verified'))
        self.assertFalse(result['active'])
        self.assertEqual(json.loads((self.site/'rollout-state.json').read_text())['checkpointMigrations'],audit)

    def test_migration_is_not_saved_when_current_verification_fails(self):
        import checkpoint_migrations
        self.advance(allow_new=True)
        before=r.inventory(self.site)
        with patch.object(checkpoint_migrations,'eligible',return_value=True), \
             patch.object(checkpoint_migrations,'migrate') as migrate:
            with self.assertRaisesRegex(RuntimeError,'tests failed'):
                self.advance(fingerprinter=lambda root:'a'*64,
                             verifier=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('tests failed')))
            migrate.assert_not_called()
        self.assertEqual(before,r.inventory(self.site))

    def test_short_after_verification_persists_verified_pending_start(self):
        clock=[0]
        def verify(*args,**kwargs):clock[0]=550;return {'fingerprint':CODE},'full-tests'
        result=self.advance(allow_new=True,verifier=verify,deadline=600,monotonic=lambda:clock[0])
        self.assertTrue(result['active']);self.assertEqual(result['stage'],'start');self.assertFalse(self.calls)
        self.assertTrue((self.site/'verification.json').exists())

    def test_tile_group_publishes_multiple_verified_batches_together(self):
        self.advance(allow_new=True);self.advance();self.advance()
        def group(req,cp,work,seconds):
            result,out=self.execute(req,cp,work,seconds)
            result['progress']['completedBatches']=[0,1]
            result['group']={'completedBatches':[0,1],'remainingBatches':[]}
            r.write_json(cp/'progress.json',result['progress'])
            return result,out
        result=self.advance(execute=group)
        self.assertFalse(result['needsAttention'])
        self.assertEqual(result['completedCounts']['batches'],2)
        self.assertEqual(result['stage'],'finalize')

    def test_tile_group_cooperative_pause_is_not_failure(self):
        self.advance(allow_new=True);self.advance();self.advance()
        def pause(req,cp,work,seconds):
            progress=self.inspector(req['config'],req['sourcePlan'],cp)
            return {'schemaVersion':1,'sourcePlan':req['sourcePlan'],'progress':progress,'plannedPause':True,
                    'group':{'completedBatches':[],'remainingBatches':[0,1]}},work/'unused'
        result=self.advance(execute=pause)
        self.assertFalse(result['needsAttention']);self.assertIsNone(result['retryAfter'])
        self.assertEqual(result['stage'],'tiles')

    def test_transient_retry_is_due_exactly_after_twenty_hours(self):
        self.advance(allow_new=True)
        def fail(*args):raise r.StageFailure('temporary network timeout')
        failure=self.advance(execute=fail)
        self.assertEqual(failure['retryAfter'],(NOW+dt.timedelta(hours=20)).isoformat())
        self.assertFalse(self.advance(now=NOW+dt.timedelta(hours=20)-dt.timedelta(seconds=1))['changed'])
        resumed=self.advance(now=NOW+dt.timedelta(hours=20))
        self.assertTrue(resumed['changed']);self.assertEqual(resumed['completedCounts']['sources'],2)

    def test_three_transient_failures_stop_automatic_retry(self):
        self.advance(allow_new=True)
        def fail(*args):raise r.StageFailure('temporary download failure')
        for count in range(3):
            result=self.advance(execute=fail,now=NOW+dt.timedelta(days=count,seconds=count))
            self.assertEqual(result['needsAttention'],count==2)
        self.assertIsNone(result['retryAfter'])
        self.assertFalse(self.advance(now=NOW+dt.timedelta(days=4))['changed'])

    def test_older_pinned_source_blocks_resume_without_changing_checkpoint(self):
        self.advance(allow_new=True)
        active=manifest(self.site)
        active['regions'][1]['sourceTimestamp']='2026-10-12T00:00:00Z'
        r.write_json(self.site/'manifest.json',active)
        self.plan['osmReplicationTimestamp']='2026-10-01T00:00:00Z'
        checkpoint=json.loads((self.site/r.ACTIVE/r.DESCRIPTOR).read_text())
        checkpoint['sourcePlan']=self.plan
        r.write_json(self.site/r.ACTIVE/r.DESCRIPTOR,checkpoint)
        before=r.inventory(self.site/r.ACTIVE)
        result=self.advance()
        self.assertTrue(result['needsAttention']);self.assertIn('regress',result['reason'])
        self.assertEqual(before,r.inventory(self.site/r.ACTIVE))

    def test_status_html_escapes_failure_and_name(self):
        checkpoint={'progress':r.empty_progress(self.configs[0]),'config':{**self.configs[0],'name':'<script>x</script>'},
            'nextStage':{'kind':'source'},'needsAttention':True,'lastError':{'message':'<img src=x>'},'generationId':'a'*64,'sourcePlan':None}
        status=r.status_for(checkpoint,NOW)
        r.write_status(self.site,status)
        text=(self.site/'status.html').read_text()
        self.assertNotIn('<script>',text);self.assertIn('&lt;img',text)


class PublicationRevisionTests(unittest.TestCase):
    def test_checkpoint_only_update_invalidates_stale_publisher(self):
        manifest=b'{"regions":[]}'
        expected=snapshot.publication_revision(manifest,b'old snapshot')
        def fetch(url,limit):return manifest if url.endswith('manifest.json') else b'new snapshot'
        with self.assertRaisesRegex(ValueError,'revision changed'):
            snapshot.assert_publication_unchanged(expected,fetch=fetch)

    def test_bootstrap_revision_distinguishes_new_descriptor(self):
        manifest=b'{}';expected=snapshot.publication_revision(manifest,None)
        def absent(url,limit):
            if url.endswith('manifest.json'):return manifest
            raise HTTPError(url,404,'Not found',{},None)
        self.assertEqual(snapshot.assert_publication_unchanged(expected,fetch=absent),expected)
        def exists(url,limit):return manifest if url.endswith('manifest.json') else b'{}'
        with self.assertRaises(ValueError):snapshot.assert_publication_unchanged(expected,fetch=exists)


if __name__=='__main__':unittest.main()
