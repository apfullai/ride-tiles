"""Import an exact public tile snapshot without accessing any private repository."""
import gzip,hashlib,io,json,re,tarfile
from pathlib import Path

def import_chicagoland(site,base,bootstrap=None):
    root=Path(bootstrap) if bootstrap is not None else Path(__file__).resolve().parents[1]/'bootstrap'
    conf=json.loads((root/'chicagoland.json').read_text())
    packed=b''.join(p.read_bytes() for p in sorted(root.glob('chicagoland.tar.gz.part*')))
    if hashlib.sha256(packed).hexdigest()!=conf['archiveSha256']:raise RuntimeError('Mirror archive digest mismatch')
    dest=site/'regions'/'chicagoland'/conf['expectedContentVersion'];dest.mkdir(parents=True)
    hashes={};total=0;metadata=None;report=None
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz') as tar:
      for member in tar:
        if not member.isfile():continue
        data=tar.extractfile(member).read()
        if member.name=='source-metadata.json':metadata=json.loads(data);continue
        if member.name=='report.json':report=json.loads(data);continue
        if member.name in ('source-sha256.txt','tilejson.json'):continue
        m=re.fullmatch(r'tiles/(\d+)/(\d+)/(\d+)\.pbf',member.name)
        if not m:raise RuntimeError('Unexpected mirror archive member')
        z,x,y=map(int,m.groups())
        if not(8<=z<=14 and 0<=x<2**z and 0<=y<2**z):raise RuntimeError('Invalid tile coordinate')
        raw=gzip.decompress(data) if data.startswith(b'\x1f\x8b') else data
        if not raw or len(raw)>2_000_000:raise RuntimeError('Invalid raw tile')
        key=f'{z}/{x}/{y}.pbf'
        if key in hashes:raise RuntimeError('Duplicate tile')
        hashes[key]=hashlib.sha256(raw).hexdigest();total+=len(raw)
        out=dest/'tiles'/key;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(raw)
    version=hashlib.sha256(''.join(f'{k} {hashes[k]}\n' for k in sorted(hashes)).encode()).hexdigest()[:16]
    if version!=conf['expectedContentVersion'] or len(hashes)!=conf['expectedTileCount']:raise RuntimeError('Mirror is not identical to expected regional snapshot')
    if not metadata or not report:raise RuntimeError('Mirror provenance missing')
    prefix=f'{base}/regions/chicagoland/{version}'
    (dest/'cycling-tile-database.tar.gz').write_bytes(packed)
    (dest/'tile-sha256.json').write_text(json.dumps(hashes,sort_keys=True)+'\n')
    (dest/'provenance.json').write_text(json.dumps({'kind':'identical-regional-mirror','contentVersion':version,'archiveSha256':conf['archiveSha256'],'upstreamArchiveSha256':conf['upstreamArchiveSha256'],'sourceMetadata':metadata,'license':'https://opendatacommons.org/licenses/odbl/1-0/'},indent=2)+'\n')
    (dest/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return {'id':'chicagoland','name':'Chicagoland','bounds':conf['bounds'],'minzoom':8,'maxzoom':14,'sourceLayer':'cycleinfra','tiles':[prefix+'/tiles/{z}/{x}/{y}.pbf'],'version':version,'generatedAt':metadata['generated_utc'],'sourceUrl':prefix+'/provenance.json','sourceTimestamp':min(s['osm_replication_timestamp'] for s in metadata['sources']),'categories':['trail','protected','lane','shared_path','track','shared_lane'],'tileCount':len(hashes),'tileBytes':total,'sha256Manifest':prefix+'/tile-sha256.json','sourceDownload':prefix+'/cycling-tile-database.tar.gz','mirrorOf':'chicagoland'}
