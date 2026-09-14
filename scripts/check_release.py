"""Check package boundaries and metadata before upload."""
import pathlib
import sys
import tarfile
import zipfile

def entries(path):
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            yield from ((name, archive.read(name)) for name in archive.namelist())
    elif path.name.endswith(('.tar.gz', '.crate')):
        with tarfile.open(path) as archive:
            yield from ((member.name, archive.extractfile(member).read())
                        for member in archive.getmembers() if member.isfile())

for path in pathlib.Path(sys.argv[1]).iterdir():
    if not path.name.endswith(('.whl', '.tar.gz', '.crate')):
        continue
    files = list(entries(path))
    assert files, path
    for name, data in files:
        assert not any(part in name for part in ('docs/internal/', 'codex_conv', 'codex-session', '.venv/', '.git/')), name
        if name.endswith(('METADATA', 'PKG-INFO')):
            text = data.decode()
            assert 'Name: runtop' in text, name
            assert '![Full workspace](https://' in text, name
    print(path.name, 'package boundary checks passed')
