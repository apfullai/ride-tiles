import copy
import gzip
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "publishing"))
import artifact_budget as budget


def artifact(identifier, size=1, expired=False):
    return {"id": identifier, "size_in_bytes": size, "expired": expired}


class PublicArtifacts:
    def __init__(self, artifacts=()):
        self.artifacts = list(artifacts)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        parsed = urlparse(url)
        assert parsed.scheme == "https" and parsed.netloc == "api.github.com"
        assert parsed.path == "/repos/example/tiles/actions/artifacts"
        query = parse_qs(parsed.query)
        assert set(query) == {"per_page", "page"}  # Never restrict artifact name or workflow.
        page, size = int(query["page"][0]), int(query["per_page"][0])
        return {"total_count": len(self.artifacts),
                "artifacts": copy.deepcopy(self.artifacts[(page - 1) * size:page * size])}


class ArtifactBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name) / "site"
        self.site.mkdir()
        (self.site / "manifest.json").write_text('{"regions": []}')

    def check(self, fetch):
        return budget.check_artifact_budget("example/tiles", self.site, fetch=fetch)

    def assert_closed(self, fetch, reason=None):
        report = self.check(fetch)
        self.assertFalse(report["allowed"], report)
        if reason:
            self.assertIn(reason, report["reason"])
        return report

    def test_counts_existing_21_8_mb_unexpired_artifacts_and_candidate(self):
        report = self.check(PublicArtifacts([artifact(1, 21_800_000)]))
        self.assertTrue(report["allowed"], report)
        self.assertEqual(report["artifactBytes"], 21_800_000)
        self.assertEqual(report["projectedBytes"], 21_800_000 + report["candidateBytes"])
        self.assertGreater(report["candidateBytes"], report["siteBytes"])
        self.assertEqual(report["capBytes"], 250_000_000)
        self.assertTrue(report["complete"])

    def test_exact_cap_is_allowed_but_one_extra_byte_denies_upload(self):
        allowance = budget.candidate_allowance(self.site)["candidateBytes"]
        remaining = budget.CAP_BYTES - allowance
        self.assertTrue(self.check(PublicArtifacts([artifact(1, remaining)]))["allowed"])
        report = self.assert_closed(PublicArtifacts([artifact(1, remaining + 1)]), "cannot cover")
        self.assertTrue(report["complete"])
        self.assertEqual(report["projectedBytes"], budget.CAP_BYTES + 1)

    def test_all_artifacts_are_counted_and_only_explicitly_expired_are_excluded(self):
        items = [artifact(1, 5), artifact(2, 1000, expired=True), artifact(3, 7)]
        items[0]["name"] = "old-pilot"
        items[0]["expires_at"] = "2000-01-01T00:00:00Z"  # API still reports unexpired.
        items[2]["name"] = "failed-test-diagnostic"
        report = self.check(PublicArtifacts(items))
        self.assertEqual(report["artifactBytes"], 12)
        self.assertEqual(report["unexpiredArtifactCount"], 2)

    def test_all_pages_are_read(self):
        history = PublicArtifacts([artifact(number, number) for number in range(1, 202)])
        report = self.check(history)
        self.assertTrue(report["allowed"], report)
        self.assertEqual(report["artifactBytes"], sum(range(1, 202)))
        self.assertEqual(len(history.calls), 3)

    def test_incomplete_changing_or_duplicate_pages_fail_closed(self):
        for mode in ("missing", "changed", "duplicate"):
            with self.subTest(mode=mode):
                history = PublicArtifacts([artifact(number) for number in range(1, 102)])
                def fetch(url):
                    result = history(url)
                    if "page=2" in url:
                        if mode == "missing":
                            result["artifacts"] = []
                        elif mode == "changed":
                            result["total_count"] += 1
                        else:
                            result["artifacts"][0]["id"] = 1
                    return result
                report = self.assert_closed(fetch)
                self.assertFalse(report["complete"])
                self.assertIsNone(report["artifactBytes"])

    def test_missing_malformed_or_excessive_api_data_fail_closed(self):
        for item in [{"id": 1}, artifact(1, -1), artifact(1, True), artifact(1, "50"),
                     artifact(1, expired="false"), artifact(1, expired=None)]:
            with self.subTest(item=item):
                self.assert_closed(PublicArtifacts([item]))
        for result in [None, {}, {"total_count": 1000, "artifacts": []},
                       {"total_count": 1, "artifacts": [None]}]:
            self.assert_closed(lambda url: result)

    def test_network_api_and_json_errors_fail_closed(self):
        for error in (HTTPError("https://api.github.com", 403, "rate limited", {}, None),
                      TimeoutError("timeout"), json.JSONDecodeError("invalid", "x", 0)):
            with self.subTest(error=type(error).__name__):
                def fetch(url):
                    raise error
                self.assert_closed(fetch, "artifact budget unavailable")

    def test_candidate_size_bounds_real_tar_and_gzip_even_for_long_names(self):
        nested = self.site / ("a" * 110)
        nested.mkdir()
        (nested / "tile.pbf").write_bytes(os.urandom(12_345))
        (self.site / "empty").write_bytes(b"")
        (self.site / ".hidden").write_bytes(b"hidden files are included conservatively")
        allowance = budget.candidate_allowance(self.site)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as stream:
            stream.add(self.site, arcname=".")
        raw = archive.getvalue()
        self.assertGreaterEqual(allowance["tarBytesUpperBound"], len(raw))
        self.assertGreaterEqual(allowance["candidateBytes"], len(gzip.compress(raw)))
        self.assertEqual(allowance["fileCount"], 4)
        self.assertEqual(allowance["entryCount"], 6)

    def test_every_small_file_has_header_and_padding_reservation(self):
        before = budget.candidate_allowance(self.site)
        for number in range(100):
            (self.site / f"{number}.pbf").write_bytes(b"x")
        after = budget.candidate_allowance(self.site)
        self.assertEqual(after["siteBytes"] - before["siteBytes"], 100)
        self.assertGreaterEqual(after["tarBytesUpperBound"] - before["tarBytesUpperBound"], 100 * 1024)

    def test_empty_missing_linked_and_special_candidates_are_rejected(self):
        (self.site / "manifest.json").unlink()
        self.assert_closed(PublicArtifacts(), "no files")
        self.site.rmdir()
        self.assert_closed(PublicArtifacts())
        target = Path(self.temp.name) / "target"
        target.mkdir()
        self.site.symlink_to(target, target_is_directory=True)
        self.assert_closed(PublicArtifacts(), "plain directory")
        self.site.unlink()
        self.site.mkdir()
        (self.site / "link").symlink_to(target, target_is_directory=True)
        self.assert_closed(PublicArtifacts(), "symlink or special")
        (self.site / "link").unlink()
        os.mkfifo(self.site / "fifo")
        self.assert_closed(PublicArtifacts(), "symlink or special")

    def test_invalid_repository_and_audit_limits_fail_closed(self):
        self.assertFalse(budget.check_artifact_budget("example/tiles?x", self.site, fetch=PublicArtifacts())["allowed"])
        with patch.object(budget, "MAX_API_REQUESTS", 0):
            self.assert_closed(PublicArtifacts(), "request/time limit")

    def test_cli_outputs_allowed_for_both_success_and_failure(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                output = Path(self.temp.name) / f"output-{allowed}"
                report = {"allowed": allowed, "artifactBytes": 10 if allowed else None,
                          "candidateBytes": 20, "projectedBytes": 30 if allowed else None}
                with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_REPOSITORY": "example/tiles"}), \
                     patch.object(budget, "check_artifact_budget", return_value=report) as check, \
                     patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    self.assertEqual(budget.main(["--site", str(self.site)]), 0)
                check.assert_called_once_with("example/tiles", self.site)
                self.assertIn(f"allowed={str(allowed).lower()}\n", output.read_text())
                self.assertEqual(json.loads(stdout.getvalue()), report)


if __name__ == "__main__":
    unittest.main()
