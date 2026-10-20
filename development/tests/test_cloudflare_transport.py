"""Small transport fixtures; never upload, generate tiles or dispatch Actions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloudflare_transport as c

DATA = "https://12345678.ride-tiles-data.pages.dev"
MELLOW = "https://abcdef12.ride-tiles-mellow.pages.dev"


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        self.site.mkdir()
        region = {"id": "sample", "version": "a" * 16}
        prefix = c.snapshot.BASE + "/regions/sample/" + region["version"] + "/"
        region.update(tiles=[prefix + "tiles/{z}/{x}/{y}.pbf"],
                      sourceUrl=prefix + "provenance.json",
                      sourceDownload=prefix + "source.tar.gz",
                      sha256Manifest=prefix + "tile-sha256.json")
        self.manifest = {"schemaVersion": 1, "updatedAt": "old", "regions": [region]}
        files = {"manifest.json": c.canonical(self.manifest),
                 "regions/sample/" + "a" * 16 + "/tiles/0/0/0.pbf": b"\x1f\x8bfixture",
                 "checkpoints/active/checkpoint.json": b'{"codeFingerprint":"preserved"}',
                 "mellow/one/tiles/0/0/0.pbf": b"mellow fixture",
                 "status.json": b'{"active":true}'}
        for name, data in files.items():
            p = self.site / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        self.fake_validate = patch.object(c.snapshot, "_validate_data",
            side_effect=lambda path, *a: json.loads((path / "manifest.json").read_text()))
        self.fake_validate.start()
        self.addCleanup(self.fake_validate.stop)
        c.snapshot.seal_snapshot(self.site)
        self.records = c.snapshot._records(self.site, include_metadata=True)
        self.output = self.root / "package"

    def prepare(self):
        return c.prepare(self.site, self.output)

    def test_preserves_every_original_byte_and_checkpoint(self):
        result = self.prepare()
        self.assertFalse(result["mapRefreshClaimed"])
        self.assertLess(result["files"]["root"], 20_000)
        self.assertLess(result["files"]["mellow"], 20_000)
        for name, identity in self.records.items():
            part = "mellow" if name.startswith("mellow/") else "root"
            data = (self.output / part / name).read_bytes()
            self.assertEqual(c.snapshot.digest(data), identity["sha256"])
        self.assertEqual(c.snapshot._records(self.site, True), self.records)

    def test_two_phase_binding_uses_immutable_urls(self):
        self.prepare()
        a = c.bind(self.output, MELLOW)
        self.assertEqual(a["state"], "shard-bound")
        self.assertFalse((self.output / "root/cloudflare-manifest.json").exists())
        b = c.bind(self.output, MELLOW, DATA)
        self.assertEqual(b["state"], "publication-bound")
        preserved = json.loads((self.output / "root/manifest.json").read_text())
        self.assertEqual(preserved, self.manifest)
        mapped = json.loads((self.output / "root/cloudflare-manifest.json").read_text())
        self.assertEqual(mapped["updatedAt"], "old")
        self.assertTrue(mapped["regions"][0]["tiles"][0].startswith(DATA + "/"))
        self.assertIn(MELLOW + "/mellow/:splat 302", (self.output / "root/_redirects").read_text())
        with self.assertRaises(ValueError):
            c.bind(self.output, MELLOW, DATA)

    def test_rejects_corruption_missing_and_unexpected_files(self):
        self.prepare()
        p = self.output / "root/status.json"
        old = p.read_bytes()
        p.write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "changed"):
            c.verify_package(self.output)
        p.write_bytes(old)
        p.unlink()
        with self.assertRaisesRegex(ValueError, "disappeared"):
            c.verify_package(self.output)
        p.write_bytes(old)
        (self.output / "root/token.txt").write_text("not a public asset")
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            c.verify_package(self.output)

    def test_rejects_control_injection_and_symlinks(self):
        self.prepare()
        (self.output / "root/_worker.js").write_text("export default {}")
        with self.assertRaises(ValueError):
            c.verify_package(self.output)
        (self.output / "root/_worker.js").unlink()
        (self.output / "root/link").symlink_to(self.site / "status.json")
        with self.assertRaises(ValueError):
            c.verify_package(self.output)

    def test_rejects_changed_shards_and_unsealed_source(self):
        shard = next((self.site / "snapshot").iterdir())
        shard.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_caps_include_transport_overhead(self):
        self.prepare()
        size = c.verify_package(self.output)["combinedBytes"]
        with patch.object(c, "MAX_BYTES", size - 1), self.assertRaisesRegex(ValueError, "250 MB"):
            c.verify_package(self.output)
        with patch.object(c, "MAX_FILES", 2), self.assertRaisesRegex(ValueError, "file count"):
            c.verify_package(self.output)

    def test_404_and_headers_are_mandatory(self):
        self.prepare()
        self.assertIn("Access-Control-Allow-Origin: *", (self.output / "mellow/_headers").read_text())
        self.assertIn("Cache-Control: no-store", c.HEADERS)
        (self.output / "mellow/404.html").write_text("wrong")
        with self.assertRaisesRegex(ValueError, "controls"):
            c.verify_package(self.output)

    def test_rejects_alias_unrelated_project_and_non_https(self):
        for value in ("https://ride-tiles-data.pages.dev",
                      "https://preview.ride-tiles-data.pages.dev",
                      "https://12345678.other.pages.dev", DATA + "/path",
                      DATA + "?query=yes", DATA + "#fragment",
                      DATA + "\t", "\n" + DATA, DATA.replace("1234", "12\n34"),
                      "http://12345678.ride-tiles-data.pages.dev",
                      "https://user@12345678.ride-tiles-data.pages.dev"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                c.immutable_url(value, c.ROOT_PROJECT)

    def test_transport_adapter_only_rewrites_exact_origin(self):
        calls = []
        fetch = c.transport_fetch(DATA, lambda url, limit: calls.append((url, limit)) or b"ok")
        self.assertEqual(fetch(c.snapshot.BASE + "/snapshot/archive.tar.gz.part00000", 10), b"ok")
        self.assertEqual(calls, [(DATA + "/snapshot/archive.tar.gz.part00000", 10)])
        for url in (c.snapshot.BASE + ".evil/a", "https://other/a",
                    c.snapshot.BASE + "/../secret", c.snapshot.BASE + "/%2e%2e/secret",
                    c.snapshot.BASE + "/a?b", c.snapshot.BASE + "/a#b"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch(url, 10)

    def test_original_snapshot_restores_through_transport_adapter(self):
        self.prepare()
        c.bind(self.output, MELLOW)
        def fetch(url, maximum):
            self.assertTrue(url.startswith(DATA + "/"))
            data = (self.output / "root" / url[len(DATA) + 1:]).read_bytes()
            self.assertLessEqual(len(data), maximum)
            return data
        destination = self.root / "restored"
        transport = c.transport_fetch(DATA, fetch)
        c.snapshot.restore_published(destination, fetch=transport)
        self.assertEqual(c.snapshot._records(destination, True), self.records)

    def test_prepare_rejects_existing_or_overlapping_output(self):
        with self.assertRaises(ValueError):
            c.prepare(self.site, self.site)
        with self.assertRaises(ValueError):
            c.prepare(self.site, self.site / "package")
        self.prepare()
        with self.assertRaises(ValueError):
            self.prepare()

    def test_transport_manifest_changes_only_urls(self):
        mapped = c.transport_manifest(self.manifest, DATA)
        original = copy.deepcopy(mapped)
        for r in original["regions"]:
            for k in ("sourceUrl", "sourceDownload", "sha256Manifest"):
                r[k] = c.snapshot.BASE + r[k][len(DATA):]
            r["tiles"] = [c.snapshot.BASE + u[len(DATA):] for u in r["tiles"]]
        self.assertEqual(original, self.manifest)

    def test_rejects_false_receipt_assertions_and_overrides(self):
        self.prepare()
        path = self.output / "receipt.json"
        original = json.loads(path.read_text())
        for key, value in (("canonicalFiles", 1), ("canonicalBytes", 1),
                           ("canonicalManifestSha256", "0" * 64),
                           ("sourcePublicationRevision", "0" * 64),
                           ("combinedBytes", 1), ("mapRefreshClaimed", True)):
            with self.subTest(key=key):
                changed = {**original, key: value}
                c.write_json(path, changed)
                with self.assertRaises(ValueError):
                    c.verify_package(self.output)
        c.write_json(path, original)

    def test_restore_failed_final_check_leaves_no_destination(self):
        self.prepare()
        destination = self.root / "restored"
        expected = c.verify_package(self.output)["sourcePublicationRevision"]
        def fetch(url, maximum):
            return (self.output / "root" / url[len(DATA) + 1:]).read_bytes()
        with patch.object(c.snapshot, "_fetch", side_effect=fetch), patch.object(
                c.snapshot, "assert_publication_unchanged",
                side_effect=[expected, ValueError("race")]):
            with self.assertRaisesRegex(ValueError, "race"):
                c.restore(DATA, destination, expected)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
