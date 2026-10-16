"""Append an exact metro subset to a verified existing public Pages snapshot."""
import datetime, gzip, hashlib, io, json, math, re, shutil, sys, tarfile, tempfile
from pathlib import Path
BOUNDS=[-88.65,41.35,-86.8,42.65]
VERSION='8d555e92cfa68b53'
BASE='https://apfullai.github.io/ride-tiles'
def tile_bounds(z,x,y):
 n=2**z
 lat=lambda v:math.degrees(math.atan(math.sinh(math.pi*(1-2*v/n))))
 return x/n*360-180,lat(y+1),(x+1)/n*360-180,lat(y)
def build_metro(site):
 manifest=json.loads((site/'manifest.json').read_text())

 old={str(p.relative_to(site)):hashlib.sha256(p.read_bytes()).hexdigest() for p in site.rglob('*') if p.is_file()}
 src=site/'regions/chicagoland/19785f3c1ca31233';dest=site/'regions/chicago-metro'/VERSION;hashes={};size=0
 for p in sorted((src/'tiles').rglob('*.pbf')):
  key=str(p.relative_to(src/'tiles'));z,x,y=map(int,key[:-4].split('/'));w,s,e,n=tile_bounds(z,x,y)
  if e<BOUNDS[0] or w>BOUNDS[2] or n<BOUNDS[1] or s>BOUNDS[3]:continue
  data=p.read_bytes();hashes[key]=hashlib.sha256(data).hexdigest();size+=len(data)
  out=dest/'tiles'/key;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(data)
 version=hashlib.sha256(''.join(f'{k} {hashes[k]}\n' for k in sorted(hashes)).encode()).hexdigest()[:16]
 if version!=VERSION or len(hashes)!=3868:raise ValueError('Frozen metro content differs')
 archive=dest/'cycling-tile-database.tar.gz'
 metadata=json.loads((Path(__file__).parent/'metro-source-metadata.json').read_text())
 with archive.open('wb') as raw:
  with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0,compresslevel=9) as gz:
   with tarfile.open(fileobj=gz,mode='w') as tar:
    entries={**{k:v.encode() for k,v in metadata.items()},**{'tiles/'+k:(dest/'tiles'/k).read_bytes() for k in hashes}}
    for name,data in sorted(entries.items()):
     info=tarfile.TarInfo(name);info.size=len(data);info.mtime=0;info.mode=0o644;tar.addfile(info,io.BytesIO(data))
 prefix=f'{BASE}/regions/chicago-metro/{VERSION}'
 provenance=json.loads((src/'provenance.json').read_text());provenance.update(contentVersion=VERSION,kind='exact-native-tile-subset',bounds=BOUNDS,parentContentVersion='19785f3c1ca31233',retainsBoundaryTileGeometry=True)
 (dest/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n');(dest/'tile-sha256.json').write_text(json.dumps(hashes,sort_keys=True)+'\n')
 region=dict(next(r for r in manifest['regions'] if r['id']=='chicagoland'));region.update(id='chicago-metro',name='Chicago metro',bounds=BOUNDS,version=VERSION,tiles=[prefix+'/tiles/{z}/{x}/{y}.pbf'],tileCount=len(hashes),tileBytes=size,sourceUrl=prefix+'/provenance.json',sha256Manifest=prefix+'/tile-sha256.json',mirrorOf='chicago-metro')
 region['sourceDownload']=prefix+'/cycling-tile-database.tar.gz'
 region['sourceArchiveSha256']=hashlib.sha256(archive.read_bytes()).hexdigest()
 manifest['regions'].insert(0,region);manifest['updatedAt']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 (site/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
 if not all(hashlib.sha256((site/k).read_bytes()).hexdigest()==v for k,v in old.items() if k!='manifest.json'):
  raise ValueError('Existing snapshot files changed')
 print(json.dumps({'metroVersion':version,'tileCount':len(hashes),'tileBytes':size,'preservedFiles':len(old)-1}))

def file_hashes(root):
 return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}

def validate(site, manifest):
 if manifest.get('schemaVersion') != 1 or not isinstance(manifest.get('regions'),list):
  raise ValueError('Invalid manifest')
 if any(p.is_symlink() for p in site.rglob('*')):
  raise ValueError('Snapshot must not contain symlinks')
 ids=set()
 for region in manifest['regions']:
  rid=region.get('id','');version=region.get('version','')
  if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',rid) or not re.fullmatch(r'[0-9a-f]{16}',version) or rid in ids:
   raise ValueError('Invalid or duplicate region identity')
  ids.add(rid)
  bounds=region.get('bounds',[])
  if (len(bounds)!=4 or any(not isinstance(v,(float,int)) or isinstance(v,bool) or not math.isfinite(v) for v in bounds)
      or not -180<=bounds[0]<bounds[2]<=180 or not -90<=bounds[1]<bounds[3]<=90):
   raise ValueError('Invalid region bounds')
  root=site/'regions'/rid/version
  expected=json.loads((root/'tile-sha256.json').read_text())
  normalized={}
  for key,value in expected.items():
   key=key.removeprefix('tiles/')
   match=re.fullmatch(r'(\d+)/(\d+)/(\d+)\.pbf',key)
   if not match or key in normalized or not re.fullmatch(r'[0-9a-f]{64}',value):
    raise ValueError('Invalid tile hash entry')
   z,x,y=map(int,match.groups())
   if not 0<=z<=24 or not 0<=x<2**z or not 0<=y<2**z:
    raise ValueError('Invalid tile coordinate')
   normalized[key]=value
  actual=file_hashes(root/'tiles')
  version_hash=hashlib.sha256(''.join(f'{k} {actual[k]}\n' for k in sorted(actual)).encode()).hexdigest()[:16]
  if (not actual or actual!=normalized or len(actual)!=region.get('tileCount') or version_hash!=version
      or sum(p.stat().st_size for p in (root/'tiles').rglob('*') if p.is_file())!=region.get('tileBytes')):
   raise ValueError('Region content validation failed')
 chicago=next((r for r in manifest['regions'] if r['id']=='chicagoland'),None)
 if not chicago or chicago['version']!='19785f3c1ca31233':
  raise ValueError('Frozen Chicago parent is missing')

def main(site):
 site=Path(site)
 manifest=json.loads((site/'manifest.json').read_text())
 validate(site,manifest)
 existing=next((r for r in manifest['regions'] if r['id']=='chicago-metro'),None)
 # Generate and validate in isolation. Failed validation never mutates input.
 with tempfile.TemporaryDirectory(dir=site.parent) as temp:
  staging=Path(temp)/'site';shutil.copytree(site,staging)
  candidate=dict(manifest);candidate['regions']=[r for r in manifest['regions'] if r['id']!='chicago-metro']
  (staging/'manifest.json').write_text(json.dumps(candidate))
  metro=Path('regions/chicago-metro')/VERSION
  if (staging/metro).exists():shutil.rmtree(staging/metro)
  build_metro(staging)
  result=json.loads((staging/'manifest.json').read_text());validate(staging,result)
  generated=result['regions'][0]
  if existing:
   if existing!=generated or file_hashes(site/metro)!=file_hashes(staging/metro):
    raise ValueError('Existing metro conflicts with frozen exact mirror')
   return  # Byte-for-byte no-op, including manifest timestamp and ordering.
  if (site/metro).exists():
   raise ValueError('Unlisted metro destination already exists')
  (site/metro).parent.mkdir(parents=True,exist_ok=True)
  shutil.move(str(staging/metro),str(site/metro))
  (staging/'manifest.json').replace(site/'manifest.json')

if __name__=='__main__':main(Path(sys.argv[1]))
