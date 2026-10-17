import json,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from publish_overlay import assert_checkpoint_compatible
class CheckpointGuardTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name);self.checkpoint=self.root/'checkpoint.json';self.checkpoint.write_text('{"codeFingerprint":"old","retained":"exact bytes"}\n');self.original=self.checkpoint.read_bytes()
 def test_matching_fingerprint_never_calls_migration(self):
  fake=types.SimpleNamespace(eligible=Mock(side_effect=AssertionError('Not needed')),migrate=Mock(side_effect=AssertionError('Never migrate')))
  with patch.dict('sys.modules',{'checkpoint_migrations':fake}):assert_checkpoint_compatible(self.checkpoint,self.root,'old')
  fake.eligible.assert_not_called();fake.migrate.assert_not_called();self.assertEqual(self.checkpoint.read_bytes(),self.original)
 def test_only_exact_eligible_mismatch_accepted_without_write(self):
  fake=types.SimpleNamespace(eligible=Mock(return_value=True),migrate=Mock(side_effect=AssertionError('Never migrate')))
  with patch.dict('sys.modules',{'checkpoint_migrations':fake}):assert_checkpoint_compatible(self.checkpoint,self.root,'new')
  fake.eligible.assert_called_once_with(self.root,json.loads(self.original),self.root);fake.migrate.assert_not_called();self.assertEqual(self.checkpoint.read_bytes(),self.original)
 def test_unknown_mismatch_fails_without_write(self):
  fake=types.SimpleNamespace(eligible=Mock(return_value=False),migrate=Mock(side_effect=AssertionError('Never migrate')))
  with patch.dict('sys.modules',{'checkpoint_migrations':fake}):
   with self.assertRaises(ValueError):assert_checkpoint_compatible(self.checkpoint,self.root,'new')
  fake.migrate.assert_not_called();self.assertEqual(self.checkpoint.read_bytes(),self.original)
