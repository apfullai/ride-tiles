"""Bounded overlay-only publication preparation; never advances a city."""
import argparse,hashlib,json,os,re,stat,sys,tempfile,urllib.request,zipfile
from pathlib import Path
from merge_overlay import inventory,merge,sha


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
 if checkpoint.exists() and json.loads(checkpoint.read_bytes()).get('codeFingerprint')!=fingerprint:
  raise ValueError('Active city fingerprint differs from exact pinned publisher')
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
 p=argparse.ArgumentParser();p.add_argument('--publisher',required=True);p.add_argument('--pin',required=True);p.add_argument('--site',required=True);p.add_argument('--report',required=True);a=p.parse_args()
 pin=json.loads(Path(a.pin).read_bytes())
 if not re.fullmatch('[0-9a-f]{16}',pin['version']):raise ValueError('Invalid pinned version')
 expected='https://test.ride.ap0z.com/mellow/xyz/'+pin['version']+'/source.zip'
 if pin['url']!=expected:raise ValueError('Unexpected source URL')
 with tempfile.TemporaryDirectory() as d:
  archive=Path(d)/'source.zip'
  with urllib.request.urlopen(pin['url'],timeout=30) as r:
   if r.geturl()!=pin['url']:raise ValueError('Unexpected source redirect')
   data=r.read(24_000_001)
  if len(data)!=pin['bytes'] or len(data)>24_000_000:raise ValueError('Archive byte count mismatch')
  archive.write_bytes(data);payload=Path(d)/'xyz';extract_zip(archive,payload,pin['sha256'])
  prepare(a.publisher,payload,pin,a.site,a.report)
if __name__=='__main__':main()
