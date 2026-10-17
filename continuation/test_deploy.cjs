const test=require('node:test'),assert=require('node:assert/strict');const{run}=require('./deploy.cjs');
const now=()=>Date.parse('2026-10-17T10:00:00Z');
const receipt={artifactId:12096579037,sourceHead:'4711212d901d10cf0ff1c100625798729c9748e5',sourceRun:39022469343,main:'ae037d0d4e37e82314a679006ebe4d86bc0497f5',digest:'sha256:7c6616506c78a1f5226243bbff1c66b73aa9df844ea43fbef18876ecb11af558',bytes:201912458,capacity:{allowed:true,complete:true},checkedAt:'2026-10-17T10:00:00Z'};
const env={GITHUB_REPOSITORY:'apfullai/ride-tiles',GITHUB_REF:'refs/heads/feat/mellow-z17-mirror',GITHUB_RUN_ATTEMPT:'1',GITHUB_RUN_ID:'999',GH_TOKEN:'fixture'};
function setup(status='succeed'){const calls=[];return{calls,core:{getIDToken:async()=> 'fixture-oidc',info:()=>{}},fetchImpl:async(url,options)=>{calls.push({url,...options});return new Response(JSON.stringify(options.method==='POST'?{id:'deploy-fixture'}:{status}));},now,sleep:async()=>{},receipt,env};}
test('one POST then read-only success; never upload or delete',async()=>{const x=setup();assert.equal((await run(x)).status,'succeed');assert.deepEqual(x.calls.map(c=>c.method),['POST','GET']);assert(x.calls.every(c=>c.redirect==='error'));});
test('stale or mismatched admission sends nothing',async()=>{for(const r of [{...receipt,artifactId:1},{...receipt,checkedAt:'2026-10-16T00:00:00Z'}]){const x=setup();await assert.rejects(run({...x,receipt:r}));assert.equal(x.calls.length,0);}});
test('unknown create outcome never retries',async()=>{const x=setup();x.fetchImpl=async(u,o)=>{x.calls.push(o);throw Error('network');};await assert.rejects(run(x));assert.equal(x.calls.length,1);});
test('terminal failure never creates second deployment',async()=>{const x=setup('deployment_failed');await assert.rejects(run(x));assert.equal(x.calls.filter(c=>c.method==='POST').length,1);});
test('pending bounded at24 GETs and one POST',async()=>{const x=setup('pending');await assert.rejects(run(x));assert.equal(x.calls.length,25);});

test('exact source commit is deployment build version',async()=>{const x=setup();await run(x);assert.equal(JSON.parse(x.calls[0].body).pages_build_version,receipt.sourceHead);});
test('official temporary statuses keep bounded read-only polling',async()=>{for(const value of ['unknown_status','not_found','deployment_attempt_error','new_pending_status']){const x=setup(value);await assert.rejects(run(x),/bounded deadline/);assert.equal(x.calls.filter(c=>c.method==='POST').length,1);assert.equal(x.calls.filter(c=>c.method==='GET').length,24);}});

test('delayed OIDC cannot create after receipt expiry',async()=>{const x=setup();let time=now();x.now=()=>time;x.core.getIDToken=async()=>{time+=90001;return 'fixture-oidc';};await assert.rejects(run(x),/expired during OIDC/);assert.equal(x.calls.length,0);});
