import json
from pathlib import Path
import tempfile
import unittest
from merge_overlay import merge, inventory, sha

class MergeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.site=self.root/'site';self.site.mkdir()
  (self.site/'checkpoint.json').write_bytes(b'{"unchanged":"exact"}\n')
  (self.site/'manifest.json').write_bytes(b'city manifest exact')
  self.overlay=self.root/'overlay';self.overlay.mkdir();(self.overlay/'8/1').mkdir(parents=True)
  (self.overlay/'8/1/2.pbf').write_bytes(b'tile');(self.overlay/'index.json').write_bytes(b'{}');(self.overlay/'empty.pbf').write_bytes(b'')
  self.version='2bafe73bf6586407'
  self.m={'schemaVersion':1,'version':self.version,'tiles':1,'indexSHA256':sha(b'{}'),'indexBytes':2,'files':{'8/1/2.pbf':{'sha256':sha(b'tile'),'bytes':4}}}
  self.pin=self.write_manifest()
 def write_manifest(self):
  data=json.dumps(self.m).encode();(self.overlay/'manifest.json').write_bytes(data);return sha(data)
 def run_merge(self):return merge(self.site,self.overlay,self.pin,self.version)
 def test_preservation_and_idempotency(self):
  before=inventory(self.site);self.run_merge();after=inventory(self.site);self.run_merge();self.assertEqual(after,inventory(self.site))
  for p,h in before.items():self.assertEqual(after[p],h)
 def test_corrupt_tile_fails_before_mutation(self):
  before=inventory(self.site);(self.overlay/'8/1/2.pbf').write_bytes(b'evil')
  with self.assertRaises(ValueError):self.run_merge()
  self.assertEqual(before,inventory(self.site))
 def test_manifest_pin_failure(self):
  (self.overlay/'manifest.json').write_text('{}')
  with self.assertRaises(ValueError):self.run_merge()
 def test_unlisted_file(self):
  (self.overlay/'extra').write_text('bad')
  with self.assertRaises(ValueError):self.run_merge()
 def test_symlink_rejected(self):
  (self.overlay/'link').symlink_to(self.site/'checkpoint.json')
  with self.assertRaises(ValueError):self.run_merge()
 def test_immutable_collision(self):
  self.run_merge();p=self.site/f'mellow/xyz/{self.version}/8/1/2.pbf';p.write_bytes(b'bad')
  with self.assertRaises(ValueError):self.run_merge()
 def test_path_escape(self):
  self.m['files']={'../outside':{'sha256':sha(b'tile'),'bytes':4}};self.pin=self.write_manifest()
  with self.assertRaises(ValueError):self.run_merge()
if __name__=='__main__':unittest.main()
