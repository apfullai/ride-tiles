import copy, pathlib, subprocess, sys, tempfile, unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import verified_code as v
class ProofTests(unittest.TestCase):
 def setUp(self):
  self.records={'tools/build.py':'b'*40,'bootstrap/part000':'c'*40}
  self.receipt={'schemaVersion':1,'fingerprint':v.fingerprint(self.records),'runId':7,'commit':'a'*40}
  self.run={'repository':{'full_name':v.REPOSITORY},'head_repository':{'full_name':v.REPOSITORY},'path':v.WORKFLOW,'head_sha':'a'*40,'head_branch':'main','event':'push','status':'completed','conclusion':'success','run_attempt':1}
  self.job={'name':'test','run_id':7,'status':'completed','conclusion':'success','steps':[{'name':n,'status':'completed','conclusion':'success'} for n in v.REQUIRED_STEPS]}
  self.tree={'truncated':False,'tree':[{'path':p,'type':'blob','mode':'100644','sha':s} for p,s in self.records.items()]}
 def fetch(self,url):
  if '/git/trees/' in url:return self.tree
  if '/jobs?' in url:return {'total_count':1,'jobs':[self.job]}
  return self.run
 def test_exact_proven_tree(self):self.assertTrue(v.prove_receipt(self.receipt,self.records,self.fetch))
 def test_successful_skipped_run_not_proof(self):
  self.job['conclusion']='skipped';self.assertFalse(v.prove_receipt(self.receipt,self.records,self.fetch))
 def test_skipped_suite_not_proof(self):
  self.job['steps'][0]['conclusion']='skipped';self.assertFalse(v.prove_receipt(self.receipt,self.records,self.fetch))
 def test_wrong_repository_event_workflow_and_sha(self):
  for key,val in [('head_repository',{'full_name':'evil/fork'}),('event','pull_request'),('path','.github/workflows/rollout.yml'),('head_sha','b'*40)]:
   old=copy.deepcopy(self.run);self.run[key]=val;self.assertFalse(v.prove_receipt(self.receipt,self.records,self.fetch));self.run=old
 def test_changed_input_or_truncated_tree(self):
  self.tree['tree'][0]['sha']='d'*40;self.assertFalse(v.prove_receipt(self.receipt,self.records,self.fetch))
  self.tree['truncated']=True;self.assertFalse(v.prove_receipt(self.receipt,self.records,self.fetch))
 def test_research_bootstrap_and_arbitrary_fixtures_selected(self):
  for path in ('bootstrap/a.bin','research/census.xlsx','tests/f.bin','config/new-data.txt'):self.assertTrue(v.selected(path))
  self.assertFalse(v.selected('config/rollout-request.json'));self.assertFalse(v.selected('tools/__pycache__/x.pyc'))
 def test_source_symlink_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t);(p/'tools').mkdir();(p/'other').write_text('x');(p/'tools/x').symlink_to(p/'other')
   with self.assertRaises(ValueError):v.source_records(p)
 def test_verification_runner_timeout_and_failure(self):
  with tempfile.TemporaryDirectory() as t:
   with self.assertRaises(subprocess.TimeoutExpired):v.run_group([sys.executable,'-c','import time;time.sleep(30)'],cwd=t,check=True,timeout=.05)
   with self.assertRaises(subprocess.CalledProcessError):v.run_group([sys.executable,'-c','raise SystemExit(2)'],cwd=t,check=True,timeout=2)
 def test_local_receipt_cannot_skip(self):
  r=dict(self.receipt,runId=None,localFullTestsPassed=True);self.assertFalse(v.prove_receipt(r,self.records,self.fetch))
if __name__=='__main__':unittest.main()
