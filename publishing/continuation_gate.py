"""Cheap readiness advisory; the builder must independently enforce the mode."""
import argparse,datetime as dt,json,os,re
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError
STATUS='https://apfullai.github.io/ride-tiles/status.json'
MONDAY='23 9 * * 1'
CONTINUATION='23 9 * * 0,2-6'

def choose_mode(event,schedule,requested=''):
    if requested and not re.fullmatch('[a-z0-9-]{1,48}',requested):raise ValueError('Invalid requested region')
    if event=='schedule':
        if schedule not in (MONDAY,CONTINUATION) or requested:raise ValueError('Unexpected schedule')
        return schedule==MONDAY,''
    if event in ('workflow_dispatch','push'):return True,requested
    raise ValueError('Unsupported publisher event')

def ready(status,allow_new,now):
    if status is None:return allow_new,'no published active checkpoint'
    if not isinstance(status,dict) or type(status.get('active'))is not bool:raise ValueError('Invalid status')
    if not status['active']:return allow_new,'weekly/manual selection' if allow_new else 'no active city to resume'
    if type(status.get('needsAttention'))is not bool:raise ValueError('Invalid attention state')
    if status['needsAttention']:return False,'active city needs attention'
    retry=status.get('retryAfter')
    if retry is not None:
        stamp=dt.datetime.fromisoformat(retry.replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError('Retry time lacks timezone')
        if stamp>now:return False,'active city retry is not due'
    return True,'active city continuation'

def fetch_status():
    try:
        with urlopen(STATUS,timeout=10) as r:
            if r.status!=200 or r.geturl()!=STATUS:raise ValueError('Unexpected status response')
            raw=r.read(65537)
            if len(raw)>65536:raise ValueError('Status too large')
            return json.loads(raw)
    except HTTPError as e:
        if e.code==404:return None
        raise

def main():
    report={'eligible':False,'allowNew':False,'startRegion':'','reason':'not evaluated'}
    try:
        event=os.environ.get('GITHUB_EVENT_NAME',''); requested=os.environ.get('REQUESTED_START_REGION','')
        if event=='push':
            request=json.loads(Path('config/rollout-request.json').read_text());requested=request.get('startRegion','')
            if not isinstance(requested,str):raise ValueError('Invalid start region')
        allow,requested=choose_mode(event,os.environ.get('EVENT_SCHEDULE',''),requested)
        eligible,reason=ready(fetch_status(),allow,dt.datetime.now(dt.timezone.utc))
        report.update(eligible=eligible,allowNew=allow,startRegion=requested,reason=reason)
    except Exception as e:report['reason']='Readiness unavailable: '+type(e).__name__+': '+str(e)[:200]
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as f:
            f.write('eligible='+str(report['eligible']).lower()+'\nallow_new='+str(report['allowNew']).lower()+'\nstart_region='+report['startRegion']+'\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('## Continuation readiness\n\n'+json.dumps(report,sort_keys=True)+'\n')
    print(json.dumps(report,sort_keys=True))
if __name__=='__main__':main()
