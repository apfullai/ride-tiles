import io,tempfile,unittest,zipfile
from pathlib import Path
from publish_overlay import extract_zip
from merge_overlay import sha
class ArchiveTests(unittest.TestCase):
 def archive(self,d,entries):
  p=Path(d)/'a.zip'
  with zipfile.ZipFile(p,'w') as z:
   for name,data in entries:z.writestr(name,data)
  return p
 def test_roundtrip(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.archive(d,[('8/1/2.pbf',b'x')]);out=Path(d)/'out';extract_zip(p,out,sha(p.read_bytes()));self.assertEqual((out/'8/1/2.pbf').read_bytes(),b'x')
 def test_traversal(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.archive(d,[('../escape',b'x')]);out=Path(d)/'out'
   with self.assertRaises(ValueError):extract_zip(p,out,sha(p.read_bytes()))
   self.assertFalse(out.exists())
 def test_bad_pin(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.archive(d,[('x',b'x')])
   with self.assertRaises(ValueError):extract_zip(p,Path(d)/'out','0'*64)
