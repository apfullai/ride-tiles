// Local-reviewed REST continuation. Exactly one create call; no retry/reupload.
const fs = require('node:fs');
const BASE = 'https://api.github.com/repos/apfullai/ride-tiles/pages/deployments';
const ID = 12096579037;
const HEAD = '4711212d901d10cf0ff1c100625798729c9748e5';
async function run({core, fetchImpl=fetch, now=Date.now, sleep=ms=>new Promise(r=>setTimeout(r,ms)), receipt=JSON.parse(fs.readFileSync('continuation-receipt.json','utf8')), env=process.env}) {
  if (env.GITHUB_REPOSITORY !== 'apfullai/ride-tiles' || env.GITHUB_REF !== 'refs/heads/feat/mellow-z17-mirror' || env.GITHUB_RUN_ATTEMPT !== '1' || !/^\d+$/.test(env.GITHUB_RUN_ID||'')) throw Error('Unexpected execution identity');
  if (receipt.artifactId!==ID || receipt.sourceHead!==HEAD || receipt.sourceRun!==39022469343 || receipt.main!=='ae037d0d4e37e82314a679006ebe4d86bc0497f5' || receipt.digest!=='sha256:7c6616506c78a1f5226243bbff1c66b73aa9df844ea43fbef18876ecb11af558' || receipt.bytes!==201912458 || receipt.capacity?.allowed!==true || receipt.capacity?.complete!==true) throw Error('Invalid admission receipt');
  const age=now()-Date.parse(receipt.checkedAt);
  if (!Number.isFinite(age)||age<0||age>90000) throw Error('Stale admission receipt');
  let oidc;try{oidc=await core.getIDToken();}catch{throw Error('OIDC issuance failed');}
  const postOidcAge=now()-Date.parse(receipt.checkedAt);
  if(!Number.isFinite(postOidcAge)||postOidcAge<0||postOidcAge>90000)throw Error('Admission expired during OIDC issuance');
  async function request(url,method,body) {
    const response=await fetchImpl(url,{method,redirect:'error',signal:AbortSignal.timeout(10000),headers:{Authorization:`Bearer ${env.GH_TOKEN}`,Accept:'application/vnd.github+json','Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'},...(body?{body:JSON.stringify(body)}:{})});
    if (!response.ok) throw Error(`Deployment request failed HTTP ${response.status}; do not retry blindly`);
    const chunks=[];let size=0;
    for await(const chunk of response.body){size+=chunk.length;if(size>65536){throw Error('Oversized deployment response');}chunks.push(chunk);}
    try{return JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw Error('Invalid deployment response JSON');}
  }
  // This is the sole mutation; all polling below is GET. Unknown outcomes stop.
  const result=await request(BASE,'POST',{artifact_id:ID,pages_build_version:HEAD,environment:'github-pages',oidc_token:oidc});
  if(typeof result.id!=='string'||!/^[A-Za-z0-9-]{1,128}$/.test(result.id))throw Error('Unrecognized deployment ID; reconcile manually');
  core.info(`Pages deployment created: ${result.id}`);
  const start=now();
  for(let i=0;i<24 && now()-start<150000;i++) {
    await sleep(5000);
    const status=await request(`${BASE}/${result.id}`,'GET');
    if(status.status==='succeed'){core.info(`Pages deployment succeeded: ${result.id}`);return {id:result.id,status:'succeed'};}
    if(typeof status.status!=='string'||!/^[a-z_]{1,64}$/.test(status.status))throw Error('Malformed deployment status; reconcile manually');
    // Match official deploy-pages: only documented final failures terminate;
    // other valid status strings remain unresolved within the same GET budget.
    if(['deployment_failed','deployment_perms_error','deployment_content_failed','deployment_cancelled','deployment_lost'].includes(status.status))throw Error(`Deployment did not succeed (${status.status}); reconcile manually`);
  }
  throw Error(`Deployment ${result.id} unresolved at bounded deadline; reconcile before another action`);
}
module.exports={run};
