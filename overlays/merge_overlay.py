"""Add an independently pinned immutable overlay to an already restored site.

Call existing seal_snapshot/validate_site afterwards, then compare-live before
publication. Never modifies city checkpoint, manifest, or generator sources.
"""
import hashlib
import json
import re
import shutil
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inventory(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Expected regular directory')
    result = {}
    for p in root.rglob('*'):
        if p.is_symlink() or not (p.is_file() or p.is_dir()):
            raise ValueError('Non-regular entry')
        if p.is_file():
            result[p.relative_to(root).as_posix()] = sha(p.read_bytes())
    return result


def merge(site, overlay, manifest_sha256, version):
    site, overlay = Path(site), Path(overlay)
    if not re.fullmatch('[0-9a-f]{16}', version):
        raise ValueError('Invalid immutable version')
    if not re.fullmatch('[0-9a-f]{64}', manifest_sha256):
        raise ValueError('Invalid manifest pin')
    before, source = inventory(site), inventory(overlay)
    if source.get('manifest.json') != manifest_sha256:
        raise ValueError('Manifest pin mismatch')
    m = json.loads((overlay/'manifest.json').read_bytes())
    if m.get('schemaVersion') != 1 or m.get('version') != version:
        raise ValueError('Wrong schema/version')
    expected = {'manifest.json': manifest_sha256,
                'index.json': m['indexSHA256'], 'empty.pbf': sha(b'')}
    licenses = m.get('licenses', {})
    if not isinstance(licenses, dict) or set(licenses) - {'MELLOW-MIT-LICENSE.txt', 'OSM-DATA-LICENSE.txt'}:
        raise ValueError('Unexpected license path')
    for name, record in licenses.items():
        digest = record['sha256'] if isinstance(record, dict) else record
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid license digest')
        if isinstance(record, dict) and (overlay/name).stat().st_size != record['bytes']:
            raise ValueError('License size mismatch')
        expected[name] = digest
    if type(m['tiles']) is not int or m['tiles'] != len(m['files']):
        raise ValueError('Tile count mismatch')
    for name, record in m['files'].items():
        match = re.fullmatch(r'(8|9|1[0-7])/(0|[1-9]\d*)/(0|[1-9]\d*)\.pbf', name)
        if not match:
            raise ValueError('Unsafe tile path')
        z, x, y = map(int, match.groups())
        if x >= 2**z or y >= 2**z:
            raise ValueError('Invalid tile coordinates')
        if not re.fullmatch('[0-9a-f]{64}', record['sha256']):
            raise ValueError('Invalid tile digest')
        if (overlay/name).stat().st_size != record['bytes']:
            raise ValueError('Tile size mismatch')
        expected[name] = record['sha256']
    if source != expected or (overlay/'index.json').stat().st_size != m['indexBytes']:
        raise ValueError('Overlay inventory mismatch')
    prefix = f'mellow/xyz/{version}/'
    # Existing destination must be exactly identical, including no extra files.
    existing = {p[len(prefix):]: h for p, h in before.items() if p.startswith(prefix)}
    if existing and existing != source:
        raise ValueError('Immutable overlay collision')
    destination = site/prefix
    if not existing:
        if destination.exists():
            raise ValueError('Unexpected empty destination')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(overlay, destination)
    expected_site = {**before, **{prefix+p: h for p, h in source.items()}}
    if inventory(site) != expected_site:
        raise ValueError('Pre-existing publication changed')
    return {'preservedFiles': len(before), 'overlayFiles': len(source),
            'version': version, 'manifestSHA256': manifest_sha256}
