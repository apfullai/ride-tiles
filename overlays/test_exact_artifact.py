import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(os.environ['PUBLISHER_ROOT'])/'publishing'))
import exact_artifact as e
from verify_uploaded_artifact import verify
sys.path.insert(0,str(Path(os.environ['PUBLISHER_ROOT'])/'publishing'))
class ExactArtifact(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name);self.site=self.root/'site';self.site.mkdir();(self.site/'a').write_bytes(b'hello')
 def test_exact_repeatable(self):
  a=e.pack(self.site,self.root/'one/artifact.tar');b=e.pack(self.site,self.root/'two/artifact.tar');self.assertEqual(a,b);self.assertEqual(a['candidateBytes'],a['tarBytes']+(a['tarBytes']+99)//100+65536)
 def test_symlink_rejected(self):
  (self.site/'link').symlink_to('a')
  with self.assertRaises(ValueError):e.pack(self.site,self.root/'artifact.tar')
 def test_tamper_rejected(self):
  e.pack(self.site,self.root/'artifact.tar');(self.site/'a').write_bytes(b'other')
  with self.assertRaises(ValueError):e.verify_tar(self.root/'artifact.tar',e.inventory(self.site))
 def test_inside_output_rejected(self):
  with self.assertRaises(ValueError):e.pack(self.site,self.site/'artifact.tar')
 def test_budget_bound_and_bad_metadata(self):
  for artifacts,allowed in [([],True),([{'size_in_bytes':250000000,'expired':False}],False),([{'size_in_bytes':True,'expired':False}],False),([{'size_in_bytes':1,'expired':'no'}],False)]:
   with self.subTest(artifacts=artifacts):
    result=e.audit('apfullai/ride-tiles',{'candidateBytes':100},fetch=lambda url:{'total_count':len(artifacts),'artifacts':artifacts});self.assertEqual(result['allowed'],allowed)
 def test_upload_guard(self):
  good={'id':123,'name':'github-pages','expired':False,'size_in_bytes':99,'workflow_run':{'id':456}}
  verify(good,{'candidateBytes':100},'123','456')
  for key,val in [('id',321),('name','other'),('expired',True),('size_in_bytes',101),('workflow_run',{'id':999})]:
   with self.subTest(key=key):
    with self.assertRaises(ValueError):verify({**good,key:val},{'candidateBytes':100},'123','456')
if __name__=='__main__':unittest.main()
