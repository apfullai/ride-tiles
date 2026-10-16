# Resumable, budget-bounded US tile rollout

## Scheduling and admission

`.github/workflows/rollout.yml` is the sole publisher. Monday 09:23 UTC may select the oldest eligible due refresh, otherwise the next queued city. The other daily 09:23 UTC events only continue an active generation; the original cron event, not a possibly delayed runner's weekday, determines this mode. A fresh manual dispatch or deliberate `config/rollout-request.json` change can also request work. Ordinary code commits run verification only. `start_region`/`startRegion` explicitly selects an existing mutable region only when there is no active generation; it cannot replace blocked work or regenerate the exact metro mirror.

There is only one active city. It remains active until fully published or visibly blocked. Pausing for the monthly budget does not start another city or silently replace source pins. Monday selection retains the oldest-due 90-day target and legacy backoff metadata. No guarantee is made that 50 cities can be refreshed within 90 days at 100 minutes/month. The current client limit is 50 manifest entries, including the extra Chicago metro entry; the final 50th city needs a separately reviewed client change.

Each daily readiness gate itself consumes runner time (roughly one rounded minute), potentially about 30 minutes/month before useful work. A bounded public status read avoids a build when there is no active work, a retry is premature, or operator attention is needed. The builder independently validates every decision; public status is advisory, never permission to start a city midweek. There are no self-dispatch loops, paid runners or new credentials.

The existing admission ceiling remains 100 elapsed publishing runner-minutes per UTC month. Each completed job rounds up individually, including failures/retries; crossing-month jobs are conservatively charged to the later month. Every admitted run reserves all timeouts: budget 2 + build 15 + deploy 5 = 22 minutes. Unavailable/incomplete history, rate limits and competing publishers fail closed. Account-wide usage is unavailable. Rejected gates still consume time, so this is not an absolute account billing cap. Preserve current-month publisher history and do not repeatedly dispatch a denied run. Attempts > 1 cannot build/deploy using an old gate; use a fresh admitted run.

## Durable progress

The active descriptor and compact stage files live under `checkpoints/active/`, separate from the map's active `manifest.json`. Each completed coarse stage becomes durable only after a successful Pages deployment:

1. Discover a common dated source release and acquire a bounded group of filtered/cropped states.
2. Continue remaining pinned states in bounded groups, reusing verified completed crops.
3. Merge the sources and create a canonical global feature/tile index.
4. Encode a time-bounded group of disjoint tile batches using the same global feature IDs, ordering, clipping and properties as uninterrupted generation.
5. Validate the complete region, derive its immutable content version and atomically promote the manifest entry.

The tile stage groups many small batches within a bounded processing window; it does not require one deployment per 128 tiles. Source/index/tile receipts include exact hashes and output inventories. Temporary downloads and unfinished output stay in disposable work directories. A crash or failed upload can lose the current unpublished coarse stage, but not previously deployed checkpoints. Local batch completion alone is not durable. Expired transport artifacts can be recreated from the last published snapshot without restarting completed stages.

The process watchdog remains 540 seconds, with an earlier internal deadline and 90 seconds reserved for final validation/sealing. Stage process groups have independent hard limits; cooperative tile processing stops before them. Completed verification may be saved without beginning a stage when insufficient time remains. Runtime, byte, corruption or source failures preserve active coverage and publish bounded progress/error information when it is safe to do so. Transient stage failures wait 20 hours, allowing the next daily gate to retry, and require attention after three failed attempts. Permanent source, corruption, configuration or code-compatibility failures require attention immediately. Existing checkpoint bytes are preserved while the public status explains the blocker.

## Sources and deterministic resume

Only the existing public Geofabrik provider is used. Pin the newest common available dated release and its provider checksum, inspect its exact OSM replication timestamp before acquisition, then record full-source SHA256 and crop hashes after download. All states must have the same replication timestamp. A candidate must never be older than the currently published region. Never substitute `*-latest` after resume.

Dated files can disappear. Acquisition deadlines are conservative policy limits, not a promise of provider retention. If a missing source pin expires before acquisition, report the exact blocked stage; do not mix a later state or silently restart. Once all compact crops are durable, generation can continue without the upstream downloads. Geometry/export runtime fingerprints and generator/config fingerprints must match; changes block incompatible resume. Existing completed source/tiles remain available for explicit reviewed recovery.

Downloads retain 1.5 GB per-file and 3 GB per-run ceilings, 20-second connect and bounded transfer timeouts, and no automatic download retries. Metadata reads also count toward the run ledger. These are public OSM inputs, not private application or route data. The complete filtered regional source and provenance remain published for ODbL access.

## Code verification

A standalone successful `snapshot-tests.yml` run proves the full generator and publishing suites. Reuse requires the correct repository, main branch, allowed event, successful named suite steps and an identical Git blob inventory of tools, publishing code, tests/fixtures, bootstrap/research inputs, config, requirements and workflows. A receipt is only a lookup hint; a successful skipped/budget-denied publisher is not proof. Metadata-only request commits do not invalidate identical code.

Every continuation still runs nonempty focused resumability tests and validates all restored hashes, stage inventories and runtime compatibility. Missing/expired/unverifiable CI proof runs the full suites under the same absolute deadline. No unchecked source can skip testing merely by claiming a matching fingerprint.

## Publication and storage

Every published region/version and source archive is retained. Exact old broad Chicago 19785f3c1ca31233 and metro 8d555e92cfa68b53 remain immutable; the metro manifest remains exact. Cloudflare's separate release is unchanged. Intermediate deployments must leave active manifest bytes untouched. Only complete final validation changes active coverage.

`snapshot.json` hashes all data files and a deterministic archive split into 8 MB shards. Checkpoint files are included once; snapshot descriptors and recovery shards are never archived recursively. The full publication revision binds both active manifest and snapshot descriptor, so a newer checkpoint-only deployment cannot be overwritten by a stale publisher. Check revision before upload and deployment, with the existing shared concurrency lock. This is still a client-side check, not server-side compare-and-swap.

The 250,000,000-byte site ceiling includes old versions, sources, checkpoints and recovery transport. Every file stays below 24 MB. Baseline artifact headroom is checked before expensive work; the final exact artifact guard counts all unexpired repository artifacts plus a conservative tar estimate under 250 MB. A prior one-day artifact may still be unexpired at the next daily check, causing a safe delay. No artifacts are deleted, no caches are added, and no paid storage is enabled. Site history growth will eventually pause expansion rather than prune previous coverage automatically.

The only Actions artifact is one-day Pages transport. Durable progress comes from the published snapshot, not artifact retention. Public runner compute is free under GitHub's current policy, but account-shared artifact storage and other repositories' usage are not observable here; these limits are not a zero-cost guarantee.

## Observability and acceptance

`status.json` and `status.html` show city, actual pinned source date, verified sources completed/total, tile batches completed/total once known, next stage, retry time, errors and last completed version. Actions summaries and flushed phase logs include budget, bytes and progress. No analytics or fabricated completion percentages are used.

Tests cover interrupted/resumed raw tile equality, missing/duplicate batches, source expiry, mixed timestamps, changed runtime/config/code, corrupted inputs, byte/runtime/monthly exhaustion, stale checkpoint publishers and unchanged active/old regions. Run:

    python -m unittest discover -s tests -v
    python -m unittest discover -s publishing/tests -v

References: [Geofabrik retention and extracts](https://download.geofabrik.de/technical.html), [Osmium same-time merge requirement](https://docs.osmcode.org/osmium/latest/osmium-merge.html), [Actions public-runner billing](https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions), [Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits), [public workflow API](https://docs.github.com/en/rest/actions/workflow-runs), [artifact API](https://docs.github.com/en/rest/actions/artifacts).

## Chunked public source extracts and the first checkpoint migration

New completed regions expose `sourceFormat: "osm-pbf-chunks-v1"`.
`sourceDownload` points to the version's `cycling-source.json`, with the ordered
chunk paths, byte counts, per-chunk SHA-256, and full reconstructed PBF SHA-256.
Download those relative paths, concatenate them in listed order into
`cycling-source.osm.pbf`, and verify its declared byte count and SHA-256.
Each chunk is at most 8,000,000 bytes. The region's `sourceArchiveSha256` and
provenance `sourcePackageSha256` identify the descriptor; provenance
`sourceExtractSha256` identifies the reconstructed PBF. Existing single-file
source extracts and frozen archives retain their original URLs and bytes.
The app's tile URLs and manifest schema version do not change.

An admitted source stage may acquire a contiguous group of remaining pinned
states while its cooperative clock permits. The process watchdog and save
reserve still apply. A successful index stage verifies its packed merged PBF
and feature index, then removes redundant raw state-crop chunks only from the
disposable candidate. Compacted source receipts retain original source/crop
metadata and exact original receipt hashes, linked to the verified merged PBF
and index. The previously published checkpoint remains intact until the new
snapshot is deployed. Failure or interruption never changes that live snapshot.

One compatibility migration is explicitly allowlisted: the exact Illinois-only
checkpoint published by run `38619620372` from code `57493447e97a2957c11be6d1cf4d2241841d59c7`.
Its descriptor SHA-256 is
`48e41324d47dd639b268fa1cb36af97681a219e79a20b9f4ad3a8c7b434e82e3`.
The migration verifies its outer inventory, source receipt and streamed whole
crop identity, unchanged source acquisition/pinning/runtime/hash semantics and
dependencies, then requires successful current-code verification. Only the code
fingerprint and an explicit migration audit record change; generation, source
plan, runtime, progress, and existing crop bytes are preserved. Subsequent
ordinary stages advance that generation. Other code/config/runtime mismatches
remain blocked rather than silently reset or repinned.

Chunking removes a single-file limit; it does not increase the 250 MB site or
repository-artifact ceilings. A still-live one-day artifact can postpone the
next checkpoint, and the pinned daily sources can expire before all states are
acquired. These conditions still fail closed without deletion or a budget
increase. Successful metadata discovery does not guarantee a complete city will
fit the remaining storage or runtime allowance.
