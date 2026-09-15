# Command-line reference

This reference describes the current commands. Run `runtop --edition` or `rt --edition`
to identify the edition behind a command.

`runtop` opens full when it is installed and available on the executable path.
`rt` always opens lite. Starting either without a subcommand opens its TUI.

## Offline documentation

The bundled `docs` command is available in 0.1.2 and later.

`runtop docs` and `rt docs` print the bundled manual. Use `docs --list` to list
its topics, `docs cli` for one topic, or `docs --json` for a structured manual.
These commands work without a terminal, engine or network connection. See the
[agent guide](AUTOMATION.md) for the JSON schema and automation conventions.

## Quick start

These examples use bundled data and do not contact an engine:

```sh
runtop --demo
rt --demo
runtop ps --demo -a --json
rt ps --demo --images
```

For a live session, omit `--demo`. An existing engine or configured machine is
required; runtop does not install an engine or create a VM.

```sh
runtop --doctor
runtop --target TARGET_KEY
rt --target TARGET_KEY --read-only
```

Replace `TARGET_KEY` with a key printed by diagnostics or present in a snapshot.
Remote targets are always read-only. Local lifecycle actions are available in the
TUI after confirmation; there are no `runtop start`, `stop`, or `remove` CLI commands.

## Command availability

| Operation | Full | Lite |
|---|---|---|
| Open the workspace | `runtop` | `rt` |
| Offline documentation | `runtop docs` | `rt docs` |
| List containers or images | `runtop ps` | `rt ps` |
| Diagnose connectivity | `runtop --doctor` | `rt --doctor` or `rt doctor` |
| Dump one target as snapshot JSON | `runtop --dump KEY` | `rt --dump KEY` |
| Record, export, search, or inspect log archives | `runtop logs …` | Requires full |
| Disable local mutations for the session | No command-line switch | `rt --read-only` |
| Disable mouse capture | No command-line switch | `rt --no-mouse` |

With full, place `ps`, `logs` or `docs` immediately after `runtop`, then their options.
Each has its own parser. For example, use `runtop ps --target KEY`, not
`runtop --target KEY ps`.

## List containers and images

```sh
runtop ps
runtop ps -a --stats
rt ps --images
runtop ps --contexts -a --json
rt ps --target TARGET_KEY -a --json
```

| Option | Meaning |
|---|---|
| `-a`, `--all` | Include non-running containers |
| `-t KEY`, `--target KEY` | Select one target by key or name; a key avoids ambiguity |
| `--contexts` | Include remote targets in the listing |
| `--no-contexts` | Skip context discovery in live mode |
| `--images` | Print the image table instead of the container table |
| `--stats` | Request local CPU and memory samples; can add sampling delay |
| `--json` | Write a `runtop.snapshot/v1` document to standard output |
| `--demo` | Use bundled synthetic data |
| `--snapshot FILE` | Read a saved snapshot instead of live engines |

Without `--contexts`, listings select local targets. An explicit `--target` can
select a remote target. Missing metrics display as `—`, not zero.

Text output is tab-separated. JSON contains the snapshot document with targets,
containers, and images; `--images` changes the text table, not the JSON schema.
Consumers should check each target's state and error fields. See the
[data contract](../spec/SPEC.md) for fields and normalization.

| `ps` exit code | Meaning |
|---|---|
| 0 | Listing completed; inspect target states for stopped or unsupported targets |
| 1 | No selected targets, or discovery/execution failed before a useful listing |
| 2 | Invalid arguments or unknown target |
| 3 | Partial/unavailable data, including an unreachable target or image request failure |

A nonzero exit code can accompany useful JSON. Diagnostics go to standard error.
For automation, capture the exit code before deciding whether to process partial data.

## Workspace and diagnostics options

Both editions support `--help`, `--version`, `--edition`, `--target KEY`, `--demo`,
`--snapshot FILE`, `--dump KEY`, `--doctor`, `--keys TOKENS`, and `--quit-after SECS`.

`--dump KEY` emits one normalized snapshot and exits. `ps --target KEY -a --json`
is the preferred automation interface, with the shared exit-code behavior above.
Dump failure codes differ between editions.

`--doctor` probes configured targets and reports connectivity. It exits 0 when
checks succeed or only stopped machines are found, and 1 on discovery/connectivity
failure or no targets. Diagnostics perform live checks even with `--demo` or
`--snapshot`; they are not offline snapshot inspection commands.

Lite additionally supports `--no-contexts` at the workspace level, `--read-only`,
`--no-mouse`, and `-t` as a short form of `--target`. Full supports `--no-contexts`
and `-t` on `ps`, but not on its workspace parser.

`--keys` and `--quit-after` support scripted demos and recordings:

```sh
rt --demo --keys "wait:1 m down enter" --quit-after 5
runtop --demo --keys "wait:1 down" --quit-after 5
```

These are TUI key sequences, so they require a terminal. `wait:N` inserts a delay.
Use demo data when experimenting; scripted keys in a live session act on live data.

## Log archives — full only

```sh
runtop logs record --target TARGET_KEY --project PROJECT --directory ./logs \
  --max-mib 64 --file-mib 8 --keep-days 7 --duration 60
runtop logs export --target TARGET_KEY --container CONTAINER --directory ./logs --tail 500
runtop logs search error -i --directory ./logs --limit 200 --json
runtop logs status --directory ./logs --json
```

`record` follows the selected existing container IDs. Use either `--project NAME`
or one or more `--container ID_OR_NAME` options. It records until Ctrl+C unless a
positive `--duration SECS` is set. It does not automatically follow replacement IDs.

`export` writes a finite recent tail using the same target, selection, and storage
options. Defaults: 64 MiB total, 8 MiB per file, seven days retention; `--tail`
defaults to 0 for record and 200 for export, with a maximum of 10,000.
`--demo` supplies synthetic data for record/export.

`search` reads saved archives using literal text, optionally with `-i` /
`--ignore-case`, `--container`, `--project`, `--since` (a timezone-aware timestamp),
and `--limit` (default 200). Its `--json` output is one JSON object per matching
record, **JSON Lines**, rather than one array. `status --json` emits one object
with archive file count and bytes. Neither command needs a live engine.

| Archive command | Exit codes |
|---|---|
| `record` | 0 completed/stopped normally; 1 capture failed; 2 invalid input or handled file/selection error |
| `export` | 0 exported; 2 invalid input or handled file/selection error |
| `search` | 0 matches; 1 no matches; 2 invalid input or archive error |
| `status` | 0 inspected; 2 invalid input or archive error |

Recording is opt-in. Rotation removes only owned archive files, and only one writer
may use a folder at a time. Log contents can contain application secrets.

## Preferences and help

Full saves live-session preferences in `$XDG_CONFIG_HOME/runtop/config.json`, or
`~/.config/runtop/config.json` when that variable is unset. This includes theme,
last target, Containers or Images view, sort order, pane widths, collapsed groups,
and archive defaults. Demo mode does not save these preferences.

Lite starts in dark mode; `RUNTOP_THEME=light rt` starts in light mode. Press `t`
to change theme during a session. Lite saves its theme, last target, sort order,
folds and image expansion in `lite.json`.
See [preferences](PREFERENCES.md) for precedence and reset instructions.

The lite-provided `runtop` launcher accepts `--lite` or `RUNTOP_EDITION=lite` to
stay in lite, and `RUNTOP_EDITION=full` to require full. These controls belong to
the launcher; the directly installed full executable does not interpret them.
Use `rt` when you need an unambiguous lite command.

```sh
runtop --help
runtop ps --help
runtop logs --help
runtop logs record --help
rt --help
rt ps --help
```

Inside either TUI, `?` opens the keyboard reference. See the
[workspace guide](../README.md) for navigation and feature examples.
