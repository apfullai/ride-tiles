#!/usr/bin/env python3
"""Bounded loose-file smoke checks, distinct from full snapshot restoration."""
import argparse
import concurrent.futures
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cloudflare_transport as transport


def samples(records):
    groups = {}
    for name in records:
        if name.startswith("regions/"):
            key = "/".join(name.split("/")[:3]) + ("/tiles" if "/tiles/" in name else "/metadata")
        elif name.startswith("mellow/"):
            key = "mellow/" + ("tiles" if name.endswith(".pbf") else "metadata")
        elif name.startswith("snapshot/"):
            continue  # All recovery shard bytes are verified by the restore gate.
        elif name.startswith("checkpoints/"):
            key = "checkpoints/" + ("metadata" if name.endswith(".json") else "sources")
        else:
            key = "root"
        groups.setdefault(key, []).append(name)
    result = set()
    for names in groups.values():
        names.sort()
        result.update((names[0], names[len(names) // 2], names[-1]))
    return sorted(result)


def fetch(url, maximum, *, expected_url=None):
    request = Request(url, headers={"User-Agent": "Ride-Transport-Verification/1",
                                    "Accept-Encoding": "identity",
                                    "Origin": "https://test.ride.ap0z.com",
                                    "Cache-Control": "no-cache"})
    with urlopen(request, timeout=45) as response:
        if response.geturl() != (expected_url or url):
            raise ValueError("Unexpected HTTP redirect")
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Response exceeds expected byte size")
        headers = dict(response.headers.items())
        if response.headers.get("Content-Encoding") not in (None, "identity"):
            raise ValueError("Unexpected HTTP byte transformation")
        if response.headers.get("Access-Control-Allow-Origin") != "*":
            raise ValueError("Missing public CORS response")
        if response.headers.get("Cache-Control") != "no-store":
            raise ValueError("Missing consistency cache policy")
        return data, headers


def verify(output, data_url, mellow_url):
    receipt = transport.verify_package(output)
    data_url = transport.immutable_url(data_url, transport.ROOT_PROJECT)
    mellow_url = transport.immutable_url(mellow_url, transport.MELLOW_PROJECT)
    records = json.loads((Path(output) / "canonical-inventory.json").read_text())
    def probe(name):
        base = mellow_url if name.startswith("mellow/") else data_url
        expected = records[name]
        data, headers = fetch(base + "/" + name, expected["size"])
        if len(data) != expected["size"] or transport.snapshot.digest(data) != expected["sha256"]:
            raise ValueError("Served bytes differ: " + name)
        lower = {k.lower(): v for k, v in headers.items()}
        if name.endswith(".pbf") and lower.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise ValueError("Unexpected PBF MIME type")
        return {"path": name, "bytes": len(data), "sha256": expected["sha256"],
                "contentType": lower.get("content-type"), "cors": "*",
                "cacheControl": lower.get("cache-control")}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        checked = list(executor.map(probe, samples(records)))
    for base in (data_url, mellow_url):
        missing = base + "/missing-tile-verification/0/0/0.pbf"
        try:
            fetch(missing, 1024)
        except HTTPError as error:
            if error.code != 404:
                raise
        else:
            raise ValueError("Missing tile did not return HTTP 404")
    name = next(name for name in records if name.startswith("mellow/") and name.endswith(".pbf"))
    data, _ = fetch(data_url + "/" + name, records[name]["size"],
                    expected_url=mellow_url + "/" + name)
    if transport.snapshot.digest(data) != records[name]["sha256"]:
        raise ValueError("Redirected mellow bytes differ")
    return {"schemaVersion": 1, "sampledLooseFiles": len(checked),
            "canonicalFiles": receipt["canonicalFiles"],
            "allLooseFilesChecked": False, "browserCorsThroughRedirectChecked": False,
            "missingPaths404": True, "mellowRedirectBytesMatch": True,
            "samples": checked, "dataDeploymentUrl": data_url,
            "mellowDeploymentUrl": mellow_url}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-url", required=True)
    parser.add_argument("--mellow-url", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.output, args.data_url, args.mellow_url), indent=2))
