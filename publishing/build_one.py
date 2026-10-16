"""Child process for one bounded region; parent enforces a process-group deadline."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from build_region import build

if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text())
    root = Path(sys.argv[2])
    site = root / 'site'
    site.mkdir()
    region = build(config, root / 'work', site, 'https://apfullai.github.io/ride-tiles')
    (root / 'region.json').write_text(json.dumps(region))
