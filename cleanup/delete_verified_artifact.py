"""One approved artifact deletion. No wildcard/list deletion or retry."""
import hashlib,json,os,urllib.error,urllib.request
REPO='apfullai/ride-tiles'
API='https://api.github.com/repos/'+REPO
ARTIFACT=12092658917
RUN=39012173471
SHA='c9dd4771e5670b2ab4fa9df1d1eb8b5e7122d5fa'
SIZE=133807974
DIGEST='sha256:d4bd6b2a45c125c6ad2956202c0de0a6b1be6ad11004064e3e0bab1fea735d7c'
BASE='https://apfullai.github.io/ride-tiles/'
LIVE={'manifest.json':'2c5e020ac70978a8500ce8f4d0fe3489aad5558162b20624e3e04bd333510a55','snapshot.json':'95b5d4f5f762716de8a8b46d133c521b1388b9f7a1ed12260710580e8ad0bac9','checkpoints/active/checkpoint.json':'d0c43cfa46fc427311f2fae9fd97936ea1d4bed19e0250f84b245216bf49db96'}
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None

def request(method,url):
 allowed_live={BASE+p for p in LIVE}
 allowed_api={API+'/git/ref/heads/main',API+f'/actions/runs/{RUN}',API+f'/actions/artifacts/{ARTIFACT}'}
 if url not in allowed_live|allowed_api:raise ValueError('Unapproved endpoint')
 if method not in ('GET','DELETE') or method=='DELETE' and url!=API+f'/actions/artifacts/{ARTIFACT}':raise ValueError('Unapproved mutation')
 headers={'User-Agent':'Ride-Verified-Artifact-Cleanup/1','Cache-Control':'no-cache'}
 if url in allowed_api:headers.update({'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
 req=urllib.request.Request(url,method=method,headers=headers)
 try:
  with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as r:
   data=r.read(24_000_001)
   if len(data)>24_000_000:raise ValueError('Response limit')
   return r.status,data
 except urllib.error.HTTPError as e:
  if method=='GET' and url==API+f'/actions/artifacts/{ARTIFACT}' and e.code==404:return 404,b''
  raise

def execute(call=request):
 def get_json(path):
  status,body=call('GET',API+path)
  if status!=200:raise ValueError('Expected successful metadata read')
  return json.loads(body)
 def live():
  result={}
  for path,expected in LIVE.items():
   status,data=call('GET',BASE+path)
   if status!=200 or hashlib.sha256(data).hexdigest()!=expected:raise ValueError('Published recovery identity changed: '+path)
   result[path]=expected
  return result
 if get_json('/git/ref/heads/main').get('object',{}).get('sha')!=SHA:raise ValueError('Main changed')
 run=get_json(f'/actions/runs/{RUN}')
 if not (run.get('id')==RUN and run.get('head_sha')==SHA and run.get('head_branch')=='main' and run.get('status')=='completed' and run.get('conclusion')=='success' and run.get('path','').split('@')[0]=='.github/workflows/rollout.yml' and run.get('repository',{}).get('full_name')==REPO):raise ValueError('Successful exact publication run not verified')
 artifact=get_json(f'/actions/artifacts/{ARTIFACT}')
 workflow=artifact.get('workflow_run',{})
 if not (artifact.get('id')==ARTIFACT and artifact.get('name')=='github-pages' and artifact.get('size_in_bytes')==SIZE and artifact.get('expired') is False and artifact.get('digest')==DIGEST and workflow.get('id')==RUN and workflow.get('head_sha')==SHA and workflow.get('head_branch')=='main' and workflow.get('repository_id')==1420913307 and workflow.get('head_repository_id')==1420913307):raise ValueError('Artifact identity mismatch')
 before=live()
 # No mutation is attempted until all identity and recovery checks pass.
 status,_=call('DELETE',API+f'/actions/artifacts/{ARTIFACT}')
 if status!=204:raise RuntimeError('Deletion outcome unconfirmed; do not retry automatically')
 status,_=call('GET',API+f'/actions/artifacts/{ARTIFACT}')
 if status!=404:raise RuntimeError('Artifact absence not confirmed; do not retry deletion')
 after=live()
 return {'deletedArtifact':ARTIFACT,'successfulRun':RUN,'artifactBytes':SIZE,'artifactDigest':DIGEST,'apiAbsenceConfirmed':True,'liveBefore':before,'liveAfter':after,'liveUnchanged':before==after,'bulkRecoveryProof':'2026-10-17T08:13Z:11071files/150075982bytes;artifact not used','limits':'Does not prove future site availability or local generator runtime parity.'}

if __name__=='__main__':
 if os.environ.get('GITHUB_REPOSITORY')!=REPO or os.environ.get('GITHUB_REF')!='refs/heads/feat/mellow-z17-mirror' or os.environ.get('GITHUB_RUN_ATTEMPT')!='1':raise ValueError('Unapproved cleanup execution context')
 print(json.dumps(execute(),sort_keys=True,indent=2),flush=True)
