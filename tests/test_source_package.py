import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import source_package as s


class SourcePackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.osm.pbf'
        self.source.write_bytes(bytes(range(256)) * 13)
        self.output = self.root / 'output'
        self.addCleanup(self.tmp.cleanup)
        self.chunk_patch = patch.object(s, 'CHUNK_BYTES', 1024)
        self.chunk_patch.start()
        self.addCleanup(self.chunk_patch.stop)

    def package(self):
        return s.write_source_package(self.source, self.output)

    def rewrite(self, descriptor):
        (self.output / s.DESCRIPTOR_NAME).write_text(json.dumps(descriptor))

    def test_deterministic_reassembly_preserves_bytes_and_hash(self):
        package = self.package()
        descriptor = s.validate_source_package(self.output, expected_sha256=package['descriptorSha256'])
        reconstructed = b''.join((self.output / chunk['path']).read_bytes() for chunk in descriptor['chunks'])
        self.assertEqual(reconstructed, self.source.read_bytes())
        self.assertEqual(descriptor['sha256'], hashlib.sha256(reconstructed).hexdigest())
        self.assertEqual(descriptor['bytes'], len(reconstructed))
        self.assertEqual(len(descriptor['chunks']), 4)
        self.assertTrue(all(chunk['sha256'] in chunk['path'] for chunk in descriptor['chunks']))
        second = self.root / 'second'
        self.assertEqual(package, s.write_source_package(self.source, second))
        self.assertEqual((self.output / s.DESCRIPTOR_NAME).read_bytes(), (second / s.DESCRIPTOR_NAME).read_bytes())
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            s.write_source_package(self.source, self.output)

    def test_missing_duplicate_reordered_and_outside_chunks_are_rejected(self):
        package = self.package()
        original = package['descriptor']
        for mode in ('missing', 'duplicate', 'reordered', 'outside'):
            descriptor = json.loads(json.dumps(original))
            if mode == 'missing':
                descriptor['chunks'].pop()
            elif mode == 'duplicate':
                descriptor['chunks'][1] = descriptor['chunks'][0]
            elif mode == 'reordered':
                descriptor['chunks'].reverse()
            else:
                descriptor['chunks'][0]['path'] = '../source.osm.pbf'
            self.rewrite(descriptor)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                s.validate_source_package(self.output)

    def test_changed_descriptor_and_corrupt_chunk_are_rejected(self):
        package = self.package()
        with self.assertRaisesRegex(ValueError, 'descriptor SHA256'):
            s.validate_source_package(self.output, expected_sha256='0' * 64)
        descriptor = package['descriptor']
        chunk = self.output / descriptor['chunks'][0]['path']
        content = chunk.read_bytes()
        chunk.write_bytes(b'bad' + content[3:])
        with self.assertRaisesRegex(ValueError, 'chunk SHA256'):
            s.validate_source_package(self.output)
        chunk.write_bytes(content[:-1])
        with self.assertRaisesRegex(ValueError, 'chunk size'):
            s.validate_source_package(self.output)

    def test_extra_file_symlink_and_nested_directory_are_rejected(self):
        package = self.package()
        directory = self.output / s.CHUNK_DIRECTORY
        extra = directory / 'extra'
        extra.write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            s.validate_source_package(self.output)
        extra.unlink()
        extra.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'regular files'):
            s.validate_source_package(self.output)
        extra.unlink()
        extra.mkdir()
        with self.assertRaisesRegex(ValueError, 'regular files'):
            s.validate_source_package(self.output)

    def test_full_hash_and_size_must_match(self):
        package = self.package()
        descriptor = package['descriptor']
        descriptor['sha256'] = '0' * 64
        self.rewrite(descriptor)
        with self.assertRaisesRegex(ValueError, 'Reconstructed'):
            s.validate_source_package(self.output)
        descriptor['bytes'] += 1
        self.rewrite(descriptor)
        with self.assertRaises(ValueError):
            s.validate_source_package(self.output)

    def test_oversized_empty_or_malformed_packages_fail_closed(self):
        self.source.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'size exceeds'):
            self.package()
        self.source.write_bytes(b'data')
        package = self.package()
        descriptor = package['descriptor']
        for key, value in [('bytes', True), ('bytes', s.MAX_SOURCE_BYTES + 1), ('schemaVersion', True), ('format', 'unknown')]:
            bad = {**descriptor, key: value}
            self.rewrite(bad)
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                s.validate_source_package(self.output)
        (self.output / s.DESCRIPTOR_NAME).write_text('{"format":"a","format":"b"}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            s.validate_source_package(self.output)

    def test_total_envelope_and_descriptor_path_are_bounded(self):
        package = self.package()
        with patch.object(s, 'MAX_SOURCE_BYTES', self.source.stat().st_size):
            with self.assertRaisesRegex(ValueError, 'total exceeds'):
                s.validate_source_package(self.output)
        with self.assertRaisesRegex(ValueError, 'descriptor path'):
            s.validate_source_package(self.output, '../source.osm.pbf')


if __name__ == '__main__':
    unittest.main()
