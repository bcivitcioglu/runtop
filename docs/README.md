# Documentation

- [Workspace guide](../README.md)
- [Install and update](INSTALL.md)
- [CLI reference](CLI.md)
- [Agents and automation](AUTOMATION.md)
- [Remembered preferences](PREFERENCES.md)
- [Resource use and measurement method](PERFORMANCE.md)
- [Data contract](../spec/SPEC.md)

Run `runtop docs --json` or `rt docs --json` for the installed offline manual.

## Update the documentation

Edit the public pages listed above. The site and both offline manuals share these
sources; do not edit generated files in `.local/site-docs` or `spec/manual.json`.

```sh
uv sync --locked --group docs
uv run python scripts/build_docs.py --site
uv run mkdocs serve
```

The preview prints a local URL. After changing a source page, rerun the builder;
the preview reloads the staged changes automatically. To validate before pushing:

```sh
uv run python scripts/build_docs.py --check --site
uv run mkdocs build --strict
uv run python scripts/check_site.py .local/site
```

Commit the source edits and regenerated `spec/manual.json`. The documentation
workflow publishes the website automatically on `master`. Installed offline
manuals update with package releases. Only the builder's explicit public allowlist
is included in the site or manual.
