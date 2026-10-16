import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import resumable_region as r
import tile_generator as g

CLOCK = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)
CONFIG = {'id': 'fixture', 'name': 'Fixture', 'bounds': [-74.05, 40.65, -73.85, 40.9],
          'minzoom': 8, 'maxzoom': 12, 'sourceStates': ['new-york', 'new-jersey']}
SOURCE = b'pinned public osm source fixture' * 40_000
RUNTIME = {'python': '3.11.0', 'implementation': 'cpython', 'shapely': '2.1.2', 'geos': '3.13.1', 'osmium': 'osmium test'}


def plan(config=CONFIG, expires=False):
    release = dt.date(2026, 7, 1) if expires else dt.date(2026, 10, 1)
    value = {'schemaVersion': 1, 'configSha256': r.config_hash(config), 'sourceDate': release.isoformat(),
             'osmReplicationTimestamp': release.isoformat() + 'T20:21:31Z', 'runtime': RUNTIME,
             'createdAt': dt.datetime.combine(release, dt.time(), dt.timezone.utc).isoformat(),
             'expiresAt': (dt.datetime.combine(release, dt.time(), dt.timezone.utc) + dt.timedelta(days=90)).isoformat(),
             'sources': [{'state': state, 'url': r.BASE + state + '-' + release.strftime('%y%m%d') + '.osm.pbf',
                          'md5': hashlib.md5(SOURCE).hexdigest(), 'osmReplicationTimestamp': release.isoformat() + 'T20:21:31Z'} for state in config['sourceStates']]}
    value['planId'] = r.digest(value)
    return value


def features():
    def feature(props, coords, kind='LineString'):
        return {'type': 'Feature', 'properties': props, 'geometry': {'type': kind, 'coordinates': coords}}
    return [
        feature({'id': 1, 'highway': 'cycleway', 'name': 'first'}, [[-74.04, 40.66], [-73.86, 40.89]]),
        feature({'id': 1, 'highway': 'cycleway', 'name': 'duplicate ignored'}, [[-74.04, 40.8], [-73.86, 40.7]]),
        feature({'highway': 'residential', 'cycleway': 'lane', 'name': 'anonymous'}, [[-74, 40.7], [-73.87, 40.8]]),
        feature({'id': 6, 'tags': {'highway': 'path', 'bicycle': 'yes', 'surface': 'paved'}}, [[[-74.1, 40.7], [-73.99, 40.72]], [[-73.99, 40.72], [-73.9, 40.82]]], 'MultiLineString'),
        feature({'id': 3, 'highway': 'track', 'access': 'private'}, [[-74, 40.7], [-73.99, 40.71]]),
        feature({'id': 4, 'highway': 'cycleway'}, [[-87, 41], [-87.1, 41.1]]),
        feature({'id': 8, 'highway': 'track'}, [[-74, 40.7], [-74, 40.7]]),
        feature({'id': 9, 'highway': 'cycleway', 'name': 'last'}, [[-74.01, 40.79], [-73.9, 40.79]]),
    ]


def fake_osmium(*args):
    path = Path(args[args.index('-o') + 1])
    if args[1] == 'export':
        path.write_text('\x1e' + '\n'.join(map(json.dumps, features())) + '\ninvalid json\n')
    else:
        path.write_bytes(b'compact public crop fixture')


class ResumableStagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.checkpoint = self.root / 'checkpoint'
        self.source_plan = plan()
        self.patches = [patch.object(r, 'now_utc', side_effect=lambda now=None: now or CLOCK),
                        patch.object(r, 'runtime_fingerprint', return_value=RUNTIME),
                        patch.object(r, 'read_pbf_timestamp', return_value='2026-10-01T20:21:31Z'),
                        patch.object(r.legacy, 'download_source', side_effect=lambda url, path: Path(path).write_bytes(SOURCE)),
                        patch.object(r.legacy, 'run', side_effect=fake_osmium),
                        patch.object(r, 'source_timestamp', return_value='2026-10-01T20:21:31Z')]
        for item in self.patches:
            item.start()
        self.addCleanup(self.tmp.cleanup)
        for item in self.patches:
            self.addCleanup(item.stop)

    def make_sources(self):
        for state in CONFIG['sourceStates']:
            r.build_source_stage(CONFIG, self.source_plan, state, self.work, self.checkpoint)

    def make_index(self):
        self.make_sources()
        return r.build_index_stage(CONFIG, self.source_plan, self.checkpoint, self.work, batch_size=3)

    def make_batches(self):
        index = self.make_index()
        for batch_id in reversed(index['batchIds']):
            r.build_tile_stage(self.checkpoint, batch_id, self.work)
        return index

    def test_plan_uses_newest_common_monthly_release(self):
        def metadata(url, maximum=2_000_000):
            if url.endswith('.html'):
                state = url.rsplit('/', 1)[-1].removesuffix('.html')
                dates = ['260901', '261001', '261013'] if state == 'new-york' else ['260901', '261001', '261014']
                return '\n'.join(f'<a href="{state}-{date}.osm.pbf">pbf</a>' for date in dates)
            return hashlib.md5(SOURCE).hexdigest() + '  ' + url.rsplit('/', 1)[-1].removesuffix('.md5')
        with patch.object(r, 'fetch_small', side_effect=metadata):
            pinned = r.plan_sources(CONFIG, now=CLOCK)
        self.assertEqual(pinned['sourceDate'], '2026-10-01')
        self.assertEqual(pinned['expiresAt'], '2026-12-30T00:00:00+00:00')
        self.assertTrue(all('261001' in src['url'] and 'latest' not in src['url'] for src in pinned['sources']))

    def test_newest_common_daily_release_prevents_monthly_age_regression(self):
        def metadata(url, maximum=2_000_000):
            if url.endswith('.html'):
                state = url.rsplit('/', 1)[-1].removesuffix('.html')
                return '\n'.join(f'<a href="{state}-{date}.osm.pbf">pbf</a>' for date in ['261001', '261012', '261013'])
            return hashlib.md5(SOURCE).hexdigest() + '  ' + url.rsplit('/', 1)[-1].removesuffix('.md5')
        with patch.object(r, 'fetch_small', side_effect=metadata), patch.object(r, 'read_pbf_timestamp', return_value='2026-10-13T20:21:31Z'):
            pinned = r.plan_sources(CONFIG, now=CLOCK)
        self.assertEqual(pinned['sourceDate'], '2026-10-13')
        self.assertEqual(pinned['expiresAt'], '2026-10-20T00:00:00+00:00')
        self.assertEqual(pinned['osmReplicationTimestamp'], '2026-10-13T20:21:31Z')

    def test_invalid_checksum_and_no_common_release_fail_closed(self):
        with patch.object(r, 'fetch_small', return_value='no release'):
            with self.assertRaisesRegex(RuntimeError, 'common dated'):
                r.plan_sources(CONFIG, now=CLOCK)
        def metadata(url, maximum=2_000_000):
            if url.endswith('.html'):
                state = url.rsplit('/', 1)[-1].removesuffix('.html')
                return f'<a href="{state}-261001.osm.pbf">pbf</a>'
            return hashlib.md5(SOURCE).hexdigest() + '  new-york-latest.osm.pbf'
        with patch.object(r, 'fetch_small', side_effect=metadata):
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                r.plan_sources(CONFIG, now=CLOCK)

    def test_corrupt_source_never_commits_crop(self):
        with patch.object(r.legacy, 'download_source', side_effect=lambda url, path: Path(path).write_bytes(b'x' * len(SOURCE))):
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                r.build_source_stage(CONFIG, self.source_plan, 'new-york', self.work, self.checkpoint)
        self.assertFalse((self.checkpoint / 'sources/new-york').exists())

    def test_interruption_is_not_completed_and_resume_is_idempotent(self):
        def interrupted(*args):
            fake_osmium(*args)
            if args[1] == 'extract':
                raise KeyboardInterrupt('simulated kill')
        with patch.object(r.legacy, 'run', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                r.build_source_stage(CONFIG, self.source_plan, 'new-york', self.work, self.checkpoint)
        self.assertEqual(r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)['completedStates'], [])
        receipt = r.build_source_stage(CONFIG, self.source_plan, 'new-york', self.work, self.checkpoint)
        with patch.object(r.legacy, 'download_source', side_effect=AssertionError('must not redownload')):
            again = r.build_source_stage(CONFIG, self.source_plan, 'new-york', self.work, self.checkpoint)
        self.assertEqual(receipt, again)
        self.assertEqual(set(receipt['files']), {'crop.part-0000'})
        self.assertLess(sum(p.stat().st_size for p in self.checkpoint.rglob('*') if p.is_file()), 10_000)

    def test_source_timestamp_and_mixed_timestamp_fail_closed(self):
        with patch.object(r, 'source_timestamp', return_value='2026-10-02T20:21:31Z'):
            with self.assertRaisesRegex(RuntimeError, 'release date'):
                r.build_source_stage(CONFIG, self.source_plan, 'new-york', self.work, self.checkpoint)
        r.build_source_stage(CONFIG, self.source_plan, 'new-york', self.work, self.checkpoint)
        with patch.object(r, 'source_timestamp', return_value='2026-10-01T20:22:31Z'):
            with self.assertRaisesRegex(RuntimeError, 'Mixed OSM'):
                r.build_source_stage(CONFIG, self.source_plan, 'new-jersey', self.work, self.checkpoint)
        self.assertFalse((self.checkpoint / 'sources/new-jersey').exists())

    def test_expired_sources_fail_without_latest_substitution(self):
        expired = plan(expires=True)
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            r.build_source_stage(CONFIG, expired, 'new-york', self.work, self.checkpoint)
        r.legacy.download_source.assert_not_called()

    def test_durable_crops_can_finish_after_source_expiry(self):
        self.make_sources()
        with patch.object(r, 'now_utc', return_value=dt.datetime(2027, 2, 1, tzinfo=dt.timezone.utc)):
            index = r.build_index_stage(CONFIG, self.source_plan, self.checkpoint, self.work, batch_size=3)
            for batch_id in index['batchIds']:
                r.build_tile_stage(self.checkpoint, batch_id, self.work)
            entry = r.assemble_region(CONFIG, self.source_plan, self.checkpoint, self.work, self.root / 'site', r.PUBLIC_BASE)
        self.assertGreater(entry['tileCount'], 0)

    def test_corrupt_crop_is_rejected_before_index(self):
        self.make_sources()
        crop = self.checkpoint / 'sources/new-york/crop.part-0000'
        crop.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'hash/size mismatch'):
            r.build_index_stage(CONFIG, self.source_plan, self.checkpoint, self.work, batch_size=3)

    def test_index_and_batches_match_uninterrupted_bytes_and_version(self):
        index = self.make_index()
        seq = self.root / 'uninterrupted.jsonl'
        seq.write_text('\x1e' + '\n'.join(map(json.dumps, features())) + '\ninvalid json\n')
        baseline = self.root / 'baseline'
        with patch.object(g, 'BBOX', tuple(CONFIG['bounds'])):
            g.generate(seq, baseline, range(CONFIG['minzoom'], CONFIG['maxzoom'] + 1))
        first = r.build_tile_stage(self.checkpoint, index['batchIds'][-1], self.work)
        with patch.object(g, 'generate_tile_batch', side_effect=AssertionError('must reuse completed batch')):
            self.assertEqual(first, r.build_tile_stage(self.checkpoint, index['batchIds'][-1], self.work))
        for batch_id in reversed(index['batchIds'][:-1]):
            r.build_tile_stage(self.checkpoint, batch_id, self.work)
        actual = {}
        for path in (self.checkpoint / 'batches').rglob('*.pbf'):
            name = 'tiles/' + path.relative_to(self.checkpoint / 'batches').as_posix().split('/tiles/', 1)[1]
            self.assertNotIn(name, actual)
            actual[name] = path.read_bytes()
        expected = {path.relative_to(baseline).as_posix(): gzip.decompress(path.read_bytes()) for path in (baseline / 'tiles').rglob('*.pbf')}
        self.assertEqual(expected, actual)
        entry = r.assemble_region(CONFIG, self.source_plan, self.checkpoint, self.work, self.root / 'site', r.PUBLIC_BASE)
        hashes = {key.removeprefix('tiles/'): hashlib.sha256(value).hexdigest() for key, value in expected.items()}
        version = hashlib.sha256(''.join(f'{key} {hashes[key]}\n' for key in sorted(hashes)).encode()).hexdigest()[:16]
        self.assertEqual(entry['version'], version)
        final = self.root / 'site/regions/fixture' / version
        self.assertEqual({path.relative_to(final).as_posix(): path.read_bytes() for path in (final / 'tiles').rglob('*.pbf')}, expected)
        self.assertEqual(json.loads((final / 'provenance.json').read_text())['sourcePlanId'], self.source_plan['planId'])

    def test_missing_or_duplicate_batch_never_finalizes(self):
        index = self.make_index()
        r.build_tile_stage(self.checkpoint, index['batchIds'][0], self.work)
        with self.assertRaisesRegex(RuntimeError, 'missing tile batches'):
            r.assemble_region(CONFIG, self.source_plan, self.checkpoint, self.work, self.root / 'site', r.PUBLIC_BASE)
        shutil.copytree(self.checkpoint / 'batches/000000', self.checkpoint / 'batches/000001')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected tile batch ID'):
            r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)
        self.assertFalse((self.root / 'site').exists())

    def test_corrupt_index_and_batch_are_rejected(self):
        index = self.make_index()
        target = self.checkpoint / 'index/features.part-0000'
        old = target.read_bytes()
        target.write_bytes(b'broken')
        with self.assertRaisesRegex(RuntimeError, 'hash/size mismatch'):
            r.build_tile_stage(self.checkpoint, 0, self.work)
        target.write_bytes(old)
        r.build_tile_stage(self.checkpoint, 0, self.work)
        tile = next((self.checkpoint / 'batches/000000').rglob('*.pbf'))
        tile.unlink()
        with self.assertRaisesRegex(RuntimeError, 'hash/size mismatch'):
            r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)

    def test_chunked_payload_round_trip_and_duplicate_rejected(self):
        source = self.root / 'large'
        source.write_bytes(bytes(range(256)) * 100)
        chunks = self.root / 'chunks'
        with patch.object(r, 'CHUNK_BYTES', 1_000):
            descriptor = r.pack_file(source, chunks, 'test', compress=False)
            self.assertGreater(len(descriptor['parts']), 1)
            r.unpack_file(descriptor, chunks, self.root / 'restored')
            self.assertEqual(source.read_bytes(), (self.root / 'restored').read_bytes())
            descriptor['parts'].append(descriptor['parts'][0])
            with self.assertRaisesRegex(RuntimeError, 'Duplicate checkpoint chunk'):
                r.unpack_file(descriptor, chunks, self.root / 'bad')

    def test_metadata_downloads_charge_shared_run_budget(self):
        import io
        class Response(io.BytesIO):
            def geturl(self):
                return r.BASE + 'new-york.html'
        ledger = self.root / 'budget.json'
        with patch.dict(r.os.environ, {'RIDE_DOWNLOAD_LEDGER': str(ledger)}), patch.object(r.urllib.request, 'urlopen', return_value=Response(b'abc')):
            self.assertEqual(r.fetch_small(r.BASE + 'new-york.html', maximum=10), 'abc')
        self.assertEqual(json.loads(ledger.read_text()), {'reservedBytes': 3})
        with patch.dict(r.os.environ, {'RIDE_DOWNLOAD_LEDGER': str(ledger)}), patch.object(r.urllib.request, 'urlopen', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                r.fetch_small(r.BASE + 'new-york.html', maximum=10)
        self.assertEqual(json.loads(ledger.read_text()), {'reservedBytes': 14})
        ledger.write_text(json.dumps({'reservedBytes': 2_999_999_999}))
        with patch.dict(r.os.environ, {'RIDE_DOWNLOAD_LEDGER': str(ledger)}), patch.object(r.urllib.request, 'urlopen') as network:
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                r.fetch_small(r.BASE + 'new-york.html', maximum=10)
            network.assert_not_called()

    def test_special_or_nested_unlisted_files_are_rejected(self):
        self.make_sources()
        root = self.checkpoint / 'sources/new-york'
        (root / 'extra').mkdir()
        (root / 'extra/receipt.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'hash/size mismatch'):
            r.verify_receipt(root)
        (root / 'extra/receipt.json').unlink()
        r.os.mkfifo(root / 'fifo')
        with self.assertRaisesRegex(RuntimeError, 'filesystem object'):
            r.verify_receipt(root)

    def test_public_source_above_file_cap_is_published_in_exact_chunks(self):
        self.make_sources()
        source_size = 24_661_906  # The verified live Illinois crop size.
        def oversized_merge(*args):
            if args[1] == 'merge':
                Path(args[args.index('-o') + 1]).write_bytes(b'x' * source_size)
            else:
                fake_osmium(*args)
        with patch.object(r.legacy, 'run', side_effect=oversized_merge):
            index = r.build_index_stage(CONFIG, self.source_plan, self.checkpoint, self.work, batch_size=3)
        self.assertEqual(index['mergedSource']['bytes'], source_size)
        r.build_tile_group(self.checkpoint, self.work)
        entry = r.assemble_region(CONFIG, self.source_plan, self.checkpoint, self.work, self.root / 'site', r.PUBLIC_BASE)
        root = self.root / 'site/regions/fixture' / entry['version']
        descriptor = r.validate_source_package(root, expected_sha256=entry['sourceArchiveSha256'])
        self.assertEqual(entry['sourceFormat'], 'osm-pbf-chunks-v1')
        self.assertTrue(entry['sourceDownload'].endswith('/cycling-source.json'))
        self.assertEqual(descriptor['bytes'], source_size)
        self.assertEqual(len(descriptor['chunks']), 4)
        self.assertEqual(descriptor['sha256'], hashlib.sha256(b'x' * source_size).hexdigest())
        self.assertFalse((root / 'cycling-source.osm.pbf').exists())
        self.assertTrue(all(p.stat().st_size <= r.MAX_FILE_BYTES for p in root.rglob('*') if p.is_file()))
        provenance = json.loads((root / 'provenance.json').read_text())
        self.assertEqual(provenance['sourceExtractSha256'], descriptor['sha256'])
        self.assertEqual(provenance['sourceExtractBytes'], source_size)
        self.assertEqual(provenance['sourcePackageSha256'], entry['sourceArchiveSha256'])

    def test_runtime_change_blocks_every_resume_path(self):
        self.make_index()
        with patch.object(r, 'runtime_fingerprint', return_value={**RUNTIME, 'geos': 'different'}):
            with self.assertRaisesRegex(RuntimeError, 'runtime changed'):
                r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)
            with self.assertRaisesRegex(RuntimeError, 'runtime changed'):
                r.build_tile_stage(self.checkpoint, 0, self.work)
            with self.assertRaisesRegex(RuntimeError, 'runtime changed'):
                r.build_tile_group(self.checkpoint, self.work)

    def test_plan_pins_common_header_timestamp(self):
        def metadata(url, maximum=2_000_000):
            if url.endswith('.html'):
                state = url.rsplit('/', 1)[-1].removesuffix('.html')
                return f'<a href="{state}-261001.osm.pbf">pbf</a>'
            return hashlib.md5(SOURCE).hexdigest() + '  ' + url.rsplit('/', 1)[-1].removesuffix('.md5')
        with patch.object(r, 'fetch_small', side_effect=metadata), patch.object(r, 'read_pbf_timestamp', side_effect=['2026-10-01T20:21:31Z', '2026-10-01T20:22:31Z']):
            with self.assertRaisesRegex(RuntimeError, 'mixed OSM'):
                r.plan_sources(CONFIG, now=CLOCK)

    def test_tile_groups_pause_then_resume_without_reencoding(self):
        index = self.make_index()
        with patch.object(r.time, 'monotonic', side_effect=[0, 0, 0, 0, 40, 40]):
            first = r.build_tile_group(self.checkpoint, self.work, seconds=100, reserve_seconds=30)
        self.assertEqual(first['completedBatches'], [0])
        self.assertTrue(first['plannedPause'])
        before = r.file_map(self.checkpoint / 'batches/000000')
        with patch.object(r.generator, 'generate_tile_batch', wraps=r.generator.generate_tile_batch) as encode:
            result = r.build_tile_group(self.checkpoint, self.work)
        self.assertEqual(result['completedBatches'], index['batchIds'][1:])
        self.assertEqual(result['remainingBatches'], [])
        self.assertFalse(result['plannedPause'])
        self.assertEqual(r.file_map(self.checkpoint / 'batches/000000'), before)
        self.assertEqual(encode.call_count, len(index['batchIds']) - 1)
        self.assertEqual(r.build_tile_group(self.checkpoint, self.work)['completedBatches'], [])

    def test_interrupted_tile_group_keeps_only_complete_local_batches(self):
        index = self.make_index()
        original = g.generate_tile_batch
        calls = []
        def interrupt(database, output, batch_id):
            result = original(database, output, batch_id)
            calls.append(batch_id)
            if len(calls) == 2:
                raise KeyboardInterrupt('simulated kill after writing uncommitted tiles')
            return result
        with patch.object(g, 'generate_tile_batch', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                r.build_tile_group(self.checkpoint, self.work)
        status = r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)
        self.assertEqual(status['completedBatches'], [0])
        self.assertFalse((self.checkpoint / 'batches/000001').exists())
        result = r.build_tile_group(self.checkpoint, self.work)
        self.assertEqual(result['completedBatches'], index['batchIds'][1:])
        self.assertEqual(result['remainingBatches'], [])

    def test_interrupted_index_preserves_sources_and_can_resume(self):
        self.make_sources()
        original = g.build_feature_index
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt('simulated kill before index commit')
        with patch.object(g, 'build_feature_index', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                r.build_index_stage(CONFIG, self.source_plan, self.checkpoint, self.work, batch_size=3)
        status = r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)
        self.assertEqual(status['completedStates'], CONFIG['sourceStates'])
        self.assertFalse(status['indexReady'])
        r.build_index_stage(CONFIG, self.source_plan, self.checkpoint, self.work, batch_size=3)
        self.assertTrue(r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)['indexReady'])

    def test_source_groups_pause_resume_and_never_repeat_completed_state(self):
        with patch.object(r.time, 'monotonic', side_effect=[0, 0, 0, 80, 80]):
            first = r.build_source_group(CONFIG, self.source_plan, self.checkpoint, self.work, seconds=180, reserve_seconds=30)
        self.assertEqual(first['completedStates'], ['new-york'])
        self.assertEqual(first['remainingStates'], ['new-jersey'])
        self.assertTrue(first['plannedPause'])
        first_files = r.file_map(self.checkpoint / 'sources/new-york')
        r.legacy.download_source.reset_mock()
        rest = r.build_source_group(CONFIG, self.source_plan, self.checkpoint, self.work)
        self.assertEqual(rest, {'completedStates': ['new-jersey'], 'remainingStates': [], 'plannedPause': False})
        r.legacy.download_source.assert_called_once()
        self.assertEqual(first_files, r.file_map(self.checkpoint / 'sources/new-york'))
        self.assertEqual(r.build_source_group(CONFIG, self.source_plan, self.checkpoint, self.work)['completedStates'], [])

    def test_source_group_too_little_time_does_not_download(self):
        result = r.build_source_group(CONFIG, self.source_plan, self.checkpoint, self.work, seconds=30, reserve_seconds=20)
        self.assertEqual(result, {'completedStates': [], 'remainingStates': CONFIG['sourceStates'], 'plannedPause': True})
        r.legacy.download_source.assert_not_called()

    def test_index_compacts_only_redundant_crops_and_preserves_provenance(self):
        self.make_sources()
        original = {state: (self.checkpoint / 'sources' / state / 'receipt.json').read_bytes() for state in CONFIG['sourceStates']}
        index = r.build_index_stage(CONFIG, self.source_plan, self.checkpoint, self.work, batch_size=3)
        self.assertTrue(index['compactedSourceCrops'])
        for state in CONFIG['sourceStates']:
            root = self.checkpoint / 'sources' / state
            compacted = r.verify_receipt(root)
            previous = json.loads(original[state])
            self.assertEqual(compacted['source'], previous['source'])
            self.assertEqual(compacted['crop'], previous['crop'])
            self.assertEqual(compacted['files'], {})
            self.assertEqual([path.name for path in root.iterdir()], ['receipt.json'])
            self.assertEqual(index['sourceReceiptHashes'][state], hashlib.sha256(original[state]).hexdigest())
        self.assertTrue(r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)['indexReady'])
        r.build_tile_group(self.checkpoint, self.work)
        r.assemble_region(CONFIG, self.source_plan, self.checkpoint, self.work, self.root / 'site', r.PUBLIC_BASE)

    def test_compacted_source_requires_valid_complete_index_and_original_metadata(self):
        self.make_index()
        root = self.checkpoint / 'sources/new-york'
        receipt = json.loads((root / 'receipt.json').read_text())
        receipt['source']['downloadedAt'] = '2026-10-02T00:00:00Z'
        r.write_json(root / 'receipt.json', receipt)
        with self.assertRaisesRegex(RuntimeError, 'original receipt'):
            r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)
        receipt['source']['downloadedAt'] = CLOCK.isoformat()
        r.write_json(root / 'receipt.json', receipt)
        saved_index = self.root / 'saved-index'
        (self.checkpoint / 'index').rename(saved_index)
        with self.assertRaisesRegex(RuntimeError, 'missing its canonical index'):
            r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)
        saved_index.rename(self.checkpoint / 'index')
        receipt['compactedInto']['mergedSourceSha256'] = '0' * 64
        r.write_json(root / 'receipt.json', receipt)
        with self.assertRaisesRegex(RuntimeError, 'match canonical index'):
            r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)

    def test_interrupted_compaction_cannot_mutate_previous_checkpoint(self):
        self.make_sources()
        previous = r.file_map(self.checkpoint)
        candidate = self.root / 'candidate'
        shutil.copytree(self.checkpoint, candidate)
        original = r.compact_source_crops
        def interrupt(config, plan, checkpoint, index):
            original({**config, 'sourceStates': config['sourceStates'][:1]}, plan, checkpoint, index)
            raise KeyboardInterrupt('simulated interruption after first compacted crop')
        with patch.object(r, 'compact_source_crops', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                r.build_index_stage(CONFIG, self.source_plan, candidate, self.work, batch_size=3)
        self.assertEqual(previous, r.file_map(self.checkpoint))
        self.assertFalse(r.validate_checkpoint(CONFIG, self.source_plan, self.checkpoint)['indexReady'])
        with self.assertRaisesRegex(RuntimeError, 'Incomplete source compaction'):
            r.validate_checkpoint(CONFIG, self.source_plan, candidate)
        shutil.rmtree(candidate)
        shutil.copytree(self.checkpoint, candidate)
        r.build_index_stage(CONFIG, self.source_plan, candidate, self.work, batch_size=3)
        self.assertTrue(r.validate_checkpoint(CONFIG, self.source_plan, candidate)['indexReady'])
        self.assertEqual(previous, r.file_map(self.checkpoint))

    def test_corrupt_packed_merge_does_not_remove_source_crops(self):
        self.make_sources()
        previous = r.file_map(self.checkpoint)
        original = r.pack_file
        def corrupt(source, root, name, compress=False):
            descriptor = original(source, root, name, compress=compress)
            if name == 'merged-source':
                path = Path(root) / descriptor['parts'][0]['path']
                path.write_bytes(b'bad data')
            return descriptor
        with patch.object(r, 'pack_file', side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'Corrupt checkpoint chunk'):
                r.build_index_stage(CONFIG, self.source_plan, self.checkpoint, self.work, batch_size=3)
        self.assertEqual(previous, r.file_map(self.checkpoint))
        self.assertFalse((self.checkpoint / 'index').exists())

    def test_no_batch_fits_returns_planned_pause(self):
        index = self.make_index()
        result = r.build_tile_group(self.checkpoint, self.work, seconds=30, reserve_seconds=20)
        self.assertEqual(result['completedBatches'], [])
        self.assertEqual(result['remainingBatches'], index['batchIds'])
        self.assertTrue(result['plannedPause'])

    def test_pbf_header_reader_raw_compressed_and_corrupt(self):
        import zlib
        timestamp = int(dt.datetime(2026, 10, 1, 20, 21, 31, tzinfo=dt.timezone.utc).timestamp())
        raw = g.wire(4, 2, b'OsmSchema-V0.6') + g.wire(32, 0, timestamp)
        for blob in (g.wire(1, 2, raw), g.wire(2, 0, len(raw)) + g.wire(3, 2, zlib.compress(raw))):
            header = g.wire(1, 2, b'OSMHeader') + g.wire(3, 0, len(blob))
            pbf = len(header).to_bytes(4, 'big') + header + blob
            self.assertEqual(r.pbf_header_timestamp(pbf), '2026-10-01T20:21:31Z')
            with self.assertRaisesRegex(RuntimeError, 'bounded prefix'):
                r.pbf_header_timestamp(pbf[:-1])
        with self.assertRaises(RuntimeError):
            r.pbf_header_timestamp(b'bad')

    def test_mixed_plan_or_configuration_is_rejected(self):
        bad = json.loads(json.dumps(self.source_plan))
        bad['sources'][0]['url'] = r.BASE + 'new-york-latest.osm.pbf'
        bad['planId'] = r.digest({k: v for k, v in bad.items() if k != 'planId'})
        with self.assertRaisesRegex(RuntimeError, 'pinned source identity'):
            r.validate_source_plan(CONFIG, bad, now=CLOCK)
        with self.assertRaisesRegex(RuntimeError, 'identity/config mismatch'):
            r.validate_checkpoint({**CONFIG, 'name': 'Different'}, self.source_plan, self.checkpoint)


if __name__ == '__main__':
    unittest.main()
