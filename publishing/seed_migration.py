"""Relocate the exact October 14 publication without rebuilding any tiles."""
import argparse
import hashlib
import json
from pathlib import Path
import os
from urllib.error import HTTPError
from public_origin import OLD_BASE, BASE, relocate_manifest
from rollout_snapshot import (restore_published, assert_publication_unchanged,
    publication_revision, inventory, validate_site, seal_snapshot, fetch_bytes)
from checkpoint_migrations import FROM_DESCRIPTOR
SOURCE_REVISION = '0f82807d74edc3c44da8b8bbd9aed494eef5d3360203d322843743aea863d953'

def assert_destination_absent(fetch=None):
    """Seed is one-shot: never overwrite any published destination manifest."""
    try:
        (fetch or fetch_bytes)(BASE+'/manifest.json',100_000)
    except HTTPError as exc:
        if exc.code == 404 and exc.geturl() == BASE+'/manifest.json':
            return
        raise
    raise ValueError('Migration destination is already published; seed replay refused')

def seed(site, fetch=None):
    site = Path(site)
    assert_destination_absent(fetch)
    # Exact frozen publication; no acceptance of a silently advanced source.
    assert_publication_unchanged(SOURCE_REVISION, OLD_BASE, fetch)
    receipt = {}
    restore_published(site, OLD_BASE, fetch, receipt)
    if receipt['publicationRevision'] != SOURCE_REVISION or receipt['bootstrap']:
        raise ValueError('Migration source publication changed')
    before = inventory(site)
    cp = site/'checkpoints/active/checkpoint.json'
    if hashlib.sha256(cp.read_bytes()).hexdigest() != FROM_DESCRIPTOR:
        raise ValueError('Migration checkpoint differs from approved Illinois checkpoint')
    manifest = json.loads((site/'manifest.json').read_text())
    changed = relocate_manifest(manifest, OLD_BASE, BASE)
    (site/'manifest.json').write_text(json.dumps(changed, indent=2)+'\n')
    after = inventory(site)
    if {k:v for k,v in before.items() if k!='manifest.json'} != {k:v for k,v in after.items() if k!='manifest.json'}:
        raise ValueError('Migration changed preserved data')
    validate_site(site)
    sealed = seal_snapshot(site)
    validate_site(site)
    assert_publication_unchanged(SOURCE_REVISION, OLD_BASE, fetch)
    assert_destination_absent(fetch)
    return {'sourceRevision':SOURCE_REVISION, 'preservedDataFiles':len(before)-1,
            'checkpointSha256':FROM_DESCRIPTOR, **sealed}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site',default='site')
    args=parser.parse_args()
    from verified_code import source_records, find_proof
    from actions_budget import fetch_public_json
    root=Path(__file__).resolve().parents[1]
    if not find_proof(None,source_records(root),fetch_public_json):
        raise ValueError("Seed requires successful full CI for this exact code")
    result=seed(args.site)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as f:
            f.write('changed=true\nrestored_publication_revision='+SOURCE_REVISION+'\n')
    print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
