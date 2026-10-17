import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'publishing'))
import early_capacity as early
from artifact_budget import candidate_allowance, CAP_BYTES
from rollout_snapshot import BASE

NOW = dt.datetime(2026, 10, 15, 14, tzinfo=dt.timezone.utc)


def fixture():
    manifest = b'{"schemaVersion":1,"regions":[]}'
    files = {'manifest.json': manifest, 'nested/tiles/a': b'x' * 511,
             'nested/tiles/b': b'y' * 512, 'nested/tiles/c': b'z' * 513,
             'unicode/\u00e9': b'', 'checkpoints/active/checkpoint.json': b'{}'}
    conf = {'schemaVersion': 1, 'format': 'tar+gzip-split-v1',
            'manifestSha256': hashlib.sha256(manifest).hexdigest(),
            'archiveSha256': 'a' * 64,
            'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                      for name, data in files.items()},
            'shards': [{'path': 'snapshot/archive.tar.gz.part00000', 'size': 123,
                        'sha256': 'b' * 64}]}
    raw = (json.dumps(conf) + '\n').encode()
    return manifest, raw, files


def artifact(identifier, size, expiry='2026-10-15T19:00:08Z', expired=False):
    return {'id': identifier, 'size_in_bytes': size, 'expires_at': expiry, 'expired': expired}


class History:
    def __init__(self, artifacts=(), manifest=None, descriptor=None):
        m, d, _ = fixture()
        self.manifest = m if manifest is None else manifest
        self.descriptor = d if descriptor is None else descriptor
        self.artifacts = list(artifacts)
        self.calls = []

    def __call__(self, url, limit):
        self.calls.append(url)
        if url == BASE + '/manifest.json':
            return self.manifest
        if url == BASE + '/snapshot.json':
            return self.descriptor
        parts = urlsplit(url)
        assert parts.netloc == 'api.github.com'
        assert parts.path == '/repos/example/tiles/actions/artifacts'
        query = parse_qs(parts.query)
        page = int(query['page'][0])
        return json.dumps({'total_count': len(self.artifacts),
                           'artifacts': self.artifacts[(page-1)*100:page*100]}).encode()


class EarlyCapacityTests(unittest.TestCase):
    def check(self, history, **kwargs):
        return early.check_capacity('example/tiles', fetch=history, now=NOW, **kwargs)

    def test_metadata_allowance_matches_restored_tree_without_payload_downloads(self):
        m, raw, files = fixture()
        with tempfile.TemporaryDirectory() as temp:
            site = Path(temp)
            for name, data in {**files, 'snapshot.json': raw,
                               'snapshot/archive.tar.gz.part00000': b's' * 123}.items():
                target = site / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            self.assertEqual(early.metadata_allowance(m, raw), candidate_allowance(site))
        history = History()
        result = self.check(history)
        self.assertTrue(result['allowed'], result)
        self.assertEqual(len(history.calls), 3)
        self.assertNotIn('part00000', ' '.join(history.calls))
        self.assertEqual(result['scope'], 'baseline-only; full restore and final upload checks remain mandatory')

    def test_exact_cap_and_one_byte_over(self):
        m, raw, _ = fixture()
        candidate = early.metadata_allowance(m, raw)['candidateBytes']
        self.assertTrue(self.check(History([artifact(1, CAP_BYTES-candidate)]))['allowed'])
        result = self.check(History([artifact(1, CAP_BYTES-candidate+1)]))
        self.assertFalse(result['allowed'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['retryAt'], '2026-10-15T19:00:08+00:00')

    def test_cumulative_expirations_and_past_expired_flag(self):
        candidate = 173_186_970
        items = [artifact(1, 73_034_406, '2026-10-15T17:50:29Z'),
                 artifact(2, 122_357_794)]
        retry, _ = early.retry_hint(items, candidate, NOW)
        self.assertEqual(retry, '2026-10-15T19:00:08+00:00')
        items[0]['expires_at'] = '2026-10-14T17:50:29Z'
        result = self.check(History([artifact(1, CAP_BYTES, items[0]['expires_at'])]))
        self.assertFalse(result['allowed'])
        self.assertIsNone(result['retryAt'])
        self.assertEqual(result['artifactBytes'], CAP_BYTES)
        self.assertTrue(self.check(History([artifact(1, CAP_BYTES, expired=True)]))['allowed'])

    def test_missing_bad_expiry_and_baseline_too_large(self):
        for value in (None, '', 'nonsense', '2026-10-15T19:00:08'):
            result = self.check(History([artifact(1, CAP_BYTES, value)]))
            self.assertFalse(result['allowed'])
            self.assertTrue(result['complete'])
            self.assertIsNone(result['retryAt'])
        retry, note = early.retry_hint([], CAP_BYTES+1, NOW)
        self.assertIsNone(retry)
        self.assertIn('expiry cannot resolve', note)
        retry, _ = early.retry_hint([artifact(1, 50), artifact(2, 50)], CAP_BYTES-60, NOW)
        self.assertEqual(retry, '2026-10-15T19:00:08+00:00')

    def test_missing_malformed_mismatched_descriptor_fails_closed(self):
        m, raw, _ = fixture()
        for bad in (b'', b'{}', b'not json', raw.replace(b'"schemaVersion": 1', b'"schemaVersion": 1, "schemaVersion": 1')):
            result = self.check(History(descriptor=bad))
            self.assertFalse(result['allowed'])
            self.assertFalse(result['complete'])
        result = self.check(History(manifest=m+b' '))
        self.assertFalse(result['allowed'])
        self.assertIn('live manifest', result['reason'])

    def test_unsafe_paths_sizes_and_combined_metadata_collision(self):
        m, raw, _ = fixture()
        for name, entry in [('../escape', {'size': 1, 'sha256': 'c'*64}),
                            ('bad-size', {'size': True, 'sha256': 'c'*64}),
                            ('nested', {'size': 1, 'sha256': 'c'*64}),
                            ('snapshot.json/child', {'size': 1, 'sha256': 'c'*64})]:
            conf = json.loads(raw)
            conf['files'][name] = entry
            result = self.check(History(descriptor=json.dumps(conf).encode()))
            self.assertFalse(result['allowed'], result)
            self.assertFalse(result['complete'])

    def test_artifact_pagination_and_metadata_failures(self):
        result = self.check(History([artifact(i, i) for i in range(1, 202)]))
        self.assertTrue(result['allowed'])
        self.assertEqual(result['unexpiredArtifactCount'], 201)
        for items in ([artifact(1, 1), artifact(1, 2)], [artifact(1, -1)],
                      [artifact(1, True)], [artifact(1, 5, expired='false')]):
            result = self.check(History(items))
            self.assertFalse(result['allowed'])
            self.assertFalse(result['complete'])
        history = History([artifact(i, 1) for i in range(1, 102)])
        def changing(url, limit):
            data = history(url, limit)
            if 'page=2' in url:
                d = json.loads(data); d['total_count'] += 1
                return json.dumps(d).encode()
            return data
        self.assertFalse(self.check(changing)['complete'])

    def test_network_limits_and_missing_descriptor_never_bootstrap(self):
        for error in (TimeoutError('timeout'), HTTPError(BASE+'/snapshot.json',404,'missing',{},None)):
            def broken(url, limit):
                raise error
            result = self.check(broken)
            self.assertFalse(result['allowed'])
            self.assertFalse(result['complete'])
        with patch.object(early, 'MAX_REQUESTS', 1):
            self.assertFalse(self.check(History())['allowed'])
        with patch.object(early, 'AUDIT_SECONDS', 0):
            self.assertFalse(self.check(History())['allowed'])
        self.assertFalse(early.check_capacity('example/tiles?bad', fetch=History())['allowed'])
        self.assertFalse(early.check_capacity('example/tiles', fetch=History(), now=NOW.replace(tzinfo=None))['allowed'])
        self.assertFalse(self.check(lambda url, limit: b'x' * (limit+1))['allowed'])

    def test_redirect_is_rejected_before_following(self):
        with self.assertRaisesRegex(ValueError, 'redirects'):
            early.NoRedirects().redirect_request(None,None,302,'redirect',{},'https://other.example')
        with self.assertRaisesRegex(ValueError, 'destination'):
            early.fetch_bytes('https://other.example/snapshot.json', 100)

    def test_summary_and_output_include_skip_reason_and_retry(self):
        report = self.check(History([artifact(1, CAP_BYTES)]))
        with tempfile.TemporaryDirectory() as temp:
            output, summary = Path(temp)/'output', Path(temp)/'summary'
            with patch.dict(os.environ, {'GITHUB_OUTPUT':str(output), 'GITHUB_STEP_SUMMARY':str(summary), 'GITHUB_REPOSITORY':'example/tiles'}), patch.object(early,'check_capacity',return_value=report), patch('sys.stdout',new_callable=io.StringIO):
                self.assertEqual(early.main([]),0)
            self.assertEqual(output.read_text(),'allowed=false\n')
            self.assertIn(report['reason'],summary.read_text())
            self.assertIn(report['retryAt'],summary.read_text())
            self.assertIn('API-confirmed expiry',summary.read_text())

    def test_workflow_gate_precedes_tools_restore_and_runtime_reservation(self):
        workflow = (ROOT/'.github/workflows/rollout.yml').read_text()
        self.assertLess(workflow.index('publishing/early_capacity.py'), workflow.index('publishing/actions_budget.py'))
        self.assertLess(workflow.index('publishing/early_capacity.py'), workflow.index('apt-get'))
        self.assertIn("if: steps.ready.outputs.eligible == 'true' && steps.capacity.outputs.allowed == 'true'", workflow)
        self.assertIn('20s python publishing/early_capacity.py', workflow)
        self.assertIn('65s',workflow)
        self.assertNotIn('actions: write',workflow)
        self.assertIn('python publishing/exact_artifact.py',workflow)
        self.assertIn('assert_publication_unchanged',workflow)


if __name__ == '__main__':
    unittest.main()
