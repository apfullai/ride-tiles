# Zero-Actions tile transport

This route uploads the existing, complete, sealed tile publication directly from
the VM with the already-authorized Wrangler installation. It does **not** build
new map data, change the production generator, replace GitHub Pages, switch the
Ride app, change DNS, or alter scheduled maintenance. Cloudflare transport is a
separate publication target. A successful mirror is not a completed Chicago
refresh.

## Why two projects

The October 20 preserved publication contains 25,388 files and 185,252,064 bytes,
including historical versions, public OSM source, the active checkpoint and
recovery shards. A single free Cloudflare Pages project supports 20,000 files.
The unchanged 250,000,000-byte combined site ceiling also applies to this route.
Do not delete old data or increase the ceiling to fit a deployment.

Two static-only, direct-upload projects are used:

- `ride-tiles-mellow`: all 14,312 original `mellow/` files.
- `ride-tiles-data`: all 11,076 other original files, including every recovery
  shard and the exact canonical `manifest.json` and `snapshot.json`.

Transport-only `_headers`, `404.html`, `_redirects` and the final two Cloudflare
metadata files add a few bytes/files, counted by the validator. There are no
Functions, Worker, R2/KV bindings, Git integration, custom domains or paid
features. The original 24,000,000-byte per-file bound remains stricter than
Cloudflare's 25 MiB bound.

The helper and tests live in `development/`, outside the generator fingerprint.
It never rewrites or relabels the source plan, runtime, checkpoint or CI proof.
It can package an existing validated snapshot without matching the generator's
Python patch version; actually **continuing** that generation still requires the
exact runtime documented in [README.md](README.md).

## Prepare and test

Use a complete checkout, including the hash-pinned bootstrap files. Restore the
current publication with `publishing/rollout_snapshot.py:restore_published` into
a new directory and retain its receipt. Recheck its full publication revision.
Do not restore over a candidate or use an expiring artifact.

```sh
python -m unittest discover -s development/tests -v
python -m unittest discover -s tests -v
python -m unittest discover -s publishing/tests -v
python development/cloudflare_transport.py prepare \
  --site /absolute/verified/site --output /absolute/new/package
python development/cloudflare_transport.py verify --output /absolute/new/package
```

Preparation verifies all canonical loose files, all recovery shard hashes, the
combined recovery archive hash, every current tile region, frozen Chicago
history, the full original site cap and both resulting free-tier file counts.
The output contains separate `root/` and `mellow/` upload directories plus a
local receipt and complete canonical inventory. Never upload the receipt's
parent directory, logs, repository source or credential storage.

## Verify identity and existing resources

Freshly inspect the intended Cloudflare account and project list before writes.
For the initial operation, the existing app projects must remain untouched.
Create the two named **direct-upload** projects only when absent, with production
branch `main`, no repository integration and no bindings. Stop on an unexpected
existing project, source integration, settings prompt or permission request.
Project creation uses ordinary free static hosting; it is not approval to add
paid services, credentials, domains, security settings or access grants.

Reuse the existing Wrangler storage without reading credential files:

```sh
export XDG_CONFIG_HOME=/workspace/shared/ride-wrangler-vm/auth-config
export WRANGLER=/workspace/shared/ride-wrangler-vm/node_modules/.bin/wrangler
export WRANGLER_SEND_METRICS=false
export CLOUDFLARE_ACCOUNT_ID=6e87a17c55383dc2900e7189642fcb1c
timeout -k5s 60s "$WRANGLER" pages project list
```

Do not perform a new login automatically. Existing CLI authorization does not
authorize new grants. Reconcile an uncertain upload through deployment metadata
before deciding whether to retry.

## Two-phase publication

Run deployment commands from a clean directory outside the checkout. Set the
actual verified remote source SHA; never label different source with an old SHA.
Before each upload, rerun package verification and the original GitHub source
publication-revision check. Stop on drift. No production or app traffic changes
occur here, and the scheduled publisher remains unchanged.

1. Upload `mellow/` to the dedicated mellow project as a preview deployment:

   ```sh
   timeout -k5s 180s "$WRANGLER" pages deploy /absolute/package/mellow \
     --project-name ride-tiles-mellow --branch data-<inventory-prefix> \
     --commit-hash <verified-source-sha> --commit-dirty=false
   ```

2. Verify its successful deployment metadata, project, branch, source commit,
   file-upload completion and returned immutable `https://<hash>.ride-tiles-mellow.pages.dev`
   URL. Verify representative PBF/JSON bytes and response headers on that URL.
   Bind that exact immutable URL, not a branch or project alias:

   ```sh
   python development/cloudflare_transport.py bind \
     --output /absolute/package --mellow-url https://<hash>.ride-tiles-mellow.pages.dev
   ```

3. Upload `root/` as a preview deployment in `ride-tiles-data`, with the same
   bounded command and explicit project, data branch and source SHA. Verify its
   deployment metadata and loose-file probes. The original canonical manifest
   still contains its original GitHub URLs by design.

4. Fully restore the canonical site through the immutable root deployment:

   ```sh
   python development/cloudflare_transport.py restore \
     --data-url https://<hash>.ride-tiles-data.pages.dev \
     --destination /absolute/new/roundtrip \
     --expected-revision <original-publication-revision>
   ```

   Compare the entire restored canonical inventory and active checkpoint hash
   with the original. The fetch adapter maps only the exact historical logical
   origin to that immutable transport and retains same-origin HTTP checks.
   Recovery shards never traverse the cross-origin mellow redirect.

   Run the bounded loose-file probe separately. It checks representative files
   from every historical region, checkpoint, root and mellow group, response
   headers, explicit missing-file 404s and the mellow redirect:

   ```sh
   timeout -k5s 600s python development/verify_cloudflare_http.py \
     --output /absolute/package \
     --data-url https://<hash>.ride-tiles-data.pages.dev \
     --mellow-url https://<hash>.ride-tiles-mellow.pages.dev
   ```

5. Bind the verified immutable root URL using `bind` again with `--data-url`.
   This adds `cloudflare-manifest.json` and `cloudflare-publication.json` while
   leaving every canonical byte unchanged. Upload the final `root/` package to
   branch `main` in the new data project only. The canonical Cloudflare manifest
   switch is atomic and points at the already-verified immutable data deployment,
   not a moving alias. No original manifest timestamps are changed.

6. Verify Cloudflare's canonical deployment ID and successful stage, commit and
   branch; read the canonical `cloudflare-publication.json` and compare all
   asserted facts. Recheck the original GitHub publication and app deployment
   IDs. Record new deployment IDs, URLs, package inventories and all results.

`bind` changes only the staged local package. If interrupted, `verify` fails
closed; reconstruct a fresh package from the preserved source and repeat the
explicit bindings. Never deploy a package after failed verification.

## HTTP and consumer constraints

- Every static response receives CORS `*`, `Cache-Control: no-store` and
  `nosniff`; this prioritizes consistency for the proof. Caching optimization is
  separate work. HTTP transport must not change stored bytes.
- PBF is served as `application/octet-stream`, because the publication contains
  both gzipped vector tiles and OSM PBF. No blanket `Content-Encoding: gzip` is
  added. The existing compressed tile bytes and their hashes remain unchanged.
- Explicit `404.html` prevents Cloudflare's default SPA fallback from returning
  HTML with 200 for missing tile paths. Verify real missing-path 404s on both.
- Pages canonicalizes HTML URLs (`/index.html` to `/`, for example). The smoke
  verifier allows only the expected same-origin HTML form and still verifies
  exact bytes. Tile, source, manifest and recovery paths retain strict URL checks.
- The root redirects `/mellow/*` with 302 to the exact immutable sibling.
  Cloudflare applies redirects before custom headers; do **not** claim this
  redirect is a verified browser CORS API. Use the direct pinned mellow URL from
  `cloudflare-publication.json` for future consumers and test their actual flow.
- The alternate Cloudflare manifest points only region URLs to immutable data.
  It does not change any app's configured manifest URL. Consumer migration,
  continued checkpoint publishing and handling another scheduled source update
  are separate decisions. Do not claim they are complete after this proof.
- Full snapshot restoration verifies all canonical data, but live loose-file
  smoke probes are samples. Record both claims accurately.

## Verified publication: October 20, 2026

The alternate publication is live at
[`cloudflare-manifest.json`](https://ride-tiles-data.pages.dev/cloudflare-manifest.json),
with transport identity in
[`cloudflare-publication.json`](https://ride-tiles-data.pages.dev/cloudflare-publication.json).
The [acceptance receipt](receipts/2026-10-20-cloudflare-direct-publish.json)
records immutable deployment URLs, source commits, inventories and limitations.

- All 25,388 canonical files (185,252,064 bytes) restored from Cloudflare match
  the original publication byte-for-byte, including the active checkpoint.
- 411 local tests passed; 39 representative live loose-file probes passed,
  including MIME, CORS, cache, 404 and redirect-byte checks.
- The canonical manifest switch points to already-verified immutable data.
  GitHub publication and the production app deployment were unchanged.
- No GitHub Actions were dispatched. No map generation or consumer migration
  is claimed. Browser CORS through the root-to-mellow redirect remains unverified.

## Authoritative references

Checked October 20, 2026:

- [Direct Upload and CLI limits](https://developers.cloudflare.com/pages/get-started/direct-upload/)
- [Pages file and file-size limits](https://developers.cloudflare.com/pages/platform/limits/)
- [Immutable deployments and branch aliases](https://developers.cloudflare.com/pages/configuration/preview-deployments/)
- [Redirect precedence and external-proxy restrictions](https://developers.cloudflare.com/pages/configuration/redirects/)
- [404 and SPA serving behavior](https://developers.cloudflare.com/pages/configuration/serving-pages/)
- [Wrangler Pages deploy](https://developers.cloudflare.com/workers/wrangler/commands/pages/#pages-deploy)
