# VM checkpoint continuation (candidate only)

This tooling prepares a local candidate from an already published, fully acquired
source checkpoint. It does not publish, upload an Actions artifact, dispatch a
workflow, or replace the public map. The scheduled publisher remains
`.github/workflows/rollout.yml`, with its existing runtime, storage, concurrency,
verification and deployment guards.

The VM performs the expensive index and tile work. Each command is bounded and
advances at most one coarse production stage. Further work requires another
explicit command. A completed local candidate is not a completed live refresh.

## Preconditions

- Use a complete checkout, including bootstrap archives, fixtures and research
  inputs. A sparse copy cannot establish the production code fingerprint.
- Use the exact runtime recorded by the checkpoint's source plan. This includes
  the Python patch version, implementation, Shapely, GEOS and the complete osmium
  version output. Do not change the source plan to match the VM.
- All configured state crops must already be acquired and verified. Only index,
  tile and finalization stages are allowed. This path cannot select a city,
  discover source pins, download missing raw sources or restart a generation.
- Supply the intended region, generation ID and production fingerprint when
  restoring. Read these from the verified published checkpoint and its source
  verification, not from an unrelated development run.
- Choose a new workspace outside the checkout, with sufficient free disk for
  the preserved baseline, current and previous candidates, and disposable work.

For the Chicagoland generation acquired on October 19, 2026, the preserved runtime
was CPython 3.12.15, Shapely 2.1.2, GEOS 3.13.1, osmium 1.16.0 and libosmium 2.20.0.
That is an example from this generation, not a replacement for validating the
actual source-plan runtime. A Python 3.12.14 VM must fail rather than silently
resume it. The usual project dependencies are in `requirements-dev.txt`.

## Commands

Run from the complete checkout using the matching Python environment:

The default and maximum worker budget is 900 seconds per command, including
validation, local commit and bulk cleanup. `--seconds` can lower it. Production
index/tile/finalization stages retain their existing 300-second ceiling and
finalization reserve. Descendant cancellation/reaping has a separate bounded
cleanup grace of up to 10 seconds; this is not an unlimited background build.

```sh
python development/vm_continue.py restore \
  --workspace /absolute/path/outside-the-checkout/chicagoland-candidate \
  --region chicagoland \
  --generation <verified-64-character-generation-id> \
  --fingerprint <verified-64-character-production-fingerprint>

python development/vm_continue.py continue \
  --workspace /absolute/path/outside-the-checkout/chicagoland-candidate

python development/vm_continue.py status \
  --workspace /absolute/path/outside-the-checkout/chicagoland-candidate
```

Restore uses `publishing/rollout_snapshot.py:restore_published` and the public
snapshot's hash-pinned recovery shards. It does not download an expiring Actions
artifact. It verifies the full site and checkpoint before creating local state.
The initial live manifest and snapshot revision remain the baseline for the
entire local continuation.

Continue calls `publishing/resumable_rollout.py:advance`, never its Actions-only
`main`. It retains production validation, exact code/runtime checks, stage
watchdogs, source-plan checks, deterministic ordering, historical-file
preservation, snapshot sealing and the 250,000,000-byte site ceiling. Index and
tile stages use `tools/resumable_region.py`. They can use fully acquired crops
after the source-acquisition deadline without downloading or replacing pins.

Status validates local state; it does not restore over an existing candidate.
The public status page continues to describe the last successful deployment.

## Safety and recovery

The workspace must start new or empty; an ownership marker prevents cleanup of
unrelated directories. It is single-writer locked. Immutable local revisions contain the
candidate site and metadata; a hash-bound pointer is replaced only after the new
revision validates. The published baseline, generation, source plan, runtime,
production fingerprint and wrapper hash remain pinned. Corrupt or incompatible
state fails closed.

Each continuation checks the live publication revision before and after local
work. If scheduled maintenance has changed the live snapshot, the old candidate
is not accepted for further continuation. Do not edit its baseline to force it
through; preserve it for reviewed recovery and restore the new live checkpoint
into a new workspace.

Timeouts, cancellation and stage failures must leave the last committed local
revision available. The Linux supervisor terminates and reaps its own worker
descendants, including production stage workers in separate process groups.
This applies while the supervisor is alive, including its watchdog and normal
interrupt handling. A supervisor killed with SIGKILL or lost with the VM cannot
guarantee descendant cleanup without a separate container/cgroup boundary. The
committed local pointer remains recoverable; inspect surviving processes before
resuming after that kind of failure. No such system boundary is installed here.
Orphan transactions and excess old local revisions are cleaned only inside this
owned workspace while locked. No published files or repository artifacts are
deleted. The baseline, current and previous committed local revisions are kept.
Restore and continuation require at least 1.5 GB free after safe recovery cleanup.
The supervisor stops work below 250 MB free or after a net filesystem free-space
drop greater than 2 GB. This conservative net-growth check can also be affected
by other VM activity; it is not a storage-service quota or a new publication
allowance. Each validated candidate still obeys the unchanged 250 MB site cap.
Status performs no network reads or revision pruning.

The wrapper and its tests live under `development/`, outside the existing
production fingerprint inputs. This keeps the pinned generator unchanged. The
wrapper has its own local metadata hash and must be reviewed separately. Changing
production modules, configuration, dependencies or workflow files still changes
the production fingerprint and blocks an incompatible resume. No synthetic
`GITHUB_RUN_ID`, `GITHUB_SHA`, token or CI proof is supplied by this tool.

## Publication is a separate, unresolved step

There is no VM-to-GitHub-Pages publisher in this tool. Do not upload the local
candidate as a release or point the application at its workspace.

The existing GitHub Pages publisher uploads a `github-pages` Actions artifact
and deploys it using a workflow-issued OIDC identity. Its current monthly runtime
and artifact admission checks remain in force. Moving bulk computation to the
VM does not itself provide an import transport, a deployment identity, or an
exemption from these checks. The separate Ride Cloudflare TEST application's
Wrangler path targets a different service and cannot publish this tile site.

A future publish-only/import path needs separate review of candidate transfer,
integrity and production verification, live-revision races, concurrency,
runtime/artifact admission, retained historical bytes and deployment identity.
Workflow files are themselves production fingerprint inputs, so adding an importer
also requires an explicit checkpoint-compatibility review; it cannot silently
relabel the preserved generation with a new fingerprint.
This change implements none of those external actions and changes no hosting,
domain, access, schedule, cap or production deadline.

GitHub's [Pages deployment API](https://docs.github.com/en/rest/pages/pages#create-a-github-pages-deployment)
requires an Actions-issued OIDC token and a repository artifact. Its public
[artifact REST API](https://docs.github.com/en/rest/actions/artifacts) does not
provide an artifact-upload operation. The official uploader obtains credentials
from the [Actions runtime](https://github.com/actions/toolkit/blob/main/packages/artifact/src/internal/shared/config.ts).
GitHub documents a [no-build upload/deploy job](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages#linking-separate-build-and-deploy-jobs),
but that still requires an authorized way to import the VM candidate and an
admitted Actions run. These are feasibility references, not an implemented or
approved publication path.

## Local verification

```sh
python -m unittest discover -s development/tests -v
```

These tests use bounded local fixtures and fake stages/processes. They must cover
integrity, incompatible runtimes, stale live revisions, atomic resume and process
cleanup without downloading sources, generating the full region or triggering
Actions. A passing wrapper suite is not evidence that the actual VM matches the
pinned runtime or that a complete city has been built or published.
