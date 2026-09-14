"""Build a deterministic-layout binary archive and checksum."""
import hashlib
import pathlib
import re
import sys
import tarfile

target = sys.argv[1]
if not re.fullmatch(r'[a-zA-Z0-9_-]+', target):
    raise SystemExit('invalid target')
root = pathlib.Path(__file__).resolve().parents[1]
version = re.search(r'^version = "([^"]+)"$', (root/'Cargo.toml').read_text(), re.M)[1]
source = root/'target'/target/'release'
if len(sys.argv) > 2:
    source = pathlib.Path(sys.argv[2])
out = root/'release-dist'
out.mkdir(exist_ok=True)
archive = out/f'runtop-lite-{version}-{target}.tar.gz'
with tarfile.open(archive, 'w:gz') as tar:
    for name in ('rt', 'runtop'):
        tar.add(source/name, arcname=name)
    tar.add(root/'LICENSE', arcname='LICENSE')
(archive.with_suffix(archive.suffix+'.sha256')).write_text(
    hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
print(archive.name)
