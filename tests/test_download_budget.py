"""Bounded public source transfer tests; no network or real downloads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from build_region import download_source, validate_config


class SourceBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root/'ledger.json'
        self.env = patch.dict(os.environ, {'RIDE_DOWNLOAD_LEDGER': str(self.ledger)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_success_charges_actual_bytes_and_disables_retry(self):
        def transfer(*args):
            self.assertEqual(args[args.index('--retry')+1], '0')
            self.assertEqual(args[args.index('--proto')+1], '=https')
            self.assertEqual(args[args.index('--proto-redir')+1], '=https')
            Path(args[-1]).write_bytes(b'x'*123)
        with patch('build_region.run', side_effect=transfer):
            download_source('https://download.geofabrik.de/test.pbf', self.root/'source')
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], 123)

    def test_failed_download_keeps_full_conservative_reservation(self):
        with patch('build_region.run', side_effect=subprocess.CalledProcessError(22, 'curl')):
            with self.assertRaises(subprocess.CalledProcessError):
                download_source('https://download.geofabrik.de/test.pbf', self.root/'source')
        self.assertEqual(json.loads(self.ledger.read_text())['reservedBytes'], 1_500_000_000)

    def test_remaining_bytes_bound_next_file_and_exhaustion_stops(self):
        self.ledger.write_text(json.dumps({'reservedBytes': 2_900_000_000}))
        def transfer(*args):
            self.assertEqual(args[args.index('--max-filesize')+1], '100000000')
            raise subprocess.CalledProcessError(63, 'curl')
        with patch('build_region.run', side_effect=transfer):
            with self.assertRaises(subprocess.CalledProcessError):
                download_source('https://download.geofabrik.de/test.pbf', self.root/'source')
        with patch('build_region.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'exhausted'):
                download_source('https://download.geofabrik.de/test.pbf', self.root/'next')
            run.assert_not_called()

    def test_four_state_exception_is_exact_chicago_only(self):
        good = {'id':'chicagoland','bounds':[-89,41.05,-86.65,43.25],
                'minzoom':8,'maxzoom':14,'sourceStates':['illinois','wisconsin','indiana','michigan']}
        validate_config(good)
        for change in ({'id':'other'}, {'bounds':[-89,41.05,-86.6,43.25]}):
            with self.assertRaises(AssertionError):
                validate_config(dict(good, **change))
