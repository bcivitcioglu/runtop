# runtop

A terminal workspace for containers and their machines, in the style of a desktop app.
MIT licensed for personal and commercial use.

![runtop full workspace](assets/runtop/demo.gif)

The full edition uses Python and Textual: a machines sidebar, grouped containers,
search, an inspector, project logs and confirmed actions. It works with existing
Lima and Colima Docker VMs, local Docker sockets and read-only remote contexts.

## Run from source

The full edition is in development. Registry reservations are not application releases.

```sh
git clone https://github.com/bcivitcioglu/runtop.git
cd runtop
uv sync --locked
uv run runtop --demo
uv run runtop
uv run runtop --edition
```

Python 3.11+ is required. No daemon is needed for the demo. Live mode uses your
existing runtime; runtop does not create a VM or install a container engine.

## Everyday use

- Select a machine and browse containers grouped by Compose project. `/` filters;
  `attention` finds unhealthy, restarting, dead and failed containers.
- Inspect ports, mounts, networks, restart counts, OOM failures and health output.
  `1`, `2`, `3` select Info, Logs and Stats; `i` opens details in narrow terminals.
- Select a project and press `l` for combined logs. `s`, `x`, `R` start, stop or
  restart existing members after a preview. Filters limit the affected members.
- `D` opens Docker storage accounting on demand. Shared layers, unknown usage and
  VM capacity are presented separately. Runtime data is never deleted automatically.
- `ctrl+p` opens the command palette; `?` lists keys. Use the mouse for navigation,
  tabs and pane resizing. `t` changes theme.
- `runtop --doctor` explains discovery and connectivity failures without stats or
  directory scans. `runtop --dump lima:docker` emits normalized snapshot JSON.

Failed refreshes retain the last successful data with a stale warning and block
mutations. Image-list failures leave containers usable. Remote `ssh://` and `tcp://`
Docker contexts are always read-only.

Logs and resource history stay bounded in memory by default. Preferences are stored
under `~/.config/runtop` (or `$XDG_CONFIG_HOME/runtop`). Images, volumes, build cache
and VM disks are owned by the runtime and still consume storage.

## Record and search logs

![Log archive controls](assets/runtop/archives.png)

Press `L` to open Log archives for the selected container or project. Choose a
folder, total budget, file size and retention, then press **Record**. **Stop** ends
capture; closing the controls leaves recording active until runtop exits. A visible
indicator stays on the main screen. **Export tail** saves the latest 200 lines per
selected container without following. Search saved output in the same screen.

Recording is off by default. Defaults are **64 MiB total**, **8 MiB per file** and
**7 days** retention. Rotation deletes the oldest runtop archive files as needed;
retention runs while recording or when opening a writer. Other files are untouched.
Only one writer may use a folder at a time. The cap covers runtop JSONL files,
not the runtime's own logs or unrelated files. Archive files use private permissions.

```sh
# Foreground recording; Ctrl+C or SIGTERM stops and flushes.
runtop logs record --target lima:docker --project shop --directory ~/logs/shop \
  --max-mib 64 --file-mib 8 --keep-days 7

# Bounded recording or a one-time export for automation.
runtop logs record --target lima:docker --container api --directory ~/logs/api --duration 60
runtop logs export --target lima:docker --container api --directory ~/logs/api --tail 500

# Literal search, optional filters, JSONL results for agents.
runtop logs search error -i --directory ~/logs/shop --project shop --limit 200 --json
runtop logs search --directory ~/logs/api --container api --since 2026-01-01T00:00:00Z
runtop logs status --directory ~/logs/shop --json
```

Search streams files without building an index; results are capped at 200 by default
(maximum 10,000). It returns exit code 0 for matches, 1 for no matches and 2 for invalid
input or an archive error. JSONL retains capture time, target, container, project,
service, output stream and message. Messages keep the runtime's timestamps when
available. `--since` filters capture time. Very large records are marked as truncated;
long unterminated output is split into bounded fragments. A full queue or disk error
stops capture visibly instead of buffering without limit. Saved output may contain
application secrets.

Recording follows the selected existing container IDs. It does not reconnect across
container recreation, start a background service or change runtime logging settings.

## Development

```sh
uv run ruff check
uv run ty check --error-on-warning
uv run pytest
scripts/record.sh runtop demo "$(cat scripts/tapes/runtop-demo.keys)" 24
```

Committed recordings use demo data. The [data contract](spec/SPEC.md) defines target
states, normalization and snapshot JSON.
