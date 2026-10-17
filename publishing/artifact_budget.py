#!/usr/bin/env python3
"""Fail closed before a Pages upload would exceed 250 MB of repo artifacts.

Sum ALL artifacts reported as unexpired, including old pilot artifacts and
failed-run artifacts. Reserve the fixed-format GNU tar size plus 1% and 64 KiB
for ZIP transport overhead; never rely on compression savings. The metadata
formula includes every file/directory header, 512-byte file padding, GNU long
name records, EOF blocks and final record padding. The rollout upload additionally
packs and verifies that exact tar before a fresh artifact audit.
No artifacts are deleted. An existing job token may authenticate same-repo reads.

The publishing workflows must share ride-tiles-pages concurrency and no process
may mutate the candidate between this check and upload. This is a conservative
admission guard for visible repository artifacts, not an account-level storage
or billing limit; it does not claim to measure caches or concurrent uploads
outside that publishing lock. Existing artifacts are counted until the API
marks them expired, even if their expiry timestamp is already in the past.

Public artifact metadata is readable without authentication or actions:read:
https://docs.github.com/en/rest/actions/artifacts#list-artifacts-for-a-repository
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import time

from actions_budget import BudgetUnavailable, _pages, fetch_public_json, artifact_repositories


CAP_BYTES = 250_000_000  # Decimal MB: conservative relative to 250 MiB.
TAR_BLOCK_BYTES = 512
TAR_RECORD_BYTES = 10_240
UPLOAD_OVERHEAD_BYTES = 65_536
MAX_AUDIT_SECONDS = 45
MAX_API_REQUESTS = 10


def _round_up(value, block):
    return ((value + block - 1) // block) * block


def candidate_allowance(site):
    """Inspect a directory without following links; return conservative sizes."""
    from exact_artifact import inventory
    from exact_tar_size import allowance
    records = inventory(site)
    return allowance({name: record['bytes'] for name, record in records.items()},
                     [p.relative_to(site).as_posix() for p in Path(site).rglob('*') if p.is_dir()])


def check_artifact_budget(repository, site, fetch=None):
    """Return allowed/usage report. fetch(url) may inject decoded public JSON."""
    report = {
        "allowed": False,
        "capBytes": CAP_BYTES,
        "artifactBytes": None,
        "candidateBytes": None,
        "projectedBytes": None,
        "complete": False,
        "reason": "artifact history unavailable",
    }
    try:
        if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise BudgetUnavailable("invalid repository")
        report.update(candidate_allowance(site))
        source = fetch if fetch is not None else fetch_public_json
        started = time.monotonic()
        requests = 0

        def read(url):
            nonlocal requests
            requests += 1
            if requests > MAX_API_REQUESTS or time.monotonic() - started > MAX_AUDIT_SECONDS:
                raise BudgetUnavailable("artifact API audit request/time limit reached")
            return source(url)

        repositories = artifact_repositories(repository)
        artifacts = []
        for source_repository in repositories:
            artifacts.extend(_pages(read, f"https://api.github.com/repos/{source_repository}/actions/artifacts", "artifacts"))
        report["artifactRepositories"] = repositories
        used = 0
        unexpired_count = 0
        for artifact in artifacts:
            size = artifact.get("size_in_bytes")
            expired = artifact.get("expired")
            if type(size) is not int or size < 0 or type(expired) is not bool:
                raise BudgetUnavailable("malformed artifact size or expiration state")
            if not expired:
                used += size
                unexpired_count += 1
        projected = used + report["candidateBytes"]
        report.update({
            "artifactBytes": used,
            "unexpiredArtifactCount": unexpired_count,
            "projectedBytes": projected,
            "allowed": projected <= CAP_BYTES,
            "complete": True,
            "reason": "within artifact allowance" if projected <= CAP_BYTES
                      else "artifact allowance cannot cover the candidate upload",
        })
    except Exception as exc:
        report["reason"] = f"artifact budget unavailable: {type(exc).__name__}: {exc}"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--site", type=Path, default=Path("site"))
    args = parser.parse_args(argv)
    report = check_artifact_budget(args.repository, args.site)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"allowed={str(report['allowed']).lower()}\n")
            for key, name in (("artifactBytes", "artifact_bytes"), ("candidateBytes", "candidate_bytes"),
                              ("projectedBytes", "projected_bytes")):
                stream.write(f"{name}={report[key] if report[key] is not None else 'unknown'}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("## Artifact storage admission\n\n```json\n" + json.dumps(report, indent=2, sort_keys=True) + "\n```\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
