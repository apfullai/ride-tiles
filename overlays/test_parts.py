import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import publish_overlay as p

class Parts(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name)/'parts';self.root.mkdir()
  self.data=b'a'*524288+b'end';self.pin={'bytes':len(self.data),'sha256':p.sha(self.data)}
  self.man={'schemaVersion':1,**self.pin,'parts':[]}
  for i,b in enumerate((self.data[:524288],self.data[524288:])):
   name=f'source.part-{i:03d}';(self.root/name).write_bytes(b)
   self.man['parts'].append({'path':name,'bytes':len(b),'sha256':p.sha(b)})
  self.write()
 def write(self): (self.root/'manifest.json').write_text(json.dumps(self.man))
 def reject(self):
  with patch.object(p,'download_archive',side_effect=AssertionError('No network fallback')):
   with self.assertRaises((ValueError,FileNotFoundError,KeyError,TypeError)):
    p.reassemble_archive(self.root,self.pin)
 def test_exact(self):
  with patch.object(p,'download_archive',side_effect=AssertionError('No network')):
   self.assertEqual(p.reassemble_archive(self.root,self.pin),self.data)
 def test_bad_manifest_fields(self):
  original=copy.deepcopy(self.man)
  for key,value in [('schemaVersion',2),('bytes',True),('sha256','0'*64),('parts',[]),('parts',list(reversed(original['parts'])))]:
   with self.subTest(key=key,value=value):
    self.man=copy.deepcopy(original);self.man[key]=value;self.write();self.reject()
 def test_part_guards(self):
  original=copy.deepcopy(self.man)
  for key,value in [('path','../source.part-000'),('path','/tmp/part'),('bytes',True),('bytes',524289),('sha256','0'*64)]:
   with self.subTest(key=key,value=value):
    self.man=copy.deepcopy(original);self.man['parts'][0][key]=value;self.write();self.reject()
 def test_missing(self):
  (self.root/'source.part-001').unlink();self.reject()
 def test_corrupt(self):
  (self.root/'source.part-001').write_bytes(b'bad');self.reject()
 def test_extra(self):
  (self.root/'extra').mkdir();self.reject()
 def test_symlink_part(self):
  f=self.root/'source.part-001';f.unlink();f.symlink_to(self.root/'source.part-000');self.reject()
 def test_symlink_manifest(self):
  f=self.root/'manifest.json';other=self.root.parent/'outside';f.rename(other);f.symlink_to(other);self.reject()
 def test_symlink_root_and_ancestor(self):
  link=self.root.parent/'link';link.symlink_to(self.root,target_is_directory=True)
  with self.assertRaises(ValueError):p.reassemble_archive(link,self.pin)
  alias=self.root.parent/'alias';alias.symlink_to(self.root.parent,target_is_directory=True)
  with self.assertRaises(ValueError):p.reassemble_archive(alias/'parts',self.pin)
 def test_duplicate_json_keys(self):
  (self.root/'manifest.json').write_text('{"schemaVersion":1,"schemaVersion":1}')
  self.reject()
 def test_full_archive_pin(self):
  self.man['sha256']=self.pin['sha256']='0'*64;self.write();self.reject()
 def test_main_never_fetches_source_url(self):
  pin={**self.pin,'version':'2bafe73bf6586407','url':'https://test.ride.ap0z.com/mellow/xyz/2bafe73bf6586407/source.zip'}
  pinfile=self.root.parent/'pin.json';pinfile.write_text(json.dumps(pin))
  args=['publish_overlay.py','--archive-parts',str(self.root),'--publisher','publisher','--pin',str(pinfile),'--site','site','--report','report']
  with patch.object(p.sys,'argv',args), patch.object(p,'download_archive',side_effect=AssertionError('No network')), patch.object(p,'extract_zip') as extract, patch.object(p,'prepare') as prepare:
   p.main();extract.assert_called_once();prepare.assert_called_once()
 def test_main_requires_local_parts(self):
  args=['publish_overlay.py','--publisher','publisher','--pin','pin','--site','site','--report','report']
  with patch.object(p.sys,'argv',args),patch.object(p,'download_archive',side_effect=AssertionError('No network')):
   with self.assertRaises(SystemExit):p.main()
 def test_actual_local_payload(self):
  root=Path(__file__).parent/'archive'
  pin=json.loads((Path(__file__).parent/'source-pin.json').read_bytes())
  data=p.reassemble_archive(root,pin)
  self.assertEqual(len(data),13885264);self.assertEqual(p.sha(data),pin['sha256'])
if __name__=='__main__':unittest.main()
