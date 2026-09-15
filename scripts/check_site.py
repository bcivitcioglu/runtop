"""Reject private paths in a built documentation site and confirm its public assets."""
from pathlib import Path
import sys

root=Path(sys.argv[1])
assert (root/'manual.json').is_file()
assert (root/'llms.txt').is_file()
for asset in ('assets/brand/favicon.svg', 'assets/brand/favicon-32.png', 'assets/brand/icon-512.png',
              'assets/brand/logo-on-dark.svg', 'css/runtop.css'):
    assert (root/asset).is_file(), f'missing site asset: {asset}'
index=(root/'index.html').read_text()
assert 'assets/brand/favicon.svg' in index and 'assets/brand/logo-on-dark.svg' in index, 'brand missing from site header'
assert '<picture>' not in index, 'README logo block leaked into the site page'
for path in root.rglob('*'):
    if not path.is_file(): continue
    name=path.relative_to(root).as_posix()
    assert not any(x in name for x in ('internal/', 'codex', 'benchmarks/', 'logo-preview', 'r-proposal')), name
    if path.suffix in ('.html','.json','.txt','.md'):
        text=path.read_text()
        assert 'docs/internal/' not in text, name
print('Public documentation site boundary checks passed')
