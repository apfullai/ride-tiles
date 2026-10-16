"""Reconstruct the preserved public pilot offline into a fresh destination."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from import_mirror import import_chicagoland


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(site):
    return {p.relative_to(site).as_posix(): digest(p.read_bytes())
            for p in site.rglob('*') if p.is_file()}


def restore(site, bootstrap=None):
    site = Path(site)
    bootstrap = Path(bootstrap) if bootstrap else ROOT / 'bootstrap'
    if site.exists():
        raise ValueError('Snapshot destination must not exist')
    conf = json.loads((bootstrap / 'pilot-snapshot.json').read_text())
    chunks = []
    for name, expected in sorted(conf['parts'].items()):
        if not re.fullmatch(r'pilot-remainder\.tar\.gz\.part\d{3}', name):
            raise ValueError('Invalid snapshot part name')
        data = (bootstrap / name).read_bytes()
        if digest(data) != expected:
            raise ValueError('Snapshot part digest mismatch')
        chunks.append(data)
    packed = b''.join(chunks)
    if digest(packed) != conf['archiveSha256']:
        raise ValueError('Snapshot archive digest mismatch')
    if digest((bootstrap / 'chicagoland.json').read_bytes()) != conf['chicagolandConfigSha256']:
        raise ValueError('Chicago configuration digest mismatch')
    # Stage everything before making the destination visible. The importer also
    # validates the original Chicago archive hash and tile content version.
    with tempfile.TemporaryDirectory(dir=site.parent) as temp:
        staging = Path(temp) / 'site'
        staging.mkdir()
        import_chicagoland(staging, 'https://apfullai.github.io/rides-tiles', bootstrap)
        with tarfile.open(fileobj=io.BytesIO(packed), mode='r:gz') as tar:
            seen = set()
            for member in tar:
                name = member.name
                path = PurePosixPath(name)
                if (not member.isfile() or path.is_absolute() or '..' in path.parts
                        or path.as_posix() != name or '\\' in name or name in seen
                        or name not in conf['files']):
                    raise ValueError('Unsafe or duplicate snapshot member')
                seen.add(name)
                data = tar.extractfile(member).read()
                if digest(data) != conf['files'][name]:
                    raise ValueError('Snapshot file digest mismatch')
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        if inventory(staging) != conf['files']:
            raise ValueError('Reconstructed snapshot differs from preserved public bytes')
        staging.rename(site)
    return conf['files']


if __name__ == '__main__':
    files = restore(Path(sys.argv[1]))
    print(json.dumps({'preservedFiles': len(files), 'offline': True}))
