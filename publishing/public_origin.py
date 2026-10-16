"""Exact, metadata-only relocation of the preserved public tile origin."""
import copy
OLD_BASE = 'https://apfullai.github.io/rides-tiles'
BASE = 'https://apfullai.github.io/ride-tiles'
URL_FIELDS = ('sourceUrl', 'sourceDownload', 'sha256Manifest')

def relocate_region(region, source, target):
    if source not in (OLD_BASE, BASE) or target not in (OLD_BASE, BASE):
        raise ValueError('Unapproved public origin')
    result = copy.deepcopy(region)
    prefix = source + '/regions/' + region['id'] + '/' + region['version'] + '/'
    replacement = target + prefix[len(source):]
    def change(value):
        if not isinstance(value, str) or not value.startswith(prefix):
            raise ValueError('Region URL does not match preserved identity')
        return replacement + value[len(prefix):]
    for field in URL_FIELDS:
        result[field] = change(region[field])
    result['tiles'] = [change(value) for value in region['tiles']]
    return result

def relocate_manifest(manifest, source, target):
    result = copy.deepcopy(manifest)
    result['regions'] = [relocate_region(region, source, target) for region in manifest['regions']]
    return result
