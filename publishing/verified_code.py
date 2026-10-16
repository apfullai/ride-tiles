"""Full-test reuse requires successful GitHub CI plus identical input blobs."""
import hashlib,json,os,re,signal,stat,subprocess,sys,time
from pathlib import Path
REPOSITORY='apfullai/ride-tiles'
WORKFLOW='.github/workflows/snapshot-tests.yml'
REQUIRED_STEPS=('Test queue and generator semantics','Test offline reconstruction, preservation and integrity guards')
SCHEMA=1

def selected(path):
    parts=path.split('/')
    if '__pycache__' in parts or path.endswith(('.pyc','.pyo')):return False
    return (path.startswith(('tools/','publishing/','tests/','bootstrap/','research/','.github/workflows/'))
            or path.startswith('config/') and path!='config/rollout-request.json'
            or re.fullmatch(r'requirements[^/]*\.txt',path) is not None)

def blob_sha(data):return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
def fingerprint(records):return hashlib.sha256(''.join(f'{p} {records[p]}\n' for p in sorted(records)).encode()).hexdigest()
def source_records(root):
    root=Path(root); result={}
    for prefix in ('tools','publishing','tests','bootstrap','research','.github/workflows','config'):
        p=root/prefix
        if p.is_symlink():raise ValueError('Selected root is a symlink')
    for p in root.rglob('*'):
        path=p.relative_to(root).as_posix()
        if not selected(path):continue
        mode=p.lstat().st_mode
        if stat.S_ISDIR(mode):continue
        if not stat.S_ISREG(mode):raise ValueError('Selected input is not a regular file')
        result[path]=blob_sha(p.read_bytes())
    return result

def current_fingerprint(root):return fingerprint(source_records(root))
def prove_receipt(receipt,records,fetch):
    if not isinstance(receipt,dict) or receipt.get('schemaVersion')!=SCHEMA or receipt.get('fingerprint')!=fingerprint(records):return False
    rid=receipt.get('runId'); sha=receipt.get('commit')
    if type(rid) is not int or rid<1 or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{40}',sha):return False
    base=f'https://api.github.com/repos/{REPOSITORY}'
    run=fetch(f'{base}/actions/runs/{rid}')
    if (run.get('repository',{}).get('full_name')!=REPOSITORY or run.get('head_repository',{}).get('full_name')!=REPOSITORY
        or run.get('path','').split('@')[0]!=WORKFLOW or run.get('head_sha')!=sha
        or run.get('head_branch')!='main' or run.get('event') not in ('push','workflow_dispatch')
        or run.get('status')!='completed' or run.get('conclusion')!='success'):return False
    attempt=run.get('run_attempt')
    if type(attempt)is not int or attempt<1:return False
    jobs=fetch(f'{base}/actions/runs/{rid}/attempts/{attempt}/jobs?per_page=100')
    items=jobs.get('jobs',[])
    if jobs.get('total_count')!=len(items):return False
    tests=[j for j in items if j.get('name')=='test' and j.get('conclusion')=='success' and j.get('status')=='completed' and j.get('run_id')==rid]
    if len(tests)!=1:return False
    steps=tests[0].get('steps',[])
    for name in REQUIRED_STEPS:
        matches=[s for s in steps if s.get('name')==name]
        if len(matches)!=1 or matches[0].get('conclusion')!='success' or matches[0].get('status')!='completed':return False
    tree=fetch(f'{base}/git/trees/{sha}?recursive=1')
    if tree.get('truncated') is not False or not isinstance(tree.get('tree'),list):return False
    remote={}
    for item in tree['tree']:
        path=item.get('path','')
        if selected(path) and item.get('type')!='tree':
            if item.get('type')!='blob' or item.get('mode') not in ('100644','100755') or path in remote:return False
            remote[path]=item.get('sha')
    return remote==records

def find_proof(old,records,fetch):
    checked=set()
    if old:
        checked.add(old.get('runId'))
        if prove_receipt(old,records,fetch):return old
    listing=fetch(f'https://api.github.com/repos/{REPOSITORY}/actions/workflows/snapshot-tests.yml/runs?status=success&branch=main&per_page=10')
    for run in listing.get('workflow_runs',[])[:10]:
        if run.get('id') in checked:continue
        hint={'schemaVersion':SCHEMA,'fingerprint':fingerprint(records),'runId':run.get('id'),'commit':run.get('head_sha')}
        if prove_receipt(hint,records,fetch):return hint
    return None

def run_group(command,*,cwd,check,timeout):
    process=subprocess.Popen(command,cwd=cwd,start_new_session=True)
    try:
        code=process.wait(timeout=timeout)
        if check and code:raise subprocess.CalledProcessError(code,command)
        return subprocess.CompletedProcess(command,code)
    except BaseException:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait()
        raise

def verify_code(root,site,*,fetch,timeout=300,runner=run_group):
    root=Path(root);site=Path(site);records=source_records(root)
    if not records:raise ValueError('No verification source files')
    old=None;p=site/'verification.json'
    if p.exists():
        try:old=json.loads(p.read_text())
        except (ValueError,OSError):pass
    end=time.monotonic()+timeout; proof_end=min(end-30,time.monotonic()+20);calls=0
    def bounded(url):
        nonlocal calls
        calls+=1
        if calls>12 or time.monotonic()>proof_end:raise TimeoutError('Verification proof budget exhausted')
        return fetch(url)
    try:proof=find_proof(old,records,bounded)
    except Exception:proof=None
    probe="import sys,unittest; s=unittest.defaultTestLoader.discover(sys.argv[1],pattern='test_resumable*.py'); assert s.countTestCases()>0,'No focused resume tests'; r=unittest.TextTestRunner(verbosity=2).run(s); sys.exit(not r.wasSuccessful())"
    commands=([[sys.executable,'-c',probe,path] for path in ('tests','publishing/tests')] if proof else
              [[sys.executable,'-m','unittest','discover','-s',path,'-v'] for path in ('tests','publishing/tests')])
    for command in commands:
        remaining=end-time.monotonic()
        if remaining<=0:raise TimeoutError('Verification deadline exhausted')
        runner(command,cwd=root,check=True,timeout=remaining)
    if proof:return proof,'reused-proven-full-tests-with-focused-checks'
    # This receipt reports local success only. It cannot authorize reuse: the
    # next continuation still needs the separately verified standalone CI run.
    return {'schemaVersion':SCHEMA,'fingerprint':fingerprint(records),'runId':None,'commit':os.environ.get('GITHUB_SHA'),'localFullTestsPassed':True},'full-tests-passed'
