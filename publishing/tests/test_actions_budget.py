import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "publishing"))
import actions_budget as budget

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
BASE = "/repos/example/tiles/actions/runs"


def timestamp(value):
    return value.isoformat().replace("+00:00", "Z")


def run(identifier, path="rollout", attempt=1, status="completed", created=None, updated=None):
    return {
        "id": identifier,
        "path": f".github/workflows/{path}.yml",
        "run_attempt": attempt,
        "status": status,
        "created_at": timestamp(created or NOW - timedelta(days=1)),
        "updated_at": timestamp(updated or NOW - timedelta(hours=1)),
    }


def job(identifier, run_id, seconds=1, name="build", conclusion="success", started=None):
    started = started or NOW - timedelta(hours=2)
    return {
        "id": identifier,
        "run_id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": timestamp(started),
        "completed_at": timestamp(started + timedelta(seconds=seconds)),
    }


class PublicHistory:
    """In-memory paginated API; no credentials or network in accounting tests."""

    def __init__(self):
        self.runs = [run(999, status="in_progress", created=NOW - timedelta(seconds=30), updated=NOW)]
        gate = job(9990, 999, name="budget", started=NOW - timedelta(seconds=30))
        gate.update(status="in_progress", conclusion=None, completed_at=None)
        self.jobs = {(999, 1): [gate]}
        self.calls = []

    def add(self, item, attempts):
        self.runs.append(item)
        for number, jobs in enumerate(attempts, 1):
            self.jobs[(item["id"], number)] = jobs
        return item

    def __call__(self, url):
        self.calls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert parsed.scheme == "https" and parsed.netloc == "api.github.com"
        assert set(query) == {"per_page", "page"}  # No created filter: include old-run retries.
        page = int(query["page"][0])
        size = int(query["per_page"][0])
        if parsed.path == BASE:
            key, values = "workflow_runs", self.runs
        else:
            suffix = parsed.path[len(BASE) + 1:].split("/")
            assert suffix[1] == "attempts" and suffix[3] == "jobs"
            key, values = "jobs", self.jobs[(int(suffix[0]), int(suffix[2]))]
        return {"total_count": len(values), key: copy.deepcopy(values[(page - 1) * size:page * size])}


class ActionsBudgetTests(unittest.TestCase):
    def check(self, history, attempt=1, now=NOW):
        return budget.check_budget("example/tiles", 999, attempt, now=now, fetch=history)

    def assert_closed(self, history, reason=None):
        report = self.check(history)
        self.assertFalse(report["allowed"], report)
        if reason:
            self.assertIn(reason, report["reason"])
        return report

    def test_reserves_all_job_timeouts_without_double_counting_current_gate(self):
        report = self.check(PublicHistory())
        self.assertEqual(report["usedMinutes"], 0)
        self.assertEqual(report["reservedMinutes"], 22)
        self.assertEqual(report["capMinutes"], 100)
        self.assertTrue(report["allowed"])
        self.assertTrue(report["complete"])

    def test_exact_boundary_allowed_but_exhausted_allowance_denied(self):
        history = PublicHistory()
        history.add(run(1), [[job(1, 1, seconds=78 * 60)]])
        self.assertTrue(self.check(history)["allowed"])
        history.jobs[(1, 1)][0]["completed_at"] = timestamp(NOW - timedelta(hours=2) + timedelta(seconds=78 * 60 + 1))
        report = self.assert_closed(history, "full rollout reservation")
        self.assertEqual(report["usedMinutes"], 79)
        self.assertTrue(report["complete"])

    def test_failures_cancelled_jobs_and_every_retry_are_counted(self):
        history = PublicHistory()
        history.add(run(1, attempt=3), [
            [job(10, 1, seconds=61, conclusion="failure"), job(11, 1, seconds=1, conclusion="cancelled")],
            [job(12, 1, seconds=1, conclusion="timed_out")],
            [job(13, 1, seconds=60)],
        ])
        self.assertEqual(self.check(history)["usedMinutes"], 5)
        self.assertTrue(any("/attempts/1/jobs" in url for url in history.calls))
        self.assertTrue(any("/attempts/3/jobs" in url for url in history.calls))

    def test_legitimate_skipped_jobs_without_runner_timestamps_are_zero(self):
        history = PublicHistory()
        skipped = job(10, 1, conclusion="skipped")
        skipped.update(started_at=None, completed_at=None)
        history.add(run(1), [[skipped, job(11, 1, seconds=61, conclusion="skipped")]])
        report = self.check(history)
        self.assertTrue(report["allowed"], report)
        self.assertEqual(report["usedMinutes"], 2)

    def test_only_exact_null_null_completed_skipped_shape_is_exempt(self):
        for mutation in ("failure", "in_progress", "partial", "missing_field"):
            with self.subTest(mutation=mutation):
                history = PublicHistory()
                skipped = job(10, 1, conclusion="skipped")
                skipped.update(started_at=None, completed_at=None)
                if mutation == "failure":
                    skipped["conclusion"] = "failure"
                elif mutation == "in_progress":
                    skipped["status"] = "in_progress"
                elif mutation == "partial":
                    skipped["started_at"] = timestamp(NOW - timedelta(hours=2))
                else:
                    del skipped["started_at"]
                history.add(run(1), [[skipped]])
                self.assert_closed(history)

    def test_verified_unallocated_skipped_job_ignores_administrative_clock_inversion(self):
        history = PublicHistory()
        skipped = job(10, 1, seconds=-1, conclusion="skipped", name="deploy")
        skipped.update(steps=[], runner_id=None, runner_name=None,
                       runner_group_id=None, runner_group_name=None)
        history.add(run(1), [[skipped]])
        report = self.check(history)
        self.assertTrue(report["allowed"], report)
        self.assertEqual(report["usedMinutes"], 0)
        # These timestamp fields are not execution measurements in this shape.
        skipped.update(started_at="administrative", completed_at="administrative")
        self.assertEqual(self.check(history)["usedMinutes"], 0)

    def test_negative_duration_exception_requires_every_no_execution_field(self):
        runner_fields = ("runner_id", "runner_name", "runner_group_id", "runner_group_name")
        for mutation in ("status", "conclusion", "steps", "missing_steps", *runner_fields,
                         *("missing_"+name for name in runner_fields)):
            with self.subTest(mutation=mutation):
                history = PublicHistory()
                skipped = job(10, 1, seconds=-1, conclusion="skipped")
                skipped.update(steps=[], runner_id=None, runner_name=None,
                               runner_group_id=None, runner_group_name=None)
                if mutation == "status": skipped["status"] = "in_progress"
                elif mutation == "conclusion": skipped["conclusion"] = "success"
                elif mutation == "steps": skipped["steps"] = [{"status":"completed"}]
                elif mutation.startswith("missing_"): del skipped[mutation[8:]]
                else: skipped[mutation] = 123 if mutation.endswith("id") else "assigned"
                history.add(run(1), [[skipped]])
                self.assert_closed(history)

    def test_non_skipped_negative_durations_fail_and_actual_rounding_is_unchanged(self):
        for conclusion in ("success", "failure", "cancelled", "timed_out"):
            history = PublicHistory()
            entry = job(10, 1, seconds=-1, conclusion=conclusion)
            entry.update(steps=[], runner_id=None, runner_name=None,
                         runner_group_id=None, runner_group_name=None)
            history.add(run(1), [[entry]])
            self.assert_closed(history)
        history = PublicHistory()
        history.add(run(1), [[job(10, 1, seconds=1), job(11, 1, seconds=60),
                             job(12, 1, seconds=61)]])
        self.assertEqual(self.check(history)["usedMinutes"], 4)

    def test_current_run_prior_attempt_is_not_excluded(self):
        history = PublicHistory()
        history.runs[0]["run_attempt"] = 2
        history.jobs[(999, 2)] = history.jobs[(999, 1)]
        history.jobs[(999, 1)] = [job(9980, 999, seconds=61, name="budget", conclusion="failure")]
        report = self.check(history, attempt=2)
        self.assertTrue(report["allowed"], report)
        self.assertEqual(report["usedMinutes"], 2)

    def test_historical_publishers_and_all_their_jobs_are_counted(self):
        history = PublicHistory()
        history.add(run(1, path="pilot-pages"), [[job(10, 1, seconds=61), job(11, 1, seconds=61, name="deploy")]])
        history.add(run(2, path="append-metro"), [[job(20, 2, seconds=61)]])
        history.add(run(3, path="snapshot-tests"), [])
        self.assertEqual(self.check(history)["usedMinutes"], 6)

    def test_cross_month_jobs_are_charged_in_full_and_old_run_retry_is_found(self):
        history = PublicHistory()
        boundary = NOW.replace(day=1, hour=0)
        history.add(run(1, attempt=2, created=boundary - timedelta(days=10)), [
            [job(10, 1, seconds=60, started=boundary - timedelta(days=9))],
            [job(11, 1, seconds=120, started=boundary - timedelta(seconds=90))],
        ])
        self.assertEqual(self.check(history)["usedMinutes"], 2)

    def test_completed_old_runs_do_not_require_expired_job_history(self):
        history = PublicHistory()
        history.add(run(1, created=NOW - timedelta(days=80), updated=NOW - timedelta(days=40)), [])
        self.assertTrue(self.check(history)["allowed"])
        self.assertFalse(any("/runs/1/attempts" in url for url in history.calls))

    def test_both_run_and_job_pagination_are_exhausted(self):
        history = PublicHistory()
        for identifier in range(1, 102):
            history.add(run(identifier, path="snapshot-tests"), [])
        history.add(run(200), [[job(2000 + number, 200, seconds=0) for number in range(101)]])
        self.assertTrue(self.check(history)["allowed"])
        self.assertTrue(any("/runs?per_page=100&page=2" in url for url in history.calls))
        self.assertTrue(any("/runs/200/attempts/1/jobs?per_page=100&page=2" in url for url in history.calls))

    def test_missing_page_and_changed_totals_fail_closed(self):
        for mode in ("missing", "changed", "duplicate"):
            with self.subTest(mode=mode):
                history = PublicHistory()
                history.add(run(1), [[job(10 + number, 1, seconds=0) for number in range(101)]])
                def fetch(url):
                    result = history(url)
                    if "/jobs?" in url and "page=2" in url:
                        if mode == "missing":
                            result["jobs"] = []
                        elif mode == "changed":
                            result["total_count"] = 102
                        else:
                            result["jobs"][0]["id"] = 10
                    return result
                self.assert_closed(fetch)

    def test_audit_ceiling_and_request_limit_fail_closed(self):
        history = PublicHistory()
        self.assert_closed(lambda url: {"total_count": 1000, "workflow_runs": []}, "audit ceiling")
        history.add(run(1, attempt=51), [])
        self.assert_closed(history, "audit limit")
        with patch.object(budget, "MAX_REQUESTS", 1):
            self.assert_closed(PublicHistory(), "request/time limit")

    def test_api_errors_and_malformed_json_fail_closed(self):
        for error in [HTTPError("https://api.github.com", 403, "rate limited", {}, None),
                      TimeoutError("timeout"), json.JSONDecodeError("bad", "x", 0)]:
            with self.subTest(error=type(error).__name__):
                def fetch(url):
                    raise error
                report = self.assert_closed(fetch, "budget unavailable")
                self.assertIsNone(report["usedMinutes"])
                self.assertFalse(report["complete"])
        for payload in [None, [], {}, {"total_count": True, "workflow_runs": []}]:
            self.assert_closed(lambda url: payload)

    def test_malformed_job_timestamps_and_missing_attempt_are_closed(self):
        for field, value in [("started_at", None), ("completed_at", None),
                             ("started_at", "2026-10-13T12:00:00"),
                             ("completed_at", "garbage"),
                             ("completed_at", timestamp(NOW + timedelta(seconds=1))),
                             ("completed_at", timestamp(NOW - timedelta(days=3)))]:
            with self.subTest(field=field, value=value):
                history = PublicHistory()
                entry = job(10, 1)
                entry[field] = value
                history.add(run(1), [[entry]])
                self.assert_closed(history)
        history = PublicHistory()
        history.add(run(1, attempt=2), [[job(10, 1)]])
        self.assert_closed(history)

    def test_current_gate_must_be_visible_valid_and_within_timeout(self):
        for mutation in ("missing", "old", "future", "wrong_job", "wrong_attempt"):
            with self.subTest(mutation=mutation):
                history = PublicHistory()
                gate = history.jobs[(999, 1)][0]
                if mutation == "missing":
                    history.jobs[(999, 1)] = []
                elif mutation == "old":
                    gate["started_at"] = timestamp(NOW - timedelta(seconds=121))
                elif mutation == "future":
                    gate["started_at"] = timestamp(NOW + timedelta(seconds=1))
                elif mutation == "wrong_job":
                    gate["name"] = "unbounded-matrix"
                else:
                    history.runs[0]["run_attempt"] = 2
                self.assert_closed(history)

    def test_competing_runs_and_unfinished_historical_jobs_fail_closed(self):
        for status in ("queued", "in_progress", "waiting", "unknown"):
            with self.subTest(status=status):
                history = PublicHistory()
                history.add(run(1, path="append-metro", status=status), [])
                self.assert_closed(history)
        history = PublicHistory()
        unfinished = job(10, 1)
        unfinished.update(status="in_progress", completed_at=None)
        history.add(run(1), [[unfinished]])
        self.assert_closed(history, "unfinished historical job")

    def test_current_pending_jobs_fit_full_reservation(self):
        history = PublicHistory()
        pending = job(9991, 999)
        pending.update(status="queued", started_at=None, completed_at=None)
        history.jobs[(999, 1)].append(pending)
        self.assertTrue(self.check(history)["allowed"])

    def test_duplicate_current_jobs_cannot_share_one_reservation(self):
        history = PublicHistory()
        for identifier in (9991, 9992):
            pending = job(identifier, 999)
            pending.update(status="queued", started_at=None, completed_at=None)
            history.jobs[(999, 1)].append(pending)
        self.assert_closed(history, "duplicate reserved jobs")

    def test_a_completed_current_job_is_charged_on_top_of_reservation(self):
        history = PublicHistory()
        history.jobs[(999, 1)].append(job(9991, 999, seconds=61))
        report = self.check(history)
        self.assertTrue(report["allowed"], report)
        self.assertEqual(report["usedMinutes"], 2)

    def test_missing_run_metadata_and_duplicate_attempt_jobs_fail_closed(self):
        for field, value in [("path", None), ("run_attempt", None), ("created_at", None),
                             ("updated_at", "invalid"), ("run_attempt", True)]:
            with self.subTest(field=field, value=value):
                history = PublicHistory()
                history.runs[0][field] = value
                self.assert_closed(history)
        history = PublicHistory()
        duplicate = job(10, 1)
        history.add(run(1, attempt=2), [[duplicate], [duplicate]])
        self.assert_closed(history, "duplicate job across run attempts")

    def test_invalid_inputs_fail_closed(self):
        for repository, run_id, attempt, now in [
            ("example/tiles?x=y", 999, 1, NOW), ("example/tiles", True, 1, NOW),
            ("example/tiles", 999, 0, NOW), ("example/tiles", 999, 1, NOW.replace(tzinfo=None)),
        ]:
            report = budget.check_budget(repository, run_id, attempt, now, PublicHistory())
            self.assertFalse(report["allowed"], report)

    def test_public_fetch_has_no_authorization_header(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def geturl(self):
                return "https://api.github.com/repos/example/tiles/actions/runs"
            def read(self, limit):
                return b'{"total_count": 0, "workflow_runs": []}'
        with patch.dict(os.environ, {"GITHUB_TOKEN": "must-not-be-used", "GH_TOKEN": "must-not-be-used"}), \
             patch.object(budget, "build_opener") as opener:
            opener.return_value.open.return_value = Response()
            budget.fetch_public_json(Response().geturl())
            request = opener.return_value.open
        self.assertNotIn("authorization", {key.lower() for key in request.call_args.args[0].headers})

    def test_existing_job_token_is_isolated_to_exact_repository(self):
        base = "https://api.github.com/repos/"
        with patch.dict(os.environ, {"TILE_GITHUB_READ_TOKEN": "ephemeral-test",
                                    "GITHUB_REPOSITORY": "apfullai/ride-tiles"}):
            self.assertEqual(budget.api_read_headers(base + "apfullai/ride-tiles/actions/runs")["Authorization"],
                             "Bearer ephemeral-test")
            for suffix in ("apfullai/rides-tiles/actions/runs", "apfullai/ride-tiles-evil/actions/runs",
                           "other/ride-tiles/actions/runs", "apfullai/ride-tiles/../other/actions/runs",
                           "apfullai/ride-tiles/issues", "apfullai/ride-tiles/actions/runs%2F.."):
                self.assertNotIn("Authorization", budget.api_read_headers(base + suffix))
            for url in ("http://api.github.com/repos/apfullai/ride-tiles/a",
                        "https://api.github.com.evil/repos/apfullai/ride-tiles/a",
                        "https://api.github.com@evil/repos/apfullai/ride-tiles/a",
                        "https://apfullai.github.io/ride-tiles/manifest.json"):
                with self.assertRaises(budget.BudgetUnavailable):
                    budget.api_read_headers(url)
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "other/ride-tiles"}):
                self.assertNotIn("Authorization", budget.api_read_headers(base + "apfullai/ride-tiles/actions/runs"))

    def test_api_redirect_is_rejected_before_following(self):
        with self.assertRaisesRegex(budget.BudgetUnavailable, "redirects"):
            budget.NoApiRedirects().redirect_request(None, None, 302, "redirect", {}, "https://evil.example/")

    def test_cli_emits_machine_readable_allowance_and_denial(self):
        for allowed in (False, True):
            with self.subTest(allowed=allowed), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                report = {"allowed": allowed, "usedMinutes": 0 if allowed else None, "reservedMinutes": 22}
                with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_REPOSITORY": "example/tiles",
                                              "GITHUB_RUN_ID": "999", "GITHUB_RUN_ATTEMPT": "1"}), \
                     patch.object(budget, "check_budget", return_value=report) as check, \
                     patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    self.assertEqual(budget.main([]), 0)
                check.assert_called_once_with("example/tiles", 999, 1)
                self.assertIn(f"allowed={str(allowed).lower()}\n", output.read_text())
                self.assertEqual(json.loads(stdout.getvalue()), report)


if __name__ == "__main__":
    unittest.main()
