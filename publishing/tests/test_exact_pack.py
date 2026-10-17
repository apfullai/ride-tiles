import copy,datetime as dt,hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'publishing'))
import exact_artifact as e
import exact_pack_migration as migration
from exact_tar_size import allowance
from verify_uploaded_artifact import verify
from verified_code import current_fingerprint
class ExactPack(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.site=self.root/'site';self.site.mkdir();(self.site/'a').write_bytes(b'hello')
 def test_long_names_unicode_hardlinks_and_empty_directories(self):
  for n in (97,98,99,100,101,200):(self.site/('x'*n)).write_bytes(b'long')
  (self.site/('é'*60)).write_bytes(b'unicode');(self.site/'empty-directory').mkdir();os.link(self.site/'a',self.site/'hardlink')
  a=e.pack(self.site,self.root/'one/artifact.tar');b=e.pack(self.site,self.root/'two/artifact.tar');self.assertEqual(a,b)
  self.assertEqual(a['candidateBytes'],a['tarBytes']+(a['tarBytes']+99)//100+65536)
 def test_tar_environment_cannot_override_flags(self):
  a=e.pack(self.site,self.root/'one/artifact.tar')
  with patch.dict(os.environ,{'TAR_OPTIONS':'--format=pax --mtime=2020-01-01','POSIXLY_CORRECT':'1'}):
   b=e.pack(self.site,self.root/'two/artifact.tar')
  self.assertEqual(a,b)
 def test_post_upload_uses_only_supported_listing(self):
  import verify_uploaded_artifact as uploaded
  receipt=self.root/'receipt.json';receipt.write_text(json.dumps({'allowed':True,'complete':True,'candidateBytes':100}))
  urls=[]
  def fetch(url):
   urls.append(url);self.assertIn('/actions/artifacts?per_page=',url)
   return {'total_count':1,'artifacts':[{'id':123,'name':'github-pages','expired':False,'size_in_bytes':99,'workflow_run':{'id':456}}]}
  with patch.object(sys,'argv',['verify','--publisher',str(ROOT),'--receipt',str(receipt)]),patch.dict(os.environ,{'GITHUB_REPOSITORY':'apfullai/ride-tiles','UPLOADED_ARTIFACT_ID':'123','GITHUB_RUN_ID':'456'}),patch('actions_budget.fetch_public_json',side_effect=fetch):
   uploaded.main()
  self.assertGreaterEqual(len(urls),2)
 def test_sealed_metadata_prediction_matches_actual(self):
  from early_capacity import metadata_allowance
  from rollout_snapshot import seal_snapshot
  # Use the complete reconstructed public site in a separate bounded local proof;
  # this fixture verifies the same formula without fetching data.
  files={name:record['bytes'] for name,record in e.inventory(self.site).items()}
  actual=e.pack(self.site,self.root/'artifact.tar')
  self.assertEqual(allowance(files)['tarBytesUpperBound'],actual['tarBytes'])
 def test_symlink_rejected(self):
  (self.site/'link').symlink_to('a')
  with self.assertRaises(ValueError):e.pack(self.site,self.root/'artifact.tar')
 def test_tamper_rejected(self):
  e.pack(self.site,self.root/'artifact.tar');(self.site/'a').write_bytes(b'other')
  with self.assertRaises(ValueError):e.verify_tar(self.root/'artifact.tar',e.inventory(self.site))
 def test_budget_bound_and_bad_metadata(self):
  for artifacts,allowed in [([],True),([{'size_in_bytes':250000000,'expired':False}],False),([{'size_in_bytes':True,'expired':False}],False),([{'size_in_bytes':1,'expired':'no'}],False)]:
   with self.subTest(artifacts=artifacts):
    self.assertEqual(e.audit('example/tiles',{'candidateBytes':100},fetch=lambda url:{'total_count':len(artifacts),'artifacts':artifacts})['allowed'],allowed)
 def test_upload_guard(self):
  good={'id':123,'name':'github-pages','expired':False,'size_in_bytes':99,'workflow_run':{'id':456}}
  verify(good,{'candidateBytes':100},'123','456')
  for key,val in [('id',321),('name','other'),('expired',True),('size_in_bytes',101),('workflow_run',{'id':999})]:
   with self.subTest(key=key):
    with self.assertRaises(ValueError):verify({**good,key:val},{'candidateBytes':100},'123','456')
class ExactMigration(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.cp=Path(self.tmp.name)
  for state in ('illinois','wisconsin'):
   d=self.cp/'sources'/state;d.mkdir(parents=True);b=state.encode();h=hashlib.sha256(b).hexdigest();(d/'crop.part-0000').write_bytes(b)
   (d/'receipt.json').write_text(json.dumps({'crop':{'encoding':'raw','bytes':len(b),'sha256':h,'parts':[{'path':'crop.part-0000','bytes':len(b),'sha256':h}]}}))
  from rollout_snapshot import _records
  self.checkpoint={'codeFingerprint':migration.FROM_CODE,'progress':{'completedStates':['illinois','wisconsin'],'indexReady':False,'batchIds':[],'batchCount':0,'completedBatches':[]},'nextStage':{'kind':'source','state':'indiana'},'sourcePlan':{'planId':'exact-plan'},'generationId':'kept','files':_records(self.cp,include_metadata=True),'migration':{'id':'previous'}}
  (self.cp/'checkpoint.json').write_text(json.dumps(self.checkpoint));self.raw=(self.cp/'checkpoint.json').read_bytes()
  mock=patch.object(migration,'FROM_DESCRIPTOR',hashlib.sha256(self.raw).hexdigest());mock.start();self.addCleanup(mock.stop)
 def test_verified_migration_preserves_all_fields_and_disk(self):
  self.assertTrue(migration.eligible(self.cp,self.checkpoint,ROOT));fp=current_fingerprint(ROOT)
  result=migration.migrate(self.cp,self.checkpoint,ROOT,target_fingerprint=fp,verified_fingerprint=fp,now=dt.datetime.now(dt.timezone.utc))
  self.assertEqual({k:v for k,v in result.items() if k not in ('migration','codeFingerprint')},{k:v for k,v in self.checkpoint.items() if k not in ('migration','codeFingerprint')})
  self.assertEqual(result['migration']['previousMigration'],{'id':'previous'});self.assertEqual((self.cp/'checkpoint.json').read_bytes(),self.raw)
 def test_changed_descriptor_or_crop_rejected(self):
  wrong=copy.deepcopy(self.checkpoint);wrong['nextStage']={'kind':'source','state':'other'};self.assertFalse(migration.eligible(self.cp,wrong,ROOT))
  (self.cp/'sources/illinois/crop.part-0000').write_bytes(b'changed');self.assertFalse(migration.eligible(self.cp,self.checkpoint,ROOT))
 def test_unknown_or_unverified_fingerprint_rejected(self):
  fp=current_fingerprint(ROOT)
  for target,verified in [(fp,'0'*64),('a'*64,'a'*64),(migration.FROM_CODE,migration.FROM_CODE)]:
   with self.assertRaises(ValueError):migration.migrate(self.cp,self.checkpoint,ROOT,target_fingerprint=target,verified_fingerprint=verified,now=dt.datetime.now(dt.timezone.utc))
 def test_modified_generator_rejected(self):
  from verified_code import source_records
  records=source_records(ROOT);records['tools/tile_generator.py']='0'*40
  with patch('verified_code.source_records',return_value=records):self.assertFalse(migration.eligible(self.cp,self.checkpoint,ROOT))
if __name__=='__main__':unittest.main()
