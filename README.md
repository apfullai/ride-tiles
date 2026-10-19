# ride-tiles
Map tiles

## Public cycling tiles

A budget-bounded, gradually expanding GitHub Pages pilot serving public OpenStreetMap cycling-infrastructure tiles for a personal, noncommercial map. This repository contains only standalone tile-generation code, configuration, documentation and public map data. It does not contain Ride application source or anyone's private routes.

The pilot includes an exact Chicagoland mirror (bounds -89, 41.05, -86.65, 43.25; content version `19785f3c1ca31233`) plus a compact New York City rectangle: west -74.05, south 40.65, east -73.85, north 40.90, at zooms 8–14. Coverage is not nationwide. Only the published `manifest.json` declares usable coverage, and a failed build must not replace the previous successful deployment.

## Data and attribution

© [OpenStreetMap contributors](https://www.openstreetmap.org/copyright). Public OSM data and derived databases are licensed under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). Each published region includes provenance, source SHA-256 hashes, replication timestamps, tile hashes and a generation report. New regions include the filtered public OSM source extract; the Chicagoland mirror includes its entire public tile database archive and upstream-source provenance. Preserve attribution when displaying the map. This data is indicative infrastructure tagging, not a guarantee of legal access, safety, or current conditions.

Standalone code is MIT-licensed; this does not relicense the OSM data.

## Gradual rollout and refresh

The authoritative publisher is `.github/workflows/rollout.yml`. Metadata-only checks run at 02:23 and 18:23 UTC daily. Monday at 02:23 may select eligible work; every other slot only resumes an active city. A temporary 10:23 UTC slot on October 17–19, 2026 may acquire remaining sources only for the exact preserved Illinois generation, before its October 20 deadline. It exits early for another generation, a completed source stage, or any later year. The builder independently repeats this restriction against the restored checkpoint before advancing, so stale advisory metadata cannot authorize a different stage. GitHub cron may be delayed, so this does not guarantee the deadline. Manual dispatch or a deliberate `config/rollout-request.json` change may request an admitted run. Ordinary source pushes only verify code. Legacy publishers cannot deploy.

Each admitted run advances one durable coarse checkpoint: a pinned state crop, canonical index, time-bounded group of tile batches, or final promotion. One city is active at a time. The active map manifest stays unchanged until the entire replacement validates; a crash can lose only the current unpublished stage. The next run verifies saved hashes and resumes. See [the operating guide](GRADUAL-ROLLOUT.md), [live progress](https://apfullai.github.io/ride-tiles/status.html), and [the city queue](CITY-ROLLOUT-QUEUE.md). Existing mutable coverage is selected oldest-due first with a 90-day target, subject to budget; neither daily completion nor coverage of all 50 cities is guaranteed.

The broad `chicagoland` entry may advance to newer generated tiles. The original `19785f3c1ca31233` directory and exact `chicago-metro` `8d555e92cfa68b53` mirror remain byte-for-byte frozen. Every earlier published version, region, and public OSM source archive is retained until the 250 MB cumulative site ceiling stops further growth. No pruning occurs.

Before tool installation or bulk recovery, a metadata-only capacity gate checks the published manifest and snapshot inventory against every unexpired repository artifact. A blocked run records its reason and earliest potential retry in the Actions summary. Expiry timestamps are estimates, not proof that bytes are released; the API must report artifacts expired. Missing or invalid metadata fails closed. Passing this gate establishes only baseline capacity: the restored baseline and final upload remain independently checked.

The monthly admission guard uses public GitHub Actions job history, counts failed and repeated attempts, rounds every job up, and reserves the full 22-minute job-timeout envelope before admitting work against a 100-minute threshold. It fails closed if accounting is unavailable. A strict total Actions-minute cap is impossible inside Actions: repeated rejected/manual gate runs still consume runner time. This guard prevents further costly admitted rollout work when it cannot fit; it is not an account-wide billing limit.

Only standard `ubuntu-24.04` runners are used. Monthly publishing admission remains 100 minutes, including all visible retries and rounded job time; twice-daily gates themselves may use about 60–62 rounded minutes/month. Jobs remain capped at 15 minutes, with process/stage watchdogs and time reserved to save progress. Full tests are reused only with successful GitHub CI and exact source-input proof; every continuation runs focused safety checks. Source downloads retain 1.5 GB/file and 3 GB/run limits. Published files, historical versions, sources, checkpoints and nonrecursive recovery shards must fit 250 MB; repository artifacts have a separate 250 MB ceiling. No automatic historical-data pruning, new credentials, paid runners or caches are introduced. Pages transport artifacts last one day; durable progress does not depend on them.

Durable recovery comes from `snapshot.json` plus hash-pinned transport shards published with the site, independently of expiring Actions artifacts. Corrupt or unavailable recovery data, stale live-manifest races, invalid regions, and over-budget candidates never replace current coverage. The initial checked-in snapshot is used only when the live descriptor is absent (HTTP 404) and the complete live manifest matches that known snapshot.

## VM-first development and verification

Run development builds, bulk-data experiments and unit checks on the development VM before publishing source changes. Use the existing Python tools and test suites from a complete checkout, including its checked-in bootstrap archives and fixtures:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s publishing/tests -v
```

Keep build experiments in fresh, disposable VM output/work directories. For an optional legacy pilot generation, install the official distribution's `osmium-tool` and use:

```sh
.venv/bin/python tools/build_region.py --region-config config/pilot-regions.json --output site-dev --work build-dev
```

That command downloads public OSM inputs and can use substantial time and disk. It writes a local candidate only; it does not publish or resume the live checkpoint. Do not use its legacy limits as production admission limits, upload its output as a release, or replace the current published manifest with it.

Use the VM for the edit/build/test loop rather than repeatedly dispatching GitHub Actions or changing `config/rollout-request.json` to test development changes. Run final local checks and review their results before pushing. Existing source-triggered `snapshot-tests.yml` verification and the scheduled, guarded `rollout.yml` maintenance publisher remain in place. Local test success does not replace the exact CI proof required for production test reuse. The Ride application's existing Cloudflare automation is separate and remains unchanged.

The current operating instructions in this README and [GRADUAL-ROLLOUT.md](GRADUAL-ROLLOUT.md) supersede the historical no-cron/manual-publication advice in `publishing/README.md`. Both legacy publisher workflows are retired and cannot deploy.

## Publication credentials

Publication uses the existing repository's short-lived `GITHUB_TOKEN` and Pages OIDC permissions only. Same-repository public API reads use the existing job token to avoid shared-runner anonymous rate limits; it is never persisted or sent to Pages, source hosts, other repositories, or redirects. No paid runner, new secret, private repository, custom domain or Cloudflare changes are introduced. GitHub Pages publication is public; the production Ride app and its Cloudflare regional release are separate.

## Manifest contract

`manifest.json`: `schemaVersion: 1`, `updatedAt`, `attribution`, `license`, `regions` array. Each region has `id`, `name`, `bounds` (west/south/east/north), `minzoom`, `maxzoom`, `sourceLayer`, `tiles` (URL-template array), `version`, `generatedAt`, `sourceUrl` (provenance), `sourceTimestamp`, `categories`, `tileCount`, `tileBytes`, `sha256Manifest`, and `sourceDownload`. New generated regions use `sourceFormat: "osm-pbf-chunks-v1"`; `sourceDownload` points to a hash-pinned descriptor with ordered <=8 MB chunks and the reconstructed PBF hash. Concatenate the listed chunks in order and verify the declared byte count and SHA-256. Legacy source URLs remain unchanged. Tile paths include a version identifier. Missing/invalid manifest regions must not be treated as coverage.

## Repository migration

This repository retains the new repository's original README and clean commit history; historical source commits are not imported. The one-shot seed restores the exact pinned public snapshot from the former `apfullai/rides-tiles` origin, relocates only manifest URLs, and preserves every other data file and the active Illinois checkpoint. It refuses an already-published destination and rechecks both origins before deployment. Full standalone CI for the exact source tree is required.

The old publisher was paused at 2026-10-15 15:29:38 UTC. A complete post-pause audit carries 28 rounded publishing job-minutes into the unchanged October 100-minute limit; the seed reserves the same 22 minutes as a normal rollout. This is a project admission metric, not billed GitHub minutes. A complete post-pause API audit of all 23 old-repository runs confirmed zero remaining artifacts at 2026-10-15 21:24:11 UTC. The ledger records this closeout so future updates do not depend on the old repository; new-repository artifacts remain audited against the same 250 MB guard. Expiry timestamps alone never release capacity.

Complete all migration ledger and workflow changes, then verify CI, before the first checkpoint continuation. These inputs are fingerprinted. Scheduling is enabled after the new publication and consumer gate verify; every admitted update remains capacity- and budget-gated. The temporary historical cron can be removed after the active checkpoint completes, avoiding a mid-generation fingerprint change.

The October 16 migration closeout accounts for 33 rounded publishing minutes (28 carried, 2 for the failed seed, 3 for the successful seed). At most 32 remaining regular slots plus 3 temporary slots add up to 35 typical one-minute gate charges, or at most 70 at their two-minute timeouts; slots missed before activation reduce these totals. Three one-minute build/deploy source stages would add 6 more minutes (illustrative total 74, not a guarantee). Actual slow stages count in full; each admission still reserves 22 minutes against 100. Cheap rejected checks also consume minutes, so this is an admission guard rather than an absolute billing cap.
