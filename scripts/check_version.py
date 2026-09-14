"""Reject release tags that do not match package metadata."""
import pathlib
import re
import sys

kind, expected = sys.argv[1:]
manifest = pathlib.Path('Cargo.toml' if kind == 'rust' else 'pyproject.toml').read_text()
match = re.search(r'^version = "([^"]+)"$', manifest, re.M)
if match is None or match[1] != expected:
    raise SystemExit('release tag does not match package version')
