#!/usr/bin/env python3
"""Fail-closed admission guard for the public tile-publishing workflows.

This is an elapsed runner-minute guard, not GitHub billing data. Each completed
job in every visible attempt is rounded up separately. A job crossing the UTC
month boundary is charged in full in the current month (conservative). The
current gate is covered by the full 2 + 24 + 5 minute reservation rather than
counted twice. All other completed jobs, including failures and prior attempts,
are counted. A competing unfinished publishing run prevents admission.

The workflow must use the shared ride-tiles-pages concurrency group, enforce
budget/build/deploy timeouts of 2/24/5 minutes, and prohibit rerunning only failed
build/deploy jobs with an old successful gate. Future publishers must be added
to PUBLISHING_WORKFLOWS. Deleted/expired run history cannot be reconstructed.
Unlimited dispatches or gate-only reruns can themselves consume minutes, so a
literal total Actions spending cap cannot be enforced by an Actions job.

Public REST reads require no token or actions:read permission:
https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository
https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener


CAP_MINUTES = 100
JOB_TIMEOUT_MINUTES = {"budget": 2, "build": 24, "deploy": 5}
RESERVED_MINUTES = sum(JOB_TIMEOUT_MINUTES.values())
PUBLISHING_WORKFLOWS = frozenset({
    ".github/workflows/rollout.yml",
    ".github/workflows/pilot-pages.yml",
    ".github/workflows/append-metro.yml",
})
ROLLOUT_WORKFLOW = ".github/workflows/rollout.yml"
PER_PAGE = 100
# Deliberately bounded even for the unfiltered endpoint. Filtered run searches
# have a documented 1,000-result ceiling; do not silently accept truncation.
MAX_LIST_ITEMS = 1000
MAX_REQUESTS = 50
MAX_AUDIT_SECONDS = 80
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ACTIVE_STATUSES = frozenset({
    "queued", "in_progress", "waiting", "pending", "requested",
})


MIGRATION_SOURCE_REPOSITORY = "apfullai/rides-tiles"
MIGRATION_TARGET_REPOSITORY = "apfullai/ride-tiles"
MIGRATION_MONTH = "2026-10"
MIGRATION_LEDGER_PATH = Path(__file__).resolve().parents[1] / "config/migration-budget.json"


class BudgetUnavailable(ValueError):
    """The public history cannot establish a safe admission decision."""


class NoApiRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BudgetUnavailable("API redirects are not allowed")


def api_read_headers(url):
    """Optional existing job token is confined to this repository's API reads."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.github.com"
            or parsed.fragment or not parsed.path.startswith("/repos/")):
        raise BudgetUnavailable("unexpected API destination")
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "ride-tiles-actions-budget"}
    token = os.environ.get("TILE_GITHUB_READ_TOKEN")
    target_path = "/repos/" + MIGRATION_TARGET_REPOSITORY + "/"
    endpoint = parsed.path.removeprefix(target_path)
    permitted = re.fullmatch(
        r"actions/(?:artifacts|runs(?:/[1-9][0-9]*(?:/attempts/[1-9][0-9]*/jobs)?)?|"
        r"workflows/snapshot-tests\.yml/runs)|git/trees/[0-9a-f]{40}", endpoint)

    if (token and os.environ.get("GITHUB_REPOSITORY") == MIGRATION_TARGET_REPOSITORY
            and parsed.path.startswith(target_path) and permitted):
        headers["Authorization"] = "Bearer " + token
    return headers


def fetch_public_json(url):
    """Read bounded public metadata; never follow a credential-bearing redirect."""
    if not url.startswith("https://api.github.com/repos/"):
        raise BudgetUnavailable("unexpected API destination")
    request = Request(url, headers=api_read_headers(url))
    with build_opener(NoApiRedirects()).open(request, timeout=5) as response:
        if response.status != 200 or response.geturl() != url:
            raise BudgetUnavailable("unexpected API response or redirect")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise BudgetUnavailable("API response exceeds audit limit")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise BudgetUnavailable("API response is not an object")
    return result


def _positive_integer(value, label):
    if type(value) is not int or value < 1:
        raise BudgetUnavailable(f"invalid {label}")
    return value


def _timestamp(value, label):
    if not isinstance(value, str) or not value:
        raise BudgetUnavailable(f"missing {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BudgetUnavailable(f"invalid {label}") from exc
    if parsed.tzinfo is None:
        raise BudgetUnavailable(f"timezone missing from {label}")
    return parsed.astimezone(timezone.utc)


def read_migration_ledger(now=None):
    """Read an explicit checked-in cutover record; absence never grants a reset.

    Pending records carry no runtime approval and no artifact-release evidence.
    Finalization requires the old publisher paused, a complete runtime audit,
    and optionally a separately observed zero-artifact closeout.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise BudgetUnavailable("migration audit clock requires a timezone")
    path = Path(MIGRATION_LEDGER_PATH)
    if path.is_symlink() or not path.is_file():
        raise BudgetUnavailable("migration budget ledger is missing or not a plain file")
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise BudgetUnavailable("migration budget ledger exceeds size limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BudgetUnavailable("duplicate migration ledger field")
            result[key] = value
        return result
    ledger = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(ledger, dict) or set(ledger) != {"schemaVersion", "sourceRepository",
            "targetRepository", "month", "state", "oldPublisherPaused", "publisherPausedAt", "runtime", "artifacts"}
            or type(ledger.get("schemaVersion")) is not int
            or ledger["schemaVersion"] != 1
            or ledger.get("sourceRepository") != MIGRATION_SOURCE_REPOSITORY
            or ledger.get("targetRepository") != MIGRATION_TARGET_REPOSITORY
            or ledger.get("month") != MIGRATION_MONTH
            or ledger.get("state") not in ("pending", "finalized")
            or type(ledger.get("oldPublisherPaused")) is not bool):
        raise BudgetUnavailable("invalid migration budget ledger identity or state")
    if ledger["state"] == "pending":
        if (ledger["oldPublisherPaused"] or ledger.get("publisherPausedAt") is not None
                or ledger.get("runtime") is not None or ledger.get("artifacts") is not None):
            raise BudgetUnavailable("pending migration ledger cannot certify usage or closeout")
        return ledger
    if not ledger["oldPublisherPaused"]:
        raise BudgetUnavailable("finalized migration ledger requires paused old publisher")
    paused = _timestamp(ledger.get("publisherPausedAt"), "publisher pause")
    runtime = ledger.get("runtime")
    if (not isinstance(runtime, dict) or set(runtime) != {"complete", "usedMinutes", "observedAt", "evidenceRunIds"}
            or runtime.get("complete") is not True):
        raise BudgetUnavailable("migration runtime audit is incomplete")
    used = runtime.get("usedMinutes")
    runs = runtime.get("evidenceRunIds")
    if (type(used) is not int or used < 0 or not isinstance(runs, list) or not runs
            or any(type(identifier) is not int or identifier < 1 for identifier in runs)
            or len(set(runs)) != len(runs)):
        raise BudgetUnavailable("invalid migration runtime usage or run evidence")
    observed = _timestamp(runtime.get("observedAt"), "runtime audit")
    if not paused <= observed <= now or observed.strftime("%Y-%m") != MIGRATION_MONTH:
        raise BudgetUnavailable("migration runtime audit must follow pause in the pinned month")
    artifacts = ledger.get("artifacts")
    if artifacts is not None:
        if (not isinstance(artifacts, dict) or set(artifacts) != {"complete", "unexpiredArtifactBytes", "unexpiredArtifactCount", "observedAt"}
                or artifacts.get("complete") is not True
                or type(artifacts.get("unexpiredArtifactBytes")) is not int
                or artifacts["unexpiredArtifactBytes"] != 0
                or type(artifacts.get("unexpiredArtifactCount")) is not int
                or artifacts["unexpiredArtifactCount"] != 0):
            raise BudgetUnavailable("migration artifact closeout must certify zero bytes and count")
        observed = _timestamp(artifacts.get("observedAt"), "artifact closeout")
        if not paused <= observed <= now:
            raise BudgetUnavailable("migration artifact closeout must follow publisher pause")
    return ledger


def migration_runtime_minutes(repository, now):
    """Preserve the old October usage after old public history becomes unavailable."""
    if repository != MIGRATION_TARGET_REPOSITORY:
        return 0
    ledger = read_migration_ledger(now)
    if ledger["state"] != "finalized":
        raise BudgetUnavailable("migration runtime ledger is pending; pause and audit old publisher first")
    month = now.astimezone(timezone.utc).strftime("%Y-%m")
    if month < MIGRATION_MONTH:
        raise BudgetUnavailable("runtime audit clock precedes migration month")
    return ledger["runtime"]["usedMinutes"] if month == MIGRATION_MONTH else 0


def artifact_repositories(repository, now=None):
    """Carry old storage until a checked-in, API-verified zero closeout exists."""
    if repository != MIGRATION_TARGET_REPOSITORY:
        return [repository]
    ledger = read_migration_ledger(now)
    if ledger["state"] == "finalized" and ledger.get("artifacts") is not None:
        return [repository]
    return [repository, MIGRATION_SOURCE_REPOSITORY]


def _pages(fetch, endpoint, key):
    """Verify every numbered page against a stable total, not just page one."""
    items = []
    seen = set()
    total = None
    page = 1
    while True:
        payload = fetch(endpoint + "?" + urlencode({"per_page": PER_PAGE, "page": page}))
        if not isinstance(payload, dict):
            raise BudgetUnavailable(f"malformed {key} response")
        count = payload.get("total_count")
        values = payload.get(key)
        if type(count) is not int or count < 0 or not isinstance(values, list):
            raise BudgetUnavailable(f"malformed {key} pagination")
        if count >= MAX_LIST_ITEMS:
            raise BudgetUnavailable(f"{key} reaches the {MAX_LIST_ITEMS}-item audit ceiling")
        if total is None:
            total = count
        elif total != count:
            raise BudgetUnavailable(f"{key} total changed during pagination")
        expected = min(PER_PAGE, total - len(items))
        if len(values) != expected:
            raise BudgetUnavailable(f"incomplete {key} pagination")
        for value in values:
            if not isinstance(value, dict):
                raise BudgetUnavailable(f"malformed {key} entry")
            identifier = _positive_integer(value.get("id"), f"{key} id")
            if identifier in seen:
                raise BudgetUnavailable(f"duplicate {key} entry")
            seen.add(identifier)
            items.append(value)
        if len(items) == total:
            return items
        page += 1


def _completed_minutes(job, month_start, now):
    if job.get("status") != "completed":
        raise BudgetUnavailable("unfinished historical job")
    # A conditionally skipped job can have inverted administrative timestamps
    # even though no runner was allocated (observed in job 116472264917).
    # Require explicit empty steps and all four explicit null runner fields;
    # missing fields, assigned runners, or executed steps are not this exception.
    runner_fields = ("runner_id", "runner_name", "runner_group_id", "runner_group_name")
    if (job.get("conclusion") == "skipped" and job.get("steps") == []
            and all(name in job and job[name] is None for name in runner_fields)):
        return 0
    # Also preserve the existing explicit null/null skipped timestamp shape.
    # Missing timestamps on an executed/failed job still fail closed.
    if (job.get("conclusion") == "skipped"
            and "started_at" in job and job["started_at"] is None
            and "completed_at" in job and job["completed_at"] is None):
        return 0
    started = _timestamp(job.get("started_at"), "job started_at")
    completed = _timestamp(job.get("completed_at"), "job completed_at")
    if completed < started or completed > now:
        raise BudgetUnavailable("inconsistent job timestamps")
    # Include the full duration, not only the post-boundary fraction. A job
    # ending exactly at the boundary consumed no time in this month.
    if completed <= month_start:
        return 0
    return math.ceil((completed - started).total_seconds() / 60)


def check_budget(repository, run_id, attempt, now=None, fetch=None):
    """Return an admission report; any missing evidence yields allowed=False.

    ``fetch`` is injectable: callable(absolute_url) -> decoded JSON dictionary.
    The scan is unfiltered by creation time: an old-created run can be rerun in
    this month. All attempts of a relevant run are requested explicitly, even
    when the latest attempt was successful. Runs whose last update predates
    this month can be safely skipped after validating their run timestamps.
    ``usedMinutes`` is None when the accounting could not be completed.
    """
    report = {
        "allowed": False,
        "capMinutes": CAP_MINUTES,
        "usedMinutes": None,
        "reservedMinutes": RESERVED_MINUTES,
        "complete": False,
        "reason": "budget history unavailable",
        "workflows": sorted(PUBLISHING_WORKFLOWS),
    }
    try:
        if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise BudgetUnavailable("invalid repository")
        run_id = _positive_integer(run_id, "run id")
        attempt = _positive_integer(attempt, "run attempt")
        now = now if now is not None else datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise BudgetUnavailable("now must be a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        report["month"] = month_start.strftime("%Y-%m")
        carried_minutes = migration_runtime_minutes(repository, now)
        report["carriedMinutes"] = carried_minutes
        source = fetch if fetch is not None else fetch_public_json
        started_audit = time.monotonic()
        request_count = 0

        def read(url):
            nonlocal request_count
            request_count += 1
            if request_count > MAX_REQUESTS or time.monotonic() - started_audit > MAX_AUDIT_SECONDS:
                raise BudgetUnavailable("public API audit request/time limit reached")
            return source(url)

        endpoint = f"https://api.github.com/repos/{repository}/actions/runs"
        runs = _pages(read, endpoint, "workflow_runs")
        current = next((run for run in runs if run["id"] == run_id), None)
        if current is None:
            raise BudgetUnavailable("current run missing from public history")
        used = carried_minutes
        counted_jobs = set()
        current_job_names = set()
        gate_seen = False
        for run in runs:
            path = run.get("path")
            if not isinstance(path, str) or not path:
                raise BudgetUnavailable("workflow path missing from run history")
            path = path.split("@", 1)[0]
            if path not in PUBLISHING_WORKFLOWS:
                if run["id"] == run_id:
                    raise BudgetUnavailable("current workflow is not the rollout")
                continue
            latest_attempt = _positive_integer(run.get("run_attempt"), "historical run attempt")
            created = _timestamp(run.get("created_at"), "run created_at")
            updated = _timestamp(run.get("updated_at"), "run updated_at")
            if updated < created or created > now or updated > now:
                raise BudgetUnavailable("inconsistent run timestamps")
            is_current = run["id"] == run_id
            status = run.get("status")
            if status not in ACTIVE_STATUSES | {"completed"}:
                raise BudgetUnavailable("unknown workflow run status")
            if is_current:
                if path != ROLLOUT_WORKFLOW or latest_attempt != attempt or status != "in_progress":
                    raise BudgetUnavailable("current run/attempt does not match the active rollout")
            elif status != "completed":
                raise BudgetUnavailable("another publishing run is unfinished")
            if updated < month_start:
                continue
            if latest_attempt > MAX_REQUESTS:
                raise BudgetUnavailable("run attempts exceed public API audit limit")
            for number in range(1, latest_attempt + 1):
                jobs = _pages(read, f"{endpoint}/{run['id']}/attempts/{number}/jobs", "jobs")
                if not jobs:
                    raise BudgetUnavailable("attempt job history is empty")
                for job in jobs:
                    if job.get("run_id") != run["id"]:
                        raise BudgetUnavailable("job belongs to an unexpected run")
                    if "run_attempt" in job and job["run_attempt"] != number:
                        raise BudgetUnavailable("job belongs to an unexpected attempt")
                    if job["id"] in counted_jobs:
                        raise BudgetUnavailable("duplicate job across run attempts")
                    counted_jobs.add(job["id"])
                    current_attempt = is_current and number == attempt
                    if current_attempt:
                        name = job.get("name")
                        if name not in JOB_TIMEOUT_MINUTES:
                            raise BudgetUnavailable("current rollout contains an unreserved job")
                        if name in current_job_names:
                            raise BudgetUnavailable("current rollout contains duplicate reserved jobs")
                        current_job_names.add(name)
                        if name == "budget" and job.get("status") == "in_progress":
                            if gate_seen:
                                raise BudgetUnavailable("multiple current budget jobs")
                            gate_started = _timestamp(job.get("started_at"), "gate started_at")
                            elapsed = (now - gate_started).total_seconds()
                            if not 0 <= elapsed <= JOB_TIMEOUT_MINUTES["budget"] * 60:
                                raise BudgetUnavailable("current gate exceeds its reserved timeout")
                            if job.get("completed_at") is not None:
                                raise BudgetUnavailable("active gate has a completion timestamp")
                            gate_seen = True
                            continue  # Already covered by the full current reservation.
                        if job.get("status") in {"queued", "waiting", "pending", "requested"}:
                            if job.get("started_at") is not None or job.get("completed_at") is not None:
                                raise BudgetUnavailable("queued current job has execution timestamps")
                            continue  # Future build/deploy are covered by the reservation.
                    used += _completed_minutes(job, month_start, now)
        if not gate_seen:
            raise BudgetUnavailable("active budget job missing from public history")
        report.update({
            "usedMinutes": used,
            "complete": True,
            "allowed": used + RESERVED_MINUTES <= CAP_MINUTES,
            "reason": "within monthly allowance" if used + RESERVED_MINUTES <= CAP_MINUTES
                      else "monthly allowance cannot cover the full rollout reservation",
        })
    except Exception as exc:
        # A closed gate is safer than a partial estimate, including on 403/429,
        # transport/JSON errors, unavailable old attempts, or schema changes.
        report["reason"] = f"budget unavailable: {type(exc).__name__}: {exc}"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--run-id", type=int, default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--attempt", type=int, default=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    args = parser.parse_args(argv)
    report = check_budget(args.repository, args.run_id, args.attempt)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"allowed={str(report['allowed']).lower()}\n")
            stream.write(f"used_minutes={report['usedMinutes'] if report['usedMinutes'] is not None else 'unknown'}\n")
            stream.write(f"reserved_minutes={report['reservedMinutes']}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("## Monthly rollout admission\n\n```json\n" + json.dumps(report, indent=2, sort_keys=True) + "\n```\n")
    print(json.dumps(report, sort_keys=True))
    return 0  # A denied rollout is an intentional, successful no-op.


if __name__ == "__main__":
    raise SystemExit(main())
