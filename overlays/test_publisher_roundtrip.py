"""Exercise unmodified snapshot code using its real offline bootstrap fixture."""
import os,shutil,sys,tempfile,unittest
from pathlib import Path
import test_merge_overlay

class PublisherRoundtripTests(unittest.TestCase):
 def test_subsequent_unmodified_publication_preserves_overlay(self):
  root=Path(os.environ['PUBLISHER_ROOT']).resolve()
  sys.path.insert(0,str(root/'publishing'))
  import rollout_snapshot as snapshot
  from verified_code import current_fingerprint
  fingerprint=current_fingerprint(root)
  fixture=test_merge_overlay.MergeTests();fixture.setUp()
  self.addCleanup(fixture.doCleanups)
  site=fixture.root/'real-site'
  snapshot._bootstrap(site)
  (site/"checkpoints/active").mkdir(parents=True)
  (site/"checkpoints/active/checkpoint.json").write_bytes(b'{"fixture":"exact checkpoint bytes"}\n')
  before=snapshot.inventory(site)
  fixture.site=site;fixture.run_merge()
  snapshot.seal_snapshot(site);snapshot.validate_site(site)
  expected=snapshot.inventory(site)
  for path,digest in before.items():self.assertEqual(expected[path],digest)
  def restore_from(source,target):
   def fetch(url,max_bytes):
    prefix=snapshot.BASE+'/'
    self.assertTrue(url.startswith(prefix));data=(source/url[len(prefix):]).read_bytes();self.assertLessEqual(len(data),max_bytes);return data
   snapshot.restore_published(target,fetch=fetch)
  restored=fixture.root/'restored';restore_from(site,restored)
  self.assertEqual(snapshot.inventory(restored),expected)
  # This is the exact existing city publisher's candidate-copy + seal path.
  candidate=fixture.root/'candidate';shutil.copytree(restored,candidate)
  snapshot.seal_snapshot(candidate);snapshot.validate_site(candidate)
  next_restore=fixture.root/'next-restore';restore_from(candidate,next_restore)
  self.assertEqual(snapshot.inventory(next_restore),expected)
  self.assertEqual(current_fingerprint(root),fingerprint)
