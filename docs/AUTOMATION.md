# Agent and automation guide

Start with the installed executable's version and its bundled manual:

```sh
runtop --edition
runtop --version
runtop docs --list --json
runtop docs agents --json
runtop docs cli --json
```

The same `docs` interface is available through `rt`. It needs no terminal, engine,
network connection or repository checkout. `docs --json` returns the whole manual;
a topic limits output. `docs --list --json` returns topic metadata without bodies.
The JSON object has `schema: "runtop.docs/v1"`, package `version`, selected
`edition`, `format: "markdown"`, and `topics` containing `id`, `title`, and
`content` (content is omitted for a listing). Unknown topics and invalid options
exit 2. Successful documentation queries exit 0 and write to standard output.

## Read structured state

```sh
rt ps -a --json
rt ps --target TARGET_KEY -a --stats --json
runtop ps --contexts -a --json
```

Use the explicit target key when possible. Parse `runtop.snapshot/v1`, rather
than terminal tables. Check both the process exit code and each target's state,
staleness and error fields. Exit 3 can accompany useful partial data. Missing
metrics are unknown, not zero. Add `--contexts` only when remote listings are
wanted. `--no-contexts` disables context discovery in live listing mode.

For local offline experiments, use `rt ps --demo -a --json` or
`runtop ps --snapshot FILE -a --json`. The CLI reference documents argument order
and edition differences. `--doctor` performs live checks even when demo flags
are present; use snapshots for offline inspection.

## Actions and logs

The command-line interface exposes listings, diagnostics and, in full, archive
operations. It has no noninteractive container start/stop/remove subcommands.
Lifecycle actions in the TUI require confirmation and bind to captured targets
and container IDs. Do not use scripted TUI keys as a stable mutation API.
Remote targets are read-only; `rt --read-only` also disables local TUI mutations.

Full's `logs record` and `logs export` require an explicit target, scope and
archive directory. Recording follows existing IDs and does not reconnect across
replacement containers. `logs search --json` writes JSON Lines: one object per
match. `logs status --json` writes one object. Use the CLI reference for their
per-command exit codes and storage limits.

## Installed documentation

The manual is packaged with both editions and identifies the executable's version.
The public website may describe a newer release. Prefer installed documentation
when automating a pinned version. Help and docs commands have no engine side effects.
