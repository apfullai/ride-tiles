"""Bounded overlay-only publication preparation; never advances a city."""
import argparse,hashlib,json,os,re,stat,sys,tempfile,urllib.request,zipfile
from pathlib import Path
from merge_overlay import inventory,merge,sha


class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self, req, fp, code, msg, headers, newurl):
  # Reject before constructing or sending any request to the Location target.
  return None


def download_archive(url):
 opener=urllib.request.build_opener(NoRedirect())
 with opener.open(url,timeout=30) as response:
  if response.status!=200 or response.geturl()!=url:
   raise ValueError('Unexpected archive response')
  return response.read(24_000_001)


def reassemble_archive(directory, pin):
 """Consume only committed local parts; no network fallback on any failure."""
 root=Path(directory).absolute()
 if any(p.is_symlink() for p in (root,*root.parents)) or not root.is_dir():
  raise ValueError('Archive directory must be regular without symlink ancestors')
 manifest=root/'manifest.json'
 if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size>16384:
  raise ValueError('Invalid archive parts manifest')
 def unique(pairs):
  out={}
  for k,v in pairs:
   if k in out:raise ValueError('Duplicate manifest key')
   out[k]=v
  return out
 m=json.loads(manifest.read_bytes(),object_pairs_hook=unique)
 if set(m)!={'schemaVersion','bytes','sha256','parts'} or type(m['schemaVersion']) is not int or m['schemaVersion']!=1:
  raise ValueError('Invalid parts manifest schema')
 size=m['bytes']; parts=m['parts']; chunk=524288
 if type(size) is not int or not 0<size<=24000000 or size!=pin['bytes'] or m['sha256']!=pin['sha256']:
  raise ValueError('Archive parts pin mismatch')
 if not isinstance(parts,list) or len(parts)!=(size+chunk-1)//chunk:
  raise ValueError('Invalid part count')
 names={'manifest.json'}; output=bytearray()
 for i,part in enumerate(parts):
  name=f'source.part-{i:03d}'; expected_size=min(chunk,size-i*chunk)
  if not isinstance(part,dict) or set(part)!={'path','bytes','sha256'} or part['path']!=name or type(part['bytes']) is not int or part['bytes']!=expected_size:
   raise ValueError('Invalid part path/order/size')
  if not isinstance(part['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',part['sha256']):
   raise ValueError('Invalid part digest')
  path=root/name
  if path.is_symlink() or not path.is_file() or path.stat().st_size!=expected_size:
   raise ValueError('Part must be regular and exact size')
  with path.open('rb') as stream:data=stream.read(expected_size+1)
  if len(data)!=expected_size or sha(data)!=part['sha256']:
   raise ValueError('Part identity mismatch')
  names.add(name); output.extend(data)
 if {p.name for p in root.iterdir()}!=names:
  raise ValueError('Unexpected archive directory entries')
 data=bytes(output)
 if len(data)!=size or sha(data)!=pin['sha256']:
  raise ValueError('Reassembled archive identity mismatch')
 return data


def extract_zip(archive, destination, expected_hash):
 data=Path(archive).read_bytes()
 if len(data)>24_000_000 or sha(data)!=expected_hash:raise ValueError('Archive identity mismatch')
 destination=Path(destination)
 if destination.exists():raise ValueError('Extraction destination exists')
 with zipfile.ZipFile(archive) as z:
  entries=z.infolist()
  if len(entries)>14500 or sum(e.file_size for e in entries)>24_000_000:raise ValueError('Archive expansion limit')
  seen=set()
  for e in entries:
   p=Path(e.filename)
   if e.is_dir() or p.is_absolute() or '..' in p.parts or '\\' in e.filename or p.as_posix()!=e.filename or e.filename in seen or e.file_size>2_000_000:raise ValueError('Unsafe ZIP entry')
   mode=e.external_attr>>16
   if stat.S_IFMT(mode) not in (0,stat.S_IFREG):raise ValueError('Nonregular ZIP entry')
   seen.add(e.filename)
  destination.mkdir()
  for e in entries:
   p=destination/e.filename;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(e))


def assert_checkpoint_compatible(checkpoint, publisher, fingerprint):
 checkpoint=Path(checkpoint)
 if not checkpoint.exists():return
 parsed=json.loads(checkpoint.read_bytes())
 if parsed.get('codeFingerprint')==fingerprint:return
 from checkpoint_migrations import eligible
 if not eligible(checkpoint.parent,parsed,publisher):
  raise ValueError('Active city fingerprint differs without exact existing compatibility proof')
 # Eligibility is read-only. Never migrate or rewrite this preserved checkpoint.


def prepare(publisher, payload, pin, site, report):
 publisher=Path(publisher).resolve();site=Path(site).resolve()
 sys.path.insert(0,str(publisher/'publishing'))
 from verified_code import current_fingerprint
 from rollout_snapshot import restore_published,seal_snapshot,validate_site,assert_publication_unchanged,inventory as data_inventory
 fingerprint=current_fingerprint(publisher);restored={}
 restore_published(site,receipt=restored)
 if restored.get('bootstrap'):raise ValueError('Require complete current snapshot; no bootstrap fallback')
 before=data_inventory(site)
 checkpoint_files={p:h for p,h in before.items() if p.startswith('checkpoints/')}
 checkpoint=site/'checkpoints/active/checkpoint.json'
 assert_checkpoint_compatible(checkpoint,publisher,fingerprint)
 result=merge(site,payload,pin['manifestSHA256'],pin['version'])
 sealed=seal_snapshot(site);validate_site(site)
 after=data_inventory(site)
 if any(after.get(p)!=h for p,h in before.items()):raise ValueError('Existing site or checkpoint changed')
 if current_fingerprint(publisher)!=fingerprint:raise ValueError('Publisher source changed')
 assert_publication_unchanged(restored['publicationRevision'])
 output={**result,'sourceFingerprint':fingerprint,'restoredPublicationRevision':restored['publicationRevision'],'preservedCheckpointFiles':checkpoint_files,'sealed':sealed}
 Path(report).write_text(json.dumps(output,sort_keys=True,indent=2)+'\n')
 with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('restored_publication_revision='+restored['publicationRevision']+'\n')


def main():
 p=argparse.ArgumentParser();p.add_argument('--archive-parts',required=True);p.add_argument('--publisher',required=True);p.add_argument('--pin',required=True);p.add_argument('--site',required=True);p.add_argument('--report',required=True);a=p.parse_args()
 pin=json.loads(Path(a.pin).read_bytes())
 if not re.fullmatch('[0-9a-f]{16}',pin['version']):raise ValueError('Invalid pinned version')
 expected='https://test.ride.ap0z.com/mellow/xyz/'+pin['version']+'/source.zip'
 if pin['url']!=expected:raise ValueError('Unexpected source URL')
 with tempfile.TemporaryDirectory() as d:
  archive=Path(d)/'source.zip'
  data=reassemble_archive(a.archive_parts,pin)
  if len(data)!=pin['bytes'] or len(data)>24_000_000:raise ValueError('Archive byte count mismatch')
  archive.write_bytes(data);payload=Path(d)/'xyz';extract_zip(archive,payload,pin['sha256'])
  prepare(a.publisher,payload,pin,a.site,a.report)
if __name__=='__main__':main()
