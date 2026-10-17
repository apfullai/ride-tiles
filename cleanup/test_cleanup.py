import copy,hashlib,json,unittest
from unittest.mock import patch
import delete_verified_artifact as c
class CleanupTests(unittest.TestCase):
 def setUp(self):
  self.calls=[];self.deleted=False
  self.run={'id':c.RUN,'head_sha':c.SHA,'head_branch':'main','status':'completed','conclusion':'success','path':'.github/workflows/rollout.yml','repository':{'full_name':c.REPO}}
  self.artifact={'id':c.ARTIFACT,'name':'github-pages','size_in_bytes':c.SIZE,'expired':False,'digest':c.DIGEST,'workflow_run':{'id':c.RUN,'head_sha':c.SHA,'head_branch':'main','repository_id':1420913307,'head_repository_id':1420913307}}
  self.data={p:p.encode() for p in c.LIVE};self.hashes={p:hashlib.sha256(b).hexdigest() for p,b in self.data.items()}
  self.delete_status=204;self.absence=404;self.main=c.SHA;self.corrupt_after=False
 def call(self,method,url):
  self.calls.append((method,url))
  if method=='DELETE':
   self.assertEqual(url,c.API+f'/actions/artifacts/{c.ARTIFACT}');self.deleted=True;return self.delete_status,b''
  if url.startswith(c.BASE):
   name=url[len(c.BASE):];return 200,b'changed' if self.deleted and self.corrupt_after else self.data[name]
  if url.endswith('/git/ref/heads/main'):return 200,json.dumps({'object':{'sha':self.main}}).encode()
  if url.endswith(f'/actions/runs/{c.RUN}'):return 200,json.dumps(self.run).encode()
  if url.endswith(f'/actions/artifacts/{c.ARTIFACT}'):
   return (self.absence,b'') if self.deleted else (200,json.dumps(self.artifact).encode())
  raise AssertionError('Unexpected endpoint')
 def execute(self):
  with patch.object(c,'LIVE',self.hashes):return c.execute(self.call)
 def assert_no_delete(self):self.assertFalse(any(m=='DELETE' for m,u in self.calls))
 def test_success_deletes_exactly_once_and_checks_after(self):
  report=self.execute();self.assertTrue(report['apiAbsenceConfirmed']);self.assertTrue(report['liveUnchanged']);self.assertEqual(sum(m=='DELETE' for m,u in self.calls),1);self.assertEqual(len([u for m,u in self.calls if u.startswith(c.BASE)]),6)
 def test_artifact_identity_mismatches_never_delete(self):
  for key,value in [('id',1),('name','other'),('size_in_bytes',1),('expired',True),('digest','wrong')]:
   with self.subTest(key=key):
    original=copy.deepcopy(self.artifact);self.artifact[key]=value;self.calls=[]
    with self.assertRaises(ValueError):self.execute()
    self.assert_no_delete();self.artifact=original
 def test_artifact_run_and_repository_mismatches_never_delete(self):
  for key,value in [('id',1),('head_sha','other'),('head_branch','other'),('repository_id',1),('head_repository_id',1)]:
   with self.subTest(key=key):
    original=copy.deepcopy(self.artifact);self.artifact['workflow_run'][key]=value;self.calls=[]
    with self.assertRaises(ValueError):self.execute()
    self.assert_no_delete();self.artifact=original
 def test_run_identity_mismatches_never_delete(self):
  for key,value in [('id',1),('head_sha','other'),('head_branch','other'),('status','in_progress'),('conclusion','failure'),('path','other.yml'),('repository',{'full_name':'other/repo'})]:
   with self.subTest(key=key):
    original=copy.deepcopy(self.run);self.run[key]=value;self.calls=[]
    with self.assertRaises(ValueError):self.execute()
    self.assert_no_delete();self.run=original
 def test_changed_main_never_deletes(self):
  self.main='other'
  with self.assertRaises(ValueError):self.execute()
  self.assert_no_delete()
 def test_changed_live_checkpoint_never_deletes(self):
  self.data['checkpoints/active/checkpoint.json']=b'changed'
  with self.assertRaises(ValueError):self.execute()
  self.assert_no_delete()
 def test_unconfirmed_delete_is_not_retried(self):
  self.delete_status=202
  with self.assertRaises(RuntimeError):self.execute()
  self.assertEqual(sum(m=='DELETE' for m,u in self.calls),1)
 def test_absence_not_confirmed_never_redeletes(self):
  self.absence=200
  with self.assertRaises(RuntimeError):self.execute()
  self.assertEqual(sum(m=='DELETE' for m,u in self.calls),1)
 def test_postdelete_live_change_reported(self):
  self.corrupt_after=True
  with self.assertRaises(ValueError):self.execute()
  self.assertEqual(sum(m=='DELETE' for m,u in self.calls),1)
 def test_transport_rejects_any_other_mutation_or_host(self):
  for method,url in [('DELETE',c.API+'/actions/artifacts/1'),('POST',c.API+f'/actions/artifacts/{c.ARTIFACT}'),('GET','https://api.github.com.evil.invalid/')]:
   with self.subTest(url=url),self.assertRaises(ValueError):c.request(method,url)
 def test_credentials_only_on_exact_api(self):
  import io,email.message,urllib.request,urllib.response,os
  requests=[]
  def transport(handler,req):
   requests.append(req);r=urllib.response.addinfourl(io.BytesIO(b'{}'),email.message.Message(),req.full_url,200);r.msg='OK';return r
  with patch.dict(os.environ,{'GH_TOKEN':'offline-fixture'}),patch.object(urllib.request.HTTPSHandler,'https_open',transport):
   c.request('GET',c.API+f'/actions/artifacts/{c.ARTIFACT}');c.request('GET',c.BASE+'snapshot.json')
  self.assertEqual(requests[0].get_header('Authorization'),'Bearer offline-fixture');self.assertIsNone(requests[1].get_header('Authorization'))
 def test_redirect_is_never_followed(self):
  import io,email.message,urllib.request,urllib.response,urllib.error,os
  calls=[]
  def transport(handler,req):
   calls.append(req.full_url);h=email.message.Message();h['Location']='https://elsewhere.invalid/';r=urllib.response.addinfourl(io.BytesIO(b''),h,req.full_url,302);r.msg='Redirect';return r
  with patch.dict(os.environ,{'GH_TOKEN':'offline-fixture'}),patch.object(urllib.request.HTTPSHandler,'https_open',transport):
   with self.assertRaises(urllib.error.HTTPError):c.request('GET',c.API+f'/actions/artifacts/{c.ARTIFACT}')
  self.assertEqual(calls,[c.API+f'/actions/artifacts/{c.ARTIFACT}'])
if __name__=='__main__':unittest.main()
