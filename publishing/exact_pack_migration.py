"""One exact Wisconsin checkpoint may cross the reviewed packaging-only boundary."""
import copy,hashlib,json
from pathlib import Path
FROM_CODE='4769e286133dc445276acc61648047ea3fe1d16864ba98b1f1afdb2853b139a6'
FROM_DESCRIPTOR='d0c43cfa46fc427311f2fae9fd97936ea1d4bed19e0250f84b245216bf49db96'
FROM_COMMIT='c9dd4771e5670b2ab4fa9df1d1eb8b5e7122d5fa'
ID='chicago-wisconsin-exact-pages-pack-v1-20261017'
# Only packaging/capacity logic, its tests and the existing rollout upload steps
# may change. Every other selected original source blob must remain exact.
CHANGED={'publishing/tests/test_early_capacity.py','publishing/tests/test_artifact_budget.py','.github/workflows/rollout.yml','publishing/artifact_budget.py','publishing/early_capacity.py','publishing/checkpoint_migrations.py'}
ADDED={'publishing/exact_artifact.py','publishing/exact_tar_size.py','publishing/verify_uploaded_artifact.py','publishing/exact_pack_migration.py','publishing/exact_pack_source_pins.json','publishing/tests/test_exact_pack.py'}

def eligible(root,checkpoint,code_root):
 root,code_root=Path(root),Path(code_root)
 try:
  raw=(root/'checkpoint.json').read_bytes()
  if hashlib.sha256(raw).hexdigest()!=FROM_DESCRIPTOR or json.loads(raw)!=checkpoint or checkpoint.get('codeFingerprint')!=FROM_CODE:return False
  from verified_code import source_records
  pin_bytes=(code_root/'publishing/exact_pack_source_pins.json').read_bytes()
  if hashlib.sha256(pin_bytes).hexdigest()!='5c495ff8ab6adfbeb1964d211cb9a91abc8d6e2227ca32fa05b7566f5dcd2de1':return False
  pins=json.loads(pin_bytes)
  actual=source_records(code_root)
  if set(actual)!=set(pins)|CHANGED|ADDED or any(actual.get(name)!=blob for name,blob in pins.items()):return False
  if checkpoint['progress']!={'completedStates':['illinois','wisconsin'],'indexReady':False,'batchIds':[],'batchCount':0,'completedBatches':[]} or checkpoint['nextStage']!={'kind':'source','state':'indiana'}:return False
  from rollout_snapshot import _records
  files={name:record for name,record in _records(root,include_metadata=True).items() if name!='checkpoint.json'}
  if files!=checkpoint['files']:return False
  for state in ('illinois','wisconsin'):
   source=root/'sources'/state;receipt=json.loads((source/'receipt.json').read_bytes());crop=receipt['crop'];h=hashlib.sha256();size=0
   if crop['encoding']!='raw':return False
   for i,part in enumerate(crop['parts']):
    if part['path']!=f'crop.part-{i:04d}':return False
    path=source/part['path'];ph=hashlib.sha256();n=0
    with path.open('rb') as f:
     for b in iter(lambda:f.read(1048576),b''):h.update(b);ph.update(b);size+=len(b);n+=len(b)
    if n!=part['bytes'] or ph.hexdigest()!=part['sha256']:return False
   if size!=crop['bytes'] or h.hexdigest()!=crop['sha256']:return False
  return True
 except (ValueError,TypeError,KeyError,OSError):return False

def migrate(root,checkpoint,code_root,*,target_fingerprint,verified_fingerprint,now):
 from verified_code import current_fingerprint
 if target_fingerprint==FROM_CODE or target_fingerprint!=verified_fingerprint or target_fingerprint!=current_fingerprint(code_root) or not eligible(root,checkpoint,code_root):raise ValueError('Exact packaging checkpoint migration not verified')
 result=copy.deepcopy(checkpoint);result['codeFingerprint']=target_fingerprint
 result['migration']={'id':ID,'fromCommit':FROM_COMMIT,'fromCodeFingerprint':FROM_CODE,'toCodeFingerprint':target_fingerprint,'fromDescriptorSha256':FROM_DESCRIPTOR,'at':now.isoformat(),'preservedSourcePlanId':checkpoint['sourcePlan']['planId'],'preservedFilesSha256':hashlib.sha256(json.dumps(checkpoint['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest(),'previousMigration':copy.deepcopy(checkpoint.get('migration'))}
 return result
