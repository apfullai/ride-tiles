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


class RedirectTests(unittest.TestCase):
 def test_redirect_destination_never_requested(self):
  import email.message,urllib.error,urllib.request,urllib.response
  from unittest.mock import patch
  from publish_overlay import download_archive
  source='https://test.ride.ap0z.com/mellow/xyz/2bafe73bf6586407/source.zip'
  destination='https://redirect.invalid/should-never-be-requested'
  for status in (301,302,303,307,308):
   with self.subTest(status=status):
    calls=[]
    def transport(handler,request):
     calls.append(request.full_url)
     headers=email.message.Message();headers['Location']=destination
     response=urllib.response.addinfourl(io.BytesIO(b''),headers,request.full_url,status)
     response.msg='Redirect';return response
    with patch.object(urllib.request.HTTPSHandler,'https_open',transport):
     with self.assertRaises(urllib.error.HTTPError) as error:download_archive(source)
    self.assertEqual(error.exception.code,status)
    self.assertEqual(calls,[source])
 def test_direct_success_still_reads_exact_bytes(self):
  import email.message,urllib.request,urllib.response
  from unittest.mock import patch
  from publish_overlay import download_archive
  source='https://test.ride.ap0z.com/mellow/xyz/2bafe73bf6586407/source.zip'
  def transport(handler,request):
   response=urllib.response.addinfourl(io.BytesIO(b'exact archive'),email.message.Message(),request.full_url,200)
   response.msg='OK';return response
  with patch.object(urllib.request.HTTPSHandler,'https_open',transport):
   self.assertEqual(download_archive(source),b'exact archive')
