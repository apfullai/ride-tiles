"""Exact size for our fixed GNU tar format/owner/mtime flags, without compression."""
from pathlib import PurePosixPath

def exact_tar_bytes(files,directories=None):
 directories=set(directories or ())|{'.'}
 for name,size in files.items():
  p=PurePosixPath(name)
  if not name or p.is_absolute() or '..' in p.parts or p.as_posix()!=name or type(size) is not int or not 0<=size<8589934592:raise ValueError('Unsupported tar path or size')
  directories.update(str(x) for x in p.parents)
 if any(x in files for x in directories):raise ValueError('File/directory collision')
 total=0
 for name,size in [(d,None) for d in directories]+list(files.items()):
  name=('.' if name=='.' else './'+name)+('/' if size is None else '')
  length=len(name.encode('utf-8'))
  total+=512
  if length>100:total+=512+((length+1+511)//512)*512
  if size is not None:total+=((size+511)//512)*512
 return ((total+1024+10239)//10240)*10240

def allowance(files,directories=None):
 directories=set(directories or ())|{'.'}
 for name in files:directories.update(str(p) for p in PurePosixPath(name).parents)
 size=exact_tar_bytes(files,directories)
 return {'siteBytes':sum(files.values()),'tarBytesUpperBound':size,'candidateBytes':size+(size+99)//100+65536,'fileCount':len(files),'entryCount':len(files)+len(directories),'tarFormat':'gnu-fixed-metadata'}
