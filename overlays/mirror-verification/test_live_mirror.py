import importlib.util,pathlib,unittest
P=pathlib.Path(__file__).parent
spec=importlib.util.spec_from_file_location('mirror',P/'verify_live_mirror.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Contract(unittest.TestCase):
 def test_primary_exact_origin_path_and_version(self):
  good='https://test.ride.ap0z.com/mellow/xyz/2bafe73bf6586407/11/525/760.pbf';self.assertTrue(m.PRIMARY.fullmatch(good))
  for u in [good.replace('test.ride','ride'),good.replace('https:','http:'),good.replace('2bafe73bf6586407','aaaaaaaaaaaaaaaa'),good.replace('.pbf','.json'),good.replace('test.ride.ap0z.com','test.ride.ap0z.com.evil.invalid')]:self.assertFalse(m.PRIMARY.fullmatch(u))
 def test_mirror_is_actual_exact_github(self):
  u='https://apfullai.github.io/ride-tiles/mellow/xyz/2bafe73bf6586407/11/525/760.pbf';self.assertTrue(m.MIRROR.fullmatch(u));self.assertFalse(m.PRIMARY.fullmatch(u))
 def test_only_primary_response_is_synthetic(self):
  s=(P/'verify_live_mirror.py').read_text();self.assertEqual(s.count('context.route('),1);self.assertIn('context.route(PRIMARY,fail_primary)',s);self.assertEqual(s.count('route.fulfill('),1)
 def test_default_identity_and_no_security_workaround(self):
  s=(P/'verify_live_mirror.py').read_text()
  for forbidden in ['user_agent=','proxy=','ignore_https_errors=','service_workers=','args=','force=True']:self.assertNotIn(forbidden,s)
  self.assertIn("r.status==403 or r.headers.get('cf-mitigated')=='challenge'",s)
 def test_render_version_and_deadline_proof(self):
  s=(P/'verify_live_mirror.py').read_text()
  for required in ["health['frontendAssetVersion']==a.asset_version","m['origin']=='mirror'","sum(m['rendered'].values())>0","last['mapIdleEvents']>0","r['status']==200","signal.alarm(110)"]:self.assertIn(required,s)
 def test_route_guards(self):
  for path in ['/api/route?x=1','/api/route-surface','/trace_attributes','/brouter?x=1']:self.assertTrue(m.FORBIDDEN.search('https://test.ride.ap0z.com'+path))
if __name__=='__main__':unittest.main()
