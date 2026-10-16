"""Cheap readiness advisory; the builder must independently enforce the mode."""
import argparse,datetime as dt,json,os,re
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError
STATUS='https://apfullai.github.io/ride-tiles/status.json'
MONDAY='23 2 * * 1'
CONTINUATION='23 2 * * 0,2-6'
EVENING='23 18 * * *'
ACQUISITION='23 10 17-19 10 *'
ACQUISITION_GENERATION='2726f5fdcf1b830f7f5c29eff8b058d83a78278770c7cfc3acb5c25d6f2e8c6e'
ACQUISITION_START=dt.datetime(2026,10,17,tzinfo=dt.timezone.utc)
ACQUISITION_END=dt.datetime(2026,10,20,tzinfo=dt.timezone.utc)

def choose_mode(event,schedule,requested=''):
    if requested and not re.fullmatch('[a-z0-9-]{1,48}',requested):raise ValueError('Invalid requested region')
    if event=='schedule':
        if schedule not in (MONDAY,CONTINUATION,EVENING,ACQUISITION) or requested:raise ValueError('Unexpected schedule')
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

def acquisition_ready(status, now):
    """Temporary slot may only acquire remaining sources for the frozen city."""
    if not ACQUISITION_START <= now < ACQUISITION_END:
        return False,'temporary acquisition window closed'
    eligible,reason=ready(status,False,now)
    if not eligible:return eligible,reason
    counts=status.get('completedCounts',{})
    if (status.get('generationId')!=ACQUISITION_GENERATION or status.get('stage')!='source'
        or status.get('sourceDate')!='2026-10-13'
        or status.get('sourceAcquisitionExpiresAt')!='2026-10-20T00:00:00+00:00'
        or not isinstance(counts,dict) or type(counts.get('sources')) is not int
        or counts.get('sourceTotal')!=4 or type(counts.get('sourceTotal')) is not int
        or not 1 <= counts['sources'] < 4):
        return False,'temporary slot has no matching unfinished source acquisition'
    return True,'bounded October acquisition continuation'

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
        schedule=os.environ.get('EVENT_SCHEDULE','')
        allow,requested=choose_mode(event,schedule,requested)
        now=dt.datetime.now(dt.timezone.utc)
        if event=='schedule' and schedule==ACQUISITION:
            # Do not even fetch metadata after the one-time year/date window.
            status=fetch_status() if ACQUISITION_START <= now < ACQUISITION_END else None
            eligible,reason=acquisition_ready(status,now)
        else:
            eligible,reason=ready(fetch_status(),allow,now)
        report.update(eligible=eligible,allowNew=allow,startRegion=requested,reason=reason)
    except Exception as e:report['reason']='Readiness unavailable: '+type(e).__name__+': '+str(e)[:200]
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as f:
            f.write('eligible='+str(report['eligible']).lower()+'\nallow_new='+str(report['allowNew']).lower()+'\nstart_region='+report['startRegion']+'\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('## Continuation readiness\n\n'+json.dumps(report,sort_keys=True)+'\n')
    print(json.dumps(report,sort_keys=True))
if __name__=='__main__':main()
