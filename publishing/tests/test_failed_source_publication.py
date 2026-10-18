import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_resumable_rollout as fixtures
import resumable_rollout as r

class FailedPublicationTests(unittest.TestCase):
 def fixture(self):
  f=fixtures.ResumableRolloutTests();f.setUp();self.addCleanup(f.doCleanups);return f
 def test_zero_progress_failure_preserves_every_published_byte(self):
  f=self.fixture();f.advance(allow_new=True)
  before={str(p.relative_to(f.site)):p.read_bytes() for p in f.site.rglob('*') if p.is_file()};events=[]
  def fail(*args):raise r.StageFailure('bounded timeout',{'error':{'message':'bounded timeout','permanent':False}})
  result=f.advance(execute=fail,attempt_observer=lambda *args:events.append(copy.deepcopy(args)))
  self.assertFalse(result['changed']);self.assertFalse(result['failureStatePublished']);self.assertEqual(result['failureCount'],1)
  self.assertEqual([x[0] for x in events],['started','failed'])
  self.assertEqual(before,{str(p.relative_to(f.site)):p.read_bytes() for p in f.site.rglob('*') if p.is_file()})
 def test_planned_pause_without_progress_does_not_publish_metadata(self):
  f=self.fixture();f.advance(allow_new=True)
  before=(f.site/r.ACTIVE/r.DESCRIPTOR).read_bytes()
  def pause(request,checkpoint,work,seconds):
   return {'sourcePlan':f.plan,'progress':f.inspector(request['config'],f.plan,checkpoint),'plannedPause':True},work
  result=f.advance(execute=pause)
  self.assertFalse(result['changed']);self.assertEqual(before,(f.site/r.ACTIVE/r.DESCRIPTOR).read_bytes())
 def test_build_clock_preserves_external_tail(self):
  env={'GITHUB_ACTIONS':'true','RIDE_BUILD_STARTED':'100','RIDE_BUILD_DEADLINE':'1480'}
  self.assertEqual(r.process_deadline(monotonic=lambda:200,environ=env),1290)
  self.assertEqual(r.process_deadline(monotonic=lambda:200,environ={}),700)
  for change in ({'RIDE_BUILD_DEADLINE':'nan'},{'RIDE_BUILD_DEADLINE':'1481'},{'RIDE_BUILD_STARTED':'201'},{'RIDE_BUILD_DEADLINE':'200'}):
   with self.assertRaises(ValueError):r.process_deadline(monotonic=lambda:200,environ={**env,**change})
  with self.assertRaises(ValueError):r.process_deadline(monotonic=lambda:1300,environ=env)
  with self.assertRaises(ValueError):r.process_deadline(monotonic=lambda:200,environ={'GITHUB_ACTIONS':'true'})
 def test_initial_progress_binds_future_history_origin(self):
  f=self.fixture();origin={'runId':123,'attempt':1,'commit':'a'*40}
  result=f.advance(allow_new=True,attempt_origin=origin)
  self.assertTrue(result['changed'])
  checkpoint=json.loads((f.site/r.ACTIVE/r.DESCRIPTOR).read_text())
  self.assertEqual(checkpoint['attemptHistoryOrigin'],origin)
 def test_initial_pin_only_failure_binds_future_history(self):
  f=self.fixture();origin={'runId':123,'attempt':1,'commit':'a'*40}
  def fail(*args):raise r.StageFailure('download failed',{'sourcePlan':f.plan})
  result=f.advance(allow_new=True,attempt_origin=origin,execute=fail)
  self.assertTrue(result['changed'])
  checkpoint=json.loads((f.site/r.ACTIVE/r.DESCRIPTOR).read_text())
  self.assertEqual(checkpoint['attemptHistoryOrigin'],origin)
  self.assertEqual(checkpoint['sourcePlan'],f.plan)
  self.assertEqual(checkpoint['progress']['completedStates'],[])
  self.assertEqual(checkpoint['failureCount'],1)
 def test_invalid_origin_cannot_publish_progress(self):
  f=self.fixture();before={str(p.relative_to(f.site)):p.read_bytes() for p in f.site.rglob('*') if p.is_file()}
  with self.assertRaises(ValueError):f.advance(allow_new=True,attempt_origin={'runId':True,'attempt':1,'commit':'a'*40})
  self.assertEqual(before,{str(p.relative_to(f.site)):p.read_bytes() for p in f.site.rglob('*') if p.is_file()})
 def test_failed_prefix_requires_exact_receipt_progress_and_old_files(self):
  with tempfile.TemporaryDirectory() as d:
   site=Path(d);root=site/r.ACTIVE;root.mkdir(parents=True);(root/'old').write_bytes(b'old')
   config={'sourceStates':['a','b','c']};before=r.empty_progress(config);before['completedStates']=['a']
   checkpoint={'config':config,'sourcePlan':{'planId':'p'},'progress':before,'stageCount':3,'files':r.checkpoint_files(root)}
   (root/'new').write_bytes(b'new');after={**before,'completedStates':['a','b']}
   result={'sourcePlan':{'planId':'p'},'progress':after,'sourceGroup':{'schemaVersion':1,'planId':'p','status':'failed','completedStates':['b'],'remainingStates':['c'],'plannedPause':True}}
   recovered=r.recover_source_prefix(checkpoint,result,site,{'kind':'source','state':'b'},lambda *args:after)
   self.assertEqual(recovered['progress'],after);self.assertEqual(recovered['stageCount'],4);self.assertEqual(checkpoint['progress'],before)
   for change in ({'completedStates':['c']},{'remainingStates':[]},{'planId':'q'},{'status':'complete'}):
    bad=copy.deepcopy(result);bad['sourceGroup'].update(change)
    with self.assertRaises(ValueError):r.recover_source_prefix(checkpoint,bad,site,{'kind':'source','state':'b'},lambda *args:after)
   (root/'old').write_bytes(b'changed')
   with self.assertRaises(ValueError):r.recover_source_prefix(checkpoint,result,site,{'kind':'source','state':'b'},lambda *args:after)
 def test_journaled_prefix_survives_unrecorded_later_commit(self):
  with tempfile.TemporaryDirectory() as d:
   baseline=Path(d)/'baseline';candidate=Path(d)/'candidate'
   old=baseline/r.ACTIVE/'sources'/'a';old.mkdir(parents=True);(old/'data').write_bytes(b'a')
   import shutil
   shutil.copytree(baseline,candidate)
   for name in ('b','c'):
    target=candidate/r.ACTIVE/'sources'/name;target.mkdir();(target/'data').write_bytes(name.encode())
   (candidate/'unrecorded').write_bytes(b'discard')
   config={'sourceStates':['a','b','c']};before=r.empty_progress(config);before['completedStates']=['a']
   checkpoint={'config':config,'sourcePlan':{'planId':'p'},'progress':before,'stageCount':1,'files':r.checkpoint_files(baseline/r.ACTIVE)}
   after={**before,'completedStates':['a','b']}
   result={'sourcePlan':{'planId':'p'},'progress':after,'sourceGroup':{'schemaVersion':1,'planId':'p','status':'running','completedStates':['b'],'remainingStates':['c'],'plannedPause':True}}
   def inspect(config,plan,root):return {**before,'completedStates':sorted(p.name for p in (root/'sources').iterdir())}
   recovered=r.recover_source_prefix(checkpoint,result,candidate,{'kind':'source','state':'b'},inspect,baseline=baseline)
   self.assertEqual(recovered['progress'],after);self.assertFalse((candidate/r.ACTIVE/'sources'/'c').exists());self.assertFalse((candidate/'unrecorded').exists())
   self.assertEqual((candidate/r.ACTIVE/'sources'/'a'/'data').read_bytes(),b'a')
   self.assertEqual((candidate/r.ACTIVE/'sources'/'b'/'data').read_bytes(),b'b')
if __name__=='__main__':unittest.main()
