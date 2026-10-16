import pathlib,unittest
ROOT=pathlib.Path(__file__).resolve().parents[2]
class WorkflowTests(unittest.TestCase):
 def test_bounded_daily_continuation_and_permissions(self):
  s=(ROOT/'.github/workflows/rollout.yml').read_text()
  for value in ('timeout-minutes: 15','timeout-minutes: 2','timeout-minutes: 5','retention-days: 1','--continue-only','assert_publication_unchanged','persist-credentials: false'):self.assertIn(value,s)
  self.assertNotIn("  schedule:",s)
  self.assertIn('seed_migration:',s)
  for value in ('actions: write','contents: write','workflow_dispatches','gh workflow run','workflow_run:'):self.assertNotIn(value,s)
 def test_request_only_metadata_does_not_repeat_full_ci(self):
  s=(ROOT/'.github/workflows/snapshot-tests.yml').read_text()
  self.assertEqual(s.count("'!config/rollout-request.json'"),2)
  for value in ("'publishing/**'","'tools/**'","'tests/**'","'config/**'","'.github/workflows/**'"):self.assertIn(value,s)
 def test_gate_before_budget_and_no_duplicate_fullsuite(self):
  s=(ROOT/'.github/workflows/rollout.yml').read_text()
  self.assertLess(s.index('publishing/continuation_gate.py'),s.index('publishing/actions_budget.py'))
  self.assertIn("if: steps.ready.outputs.eligible == 'true'",s)
  self.assertNotIn('unittest discover',s)
  self.assertIn('540s python publishing/resumable_rollout.py',s)
if __name__=='__main__':unittest.main()
