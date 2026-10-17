"""Read-only exact-source deployment admission. Never downloads the artifact."""
import datetime,hashlib,json,os,re,sys,urllib.request
from pathlib import Path
REPO='apfullai/ride-tiles';MAIN='ae037d0d4e37e82314a679006ebe4d86bc0497f5';RUN=39022469343;HEAD='4711212d901d10cf0ff1c100625798729c9748e5';ARTIFACT=12096579037
DIGEST='sha256:7c6616506c78a1f5226243bbff1c66b73aa9df844ea43fbef18876ecb11af558';SIZE=201912458;EXPIRY='2026-10-18T09:49:24Z'
LIVE={'manifest.json':'2c5e020ac70978a8500ce8f4d0fe3489aad5558162b20624e3e04bd333510a55','snapshot.json':'95b5d4f5f762716de8a8b46d133c521b1388b9f7a1ed12260710580e8ad0bac9','checkpoints/active/checkpoint.json':'d0c43cfa46fc427311f2fae9fd97936ea1d4bed19e0250f84b245216bf49db96'}
STEPS={'Test overlay integrity and unchanged publisher roundtrip','Restore complete current site and add only immutable overlay','Verify actual artifact size remains within free budget','Check actual uploaded artifact and fresh total before deployment'}
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None

def request(url):
 base=f'https://api.github.com/repos/{REPO}'
 allowed={base+'/git/ref/heads/main',base+f'/actions/runs/{RUN}',base+f'/actions/runs/{RUN}/attempts/1/jobs?per_page=100'}
 api=url in allowed or re.fullmatch(re.escape(base)+r'/actions/artifacts\?per_page=100&page=[1-9][0-9]*',url)
 if not api and url not in {'https://apfullai.github.io/ride-tiles/'+p for p in LIVE}:raise ValueError('Unexpected destination')
 headers={'User-Agent':'Ride-Verified-Continuation/1','Cache-Control':'no-cache'}
 if api:headers.update({'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json'})
 with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url,headers=headers),timeout=10) as r:
  if r.status!=200 or r.geturl()!=url:raise ValueError('Unexpected read response')
  data=r.read(4*1024*1024+1)
 if len(data)>4*1024*1024:raise ValueError('Oversized response')
 return json.loads(data) if api else data

def validate(run,jobs,artifact,main,live,now):
 if main.get('object',{}).get('sha')!=MAIN:raise ValueError('Main changed')
 if run.get('id')!=RUN or run.get('head_sha')!=HEAD or run.get('head_branch')!='feat/mellow-z17-mirror' or run.get('run_attempt')!=1 or run.get('status')!='completed' or run.get('conclusion')!='failure' or run.get('repository',{}).get('full_name')!=REPO or run.get('path')!='.github/workflows/rollout.yml':raise ValueError('Source run mismatch')
 if jobs.get('total_count')!=len(jobs.get('jobs',[])):raise ValueError('Incomplete job history')
 failed=[j for j in jobs['jobs'] if j.get('name')=='deploy']
 if len(failed)!=1 or failed[0].get('conclusion')!='failure' or failed[0].get('steps'):raise ValueError('Source deployment is not a pre-run failure')
 for name in ('budget','build'):
  matches=[j for j in jobs['jobs'] if j.get('name')==name]
  if len(matches)!=1 or matches[0].get('run_id')!=RUN or matches[0].get('conclusion')!='success' or matches[0].get('status')!='completed':raise ValueError('Source gates not successful')
  if name=='build':
   for step in STEPS:
    found=[s for s in matches[0].get('steps',[]) if s.get('name')==step]
    if len(found)!=1 or found[0].get('conclusion')!='success':raise ValueError('Source build step unproven')
 if artifact.get('id')!=ARTIFACT or artifact.get('name')!='github-pages' or artifact.get('expired') is not False or artifact.get('size_in_bytes')!=SIZE or artifact.get('digest')!=DIGEST or artifact.get('expires_at')!=EXPIRY:raise ValueError('Artifact identity mismatch')
 w=artifact.get('workflow_run',{})
 if w.get('id')!=RUN or w.get('head_sha')!=HEAD or w.get('head_branch')!='feat/mellow-z17-mirror' or w.get('repository_id')!=1420913307 or w.get('head_repository_id')!=1420913307:raise ValueError('Artifact source mismatch')
 if datetime.datetime.fromisoformat(EXPIRY.replace('Z','+00:00'))-now<datetime.timedelta(minutes=10):raise ValueError('Artifact expiry too close')
 if live!=LIVE:raise ValueError('Live baseline changed')

def main():
 sys.path.insert(0,str(Path('publisher/publishing')))
 from actions_budget import _pages
 from exact_artifact import audit
 base=f'https://api.github.com/repos/{REPO}'
 main=request(base+'/git/ref/heads/main');run=request(base+f'/actions/runs/{RUN}');jobs=request(base+f'/actions/runs/{RUN}/attempts/1/jobs?per_page=100')
 artifacts=_pages(request,base+'/actions/artifacts','artifacts');found=[a for a in artifacts if a.get('id')==ARTIFACT]
 if len(found)!=1:raise ValueError('Artifact missing or duplicated')
 capacity=audit(REPO,{'candidateBytes':0},fetch=request)
 if not capacity['allowed'] or capacity['artifactBytes']<SIZE:raise ValueError('Capacity audit failed')
 live={p:hashlib.sha256(request('https://apfullai.github.io/ride-tiles/'+p)).hexdigest() for p in LIVE}
 now=datetime.datetime.now(datetime.timezone.utc);validate(run,jobs,found[0],main,live,now)
 receipt={'artifactId':ARTIFACT,'sourceRun':RUN,'sourceHead':HEAD,'main':MAIN,'digest':DIGEST,'bytes':SIZE,'liveHashes':live,'checkedAt':now.isoformat(),'capacity':capacity}
 Path('continuation-receipt.json').write_text(json.dumps(receipt,sort_keys=True)+'\n');print(json.dumps(receipt,sort_keys=True))
if __name__=='__main__':
 try:main()
 except Exception as e:raise SystemExit('Read-only continuation guard failed: '+type(e).__name__)
