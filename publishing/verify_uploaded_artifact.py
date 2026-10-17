"""Read-only post-upload verification. Never deletes an unexpected artifact."""
import argparse,json,os,re,sys,time
from pathlib import Path
from exact_artifact import audit

def verify(meta,receipt,artifact_id,run_id):
 if type(meta.get('id')) is not int or str(meta['id'])!=artifact_id or meta.get('name')!='github-pages' or meta.get('expired') is not False:raise ValueError('Uploaded artifact identity mismatch')
 if type(meta.get('size_in_bytes')) is not int or not 0<meta['size_in_bytes']<=receipt['candidateBytes']:raise ValueError('Actual upload exceeds reserved bound')
 if str(meta.get('workflow_run',{}).get('id'))!=run_id:raise ValueError('Uploaded artifact run mismatch')

def main():
 p=argparse.ArgumentParser();p.add_argument('--publisher',required=True);p.add_argument('--receipt',required=True);a=p.parse_args()
 sys.path.insert(0,str(Path(a.publisher)/'publishing'))
 from actions_budget import fetch_public_json,_pages
 from artifact_budget import MAX_API_REQUESTS,MAX_AUDIT_SECONDS
 repo=os.environ['GITHUB_REPOSITORY'];artifact_id=os.environ['UPLOADED_ARTIFACT_ID'];run_id=os.environ['GITHUB_RUN_ID']
 if repo!='apfullai/ride-tiles' or not re.fullmatch('[0-9]+',artifact_id) or not re.fullmatch('[0-9]+',run_id):raise ValueError('Unexpected upload identity')
 receipt=json.loads(Path(a.receipt).read_bytes())
 if receipt.get('allowed') is not True or receipt.get('complete') is not True:raise ValueError('Missing passed pre-upload audit')
 started=time.monotonic();calls=0
 def read(url):
  nonlocal calls
  calls+=1
  if calls>MAX_API_REQUESTS or time.monotonic()-started>MAX_AUDIT_SECONDS:raise ValueError('Post-upload audit bound exceeded')
  return fetch_public_json(url)
 matches=[a for a in _pages(read,f'https://api.github.com/repos/{repo}/actions/artifacts','artifacts') if str(a.get('id'))==artifact_id]
 if len(matches)!=1:raise ValueError('Uploaded artifact absent or duplicate in complete listing')
 meta=matches[0]
 verify(meta,receipt,artifact_id,run_id)
 result=audit(repo,{'candidateBytes':0},fetch=read)
 print(json.dumps({'uploadedArtifactId':artifact_id,'actualBytes':meta['size_in_bytes'],'reservedBytes':receipt['candidateBytes'],'freshTotalAudit':result},sort_keys=True))
 if not result['allowed'] or result['artifactBytes']<meta['size_in_bytes']:raise SystemExit('Post-upload artifact audit failed; no deployment or automatic deletion')
if __name__=='__main__':main()
