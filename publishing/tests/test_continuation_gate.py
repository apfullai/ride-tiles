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
 def test_extra_slots_never_select_new_city(self):
  for slot in (c.EVENING,c.ACQUISITION):self.assertEqual(c.choose_mode('schedule',slot),(False,''))
 def test_temporary_slot_requires_exact_unfinished_generation(self):
  import copy
  now=dt.datetime(2026,10,18,10,23,tzinfo=dt.timezone.utc)
  status={'active':True,'needsAttention':False,'retryAfter':None,
   'generationId':c.ACQUISITION_GENERATION,'stage':'source','sourceDate':'2026-10-13',
   'sourceAcquisitionExpiresAt':'2026-10-20T00:00:00+00:00',
   'completedCounts':{'sources':1,'sourceTotal':4}}
  self.assertTrue(c.acquisition_ready(status,now)[0])
  for key,value in [('active',False),('generationId','other'),('stage','index'),
                    ('sourceDate','2026-10-14'),('needsAttention',True),
                    ('sourceAcquisitionExpiresAt','2026-10-21T00:00:00+00:00'),
                    ('completedCounts',{'sources':4,'sourceTotal':4}),
                    ('completedCounts',{'sources':True,'sourceTotal':4})]:
   changed=copy.deepcopy(status);changed[key]=value
   with self.subTest(key=key,value=value):self.assertFalse(c.acquisition_ready(changed,now)[0])
  for date in (c.ACQUISITION_START-dt.timedelta(seconds=1),c.ACQUISITION_END,now.replace(year=2027)):
   self.assertFalse(c.acquisition_ready(status,date)[0])
 def test_temporary_later_year_skips_even_metadata_fetch(self):
  import os,io
  from unittest.mock import patch
  class Later(dt.datetime):
   @classmethod
   def now(cls,tz=None):return cls(2027,10,18,10,23,tzinfo=dt.timezone.utc)
  with patch.dict(os.environ,{'GITHUB_EVENT_NAME':'schedule','EVENT_SCHEDULE':c.ACQUISITION},clear=True), patch.object(c.dt,'datetime',Later), patch.object(c,'fetch_status') as fetch, patch('sys.stdout',new_callable=io.StringIO) as out:
   c.main();fetch.assert_not_called();self.assertIn('temporary acquisition window closed',out.getvalue())
if __name__=='__main__':unittest.main()
