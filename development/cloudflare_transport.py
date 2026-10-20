#!/usr/bin/env python3
"""Publish transport preparation only: no build, Actions, credentials or traffic switch.

The original sealed site remains a logical GitHub-origin site. This module keeps
those bytes exact and packages them into two free-tier Cloudflare Pages uploads.
The separate transport manifest points to immutable deployments, not aliases.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "publishing"))
import rollout_snapshot as snapshot
import verified_code

MAX_FILES = 20_000
MAX_BYTES = snapshot.MAX_SITE_BYTES
ROOT_PROJECT = "ride-tiles-data"
MELLOW_PROJECT = "ride-tiles-mellow"
CONTROL_NAMES = {"_headers", "_redirects", "_worker.js", "_routes.json",
                 "functions", "404.html", "cloudflare-manifest.json",
                 "cloudflare-publication.json"}
HEADERS = """/*
  Access-Control-Allow-Origin: *
  Access-Control-Expose-Headers: Content-Length, ETag
  Cache-Control: no-store
  X-Content-Type-Options: nosniff
/*.pbf
  Content-Type: application/octet-stream
/*.json
  Content-Type: application/json
/*.tar.gz
  Content-Type: application/gzip
"""
NOT_FOUND = b"<!doctype html><title>Not found</title><p>Tile asset not found.</p>\n"


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode()


def write_json(path, value):
    path.write_bytes(canonical(value))


def regular_directory(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlinked path is not allowed")
    if not path.is_dir():
        raise ValueError("Input must be a directory")
    return path


def immutable_url(value, project):
    if not isinstance(value, str) or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("Whitespace/control characters in deployment URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != parsed.hostname
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or not re.fullmatch(r"[0-9a-f]{8}\." + re.escape(project) + r"\.pages\.dev",
                                parsed.hostname or "")):
        raise ValueError("Expected the exact immutable deployment URL for " + project)
    return value.rstrip("/")


def tree_hash(records):
    return snapshot.digest(canonical(records))


def inspect_site(site):
    """Validate full production data and every exact recovery transport byte."""
    site = regular_directory(site)
    records = snapshot._records(site, include_metadata=True)
    if any(name.split("/")[0] in CONTROL_NAMES for name in records):
        raise ValueError("Canonical site contains Cloudflare control files")
    manifest = (site / "manifest.json").read_bytes()
    raw = (site / "snapshot.json").read_bytes()
    desc = snapshot._descriptor(raw, manifest)
    expected = dict(desc["files"])
    expected["snapshot.json"] = {"size": len(raw), "sha256": snapshot.digest(raw)}
    archive = hashlib.sha256()
    for shard in desc["shards"]:
        expected[shard["path"]] = {k: shard[k] for k in ("size", "sha256")}
        archive.update((site / shard["path"]).read_bytes())
    if expected != records or archive.hexdigest() != desc["archiveSha256"]:
        raise ValueError("Canonical site or recovery bytes differ from sealed inventory")
    snapshot.validate_site(site)
    return records, {
        "schemaVersion": 1,
        "kind": "exact-byte-transport",
        "logicalOrigin": snapshot.BASE,
        "sourcePublicationRevision": snapshot.publication_revision(manifest, raw),
        "productionFingerprint": verified_code.current_fingerprint(ROOT),
        "canonicalInventorySha256": tree_hash(records),
        "canonicalFiles": len(records),
        "canonicalBytes": sum(x["size"] for x in records.values()),
        "canonicalManifestSha256": snapshot.digest(manifest),
        "canonicalSnapshotSha256": snapshot.digest(raw),
        "mapRefreshClaimed": False,
    }


def package_records(package):
    records = snapshot._records(package, include_metadata=True)
    if len(records) > MAX_FILES:
        raise ValueError("Cloudflare free-tier file count exceeded")
    return records


def verify_package(output):
    output = regular_directory(output)
    receipt = snapshot._json((output / "receipt.json").read_bytes())
    if receipt.get("schemaVersion") != 1 or receipt.get("kind") != "exact-byte-transport":
        raise ValueError("Invalid transport receipt")
    canonical_records = snapshot._json((output / "canonical-inventory.json").read_bytes())
    if tree_hash(canonical_records) != receipt["canonicalInventorySha256"]:
        raise ValueError("Canonical inventory changed")
    fields = {"schemaVersion", "kind", "logicalOrigin", "sourcePublicationRevision",
              "productionFingerprint", "canonicalInventorySha256", "canonicalFiles",
              "canonicalBytes", "canonicalManifestSha256", "canonicalSnapshotSha256",
              "mapRefreshClaimed", "state", "mellowDeploymentUrl", "dataDeploymentUrl"}
    if set(receipt) - fields:
        raise ValueError("Unexpected receipt fields")
    manifest = (output / "root/manifest.json").read_bytes()
    descriptor = (output / "root/snapshot.json").read_bytes()
    asserted = {
        "logicalOrigin": snapshot.BASE,
        "canonicalFiles": len(canonical_records),
        "canonicalBytes": sum(r["size"] for r in canonical_records.values()),
        "canonicalManifestSha256": snapshot.digest(manifest),
        "canonicalSnapshotSha256": snapshot.digest(descriptor),
        "sourcePublicationRevision": snapshot.publication_revision(manifest, descriptor),
        "mapRefreshClaimed": False,
        "productionFingerprint": verified_code.current_fingerprint(ROOT),
    }
    if any(type(receipt.get(k)) is not type(v) or receipt.get(k) != v for k, v in asserted.items()):
        raise ValueError("Receipt assertions differ from exact canonical inputs")
    combined, all_records = {}, {}
    total = 0
    for part in ("root", "mellow"):
        records = package_records(output / part)
        all_records[part] = records
        total += sum(x["size"] for x in records.values())
        for name, item in records.items():
            if name in canonical_records:
                if name in combined:
                    raise ValueError("Canonical file duplicated between packages")
                if (part == "mellow") != name.startswith("mellow/"):
                    raise ValueError("Canonical file is in the wrong package")
                combined[name] = item
            elif name not in CONTROL_NAMES:
                raise ValueError("Unexpected transport file: " + name)
    if combined != canonical_records:
        raise ValueError("Canonical files changed or disappeared")
    if total > MAX_BYTES:
        raise ValueError("Combined site exceeds unchanged 250 MB ceiling")
    for part in ("root", "mellow"):
        if ((output / part / "_headers").read_text() != HEADERS
                or (output / part / "404.html").read_bytes() != NOT_FOUND):
            raise ValueError("Transport response controls changed")
        allowed = {"_headers", "404.html"} | ({"_redirects", "cloudflare-manifest.json",
                   "cloudflare-publication.json"} if part == "root" else set())
        if any(name in CONTROL_NAMES and name not in allowed for name in all_records[part]):
            raise ValueError("Executable or unexpected Cloudflare control file")
    state = receipt.get("state")
    if state not in ("prepared", "shard-bound", "publication-bound"):
        raise ValueError("Invalid transport state")
    root = output / "root"
    if state == "prepared":
        if any((root / p).exists() for p in ("_redirects", "cloudflare-manifest.json",
                                           "cloudflare-publication.json")):
            raise ValueError("Unexpected bound metadata")
    else:
        mellow = immutable_url(receipt["mellowDeploymentUrl"], MELLOW_PROJECT)
        if (root / "_redirects").read_text() != "/mellow/* " + mellow + "/mellow/:splat 302\n":
            raise ValueError("Shard redirect changed")
        if state == "publication-bound":
            data = immutable_url(receipt["dataDeploymentUrl"], ROOT_PROJECT)
            expected_manifest = transport_manifest(snapshot._json(
                (root / "manifest.json").read_bytes()), data)
            if snapshot._json((root / "cloudflare-manifest.json").read_bytes()) != expected_manifest:
                raise ValueError("Transport manifest changed")
            if snapshot._json((root / "cloudflare-publication.json").read_bytes()) != publication(receipt):
                raise ValueError("Publication receipt changed")
        elif any((root / p).exists() for p in ("cloudflare-manifest.json",
                                             "cloudflare-publication.json")):
            raise ValueError("Unexpected publication metadata")
    return {**receipt, "state": state, "files": {p: len(r) for p, r in all_records.items()},
            "combinedBytes": total, "packageInventorySha256":
            {p: tree_hash(r) for p, r in all_records.items()}}


def prepare(site, output):
    records, receipt = inspect_site(site)
    site = Path(site).absolute()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output must not already exist")
    if output == site or site in output.parents or output in site.parents:
        raise ValueError("Input and output must be disjoint")
    if any(p.is_symlink() for p in output.parents):
        raise ValueError("Symlinked output parent is not allowed")
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        staging = Path(temporary) / "package"
        staging.mkdir()
        for part in ("root", "mellow"):
            (staging / part).mkdir()
            (staging / part / "_headers").write_text(HEADERS)
            (staging / part / "404.html").write_bytes(NOT_FOUND)
        for name in records:
            part = "mellow" if name.startswith("mellow/") else "root"
            target = staging / part / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(site / name, target)
        receipt["state"] = "prepared"
        write_json(staging / "receipt.json", receipt)
        write_json(staging / "canonical-inventory.json", records)
        verify_package(staging)
        staging.rename(output)
    return verify_package(output)


def transport_manifest(manifest, data_url):
    result = json.loads(json.dumps(manifest))
    for region in result["regions"]:
        prefix = snapshot.BASE + "/regions/" + region["id"] + "/" + region["version"] + "/"
        def relocate(value):
            if not isinstance(value, str) or not value.startswith(prefix):
                raise ValueError("Region URL differs from preserved identity")
            return data_url + value[len(snapshot.BASE):]
        for field in ("sourceUrl", "sourceDownload", "sha256Manifest"):
            region[field] = relocate(region[field])
        region["tiles"] = [relocate(value) for value in region["tiles"]]
    return result


def publication(receipt):
    return {k: receipt[k] for k in ("schemaVersion", "kind", "logicalOrigin",
            "sourcePublicationRevision", "productionFingerprint", "canonicalInventorySha256",
            "canonicalFiles", "canonicalBytes", "canonicalManifestSha256",
            "canonicalSnapshotSha256", "mapRefreshClaimed", "mellowDeploymentUrl",
            "dataDeploymentUrl")}


def bind(output, mellow_url, data_url=None):
    """Bind staged metadata locally; never deploy an interrupted/invalid package."""
    original = verify_package(output)
    mellow_url = immutable_url(mellow_url, MELLOW_PROJECT)
    if data_url:
        data_url = immutable_url(data_url, ROOT_PROJECT)
    output = Path(output)
    if original["state"] != "prepared" and original["mellowDeploymentUrl"] != mellow_url:
        raise ValueError("Shard URL is already pinned")
    if original["state"] == "publication-bound":
        raise ValueError("Published package is immutable; prepare a new output")
    receipt = snapshot._json((output / "receipt.json").read_bytes())
    receipt.update(mellowDeploymentUrl=mellow_url,
                   state="publication-bound" if data_url else "shard-bound")
    root = output / "root"
    # An interrupted bind fails verification until explicitly recovered/rebuilt.
    # No deployment is performed here, so incomplete staging cannot affect users.
    (root / "_redirects").write_text("/mellow/* " + mellow_url + "/mellow/:splat 302\n")
    if data_url:
        receipt["dataDeploymentUrl"] = data_url
        manifest = snapshot._json((root / "manifest.json").read_bytes())
        write_json(root / "cloudflare-manifest.json", transport_manifest(manifest, data_url))
        write_json(root / "cloudflare-publication.json", publication(receipt))
    write_json(output / "receipt.json", receipt)
    return verify_package(output)


def transport_fetch(data_url, fetch=None):
    """Logical origin stays pinned; actual downloads retain same-origin checks."""
    data_url = immutable_url(data_url, ROOT_PROJECT)
    fetch = fetch or snapshot._fetch
    def get(url, maximum):
        prefix = snapshot.BASE + "/"
        if not url.startswith(prefix):
            raise ValueError("Unknown logical origin")
        path = snapshot._path(url[len(prefix):])
        if "?" in path or "#" in path or "%" in path:
            raise ValueError("Ambiguous transport path")
        return fetch(data_url + "/" + path, maximum)
    return get


def restore(data_url, destination, expected_revision):
    snapshot._hash(expected_revision)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore destination must not exist")
    if any(p.is_symlink() for p in destination.parents):
        raise ValueError("Symlinked destination parent")
    fetch = transport_fetch(data_url)
    snapshot.assert_publication_unchanged(expected_revision, fetch=fetch)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "site"
        receipt = {}
        result = snapshot.restore_published(staging, fetch=fetch, receipt=receipt)
        if receipt["bootstrap"] or receipt["publicationRevision"] != expected_revision:
            raise ValueError("Unexpected restored publication")
        snapshot.assert_publication_unchanged(expected_revision, fetch=fetch)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Restore destination appeared during verification")
        staging.rename(destination)
    return {**receipt, "regions": len(result["regions"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--site", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("verify")
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("bind")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--mellow-url", required=True)
    p.add_argument("--data-url")
    p = sub.add_parser("restore")
    p.add_argument("--data-url", required=True)
    p.add_argument("--destination", type=Path, required=True)
    p.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.site, args.output)
    elif args.command == "verify":
        result = verify_package(args.output)
    elif args.command == "bind":
        result = bind(args.output, args.mellow_url, args.data_url)
    else:
        result = restore(args.data_url, args.destination, args.expected_revision)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
