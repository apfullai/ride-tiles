"""Offline regression tests for the durable Pages recovery boundary."""
import copy
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'publishing'))
import rollout_snapshot as snapshot


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + '\n')


def add_region(site, rid='tiny-region', tile=b'public tile'):
    hashes = {'0/0/0.pbf': snapshot.digest(tile)}
    version = snapshot.digest(''.join(f'{k} {hashes[k]}\n' for k in sorted(hashes)).encode())[:16]
    root = site / 'regions' / rid / version
    (root / 'tiles/0/0').mkdir(parents=True)
    (root / 'tiles/0/0/0.pbf').write_bytes(tile)
    source = b'public source extract'
    (root / 'cycling-source.osm.pbf').write_bytes(source)
    dump(root / 'tile-sha256.json', hashes)
    dump(root / 'provenance.json', {'sourceExtractSha256': snapshot.digest(source),
         'sources': [{'url': 'https://example.org/source.osm.pbf', 'sha256': snapshot.digest(source)}]})
    prefix = f'{snapshot.BASE}/regions/{rid}/{version}'
    return {'id': rid, 'name': rid, 'version': version, 'bounds': [-1, -1, 1, 1],
            'tiles': [prefix + '/tiles/{z}/{x}/{y}.pbf'], 'tileCount': 1, 'tileBytes': len(tile),
            'sourceUrl': prefix + '/provenance.json', 'sha256Manifest': prefix + '/tile-sha256.json',
            'sourceDownload': prefix + '/cycling-source.osm.pbf'}


def small_site(path):
    path.mkdir()
    dump(path / 'manifest.json', {'schemaVersion': 1, 'updatedAt': 'old',
                                 'regions': [add_region(path)]})
    dump(path / 'rollout-state.json', {'cursor': 7})
    (path / 'index.html').write_text('public index')
    return path


def remote_files(site):
    return {snapshot.BASE + '/' + p.relative_to(site).as_posix(): p.read_bytes()
            for p in site.rglob('*') if p.is_file()}


def fetch_from(files):
    def fetch(url, maximum):
        if url not in files:
            raise HTTPError(url, 404, 'Not Found', {}, None)
        return files[url]
    return fetch


class DurableSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = small_site(self.root / 'site')
        frozen = patch.object(snapshot, '_validate_frozen')
        frozen.start()
        self.addCleanup(frozen.stop)

    def seal(self):
        report = snapshot.seal_snapshot(self.site)
        return report, remote_files(self.site)

    def restore(self, files, name='restored', **kwargs):
        destination = self.root / name
        result = snapshot.restore_published(destination, fetch=fetch_from(files), **kwargs)
        return destination, result

    def chunk_source(self, source=b'public source extract'):
        from source_package import write_source_package
        manifest=json.loads((self.site/'manifest.json').read_text())
        region=manifest['regions'][0];root=self.site/'regions'/region['id']/region['version']
        original=root/'cycling-source.osm.pbf';original.write_bytes(source)
        package=write_source_package(original,root);original.unlink()
        region.update(sourceDownload=region['sourceDownload'].replace('cycling-source.osm.pbf','cycling-source.json'),
                      sourceFormat='osm-pbf-chunks-v1',sourceArchiveSha256=package['descriptorSha256'])
        provenance=json.loads((root/'provenance.json').read_text())
        provenance.update(sourceExtractSha256=snapshot.digest(source),sourceExtractBytes=len(source),
                          sourcePackageSha256=package['descriptorSha256'],sourceFormat='osm-pbf-chunks-v1')
        dump(root/'provenance.json',provenance);dump(self.site/'manifest.json',manifest)
        return root,package

    def test_chunked_source_over_file_ceiling_round_trips_with_legacy_regions(self):
        root,package=self.chunk_source(b'X'*(24_000_000+1))
        manifest=json.loads((self.site/'manifest.json').read_text())
        manifest['regions'].append(add_region(self.site,'legacy-region'))
        dump(self.site/'manifest.json',manifest)
        self.assertTrue(all(p.stat().st_size<=8_000_000 for p in (root/'cycling-source').iterdir()))
        self.assertFalse((root/'cycling-source.osm.pbf').exists())
        report,files=self.seal();restored,_=self.restore(files)
        self.assertEqual(snapshot.inventory(restored),snapshot.inventory(self.site))
        self.assertEqual(report['siteBytes'],sum(p.stat().st_size for p in self.site.rglob('*') if p.is_file()))
        self.assertLess(report['siteBytes'],snapshot.MAX_SITE_BYTES)

    def test_chunked_source_rejects_corruption_and_missing_parts(self):
        root,package=self.chunk_source()
        part=root/package['descriptor']['chunks'][0]['path'];original=part.read_bytes()
        part.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):snapshot.validate_site(self.site)
        part.write_bytes(original);part.unlink()
        with self.assertRaises((ValueError,FileNotFoundError)):snapshot.validate_site(self.site)

    def test_chunked_source_requires_both_descriptor_and_reconstructed_identity(self):
        root,package=self.chunk_source()
        for key,value in (('sourceExtractSha256','0'*64),('sourceExtractBytes',1),('sourcePackageSha256','0'*64)):
            with self.subTest(key=key):
                original=(root/'provenance.json').read_bytes()
                provenance=json.loads(original);provenance[key]=value;dump(root/'provenance.json',provenance)
                with self.assertRaises(ValueError):snapshot.validate_site(self.site)
                (root/'provenance.json').write_bytes(original)

    def test_unknown_source_format_is_not_treated_as_legacy(self):
        manifest=json.loads((self.site/'manifest.json').read_text());manifest['regions'][0]['sourceFormat']='unknown'
        dump(self.site/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'Unsupported public source format'):snapshot.validate_site(self.site)

    def test_round_trip_preserves_every_file_and_rollout_state(self):
        manifest = json.loads((self.site / 'manifest.json').read_text())
        # An unknown region, its previous content version, and custom metadata
        # beneath an existing version must survive later runs unchanged.
        manifest['regions'].append(add_region(self.site, 'unknown-region', b'unknown'))
        add_region(self.site, 'unknown-region', b'previous version')
        version = manifest['regions'][0]['version']
        (self.site / 'regions/tiny-region' / version / 'extra-metadata.txt').write_text('retain me')
        dump(self.site / 'manifest.json', manifest)
        before = snapshot.inventory(self.site)
        report, files = self.seal()
        receipt = {}
        restored, actual = self.restore(files, receipt=receipt)
        self.assertEqual(actual, manifest)
        self.assertEqual(snapshot.inventory(restored), before)
        self.assertEqual((restored / 'rollout-state.json').read_bytes(), (self.site / 'rollout-state.json').read_bytes())
        self.assertEqual(receipt, {'bootstrap': False,
            'liveManifestSha256': snapshot.digest(files[snapshot.BASE + '/manifest.json']),
            'liveSnapshotSha256': snapshot.digest(files[snapshot.BASE + '/snapshot.json']),
            'publicationRevision': snapshot.publication_revision(files[snapshot.BASE + '/manifest.json'],
                                                                 files[snapshot.BASE + '/snapshot.json'])})
        self.assertEqual(report['siteBytes'], sum(len(v) for v in files.values()))
        self.assertEqual(snapshot.validate_site(restored), manifest)

    def test_reseal_is_deterministic_and_does_not_archive_prior_snapshot(self):
        _, first = self.seal()
        snapshot.seal_snapshot(self.site)
        self.assertEqual(remote_files(self.site), first)
        conf = json.loads((self.site / 'snapshot.json').read_text())
        self.assertFalse(any(snapshot._metadata(k) for k in conf['files']))
        (self.site / 'snapshot/obsolete.part').write_text('obsolete transport')
        snapshot.seal_snapshot(self.site)
        self.assertFalse((self.site / 'snapshot/obsolete.part').exists())
        self.assertEqual(remote_files(self.site), first)

    def test_corrupt_shard_fails_without_destination(self):
        _, files = self.seal()
        shard = next(key for key in files if '/snapshot/' in key)
        files[shard] = b'corrupt'
        with self.assertRaisesRegex(ValueError, 'digest or size'):
            self.restore(files)
        self.assertFalse((self.root / 'restored').exists())

    def test_manifest_race_or_descriptor_corruption_fails_without_bootstrap(self):
        _, files = self.seal()
        for bad in (b'not JSON', b'{"schemaVersion":1}', b'{"a":1,"a":2}'):
            candidate = dict(files)
            candidate[snapshot.BASE + '/snapshot.json'] = bad
            with patch.object(snapshot, '_bootstrap', side_effect=AssertionError('No fallback')):
                with self.assertRaises(ValueError):
                    self.restore(candidate)
        files[snapshot.BASE + '/manifest.json'] += b' '
        with self.assertRaisesRegex(ValueError, 'live manifest'):
            self.restore(files)
        self.assertFalse((self.root / 'restored').exists())

    def test_only_descriptor_404_enables_exact_bootstrap(self):
        live = json.loads((self.site / 'manifest.json').read_text())
        live['updatedAt'] = 'live time'
        live_bytes = json.dumps(live).encode()
        files = {snapshot.BASE + '/manifest.json': live_bytes}
        with patch.object(snapshot, '_bootstrap', side_effect=lambda path: shutil.copytree(self.site, path)):
            restored, result = self.restore(files)
        self.assertEqual(result, live)
        self.assertEqual((restored / 'manifest.json').read_bytes(), live_bytes)
        for change in ('extra-region', 'changed-region'):
            bad = copy.deepcopy(live)
            if change == 'extra-region':
                extra = copy.deepcopy(bad['regions'][0]); extra['id'] = 'extra'
                bad['regions'].append(extra)
            else:
                bad['regions'][0]['name'] = 'changed'
            files[snapshot.BASE + '/manifest.json'] = json.dumps(bad).encode()
            with patch.object(snapshot, '_bootstrap', side_effect=lambda path: shutil.copytree(self.site, path)):
                with self.assertRaisesRegex(ValueError, 'refusing rollback'):
                    self.restore(files, name=change)
            self.assertFalse((self.root / change).exists())

    def test_network_errors_and_non404_http_errors_do_not_fallback(self):
        live = (self.site / 'manifest.json').read_bytes()
        for error in (URLError('offline'), HTTPError('url', 503, 'Unavailable', {}, None)):
            def fetch(url, maximum):
                if url.endswith('/manifest.json'):
                    return live
                raise error
            with patch.object(snapshot, '_bootstrap', side_effect=AssertionError('No fallback')):
                with self.assertRaises(type(error)):
                    snapshot.restore_published(self.root / 'restored', fetch=fetch)
            self.assertFalse((self.root / 'restored').exists())
        with patch.object(snapshot, '_bootstrap', side_effect=AssertionError('No fallback')):
            with self.assertRaises(HTTPError):
                snapshot.restore_published(self.root / 'restored', fetch=fetch_from({}))

    def test_descriptor_rejects_paths_sizes_collisions_and_missing_inventory(self):
        _, files = self.seal()
        original = json.loads(files[snapshot.BASE + '/snapshot.json'])
        mutations = [
            lambda c: c['files'].update({'../outside': {'sha256': '0' * 64, 'size': 0}}),
            lambda c: c['files'].update({'snapshot/recursive': {'sha256': '0' * 64, 'size': 0}}),
            lambda c: c['files'].update({'index.html/child': {'sha256': '0' * 64, 'size': 0}}),
            lambda c: c['files']['index.html'].update(size=snapshot.MAX_FILE_BYTES + 1),
            lambda c: c['files']['index.html'].update(size=True),
            lambda c: c['files']['index.html'].update(sha256='invalid'),
            lambda c: c['files'].pop('manifest.json'),
            lambda c: c['shards'][0].update(path='https://elsewhere.example/archive'),
            lambda c: c['shards'][0].update(size=snapshot.MAX_SHARD_BYTES + 1),
            lambda c: c.update(schemaVersion=True),
        ]
        for mutate in mutations:
            conf = copy.deepcopy(original)
            mutate(conf)
            candidate = dict(files)
            candidate[snapshot.BASE + '/snapshot.json'] = json.dumps(conf).encode()
            with self.assertRaises(ValueError):
                self.restore(candidate)
            self.assertFalse((self.root / 'restored').exists())

    def test_existing_destination_and_symlink_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            snapshot.restore_published(self.site, fetch=lambda *_: self.fail('Must not fetch'))
        (self.root / 'link').symlink_to(self.root / 'missing')
        with self.assertRaises(ValueError):
            snapshot.restore_published(self.root / 'link', fetch=lambda *_: self.fail('Must not fetch'))
        (self.site / 'bad-link').symlink_to(self.site / 'manifest.json')
        with self.assertRaisesRegex(ValueError, 'regular'):
            snapshot.seal_snapshot(self.site)

    def _replace_archive(self, files, entries, adjust=None):
        conf = json.loads(files[snapshot.BASE + '/snapshot.json'])
        packed = io.BytesIO()
        with tarfile.open(fileobj=packed, mode='w:gz') as tar:
            for name, data, kind in entries:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = '/outside'
                tar.addfile(member, io.BytesIO(data))
        data = packed.getvalue()
        conf['archiveSha256'] = snapshot.digest(data)
        conf['shards'] = [{'path': 'snapshot/archive.tar.gz.part00000', 'sha256': snapshot.digest(data), 'size': len(data)}]
        if adjust:
            adjust(conf)
        files[snapshot.BASE + '/snapshot/archive.tar.gz.part00000'] = data
        files[snapshot.BASE + '/snapshot.json'] = json.dumps(conf).encode()
        return files

    def test_unsafe_duplicate_and_unexpected_tar_members_fail(self):
        _, files = self.seal()
        data = (self.site / 'index.html').read_bytes()
        variants = [[('../outside', data, tarfile.REGTYPE)],
                    [('/outside', data, tarfile.REGTYPE)],
                    [('index.html', data, tarfile.SYMTYPE)],
                    [('index.html', data, tarfile.LNKTYPE)],
                    [('index.html', data, tarfile.DIRTYPE)],
                    [('unexpected', data, tarfile.REGTYPE)],
                    [('index.html', data, tarfile.REGTYPE)] * 2]
        for entries in variants:
            with self.subTest(entries=entries):
                bad = self._replace_archive(dict(files), entries)
                with self.assertRaises(ValueError):
                    self.restore(bad)
                self.assertFalse((self.root / 'restored').exists())
        self.assertFalse((self.root / 'outside').exists())

    def test_missing_member_and_per_file_corruption_fail(self):
        _, files = self.seal()
        entries = [(name, (self.site / name).read_bytes(), tarfile.REGTYPE)
                   for name in snapshot.inventory(self.site)]
        for bad_entries in (entries[1:], [('index.html', b'wrong bytes!', tarfile.REGTYPE)] + entries[1:]):
            bad = self._replace_archive(dict(files), bad_entries)
            with self.assertRaises(ValueError):
                self.restore(bad)
            self.assertFalse((self.root / 'restored').exists())

    def test_decompression_bomb_rejected_before_tar_parse(self):
        _, files = self.seal()
        conf = json.loads(files[snapshot.BASE + '/snapshot.json'])
        data = gzip.compress(b'\0' * 200_000)
        conf['archiveSha256'] = snapshot.digest(data)
        conf['shards'] = [{'path': 'snapshot/archive.tar.gz.part00000', 'sha256': snapshot.digest(data), 'size': len(data)}]
        files[snapshot.BASE + '/snapshot/archive.tar.gz.part00000'] = data
        files[snapshot.BASE + '/snapshot.json'] = json.dumps(conf).encode()
        with self.assertRaisesRegex(ValueError, 'decompression'):
            self.restore(files)
        self.assertFalse((self.root / 'restored').exists())

    def test_shards_are_bounded_and_hash_pinned(self):
        (self.site / 'incompressible-source.bin').write_bytes(os.urandom(24_000))
        with patch.object(snapshot, 'MAX_SHARD_BYTES', 2048):
            report, files = self.seal()
            conf = json.loads((self.site / 'snapshot.json').read_text())
            self.assertGreater(report['shards'], 5)
            self.assertTrue(all(0 < s['size'] <= 2048 for s in conf['shards']))
            restored, _ = self.restore(files)
            self.assertEqual(snapshot.inventory(restored), snapshot.inventory(self.site))

    def test_manifest_region_file_and_total_caps(self):
        original = snapshot.inventory(self.site)
        for setting, maximum in (('MAX_MANIFEST_BYTES', 100), ('MAX_FILE_BYTES', 100),
                                 ('MAX_SITE_BYTES', 100), ('MAX_REGIONS', 0)):
            with self.subTest(setting=setting), patch.object(snapshot, setting, maximum):
                with self.assertRaises(ValueError):
                    snapshot.seal_snapshot(self.site)
        self.assertEqual(snapshot.inventory(self.site), original)
        report, files = self.seal()
        with patch.object(snapshot, 'MAX_SITE_BYTES', report['dataBytes'] + 1):
            with self.assertRaisesRegex(ValueError, 'including snapshot'):
                snapshot.seal_snapshot(self.site)
        self.assertEqual(remote_files(self.site), files)

    def test_tile_source_provenance_and_local_urls_are_validated(self):
        manifest_path = self.site / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        for field, value in [('tiles', ['https://elsewhere.example/tiles/{z}/{x}/{y}.pbf']),
                             ('sourceDownload', 'https://elsewhere.example/source'),
                             ('tileCount', 2), ('tileBytes', 999), ('version', '0' * 16)]:
            candidate = copy.deepcopy(manifest)
            candidate['regions'][0][field] = value
            dump(manifest_path, candidate)
            with self.assertRaises(ValueError):
                snapshot.validate_site(self.site)
        dump(manifest_path, manifest)
        root = self.site / 'regions/tiny-region' / manifest['regions'][0]['version']
        (root / 'cycling-source.osm.pbf').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'archive hash'):
            snapshot.validate_site(self.site)

    def test_live_race_check(self):
        files = remote_files(self.site)
        expected = snapshot.digest(files[snapshot.BASE + '/manifest.json'])
        self.assertEqual(snapshot.assert_live_unchanged(expected, fetch=fetch_from(files)), expected)
        files[snapshot.BASE + '/manifest.json'] += b' '
        with self.assertRaisesRegex(ValueError, 'stale publication'):
            snapshot.assert_live_unchanged(expected, fetch=fetch_from(files))


class RealSnapshotTests(unittest.TestCase):
    def test_frozen_baseline_and_new_broad_chicago_round_trip(self):
        with tempfile.TemporaryDirectory() as temp, patch('socket.socket', side_effect=AssertionError('Network prohibited')):
            root = Path(temp)
            site = root / 'site'
            snapshot._bootstrap(site)
            snapshot.validate_site(site)
            initial = snapshot.inventory(site)
            manifest = json.loads((site / 'manifest.json').read_text())
            chicago = next(r for r in manifest['regions'] if r['id'] == 'chicagoland')
            old_version = chicago['version']
            old_root = site / 'regions/chicagoland' / old_version
            new_root = root / 'new-broad-chicago'
            shutil.copytree(old_root, new_root)
            tile = next((new_root / 'tiles').rglob('*.pbf'))
            tile.write_bytes(tile.read_bytes() * 2)
            hashes = {p.relative_to(new_root / 'tiles').as_posix(): snapshot.digest(p.read_bytes())
                      for p in (new_root / 'tiles').rglob('*.pbf')}
            version = snapshot.digest(''.join(f'{k} {hashes[k]}\n' for k in sorted(hashes)).encode())[:16]
            dump(new_root / 'tile-sha256.json', hashes)
            provenance = json.loads((new_root / 'provenance.json').read_text())
            provenance['contentVersion'] = version
            dump(new_root / 'provenance.json', provenance)
            new_root.rename(old_root.parent / version)
            for key in ('sourceUrl', 'sha256Manifest', 'sourceDownload'):
                chicago[key] = chicago[key].replace(old_version, version)
            chicago['tiles'] = [url.replace(old_version, version) for url in chicago['tiles']]
            chicago['version'] = version
            chicago['tileBytes'] = sum(p.stat().st_size for p in (old_root.parent / version / 'tiles').rglob('*.pbf'))
            dump(site / 'manifest.json', manifest)
            dump(site / 'rollout-state.json', {'cursor': 2})
            report = snapshot.seal_snapshot(site)
            self.assertLess(report['siteBytes'], snapshot.MAX_SITE_BYTES)
            restored = root / 'restored'
            result = snapshot.restore_published(restored, fetch=fetch_from(remote_files(site)))
            self.assertEqual(result, manifest)
            self.assertEqual(snapshot.inventory(restored), snapshot.inventory(site))
            restored_files = snapshot.inventory(restored)
            self.assertTrue(all(restored_files[key] == value
                                for key, value in initial.items() if key != 'manifest.json'))
            # Old Chicago and exact metro are immutable even when broad Chicago
            # points at a new content version.
            target = next((restored / 'regions/chicago-metro').rglob('*.pbf'))
            target.write_bytes(b'changed frozen bytes')
            with self.assertRaises(ValueError):
                snapshot.validate_site(restored)


if __name__ == '__main__':
    unittest.main()
