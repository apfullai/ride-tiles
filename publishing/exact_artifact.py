"""Build and verify the exact Pages tar before a fresh fail-closed artifact audit."""
import argparse,hashlib,json,os,stat,subprocess,sys,tarfile,time,re
from pathlib import Path

def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()

def inventory(root):
 root=Path(root).absolute()
 if any(p.is_symlink() for p in (root,*root.parents)) or not root.is_dir():raise ValueError('candidate site is not a plain directory')
 out={}
 for p in root.rglob('*'):
  info=p.lstat();name=p.relative_to(root).as_posix()
  if any(x in ('.git','.github') for x in p.relative_to(root).parts):raise ValueError('Repository metadata forbidden')
  if stat.S_ISREG(info.st_mode):out[name]={'bytes':info.st_size,'sha256':digest(p)}
  elif not stat.S_ISDIR(info.st_mode):raise ValueError('symlink or special entry forbidden')
 if not out:raise ValueError('candidate site has no files')
 return out

def verify_tar(archive,expected):
 seen={}
 with tarfile.open(archive,'r:') as tar:
  for m in tar:
   name=m.name
   if name.startswith('./'):name=name[2:]
   if m.isdir():
    if name not in ('','.') and (name.startswith('/') or '..' in Path(name).parts):raise ValueError('Unsafe directory')
    continue
   if not m.isfile() or m.linkname or m.issparse() or name not in expected or name in seen:raise ValueError('Unexpected tar entry')
   if m.size!=expected[name]['bytes']:raise ValueError('Tar size mismatch')
   h=hashlib.sha256();f=tar.extractfile(m)
   for b in iter(lambda:f.read(1048576),b''):h.update(b)
   if h.hexdigest()!=expected[name]['sha256']:raise ValueError('Tar hash mismatch')
   seen[name]=True
 if set(seen)!=set(expected):raise ValueError('Incomplete tar')

def pack(site,archive):
 site=Path(site).absolute();archive=Path(archive).absolute()
 if archive.name!='artifact.tar' or archive.exists() or archive.is_symlink() or site==archive.parent or site in archive.parents:raise ValueError('Archive must be new and outside site')
 before=inventory(site)
 archive.parent.mkdir(parents=True,exist_ok=True)
 if any(p.is_symlink() for p in (archive.parent,*archive.parent.parents)):raise ValueError('Unsafe output ancestor')
 subprocess.run(['tar','--format=gnu','--sort=name','--mtime=@0','--owner=0','--group=0','--numeric-owner','--mode=u+rw,go+r,a-s','--hard-dereference','-cf',str(archive),'-C',str(site),'.'],check=True,timeout=120,env={**{k:v for k,v in os.environ.items() if k not in ('TAR_OPTIONS','POSIXLY_CORRECT')},'LC_ALL':'C'})
 verify_tar(archive,before)
 if inventory(site)!=before:raise ValueError('Site changed during packing')
 size=archive.stat().st_size
 from exact_tar_size import exact_tar_bytes
 if size!=exact_tar_bytes({name:v['bytes'] for name,v in before.items()},[p.relative_to(site).as_posix() for p in site.rglob('*') if p.is_dir()]):raise ValueError('GNU tar differs from metadata prediction')
 return {'tarBytes':size,'tarSHA256':digest(archive),'candidateBytes':size+(size+99)//100+65536,'siteFiles':len(before),'siteBytes':sum(v['bytes'] for v in before.values())}

def audit(repository,packed,fetch=None):
 from actions_budget import _pages,fetch_public_json,artifact_repositories
 from artifact_budget import CAP_BYTES,MAX_API_REQUESTS,MAX_AUDIT_SECONDS
 report={**packed,'allowed':False,'complete':False,'capBytes':CAP_BYTES}
 try:
  if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise ValueError('Invalid repository')
  started=time.monotonic();count=0;source=fetch or fetch_public_json
  def read(url):
   nonlocal count
   count+=1
   if count>MAX_API_REQUESTS or time.monotonic()-started>MAX_AUDIT_SECONDS:raise ValueError('Audit bound exceeded')
   return source(url)
  repos=artifact_repositories(repository);used=0;n=0
  for repo in repos:
   for a in _pages(read,f'https://api.github.com/repos/{repo}/actions/artifacts','artifacts'):
    if type(a.get('size_in_bytes')) is not int or a['size_in_bytes']<0 or type(a.get('expired')) is not bool:raise ValueError('Malformed artifact metadata')
    if not a['expired']:used+=a['size_in_bytes'];n+=1
  projected=used+packed['candidateBytes']
  report.update(artifactRepositories=repos,artifactBytes=used,unexpiredArtifactCount=n,projectedBytes=projected,allowed=projected<=CAP_BYTES,complete=True)
 except Exception as e:report['error']=f'{type(e).__name__}: {e}'
 return report

def main():
 p=argparse.ArgumentParser();p.add_argument('--publisher',required=True);p.add_argument('--site',required=True);p.add_argument('--archive',required=True);p.add_argument('--repository',required=True);p.add_argument('--receipt',required=True);a=p.parse_args()
 sys.path.insert(0,str(Path(a.publisher)/'publishing'))
 packed=pack(a.site,a.archive);report=audit(a.repository,packed)
 # Verify the exact tar again after the API audit, before admission/output.
 if digest(Path(a.archive))!=packed['tarSHA256']:raise ValueError('Archive changed during audit')
 verify_tar(Path(a.archive),inventory(a.site))
 Path(a.receipt).write_text(json.dumps(report,sort_keys=True)+'\n')
 print(json.dumps(report,sort_keys=True))
 if os.environ.get('GITHUB_OUTPUT'):
  with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('allowed='+str(report['allowed']).lower()+'\n')
 if not report['allowed']:raise SystemExit('Exact artifact admission denied')
if __name__=='__main__':main()
