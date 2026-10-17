import copy,datetime,json,unittest
import guard as g
class Guard(unittest.TestCase):
 def setUp(self):
  self.run={'id':g.RUN,'head_sha':g.HEAD,'head_branch':'feat/mellow-z17-mirror','run_attempt':1,'status':'completed','conclusion':'failure','repository':{'full_name':g.REPO},'path':'.github/workflows/rollout.yml'}
  self.jobs={'total_count':3,'jobs':[{'name':n,'run_id':g.RUN,'status':'completed','conclusion':'success','steps':[{'name':x,'conclusion':'success'} for x in g.STEPS]} for n in ('budget','build')]+[{'name':'deploy','conclusion':'failure','steps':[]}]}
  self.a={'id':g.ARTIFACT,'name':'github-pages','expired':False,'size_in_bytes':g.SIZE,'digest':g.DIGEST,'expires_at':g.EXPIRY,'workflow_run':{'id':g.RUN,'head_sha':g.HEAD,'head_branch':'feat/mellow-z17-mirror','repository_id':1420913307,'head_repository_id':1420913307}}
  self.main={'object':{'sha':g.MAIN}};self.now=datetime.datetime(2026,10,17,10,tzinfo=datetime.timezone.utc)
 def check(self):g.validate(self.run,self.jobs,self.a,self.main,g.LIVE,self.now)
 def test_exact(self):self.check()
 def test_identity_and_gate_mismatches(self):
  for target,key,bad in [(self.run,'head_sha','bad'),(self.run,'conclusion','success'),(self.a,'digest','bad'),(self.a,'size_in_bytes',0),(self.a,'expired',True),(self.jobs,'total_count',4)]:
   with self.subTest(key=key):
    old=target[key];target[key]=bad
    with self.assertRaises(ValueError):self.check()
    target[key]=old
 def test_changed_live(self):
  with self.assertRaises(ValueError):g.validate(self.run,self.jobs,self.a,self.main,{},self.now)
 def test_expiry(self):
  self.now=datetime.datetime(2026,10,18,9,45,tzinfo=datetime.timezone.utc)
  with self.assertRaises(ValueError):self.check()
 def test_prior_started_deploy(self):
  self.jobs['jobs'][2]['steps']=[{'name':'deploy'}]
  with self.assertRaises(ValueError):self.check()
if __name__=='__main__':unittest.main()
