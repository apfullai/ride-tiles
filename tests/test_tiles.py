import gzip,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import tile_generator as g
import mapbox_vector_tile
from build_region import validate_config,KINDS,build
from import_mirror import import_chicagoland

class TilesTest(unittest.TestCase):
 def test_classification(self):
  for tags,want in [({'highway':'cycleway'},'trail'),({'highway':'residential','cycleway':'track'},'protected'),({'cycleway':'lane'},'lane'),({'highway':'path','bicycle':'yes'},'shared_path'),({'highway':'track'},'track'),({'cycleway':'shared_lane'},'shared_lane')]:
   self.assertEqual(g.classify(tags),want)
  self.assertIsNone(g.classify({'highway':'cycleway','access':'private'}))
 def test_explicit_bicycle_restrictions(self):
  # Restrictions win over infrastructure tags and a general access permission.
  infrastructure=[{'highway':'cycleway'},{'cycleway':'track'},{'cycleway':'lane'},
                  {'highway':'path'},{'highway':'track'},{'cycleway':'shared_lane'}]
  for base in infrastructure:
   for bicycle in ('no','private','dismount','use_sidepath'):
    for access in (None,'yes','permissive','no','private'):
     tags={**base,'bicycle':bicycle}
     if access is not None:tags['access']=access
     with self.subTest(tags=tags):self.assertIsNone(g.classify(tags))
 def test_generic_access_restrictions_need_explicit_bicycle_permission(self):
  for access in ('no','private'):
   for bicycle in (None,'','unknown','destination','customers','discouraged','yes;no'):
    for base in ({'highway':'cycleway'},{'highway':'track'},{'cycleway':'lane'}):
     tags={**base,'access':access}
     if bicycle is not None:tags['bicycle']=bicycle
     with self.subTest(tags=tags):self.assertIsNone(g.classify(tags))
 def test_bicycle_permission_overrides_generic_access(self):
  infrastructure=[({'highway':'cycleway'},'trail'),({'cycleway':'track'},'protected'),
                  ({'cycleway':'lane'},'lane'),({'highway':'path'},'shared_path'),
                  ({'highway':'track'},'track'),({'cycleway':'shared_lane'},'shared_lane')]
  for access in ('no','private'):
   for bicycle in ('yes','designated','official','permissive'):
    for base,kind in infrastructure:
     tags={**base,'access':access,'bicycle':bicycle}
     want='trail' if base.get('highway')=='path' and bicycle=='designated' else kind
     with self.subTest(tags=tags):self.assertEqual(g.classify(tags),want)
 def test_track_access_is_not_inferred(self):
  for tags in ({'highway':'track'},{'highway':'track','access':'unknown'},
               {'highway':'track','bicycle':'unknown'}):
   original=tags.copy()
   with self.subTest(tags=tags):
    self.assertEqual(g.classify(tags),'track')
    self.assertEqual(tags,original)
 def test_access_filter_and_properties_in_generated_mvt(self):
  permitted=[{'name':f'{access}-{bicycle}','highway':'cycleway','access':access,'bicycle':bicycle}
             for access in ('no','private') for bicycle in ('yes','designated','official','permissive')]
  permitted += [{'name':'track-untagged','highway':'track'},
                {'name':'track-unknown','highway':'track','access':'unknown','bicycle':'unknown'}]
  denied=[{'name':f'denied-{access}','highway':'cycleway','access':access} for access in ('no','private')]
  denied += [{'name':f'denied-{bicycle}','highway':'track','bicycle':bicycle,'access':'yes'}
             for bicycle in ('no','private','dismount','use_sidepath')]
  for nested in (False,True):
   with self.subTest(nested_tags=nested), tempfile.TemporaryDirectory() as td, patch.object(g,'BBOX',(-74.05,40.65,-73.85,40.9)):
    p=Path(td);src=p/'sample.jsonl';out=p/'output';features=[]
    for fid,tags in enumerate(permitted+denied,1):
     props={'id':fid,'tags':tags} if nested else {'id':fid,**tags}
     features.append({'type':'Feature','properties':props,'geometry':{'type':'LineString','coordinates':[[-74,40.7],[-73.99,40.71]]}})
    src.write_text('\n'.join(map(json.dumps,features)))
    report=g.generate(src,out,range(8,9))
    self.assertEqual(report['features_by_category'],{'trail':8,'track':2})
    decoded=[]
    for tile in (out/'tiles').rglob('*.pbf'):
     decoded.extend(mapbox_vector_tile.decode(gzip.decompress(tile.read_bytes()))['cycleinfra']['features'])
    by_name={f['properties']['name']:f['properties'] for f in decoded}
    self.assertEqual(set(by_name),{tags['name'] for tags in permitted})
    for tags in permitted:
     props=by_name[tags['name']]
     self.assertEqual(props['kind'],'track' if tags['highway']=='track' else 'trail')
     for key in ('access','bicycle'):
      if key in tags:self.assertEqual(props[key],tags[key])
      else:self.assertNotIn(key,props)
 def test_region_validation(self):
  good={'id':'nyc-pilot','bounds':[-74.05,40.65,-73.85,40.9],'minzoom':8,'maxzoom':14,'sourceStates':['new-york']}
  validate_config(good)
  for bad in [dict(good,id='../private'),dict(good,maxzoom=19),dict(good,bounds=[-180,-85,180,85]),dict(good,sourceStates=['../../etc'])]:
   with self.assertRaises(AssertionError):validate_config(bad)
 def test_real_mvt_and_clipping(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);src=p/'sample.jsonl';out=p/'output';g.BBOX=(-74.05,40.65,-73.85,40.9)
   features=[{'type':'Feature','properties':{'id':1,'highway':'cycleway'},'geometry':{'type':'LineString','coordinates':[[-74,40.7],[-73.99,40.71]]}},{'type':'Feature','properties':{'id':2,'highway':'cycleway'},'geometry':{'type':'LineString','coordinates':[[-87,41.5],[-87.1,41.6]]}}]
   src.write_text('\n'.join(map(json.dumps,features)))
   report=g.generate(src,out,range(8,9))
   self.assertEqual(report['features_by_category'],{'trail':1})
   tiles=list((out/'tiles').rglob('*.pbf'));self.assertTrue(tiles)
   for tile in tiles:
    raw=gzip.decompress(tile.read_bytes())
    decoded=mapbox_vector_tile.decode(raw)
    self.assertEqual(decoded['cycleinfra']['features'][0]['properties']['kind'],'trail')
    self.assertEqual(decoded['cycleinfra']['features'][0]['geometry']['type'],'LineString')
    self.assertTrue(decoded['cycleinfra']['features'][0]['geometry']['coordinates'])
 def test_region_pipeline_contract(self):
  cfg={'id':'nyc-pilot','name':'New York City pilot','bounds':[-74.05,40.65,-73.85,40.9],'minzoom':8,'maxzoom':8,'sourceStates':['new-york']}
  feature={'type':'Feature','properties':{'id':1,'highway':'cycleway'},'geometry':{'type':'LineString','coordinates':[[-74,40.7],[-73.99,40.71]]}}
  def fake_run(*args):
   output=Path(args[args.index('-o')+1])
   if args[0]=='curl':output.write_bytes(b'x'*1_000_001)
   elif args[1]=='export':output.write_text(json.dumps(feature))
   else:output.write_bytes(b'public osm fixture')
  with tempfile.TemporaryDirectory() as td, patch('build_region.run',side_effect=fake_run), patch('build_region.subprocess.check_output',return_value='2026-10-11T20:21:06Z'):
   p=Path(td);site=p/'site';site.mkdir()
   entry=build(cfg,p/'work',site,'https://apfullai.github.io/rides-tiles')
   self.assertRegex(entry['version'],r'^[a-f0-9]{16}$')
   dest=site/'regions'/'nyc-pilot'/entry['version']
   self.assertTrue((dest/'provenance.json').exists())
   self.assertTrue((dest/'cycling-source.osm.pbf').exists())
   self.assertEqual(entry['sourceLayer'],'cycleinfra')
   for tile in (dest/'tiles').rglob('*.pbf'):self.assertFalse(tile.read_bytes().startswith(b'\x1f\x8b'))
 def test_exact_public_mirror(self):
  with tempfile.TemporaryDirectory() as td:
   entry=import_chicagoland(Path(td),'https://apfullai.github.io/rides-tiles')
   self.assertEqual(entry['version'],'19785f3c1ca31233')
   self.assertEqual(entry['tileCount'],6782)
   self.assertEqual(entry['tileBytes'],12125167)
   self.assertEqual(entry['bounds'],[-89,41.05,-86.65,43.25])
 def test_no_empty_coverage(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'input').write_text('');g.BBOX=(-74.05,40.65,-73.85,40.9)
   with self.assertRaises(RuntimeError):g.generate(p/'input',p/'out')
if __name__=='__main__':unittest.main()
