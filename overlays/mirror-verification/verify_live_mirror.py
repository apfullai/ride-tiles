#!/usr/bin/env python3
"""One canonical desktop session; only primary Mellow tiles receive synthetic503."""
import argparse,datetime,json,re,signal,time
from pathlib import Path
from urllib.parse import urlsplit

ORIGIN='https://test.ride.ap0z.com'
VERSION='2bafe73bf6586407'
PRIMARY=re.compile(r'^https://test\.ride\.ap0z\.com/mellow/xyz/2bafe73bf6586407/\d+/\d+/\d+\.pbf(?:\?[^#]*)?$')
MIRROR=re.compile(r'^https://apfullai\.github\.io/ride-tiles/mellow/xyz/2bafe73bf6586407/\d+/\d+/\d+\.pbf(?:\?[^#]*)?$')
FORBIDDEN=re.compile(r'^https?://[^/]+/(?:api/(?:route|route-surface(?:-trace)?)|route|trace_attributes|trace_route|brouter)(?:[/?]|$)')

def run(a):
 from playwright.sync_api import sync_playwright
 out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
 report={'passed':False,'scope':'Canonical test frontend; synthetic503 only for primary Mellow tiles; actual GitHub mirror network untouched','startedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'forcedPrimary':[],'mirrorResponses':[],'fatal':[]}
 def save():(out/'mirror.json').write_text(json.dumps(report,indent=2)+'\n')
 save()
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,timeout=10000)
  context=browser.new_context(viewport={'width':1440,'height':900},color_scheme='light')
  page=context.new_page();page.set_default_timeout(5000)
  def stop(reason):
   if not report['fatal']:report['fatal'].append(reason);save();context.close()
  def check():
   if report['fatal']:raise AssertionError(report['fatal'][0])
  def request(r):
   if FORBIDDEN.search(r.url) or urlsplit(r.url).hostname=='ride.ap0z.com':stop('Forbidden route or production request')
  def response(r):
   if r.status==403 or r.headers.get('cf-mitigated')=='challenge':stop('Security response: stop without retry');return
   if PRIMARY.fullmatch(r.url) and r.status!=503:stop('Primary tile escaped fault injection');return
   if MIRROR.fullmatch(r.url):
    report['mirrorResponses'].append({'url':r.url,'status':r.status,'contentType':r.headers.get('content-type'),'contentLength':r.headers.get('content-length')});save()
  def fail_primary(route):
   assert PRIMARY.fullmatch(route.request.url)
   report['forcedPrimary'].append(route.request.url);save()
   route.fulfill(status=503,content_type='text/plain',body='Controlled primary-only map tile failure')
  context.on('request',request);context.on('response',response)
  context.route(PRIMARY,fail_primary)
  try:
   page.goto(ORIGIN,wait_until='domcontentloaded',timeout=20000);check()
   assert page.url.rstrip('/')==ORIGIN,'Unexpected app redirect'
   assert not page.evaluate("/just a moment|attention required.*cloudflare/i.test(document.title)||!!document.querySelector('#challenge-form,#challenge-running,iframe[src*=\"challenges.cloudflare.com\"]')"),'Security challenge'
   script=page.locator('script[type=module][src*="app.js"]').get_attribute('src')
   assert script and a.asset_version in script,'Wrong canonical frontend version'
   health=page.evaluate("async()=>{const r=await fetch('/__health',{cache:'no-store'});if(!r.ok)throw Error('Health unavailable');return r.json()}")
   assert health['frontendAssetVersion']==a.asset_version and health['commit'].startswith(a.commit_prefix),'Wrong canonical release'
   report['release']={'script':script,'assetVersion':health['frontendAssetVersion'],'commit':health['commit']};save()
   page.evaluate("async()=>{window.mirrorApp=await import(document.querySelector('script[type=module][src*=\"app.js\"]').src)}")
   end=time.monotonic()+70;last=None
   while time.monotonic()<end:
    check();last=page.evaluate('()=>({...mirrorApp.mapReadiness(),...mirrorApp.mellowReadiness()})')
    m=last['mellowNetwork']
    if last['styleLoaded'] and last['tilesLoaded'] and last['renderSettled'] and not last['moving'] and last['mapIdleEvents']>0 and m['visible'] and m['status']=='ready' and m['origin']=='mirror':break
    page.wait_for_timeout(100)
   else:raise AssertionError('Actual mirror readiness deadline: '+json.dumps(last))
   d=page.evaluate('mirrorApp.mapDiagnostics()');report['diagnostics']=d
   m=d['mellowNetwork'];assert m['transport']=='xyz' and m['origin']=='mirror' and m['visible']
   assert report['forcedPrimary'],'Primary failure interception not exercised'
   assert any(r['status']==200 for r in report['mirrorResponses']),'No actual GitHub tile200'
   assert all(r['status']==200 for r in report['mirrorResponses']),'GitHub tile response failed'
   forced_paths={urlsplit(u).path for u in report['forcedPrimary']}
   assert any(urlsplit(r['url']).path.removeprefix('/ride-tiles') in forced_paths for r in report['mirrorResponses'] if r['status']==200),'Mirror response did not correspond to a failed primary tile'
   assert sum(m['rendered'].values())>0 and len(m['layers'])==6,'Mellow overlay not actually rendered'
   assert all(l['visibility']=='visible' and l['sourceLayer']=='mellow' for l in m['layers'])
   assert page.locator('#legendCorner').get_attribute('data-mode')=='mellow'
   assert page.locator('#legendExpand').evaluate('(e)=>e.hidden')
   assert not d['visibleCyclingLayers'],'Ordinary overlay incorrectly co-rendered'
   assert d['appliedTheme']=='light' and d['desiredTheme']=='light'
   page.screenshot(path=str(out/'actual-github-mirror.png'),animations='disabled',timeout=5000)
   check();report['passed']=True
  except Exception as e:
   report['error']={'type':type(e).__name__,'message':str(e)[:4000]}
   if not report['fatal']:
    try:report['failureDiagnostics']=page.evaluate('window.mirrorApp?.mapDiagnostics()');page.screenshot(path=str(out/'failure.png'),timeout=2000)
    except Exception:pass
   raise
  finally:
   report['finishedAt']=datetime.datetime.now(datetime.timezone.utc).isoformat();save();context.close();browser.close()

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--asset-version',default='5dc4593aa32caa4b');p.add_argument('--commit-prefix',default='97c19f7b');a=p.parse_args()
 assert re.fullmatch('[a-f0-9]{16}',a.asset_version) and re.fullmatch('[a-f0-9]{8,40}',a.commit_prefix)
 signal.signal(signal.SIGALRM,lambda *_:(_ for _ in ()).throw(TimeoutError('110-second live mirror budget')));signal.alarm(110)
 try:run(a)
 finally:signal.alarm(0)
