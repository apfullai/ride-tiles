import pathlib,unittest
ROOT=pathlib.Path(__file__).resolve().parents[2]
class WorkflowTests(unittest.TestCase):
 def test_bounded_daily_continuation_and_permissions(self):
  s=(ROOT/'.github/workflows/rollout.yml').read_text()
  for value in ('timeout-minutes: 24','timeout-minutes: 2','timeout-minutes: 5','retention-days: 1','--continue-only','--acquisition-only','assert_publication_unchanged','persist-credentials: false'):self.assertIn(value,s)
  for cron in ('23 2 * * 1','23 2 * * 0,2-6','23 18 * * *','23 10 17-19 10 *'):
   self.assertIn("cron: '"+cron+"'",s)
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
  self.assertIn('process_deadline()-time.monotonic()',s)
  self.assertIn('start+1380',s)
  self.assertIn('55s python publishing/exact_artifact.py',s)
  self.assertIn('45s python publishing/verify_uploaded_artifact.py',s)
  self.assertLess(s.index('Establish bounded build deadline'),s.index('Install official tools'))
if __name__=='__main__':unittest.main()
