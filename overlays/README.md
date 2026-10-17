# Immutable overlay-only mirror proposal

No changes to ride-tiles main are required. Existing publisher restore/copy/reseal already carries arbitrary overlay files forward.

## Integration (release owner)

Use feature branch `feat/mellow-z17-mirror` based on exact main `c9dd4771e5670b2ab4fa9df1d1eb8b5e7122d5fa`.
Copy rollout.yml to `.github/workflows/rollout.yml` on that branch only. Copy these eight files to `overlays/`: merge_overlay.py, publish_overlay.py, source-pin.json, test_merge_overlay.py, test_archive.py, test_publisher_roundtrip.py, test_checkpoint_guard.py, README.md.
Commit source preparation without overlays/request.json, then trigger with a separate change to `overlays/request.json` and commit marker `[mellow-overlay]`. Do not dispatch until the pinned source ZIP is live. Do not merge this branch to main.

Trigger inputs are fixed in source-pin.json; no user-supplied URL or shell interpolation. Add request.json such as `{"version":"2bafe73bf6586407","request":1}`; its contents are a trigger only, never executed.

The feature workflow shares `ride-tiles-pages` concurrency with ordinary publication, uses unchanged budget/build/deploy job names and 2/15/5-minute bounds, existing 100-minute monthly budget and artifact-capacity guards. It checks out exact unmodified main into publisher/, separate from overlay-source/. No city-generation entrypoint executes. It restores the complete current snapshot, verifies immutable overlay files, preserves all prior non-transport file hashes (including checkpoints), reseals recovery metadata, and checks the live publication revision again immediately before atomic Pages deploy. The complete site must remain under existing 250MB site/artifact guards. Hash-pinned source ZIP is fetched only from canonical test.ride.ap0z.com; redirects rejected by NoRedirect before a redirected destination is requested.

If existing Pages environment restricts deployment to main, stop and report the blocker. Do not change permissions or merge workflow into main. If budget/capacity admission denies the run, stop and report rather than weaken it.

## Validation

`PUBLISHER_ROOT=/path/to/exact/main python -m unittest discover -s overlays -p 'test_*.py' -v`

Pure merge/archive tests plus real unmodified snapshot bootstrap → overlay merge → seal → restore → unchanged candidate copy/reseal → restore test. The fixture proves snapshot preservation; it does not generate or advance a city. Live current-checkpoint preservation is additionally verified by full pre/post inventory during actual preparation. Source fingerprint is unchanged before/after; active checkpoint fingerprint must match pinned source or pass only the existing pinned checkpoint_migrations.eligible() predicate for the exact preserved Illinois checkpoint. Eligibility never calls migrate() or rewrites checkpoint bytes; unknown mismatches fail.

Local full real payload extraction verified 14,312 files: 14,307 tiles, index, empty tile, manifest, two licenses. Source archive 13,885,264 bytes. All geometry remains exact decompressed archive data; no simplification or re-quantization. Transport snapshot.json and snapshot/ shards necessarily change; existing city manifest, state, verification and checkpoint files do not.

No local publication, CI trigger, account action or remote mutation has been performed by the author of this proposal.
