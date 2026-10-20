import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_cloudflare_http as http


class HttpSmokeTests(unittest.TestCase):
    def test_samples_cover_regions_mellow_and_checkpoint(self):
        names = [
            "regions/nyc/aa/tiles/1/0/0.pbf", "regions/nyc/aa/provenance.json",
            "regions/chicago/bb/tiles/2/0/0.pbf", "mellow/x/0.pbf",
            "mellow/manifest.json", "checkpoints/active/checkpoint.json",
            "checkpoints/active/source.part", "manifest.json",
            "snapshot/archive.tar.gz.part00000",
        ]
        sampled = http.samples({name: {} for name in names})
        self.assertEqual(set(sampled), set(names) - {"snapshot/archive.tar.gz.part00000"})

    def test_fetch_does_not_allow_unexpected_redirect_or_encoding(self):
        class Response:
            headers = {"Content-Encoding": "gzip"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://expected/file"
            def read(self, n): return b"x"
        with patch.object(http, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "transformation"):
                http.fetch("https://expected/file", 1)
            with self.assertRaisesRegex(ValueError, "redirect"):
                http.fetch("https://other/file", 1)

    def test_fetch_requires_cors_and_no_store(self):
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://expected/file"
            def read(self, n): return b"x"
        r = Response()
        with patch.object(http, "urlopen", return_value=r):
            with self.assertRaisesRegex(ValueError, "CORS"):
                http.fetch("https://expected/file", 1)
            r.headers["Access-Control-Allow-Origin"] = "*"
            with self.assertRaisesRegex(ValueError, "cache"):
                http.fetch("https://expected/file", 1)
            r.headers["Cache-Control"] = "no-store"
            self.assertEqual(http.fetch("https://expected/file", 1)[0], b"x")

    def test_only_explicit_expected_html_redirect_is_accepted(self):
        class Response:
            headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://expected/"
            def read(self, n): return b"x"
        with patch.object(http, "urlopen", return_value=Response()):
            self.assertEqual(http.fetch("https://expected/index.html", 1,
                                        expected_url="https://expected/")[0], b"x")
            with self.assertRaisesRegex(ValueError, "redirect"):
                http.fetch("https://expected/tiles/0/0/0.pbf", 1)


if __name__ == "__main__":
    unittest.main()
