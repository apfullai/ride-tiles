# LOCAL ONLY: deploy-only continuation

This draft is not authorized for remote publication/execution until the legitimate
GitHub Pages branch-policy change and coordinator go-ahead are both confirmed.
No branch policy, credential or security setting is modified here.

Official deploy-pages v4 cannot select another run. This uses the documented REST
POST /repos/{owner}/{repo}/pages/deployments artifact_id + pages_build_version +
oidc_token contract instead. Cross-run server acceptance is unproven; a refusal
or uncertain response stops without a second POST, reupload or alternate identity.
https://docs.github.com/en/rest/pages/pages#create-a-github-pages-deployment
https://raw.githubusercontent.com/actions/deploy-pages/v4/src/internal/api-client.js

If approved, coordinator replaces ONLY the feature branch .github/workflows/rollout.yml
with this file and copies guard.py/deploy.cjs/test files to continuation/.
A separate continuation/request.json push plus commit marker
[continue-mirror-artifact-12096579037] triggers once. No main changes or mirror
rebuild/upload. The normal frozen publisher budget guard sees the same workflow
path and budget/deploy job names, reserving its unchanged conservative22minutes
although this continuation has only2+5minutes of runners. Shared ride-tiles-pages
concurrency and github-pages environment remain authoritative.

The guard checks the failed source run's successful budget/build and named steps,
pre-run deploy failure, exact main/head/artifact/repo/digest/size/expiry, complete
fresh artifact capacity audit <=250000000 and exact live manifest/snapshot/checkpoint
hashes. No artifact bytes are downloaded. Receipt contains only public metadata.
The deployment receives a fresh ephemeral Actions OIDC token via standard core API;
no token/body is printed or persisted. One POST, then <=24 GET polls with10s per
request and150s polling deadline; action step180s. Timeout/unknown results require
read-only reconciliation, never an automatic second deployment or deletion.

Tests: python3 -m unittest test_guard -v && node --test test_deploy.cjs
5 Python +8 Node tests passed, YAML/bash/Python syntax passed. Tests are local and
use injected fixture network responses only. Actual REST acceptance remains untested.

Reviewed official context.js uses a commit SHA for pages_build_version. This
continuation now sends exact source commit4711212d901d10cf0ff1c100625798729c9748e5.
The documented create response has a string id, validated before constructing
any status URL. Official deploy-pages stops on five named terminal failures and
continues polling other statuses; this draft now matches that distinction with
bounded, syntax-validated status strings. Unknown statuses never mean success.
https://raw.githubusercontent.com/actions/deploy-pages/v4/src/internal/context.js
https://raw.githubusercontent.com/actions/deploy-pages/v4/src/internal/deployment.js
Freshness is rechecked immediately after OIDC issuance and before the sole POST; a delayed token response cannot extend admission.
