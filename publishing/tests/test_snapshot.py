import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'publishing'))
from restore_snapshot import restore, inventory, digest
from append_metro import main, VERSION, BOUNDS


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory()
        cls.original = Path(cls.base.name) / 'original'
        with patch('socket.socket', side_effect=AssertionError('Network prohibited')):
            restore(cls.original)
        cls.expected = json.loads((ROOT / 'bootstrap/pilot-snapshot.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name) / 'site'
        shutil.copytree(self.original, self.site)

    def test_exact_offline_snapshot(self):
        self.assertEqual(inventory(self.site), self.expected['files'])
        self.assertEqual(len(inventory(self.site)), 6964)
        with self.assertRaises(ValueError):
            restore(self.site)

    def test_append_and_repeat_are_immutable(self):
        old = inventory(self.site)
        main(self.site)
        after = inventory(self.site)
        self.assertTrue(all(after[k] == v for k, v in old.items() if k != 'manifest.json'))
        manifest = json.loads((self.site / 'manifest.json').read_text())
        metro = manifest['regions'][0]
        self.assertEqual((metro['version'], metro['bounds'], metro['tileCount']), (VERSION, BOUNDS, 3868))
        main(self.site)
        self.assertEqual(after, inventory(self.site))

    def test_refresh_and_extra_region_preserved(self):
        manifest = json.loads((self.site / 'manifest.json').read_text())
        nyc = next(r for r in manifest['regions'] if r['id'] == 'nyc-pilot')
        old_root = self.site / 'regions' / nyc['id'] / nyc['version']
        tile = next((old_root / 'tiles').rglob('*.pbf'))
        # Duplicate a valid protobuf feature stream: valid tile bytes with a new digest.
        tile.write_bytes(tile.read_bytes() * 2)
        hashes = inventory(old_root / 'tiles')
        version = digest(''.join(f'{k} {hashes[k]}\n' for k in sorted(hashes)).encode())[:16]
        (old_root / 'tile-sha256.json').write_text(json.dumps(hashes))
        nyc['version'] = version
        nyc['tileBytes'] = sum(p.stat().st_size for p in (old_root / 'tiles').rglob('*.pbf'))
        new_root = old_root.parent / version
        old_root.rename(new_root)
        # Update version-addressed metadata as a normal rebuild does.
        for key in ('tiles', 'sourceUrl', 'sourceDownload', 'sha256Manifest'):
            value = nyc[key]
            nyc[key] = [v.replace('920c1bc3710f6eaa', version) for v in value] if isinstance(value, list) else value.replace('920c1bc3710f6eaa', version)
        extra = copy.deepcopy(nyc)
        extra['id'] = 'extra-region'
        extra['name'] = 'Additional valid region'
        for key in ('tiles', 'sourceUrl', 'sourceDownload', 'sha256Manifest'):
            value = extra[key]
            extra[key] = [v.replace('/nyc-pilot/', '/extra-region/') for v in value] if isinstance(value, list) else value.replace('/nyc-pilot/', '/extra-region/')
        shutil.copytree(new_root, self.site / 'regions' / extra['id'] / version)
        manifest['regions'].reverse()  # Parent is not necessarily the first region.
        manifest['regions'].append(extra)
        (self.site / 'manifest.json').write_text(json.dumps(manifest))
        before = inventory(self.site)
        main(self.site)
        after = inventory(self.site)
        self.assertTrue(all(after[k] == v for k, v in before.items() if k != 'manifest.json'))
        result = json.loads((self.site / 'manifest.json').read_text())
        self.assertEqual(result['regions'][1:], manifest['regions'])

    def test_invalid_regions_do_not_mutate(self):
        manifest_path = self.site / 'manifest.json'
        original = json.loads(manifest_path.read_text())
        for field, value in [('id', '../outside'), ('version', '../outside'), ('version', '0' * 16), ('bounds', [0, 0, 0, 0])]:
            manifest = copy.deepcopy(original)
            manifest['regions'][1][field] = value
            manifest_path.write_text(json.dumps(manifest))
            before = inventory(self.site)
            with self.assertRaises((ValueError, FileNotFoundError)):
                main(self.site)
            self.assertEqual(before, inventory(self.site))

    def test_corrupt_tiles_do_not_mutate(self):
        tiles = self.site / 'regions/nyc-pilot/920c1bc3710f6eaa/tiles'
        tile = sorted(tiles.rglob('*.pbf'))[0]
        tile.write_bytes(b'corrupt')
        before = inventory(self.site)
        with self.assertRaises(ValueError):
            main(self.site)
        self.assertEqual(before, inventory(self.site))

    def test_existing_metro_conflict_does_not_mutate(self):
        main(self.site)
        manifest_path = self.site / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['regions'][0]['bounds'] = [-88.6, 41.35, -86.8, 42.65]
        manifest_path.write_text(json.dumps(manifest))
        before = inventory(self.site)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            main(self.site)
        self.assertEqual(before, inventory(self.site))

    def test_corrupt_source_rejected_before_destination_creation(self):
        bootstrap = Path(self.temp.name) / 'bootstrap'
        shutil.copytree(ROOT / 'bootstrap', bootstrap)
        part = next(bootstrap.glob('pilot-remainder.tar.gz.part*'))
        part.write_bytes(b'corrupt')
        destination = Path(self.temp.name) / 'restored'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            restore(destination, bootstrap)
        self.assertFalse(destination.exists())

    def test_unsafe_archive_members_rejected(self):
        bootstrap = Path(self.temp.name) / 'bootstrap'
        shutil.copytree(ROOT / 'bootstrap', bootstrap)
        destination = Path(self.temp.name) / 'restored'
        for name, symlink, duplicate in [('../outside', False, False), ('/absolute', False, False), ('index.html', True, False), ('index.html', False, True)]:
            conf = copy.deepcopy(self.expected)
            packed = io.BytesIO()
            with tarfile.open(fileobj=packed, mode='w:gz') as tar:
                for _ in range(2 if duplicate else 1):
                    member = tarfile.TarInfo(name)
                    data = (self.original / 'index.html').read_bytes()
                    member.size = len(data)
                    if symlink:
                        member.type = tarfile.SYMTYPE
                        member.linkname = '/outside'
                    tar.addfile(member, io.BytesIO(data))
            data = packed.getvalue()
            part = 'pilot-remainder.tar.gz.part000'
            (bootstrap / part).write_bytes(data)
            conf['parts'] = {part: digest(data)}
            conf['archiveSha256'] = digest(data)
            (bootstrap / 'pilot-snapshot.json').write_text(json.dumps(conf))
            with self.assertRaisesRegex(ValueError, 'Unsafe or duplicate'):
                restore(destination, bootstrap)
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
