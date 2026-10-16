import datetime as dt,pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import continuation_gate as c
NOW=dt.datetime(2026,10,14,tzinfo=dt.timezone.utc)
class GateTests(unittest.TestCase):
 def test_scheduled_mode_uses_event_not_delayed_weekday(self):
  self.assertEqual(c.choose_mode('schedule',c.MONDAY),(True,''));self.assertEqual(c.choose_mode('schedule',c.CONTINUATION),(False,''))
 def test_no_active_never_starts_midweek(self):
  for s in (None,{'active':False}):self.assertFalse(c.ready(s,False,NOW)[0]);self.assertTrue(c.ready(s,True,NOW)[0])
 def test_active_continues_only_when_ready(self):
  s={'active':True,'needsAttention':False,'retryAfter':None};self.assertTrue(c.ready(s,False,NOW)[0]);s['retryAfter']='2026-10-15T00:00:00Z';self.assertFalse(c.ready(s,True,NOW)[0]);s['needsAttention']=True;self.assertFalse(c.ready(s,True,NOW)[0])
 def test_explicit_start_does_not_replace_blocked_active_generation(self):
  allow,region=c.choose_mode('workflow_dispatch','','chicagoland');self.assertTrue(allow);self.assertEqual(region,'chicagoland')
  self.assertFalse(c.ready({'active':True,'needsAttention':True},allow,NOW)[0])
 def test_bad_input_rejected(self):
  for event,schedule,region in [('pull_request','', ''),('schedule','bad',''),('push','','$(oops)')]:
   with self.assertRaises(ValueError):c.choose_mode(event,schedule,region)
  with self.assertRaises(ValueError):c.ready({'active':'yes'},False,NOW)
if __name__=='__main__':unittest.main()
