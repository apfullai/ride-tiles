"""Build public OSM cycling tiles into a fresh Pages staging directory."""
import argparse, datetime as dt, gzip, hashlib, json, os, re, shutil, subprocess
from pathlib import Path
import tile_generator as generator
from import_mirror import import_chicagoland

KINDS=['trail','protected','lane','shared_path','track','shared_lane']
ATTRIBUTION='© OpenStreetMap contributors'
MAX_SOURCE_BYTES=1_500_000_000
MAX_SITE_BYTES=750_000_000
MAX_TILE_COUNT=100_000

def run(*args):
    subprocess.run(args,check=True)
def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def validate_config(c):
    assert re.fullmatch(r'[a-z0-9-]{1,48}',c['id'])
    w,s,e,n=c['bounds'];assert -180<=w<e<=180 and -85<=s<n<=85
    assert 8<=c['minzoom']<=c['maxzoom']<=14
    assert 0<(e-w)*(n-s)<=6
    assert 1<=len(c['sourceStates'])<=(4 if c['id']=='chicagoland' and c['bounds']==[-89,41.05,-86.65,43.25] else 3)
    assert all(re.fullmatch('[a-z-]+',v) for v in c['sourceStates'])

def download_source(url, destination):
    # Rollout children share a conservative per-run download ledger. No retries
    # there: a failed transfer keeps its entire reserved byte allowance charged.
    ledger_path=os.environ.get('RIDE_DOWNLOAD_LEDGER')
    maximum=MAX_SOURCE_BYTES
    if ledger_path:
        ledger=Path(ledger_path)
        state=json.loads(ledger.read_text()) if ledger.exists() else {'reservedBytes':0}
        spent=state['reservedBytes']
        if type(spent) is not int or not 0<=spent<=3_000_000_000:
            raise RuntimeError('Invalid download budget ledger')
        maximum=min(MAX_SOURCE_BYTES,3_000_000_000-spent)
        if maximum<=1_000_000:raise RuntimeError('Cumulative download budget exhausted')
        ledger.write_text(json.dumps({'reservedBytes':spent+maximum}))
    run('curl','--fail','--location','--proto','=https','--proto-redir','=https','--retry','0' if ledger_path else '2',
        '--connect-timeout','20','--max-time','180' if ledger_path else '600',
        '--max-filesize',str(maximum),url,'-o',str(destination))
    if destination.stat().st_size>maximum:raise RuntimeError('Source exceeded download reservation')
    if ledger_path:
        ledger.write_text(json.dumps({'reservedBytes':spent+destination.stat().st_size}))

def build(c, work, site, base):
    validate_config(c)
    work.mkdir(parents=True,exist_ok=False)
    bbox=','.join(map(str,c['bounds'])); sources=[]; crops=[]
    for state in c['sourceStates']:
        url=f'https://download.geofabrik.de/north-america/us/{state}-latest.osm.pbf'
        src=work/f'{state}.osm.pbf';filtered=work/f'{state}-filtered.osm.pbf';crop=work/f'{state}-cropped.osm.pbf'
        download_source(url,src)
        if not 1_000_000<src.stat().st_size<=MAX_SOURCE_BYTES:raise RuntimeError('Source size outside safety bounds')
        timestamp=subprocess.check_output(['osmium','fileinfo','-g','header.option.osmosis_replication_timestamp',str(src)],text=True).strip()
        if not timestamp:raise RuntimeError('Missing OSM source replication timestamp')
        parsed=dt.datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise RuntimeError('Source replication timestamp lacks timezone')
        sources.append({'url':url,'sha256':sha256(src),'osmReplicationTimestamp':timestamp,'downloadedAt':dt.datetime.now(dt.timezone.utc).isoformat()})
        run('osmium','tags-filter',str(src),'w/highway=cycleway,path,footway,track,pedestrian','w/cycleway','w/cycleway:left','w/cycleway:right','w/cycleway:both','w/bicycle=designated,yes,official,permissive','-o',str(filtered))
        run('osmium','extract','--bbox',bbox,'--strategy','complete_ways',str(filtered),'-o',str(crop))
        src.unlink();filtered.unlink();crops.append(crop)
    merged=work/'region.osm.pbf'
    if len(crops)==1:shutil.copyfile(crops[0],merged)
    else:run('osmium','merge',*[str(p) for p in crops],'-o',str(merged))
    seq=work/'cycling.geojsonseq'
    run('osmium','export','--attributes=id,type','-f','geojsonseq','-x','print_record_separator=false',str(merged),'-o',str(seq))
    generator.BBOX=tuple(c['bounds'])
    generated=work/'generated'
    report=generator.generate(seq,generated,range(c['minzoom'],c['maxzoom']+1))
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    version='staging'
    dest=site/'regions'/c['id']/version;dest.mkdir(parents=True)
    tile_hashes={};tile_bytes=0
    for p in sorted((generated/'tiles').rglob('*.pbf')):
        raw=gzip.decompress(p.read_bytes())
        if not raw or len(raw)>2_000_000:raise RuntimeError('Invalid tile size')
        rel=p.relative_to(generated);out=dest/rel;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(raw)
        tile_bytes+=len(raw);tile_hashes[str(rel)]=hashlib.sha256(raw).hexdigest()
    if not tile_hashes or len(tile_hashes)>MAX_TILE_COUNT or tile_bytes>MAX_SITE_BYTES:raise RuntimeError('Region exceeds pilot publication budget')
    content_hashes={k.removeprefix('tiles/'):v for k,v in tile_hashes.items()}
    version=hashlib.sha256(''.join(f'{k} {content_hashes[k]}\n' for k in sorted(content_hashes)).encode()).hexdigest()[:16]
    final=dest.parent/version;dest.rename(final);dest=final
    # Publish the complete filtered source extract to satisfy ODbL derivative database access.
    # This contains public OSM ways/nodes only; no application code or ride data.
    shutil.copyfile(merged,dest/'cycling-source.osm.pbf')
    provenance={'sources':sources,'generatorCommit':os.environ.get('GITHUB_SHA','local'),'generatorRunId':os.environ.get('GITHUB_RUN_ID'),'bounds':c['bounds'],'categories':KINDS,'generatedAt':stamp,'sourceLicense':'https://opendatacommons.org/licenses/odbl/1-0/','sourceExtractSha256':sha256(dest/'cycling-source.osm.pbf')}
    (dest/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    (dest/'tile-sha256.json').write_text(json.dumps(tile_hashes,sort_keys=True)+'\n')
    (dest/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    prefix=f'{base}/regions/{c["id"]}/{version}'
    entry={k:c[k] for k in ['id','name','bounds','minzoom','maxzoom']}
    entry.update({'sourceLayer':'cycleinfra','tiles':[prefix+'/tiles/{z}/{x}/{y}.pbf'],'version':version,'generatedAt':stamp,'sourceUrl':prefix+'/provenance.json','sourceTimestamp':min(s['osmReplicationTimestamp'] for s in sources),'categories':KINDS,'tileCount':len(tile_hashes),'tileBytes':tile_bytes,'sha256Manifest':prefix+'/tile-sha256.json','sourceDownload':prefix+'/cycling-source.osm.pbf'})
    (dest/'tilejson.json').write_text(json.dumps({'tilejson':'3.0.0','scheme':'xyz','name':c['name'],'bounds':c['bounds'],'minzoom':c['minzoom'],'maxzoom':c['maxzoom'],'tiles':entry['tiles'],'attribution':ATTRIBUTION,'vector_layers':[{'id':'cycleinfra','fields':{'kind':'String','name':'String','surface':'String'}}]},indent=2)+'\n')
    return entry

def main():
    p=argparse.ArgumentParser();p.add_argument('--region-config',default='config/pilot-regions.json');p.add_argument('--output',default='site');p.add_argument('--work',default='build');p.add_argument('--base-url',default='https://apfullai.github.io/ride-tiles');a=p.parse_args()
    site=Path(a.output);work=Path(a.work)
    if site.exists() or work.exists():raise RuntimeError('Use fresh output/work paths; refusing mixed generation')
    assert a.base_url=='https://apfullai.github.io/ride-tiles'
    site.mkdir();work.mkdir()
    regions=[import_chicagoland(site,a.base_url)]
    regions += [build(c,work/c['id'],site,a.base_url) for c in json.loads(Path(a.region_config).read_text())]
    manifest={'schemaVersion':1,'updatedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'attribution':ATTRIBUTION,'license':'https://opendatacommons.org/licenses/odbl/1-0/','regions':regions}
    (site/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (site/'.nojekyll').touch()
    (site/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Ride public cycling tiles</title><h1>Ride public cycling tiles</h1><p>Small public OpenStreetMap cycling-infrastructure extracts for a personal map project. Coverage is limited to the regions in the <a href="manifest.json">manifest</a>.</p><p>© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>. Data is available under the <a href="https://opendatacommons.org/licenses/odbl/1-0/">Open Database License</a>. Each manifest region links to provenance and the public source extract.</p>')
    size=sum(p.stat().st_size for p in site.rglob('*') if p.is_file())
    if size>MAX_SITE_BYTES:raise RuntimeError('Published bytes exceed 750 MB safety budget')
    print(json.dumps({'siteBytes':size,'regions':regions},indent=2))
if __name__=='__main__':main()
