# Bounded US cycling-tile maintenance

Status: design only. Do not enable recurring execution until the public-hosting pilot, client fallback tests, and initial publication have passed. No analytics collection is part of this design.

## Goals and limits

Publish public OpenStreetMap-derived cycling infrastructure for a growing set of US urban cycling catchments. Keep the existing local-provider → GitHub → OpenFreeMap fallback chain usable during maintenance failures. Never copy private application history, routes, credentials, usage data, or user information into this repository.

Use standard `ubuntu-24.04` GitHub-hosted runners in this public repository. Public-repository standard runners are free; this does not make storage, bandwidth, repository growth, or operational impact unlimited. Do not dispatch the private application repository, use larger runners, activate a paid service, or add a personal access token. Use job-scoped `GITHUB_TOKEN` permissions and the normal Pages deployment identity.

Initial guardrails:

- One serialized maintenance job at a time; no parallel city matrix and no automatic retry loop.
- One bounded admission opportunity weekly, plus one monthly maintenance opportunity. Each admitted job has a 15-minute timeout, including validation and publication. These are opportunities, not promises that all cities are rebuilt on each date.
- At most six automatic opportunities per calendar month, nominally at most 90 job-minutes. A 100-minute monthly planning budget includes a safety margin. Reruns, admission failures, setup overhead, and manual runs must be measured too; this is not a provider-enforced billing cap.
- An admission check reads prior runs/attempts and reserved workload before expensive downloads. Missing or incomplete history fails closed. Manual reruns may not bypass the gate. A repository kill switch disables maintenance admission.
- Published site must stay below 800,000,000 bytes, leaving margin under the 1 GB Pages limit. Set an independent source-repository growth alarm; do not commit successive generated tile snapshots to Git history.
- Keep only bounded current and previous validated publication snapshots; include both in the size calculation. Keep temporary diagnostics/artifacts briefly, normally 3 days, and avoid a second full copy when a Pages artifact already exists.
- Stop expansion on quota warnings, uncertain public visibility, unexpected runner type, publisher permission changes, unexplained source growth, validation failure, or exceeded projected maintenance cost.

The six-opportunity schedule itself bounds scheduled workload. Before coding the exact accounting gate, verify GitHub's run-attempt and job API semantics and count all jobs. Failed or rejected runs still consume some execution time; never claim an absolute account-wide hard cap from application logic.

## Freshness takes priority over expansion

Every coverage region has a source timestamp, last successful checked timestamp, last successful generated timestamp, and measured maintenance cost. A source check counts as freshness only when it proves the selected upstream source is unchanged; do not relabel old data merely because a workflow ran.

Target a maximum 90-day age for source checks and updates. Each admission first selects overdue or soon-due maintenance. New-city work is admitted only if existing regions can still be maintained inside the projected budget. Prefer a common state extract and several nearby regions when measured runtime permits, but do not launch an unbounded regional matrix.

If maintenance would exceed available runtime, pause expansion automatically. Preserve the last-known-good tile data, flag stale coverage in the manifest, and let the client fall through to its broader fallback when required by its freshness policy. Raising budgets or relaxing freshness is a separate explicit decision. Fifty fresh cities within 100 minutes/month has not been demonstrated.

Start with two or three representative additional cities and measure download bytes, extract time, generation time, published bytes, tile count, first-view bytes, and source-change rate. Extrapolate from measured results before enlarging the queue. Revisit the 15-minute job budget if a legitimate pilot cannot complete; do not silently increase it or retry indefinitely.

## Selecting US regions

Use Census Vintage 2025 `SUB-IP-EST2025-ANNRNK`, ranked July 1, 2025 incorporated-place population, to seed fifty candidate city names and stable IDs. Record the source URL and checksum. Municipal population rank determines initial priority; actual published coverage is an explicit cycling catchment with a reviewed bounding box and source-state list. Merge overlapping catchments and shared tiles rather than duplicating them. Keep candidate city identity distinct from the coverage-region identity.

Existing coverage and maintenance always precede expansion. No demand analytics is available in this first version; do not claim population priority represents measured use. Later approved aggregate demand could reorder candidates without publishing private demand data.

## Proposed build and publication contract

The pilot publisher and client have agreed the following manifest shape. Further changes must preserve their shared contract.

Builder interface: `build_region.py --region-config config/pilot-regions.json --output <clean-staging-directory>`. The configuration supplies coverage-region ID, reviewed bounds, source-state slugs, zoom range 8–14, pinned generator/dependency versions, and upstream source checksums/timestamps. The initial NYC pilot bounds are [-74.05, 40.65, -73.85, 40.9], using the New York state extract. This small pilot is not a claim of complete New York City coverage.

Builder output: deterministic raw MVT files, source layer `cycleinfra`, an immutable content identifier, and a report containing tile count/bytes, maximum tile size, feature-category counts, input checksums, source timestamps, and generator revision. Input state extracts are reused across nearby regions. Unchanged input plus unchanged generator parameters skips generation.

Agreed manifest fields: `schemaVersion: 1`, `updatedAt`, `attribution`, and a `regions` array. Each region entry contains `id`, `name`, `bounds`, `minzoom`, `maxzoom`, `sourceLayer`, a `tiles` URL-template array, `version`, `generatedAt`, `sourceUrl`, and a `categories` array of the six infrastructure kinds. Add `sourceTimestamp`, `tileCount`, `tileBytes`, and `sha256Manifest` for freshness and integrity accounting. Keep coverage and pending candidates separate. No personal data or private repository references appear in public provenance.

Use stable OSM feature IDs, deterministic ordering/compression, and per-region or smaller content hashes. Do not derive asset URLs from archives containing volatile timestamps. Unchanged tile payloads retain URLs. Timestamp-only metadata updates must not invalidate the full map cache.

Stage the complete next site, run validation and size checks, then publish atomically. Never replace the last-known-good manifest before all referenced files exist. Missing or corrupt candidate data leaves current production intact. Persist queue progress only after publication verification. Do not use expiring Actions artifacts as the only long-lived source of existing regions: recover from the last published manifest and its enumerated files or another verified durable snapshot before rebuilding the full Pages artifact. If recovery fails, stop; do not deploy a partial site that silently drops existing regions.

Use the normal custom Pages Actions workflow with `pages: write` and `id-token: write` only on the deployment job. Ordinary generation needs read-only repository access. Any state-writing permission must be narrow and justified. Do not make commits to the private application to announce tile updates; its client discovers the stable manifest.

## Availability and operational checks

Cloudflare and GitHub resources remain shared services. Public-runner usage avoids the private repository's billed-minutes concern, but cannot guarantee no effect on other applications. GitHub Pages has a 100 GB/month soft bandwidth limit and may rate-limit traffic. If direct bandwidth measurements are unavailable, label traffic headroom unknown; do not fabricate an enforced bandwidth budget.

Verify anonymous GET, CORS, correct binary response, real MVT decoding, immutable URLs, absent-tile behavior, manifest compatibility, and fallback under forced GitHub failure. Avoid requiring HTTP Range for raw MVT delivery. Monitor Pages publication and surface failed runs through ordinary repository Actions status. Public scheduled workflows may be disabled after 60 days without repository activity; document that lifecycle and verify the enabled state after maintenance changes, rather than promising indefinite execution.

## Official references

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions): public standard runners and plan/storage distinctions.
- [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits): 1 GB published site, recommended source-repository size, 100 GB monthly soft bandwidth, 10-minute Pages deployment timeout, rate limits, and permitted-use restrictions.
- [Scheduled-workflow lifecycle](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows): automatic disablement after public-repository inactivity.
- [Census city population ranking](https://www.census.gov/data/tables/time-series/demo/popest/2020s-total-cities-and-towns.html): Vintage 2025 incorporated-place population ranking.
- [Census ranking workbook](https://www2.census.gov/programs-surveys/popest/tables/2020-2025/cities/totals/SUB-IP-EST2025-ANNRNK.xlsx).

