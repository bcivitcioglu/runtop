# runtop — data contract

This document describes target states, normalization and the
`runtop.snapshot/v1` JSON document produced by the Textual app.

## Targets

| kind | key | source | writable |
|---|---|---|---|
| `lima` | `lima:<name>` | `limactl list --format json` (one object per line) | yes |
| `colima` | `colima:<profile>` | `colima list --json` (Docker profiles, including stopped ones); `$COLIMA_HOME/<profile>/docker.sock` or `~/.colima/<profile>/docker.sock` | yes |
| `host` | `host` | `$DOCKER_HOST` `unix://…`, else `$XDG_RUNTIME_DIR/docker.sock` (rootless), else `/var/run/docker.sock` (runtop on servers) | yes |
| `host` | `local:<name>` | Docker contexts with `unix://` endpoints | yes |
| `context` | `ctx:<name>` | `docker context ls --format json`, endpoints `ssh://` or `tcp://` only | **never** |

- Lima socket: `<dir>/sock/docker.sock`. Missing file → state `no_docker_socket`
  (CI VMs without docker), not "unreachable".
- Lima fields used: `name status dir vmType arch cpus memory disk` (bytes; `disk` is the cap).
- Host allocation: `du -sk <dir>` × 1024, optionally measured at most every 30 s.
  Interactive monitoring does not run this scan. This is not guest filesystem usage.
- Discover sources independently, keep successful results alongside errors, and deduplicate
  local sockets by canonical path. Prefer managed VM metadata over context aliases.
- Colima lifecycle uses `colima start|stop --profile <profile>`; Lima uses `limactl`.
  Colima containerd/incus profiles are not Docker targets.

## Target states (UI must render each distinctly; a loader is never an empty list)

`loading` (no data yet) · `ok` · `vm_stopped` · `no_docker_socket` · `unreachable`
(daemon request failed; show error) · `no_targets`. Within `ok`: `empty` (no containers) and
`no_match` (filter hides everything) are separate states.

## Engine API (local sockets)

HTTP over the unix socket. `GET /version` → prefix all calls with `/v<ApiVersion>`.

Endpoints: `/_ping`, `/version`, `/containers/json?all=1`, `/containers/{id}/json`,
`/containers/{id}/stats?stream=false&one-shot=true`, `/containers/{id}/logs`,
`POST /containers/{id}/start|stop?t=10|restart?t=10`, `DELETE /containers/{id}?force=1`,
`/images/json`, `POST /images/prune?filters={"dangling":["true"]}`.

### Container normalization

| field | rule |
|---|---|
| `id` | first 12 chars of `Id` |
| `name` | `Names` with leading `/` trimmed, sorted, joined with `,` |
| `image` | `Image` verbatim |
| `state` | `State` lower-cased (`running`, `exited`, `created`, `paused`, `restarting`, `dead`) |
| `status` | `Status` verbatim (`Up 2 hours (healthy)`, `Exited (1) 3 hours ago`) |
| `health` | from `Status`: `(healthy)`→`healthy`, `(unhealthy)`→`unhealthy`, `(health: starting)`→`starting`, else `null` |
| `exit_code` | from `Status` `Exited (N)` → `N`, else `null` |
| `project` | label `com.docker.compose.project`, else `""` (UI label: `standalone`) |
| `service` | label `com.docker.compose.service`, else `""` |
| `ports` | per entry: `PublicPort≠0` → `"<public>-><private>"`; else `PrivatePort≠0` → `"<private>"`. Dedupe (IPv4+IPv6 duplicate), sort numerically by (public-or-private, private) |

Container order: by `project`, then `name` (the UI may re-sort within groups).

### Image normalization

| field | rule |
|---|---|
| `id` | `Id` without `sha256:`, first 12 chars |
| `ref` | first `RepoTags` entry that is not `<none>:<none>`, with any `@sha256:…` suffix removed; else first `RepoDigests` as `<repo>@<first 12 hex of digest>`; else `<none>:<none>` |
| `size_bytes` | `Size` |
| `containers` | `Containers` (`-1` → `null`) |
| `dangling` | `RepoTags` empty or only `<none>:<none>` |

Image order: `size_bytes` descending.

### Stats

Given a sample `s` and a previous sample `p` (`p = s.precpu_stats` when the daemon
filled it, i.e. `stream=false` without one-shot; otherwise the previous one-shot
sample of the same container kept client-side):

```
cpu_delta = s.cpu_stats.cpu_usage.total_usage - p.cpu_usage.total_usage
sys_delta = s.cpu_stats.system_cpu_usage     - p.system_cpu_usage
ncpu      = s.cpu_stats.online_cpus  or  len(s.cpu_stats.cpu_usage.percpu_usage)  or  1
cpu_percent = cpu_delta / sys_delta * ncpu * 100     if sys_delta > 0 and cpu_delta >= 0
            = null                                    otherwise (first one-shot sample)

mem_bytes       = max(0, memory_stats.usage - cache)
  cache         = memory_stats.stats.inactive_file        (cgroup v2)
                  or memory_stats.stats.total_inactive_file (cgroup v1) or 0
mem_limit_bytes = memory_stats.limit
```

`percpu_usage` is empty on cgroup v2 — counting it under-reports CPU
by the core count. Raw `usage` includes page cache — subtracting it matches `docker stats`.

Stats are fetched only for `running` containers, max 8 concurrent, with a 4 s budget
for the complete stats round including queued requests. Unavailable samples remain unknown.

### Logs

`stdout=1&stderr=1&tail=200[&follow=1][&timestamps=1]`. If the container's
`Config.Tty` is false the body is multiplexed: repeated 8-byte headers
`[stream, 0, 0, 0, size uint32 big-endian]` (stream 1 = stdout, 2 = stderr) followed
by `size` bytes; frames may split lines. TTY containers send raw bytes. Split on `\n`,
drop a trailing `\r`. Blank lines are kept; a final line without `\n` is still emitted
(per stream). UIs strip non-SGR escape sequences before rendering.

## Remote contexts (docker CLI, read-only)

Every call is `docker --context <name> <verb> …` with verb in the allowlist
`ps images logs inspect stats version`. Anything else is a programming error and must
raise/refuse before spawning. Remote stats are not polled (each call is an ssh round trip).
Target discovery (`docker context ls --format json`) runs without `--context` and is the
only CLI call outside this allowlist.

- `ps -a --format json`: `ID Names Image State Status Labels Ports`.
  `Labels` is `k=v,k=v`; `Ports` like `0.0.0.0:8080->80/tcp, :::8080->80/tcp, 9000/tcp`
  → pairs `(\d+)->(\d+)` deduped, plus bare `(\d+)/(tcp|udp)` ports that are not either side of a pair; same numeric sort as the Engine rule.
- `images --format json`: `Repository Tag ID Size Containers`. `Size` is decimal
  (`376MB` = 376·10⁶, `22.6kB`, `1.02GB`, `1.5TB`). Repository `<none>` → dangling.
- `logs --tail 200 [-f]`: read stdout **and** stderr.

## Snapshot JSON — `runtop.snapshot/v1`

Used by `spec/fixtures/snapshots/*.json`, `runtop --demo`, `runtop --dump lima:docker`,
and the fixture/snapshot tests. snake_case; decoders ignore unknown fields.

```json
{
  "schema": "runtop.snapshot/v1",
  "generated_at": "2026-09-12T20:31:00Z",
  "targets": [
    {
      "key": "lima:docker", "kind": "lima", "name": "docker", "read_only": false,
      "endpoint": "unix:///Users/demo/.lima/docker/sock/docker.sock",
      "state": "ok", "error": null,
      "vm": {"status": "Running", "vm_type": "vz", "arch": "aarch64", "cpus": 4,
             "memory_bytes": 4294967296, "disk_bytes": 107374182400, "disk_used_bytes": 22548578304},
      "containers": [
        {"id": "b2c3d4e5f6a1", "name": "shop-api-1", "image": "shop/api:dev",
         "state": "running", "status": "Up 2 hours (healthy)", "health": "healthy", "exit_code": null,
         "project": "shop", "service": "api", "ports": ["3000->3000"],
         "stats": {"cpu_percent": 2.2, "mem_bytes": 171966464, "mem_limit_bytes": 4294967296}}
      ],
      "images": [
        {"id": "9f86d081884c", "ref": "shop/api:dev", "size_bytes": 335544320, "containers": 1, "dangling": false}
      ]
    }
  ]
}
```

- `vm` is `null` for targets other than Lima and Colima; `disk_used_bytes` may be `null` (not measured).
- `stats` is `null` when unavailable (not running, remote). A container with a first
  one-shot sample has a `stats` object whose `cpu_percent` is `null` (memory is known).
- `state` is one of the target states above; `containers`/`images` are `[]` unless `ok`.

### Optional partial-result fields

`images_error` (string, omitted on success) reports an image-list failure without
invalidating successfully read containers. `stale` (boolean, omitted when false)
marks retained UI data after a failed daemon refresh; its timestamp remains the last
successful fetch. Stale data does not append resource history and cannot drive mutations.
Cached images may be displayed after an image failure, but pruning is disabled. The
`--dump` command emits the newly read result; consumers should inspect `state` and
`images_error` to detect unavailable or partial data.

### On-demand storage

`GET /system/df` is a local read only, requested explicitly with `D`, with a 30 s timeout.
The app normalizes `engine/disk_usage.json` using these rules:

- Image layers: `LayersSize`, not the sum of image sizes (shared layers counted once).
- Containers: sum `SizeRw`; unknown if any entry lacks a nonnegative size.
- Volumes: sum `UsageData.Size`; unknown if any entry lacks a nonnegative size.
- Build cache: sum `Size`, labelled logical record sizes, which may share data.
- Empty lists have count/size zero. Categories are not additive and exclude guest OS,
  bind mounts and some logs. Unknown is never labelled zero or unused.
- No background accounting or automatic deletion. Pruning only calls Docker’s existing
  dangling-image endpoint and reports actual `SpaceReclaimed` afterwards.

### Project workflows

`l` on a Compose group combines logs with service/container prefixes and a bounded queue.
Closing the view cancels and joins its readers. `s` / `x` / `R` on a project previews the
listed members, captures their IDs and target, and applies operations to existing containers.
Filters restrict membership; new containers discovered later are not included. All project
operations confirm. These are not Compose deployments and do not execute project files or
perform dependency ordering. Partial failures are reported per container, with named failures. Remote and stale targets remain non-writable.

## Fixtures (`spec/fixtures/`)

- `engine/` — raw Engine responses captured from disposable fixture containers (env values redacted, home/user/private names scrubbed).
- `cli/` — raw `limactl list` (field subset) and `docker ps/images --format json` lines.
- `expected/` — normalized output of the rules above for those raw fixtures, generated by
  `scripts/gen_expected.py` (stdlib reference implementation). The tests assert them.
- `snapshots/demo.json` — synthetic multi-target snapshot for `--demo`, recordings and
  golden tests. No real hostnames or data.

## Action behavior

- Context targets are hard read-only: banner, mutating keys/buttons refuse with a reason,
  and the client layer cannot issue mutating calls.
- Destructive ops confirm: remove container, prune images, stop VM.
- Actions bind to the target + container id captured when the user triggered them — never
  to whatever is selected when the async work runs.
- Env values are masked by default in any detail view.

## Navigation and action keys

`↑↓`/`jk` move · `enter` open/toggle · `s` start · `x` stop · `R` restart · `X` remove ·
`e` exec · `p` prune dangling · `/` filter · `o` sort · `r` refresh · `?` help · `q` quit.

## Recording (README visuals + design validation)

`scripts/record.sh <tool> <name> "<keys>" <seconds> [COLSxROWS]` runs the tool with
`--demo --keys … --quit-after …` under asciinema, renders a GIF with agg and extracts PNG
keyframes into `assets/<tool>/`. Committed assets come from `--demo` only.

## Log archive contract

Archives are opt-in JSONL in a user-selected directory. Names match
`runtop-<20-digit creation nanoseconds>-<32 hex random digits>.jsonl`.
A `.runtop-record.lock` advisory lock permits one writer per folder. Readers stream
files in name order, tolerate an incomplete trailing line and reject symlinks.

Each v1 record contains `version: 1`, capture `time` (ISO UTC), `target`, `container`,
`name`, `project`, `service`, `stream`, `text`, and `truncated` (boolean). The serialized
record is at most 64 KiB. Capture time is distinct from runtime timestamps in `text`.

The default total JSONL budget is 64 MiB, rotated file limit 8 MiB and retention 7 days.
The oldest owned archives are removed first; unrelated files are never deleted.
Retention is applied by the active writer; no cleanup daemon is installed.
Disk I/O runs off the UI event loop and capture uses a bounded queue. A recording
is foreground-only and never resumes automatically on startup.

`runtop logs record|export|search|status --help` defines the full-edition CLI.
Search is literal, result-limited, and emits one record per line with `--json`.
It returns 0 for matches, 1 for none and 2 for errors. Recording returns nonzero
when any source or disk operation fails.
