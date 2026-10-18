# Source-attempt receipts: integration contract

This module is local implementation work; it does not change workflow permissions,
create any artifact or cache, edit deployments, or download source data.

## Baseline and bootstrap

1. Validate the existing live publication and original checkpoint descriptor bytes.
2. Compute the reviewed/current code fingerprint and independently authorize any
   exact checkpoint migration. Do not mutate baseline bytes before migration.
3. Call `baseline_from_checkpoint(raw_bytes, publication_revision, fingerprint)`.
   The helper accepts `attemptHistoryOrigin={runId,attempt,commit}` from an
   already-validated published checkpoint. The only legacy exception is the exact
   6,051-byte descriptor with SHA-256
   `79336c510d58f660178ff7e33dd0c638aec6be41f6f6c070d5edf1f95d75efe5`, published by
   run `39134231112`, attempt 1, commit
   `ae037d0d4e37e82314a679006ebe4d86bc0497f5`.
4. Carry that checkpoint's existing failure count 1, attention false and retry
   `2026-10-19T10:04:14.579685+00:00`; the old publisher run is exempted once, by
   exact ID/attempt/commit, after checking successful main-rollout API metadata.
   Its checkpoint time precedes job completion and must not double-count failure.

## Admission and observer

Call `audit_history(baseline, run_id=int(GITHUB_RUN_ID), attempt=int(GITHUB_RUN_ATTEMPT),
commit=GITHUB_SHA)`. A source attempt requires both `complete` and `allowed` true.
On explicit denial, emit `emit_no_attempt(baseline, admission, run_id=..., attempt=...,
commit=...)` before returning `changed=false`. This records that no source was
invoked without inventing or resetting failure state.

After an allowed audit, construct `AttemptObserver(baseline, admission, run_id=...,
attempt=..., commit=...)` and pass it as the `attempt_observer` callback to advance.
Apply admission's failureCount/retryAfter/needsAttention to the disposable in-memory
checkpoint after exact migration; never rewrite the restored baseline merely to
apply the overlay. The observer accepts `started`, `failed`, `progress`, `paused`.
Call `observer.finish_if_unstarted()` only after a normal return from advance.
An exception/kill deliberately leaves an incomplete receipt and prevents retries.

For no-progress failure, the observer increments the audit's cumulative count,
sets retry to terminal time + 20 hours, requests attention after failure 3 (or a
permanent failure), and updates the in-memory checkpoint so the returned status
matches the receipt. A successful no-progress planned pause retains all prior
controls. A partial prefix with an error emits `progress`, because it is unsafe
to assume it reached the live site.

A no-progress source attempt must return `changed=false` without sealing,
replacing or uploading the original snapshot. Progress publications must include
`attemptHistoryOrigin={runId,attempt,commit}` in the new checkpoint. Include this
anchor for every newly published pinned-source checkpoint from an initial `start`
stage too, including pin-only failures, even when that initial stage is outside
history auditing. A later invocation may advance
the boundary only after observing and validating the new live descriptor and
verifying the exact origin run succeeded. No future revision is embedded in the
checkpoint, avoiding circular hashes.

## Trusted evidence and bounded failure behavior

The scan verifies repository/head repository, main branch, allowed trigger,
workflow path/ID, SHA, run/attempt/job identity, and complete pagination. It checks
contiguous workflow run numbers from origin to current across all branches;
otherwise deleted runs could disappear from a superficially complete API total.
Only matching reviewed code, publication revision, generation, full source-plan
hash and stage receipts count. Other branches contribute run-number coverage but
are not source-attempt evidence. An executed source step without an unambiguous
terminal receipt is denied. Unpublished progress remains unresolved while the
old baseline is live. The exact initial legacy publisher is never counted twice.

Limits: fewer than 1,000 listed items, at most 4 attempts per run, 30 HTTP requests,
25 seconds plus a maximum 3-second in-flight network read, 1 MiB per metadata/log
response, 8 MiB combined, 8 KiB per receipt, one anonymous validated log redirect.
The existing outer workflow timeout remains the hard wall-clock stop. Expired,
deleted, unavailable, oversized or incomplete evidence is a conservative denial.
Normal retention is unchanged. Job summary is a human-readable mirror; receipt
recovery reads existing job logs, not summary HTML or a new storage service.

The default metadata transport uses the existing constrained read-token helper.
Log requests and their download redirect never carry that token. Supported log
hosts are GitHub's `productionresultssaN.blob.core.windows.net` and
`pipelines.actions.githubusercontent.com`; an unrecognized new host fails closed
and requires review. Signed download URLs are not logged or retained.

## Verification and limitation

Run `python -m unittest discover -s publishing/tests -p test_attempt_receipts.py -v`.
These focused fixture tests cover failure/backoff arithmetic, exact bootstrap
shape, missing/deleted/duplicated history, unrelated branches, source identity,
partial/unpublished progress, deferred/paused attempts, caps, and anonymous
redirect handling. The live anonymous download path has not been executed in
this local task. Official documentation states public job logs are readable
without authentication and return a short-lived 302 download URL:
https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run

## Acquisition and publication limits

The build job is bounded to 24 minutes. Its first step establishes a shared
1,380-second monotonic clock, leaving one minute below the job ceiling. The
publisher keeps 190 seconds outside its process for termination, exact packing,
upload, post-upload audit and cleanup, plus 90 seconds internally for validation
and sealing. Setup, restore, history and code verification consume that same
clock. Each source stage receives only actual remaining time, never a fresh
unconditional long timeout. At most two whole pinned source files may transfer
concurrently; each transfer is capped at 720 seconds and the shared deadline.
Raw files remain ephemeral, and crops are verified/committed in configured order.

The existing publisher-only monthly allowance remains 100 minutes. Reservations
are now 2 minutes for the budget job, 24 for build and 5 for deploy, totaling 31.
Test workflows remain outside that established publisher ledger; this does not
represent total account Actions usage. Storage admission remains 250 MB and no
artifact deletion or additional persistent storage is introduced.

More time and two connections do not guarantee provider throughput. A verified
partial prefix may be published if a later source fails; it is useful durable
work but its artifact retention can postpone the next acquisition beyond the
pinned source expiry. Expiry, source pins and the existing published failure
controls are never reset to hide that risk. No raw partial download is treated
as a complete source.

The targeted no-upload rule applies to existing pinned-source attempts with no
new completed source. Initial pin discovery and separately handled integrity
or freshness blockers retain their existing publication behavior. They are not
covered by a blanket claim that all metadata-only publications are eliminated.
