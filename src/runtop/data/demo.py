"""FixtureBackend: a ``runtop.snapshot/v1`` document served as if it were live.

Used by ``--demo``, recordings and tests. Everything is deterministic: stats jitter is a pure
function of (container id, round), logs are generated from per-image templates, and mutations
edit the in-memory snapshot (after a short simulated latency) so actions look real.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import zlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from runtop.data.engine import EngineError, LogLine
from runtop.data.models import Container, DaemonState, Image, Stats, Target, TargetSnapshot, load_document
from runtop.data.safety import ensure_writable
from runtop.data.storage import DiskUsage
from runtop.data.wire import ContainerInspect, SnapshotDocument, loads_as

DEMO_SNAPSHOT = "demo.json"


def find_demo_snapshot() -> Path:
    """``spec/fixtures/snapshots/demo.json`` from a source tree, else the packaged copy."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "spec" / "fixtures" / "snapshots" / DEMO_SNAPSHOT
        if candidate.exists():
            return candidate
    packaged = here.parent.parent / "resources" / DEMO_SNAPSHOT
    if packaged.exists():
        return packaged
    raise FileNotFoundError("demo snapshot not found")


def _wave(round_: int, seed: int, speed: float) -> float:
    phase = (seed % 628) / 100.0
    return (math.sin(round_ * speed + phase) + 0.45 * math.sin(round_ * speed * 2.7 + phase * 1.9)) / 1.45


def jitter_stats(cid: str, st: Stats, round_: int) -> Stats:
    seed = zlib.crc32(cid.encode())
    cpu = st.cpu_percent
    if cpu is not None:
        cpu = round(max(0.05, cpu * (1 + 0.55 * _wave(round_, seed, 0.85))), 2)
    mem = int(st.mem_bytes * (1 + 0.04 * _wave(round_, seed >> 3, 0.3)))
    return Stats(cpu, mem, st.mem_limit_bytes)


# ---------------------------------------------------------------- synthetic logs

_PATHS = ["/", "/api/orders", "/api/cart", "/healthz", "/api/users/42", "/static/app.js", "/api/search?q=lamp"]


def _log_line(c: Container, i: int, ts: datetime) -> LogLine:
    h = zlib.crc32(f"{c.id}:{i}".encode())
    path = _PATHS[h % len(_PATHS)]
    ms = 3 + h % 80
    status = 200 if h % 11 else (404 if h % 2 else 500)
    iso = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{h % 1000:03d}Z"
    image = c.image
    if "nginx" in image:
        return LogLine("stdout", f'{ts:%H:%M:%S} "GET {path}" {status} {512 + h % 4096}b {ms}ms')
    if "postgres" in image:
        if h % 7 == 0:
            return LogLine("stderr", f"{ts:%Y-%m-%d %H:%M:%S}.{h % 1000:03d} UTC [{60 + h % 40}] LOG:  "
                                     f"checkpoint complete: wrote {h % 90} buffers")
        return LogLine("stderr", f"{ts:%Y-%m-%d %H:%M:%S}.{h % 1000:03d} UTC [1] LOG:  duration: {ms}.{h % 9}1 ms  "
                                 f"statement: SELECT * FROM orders WHERE id = {h % 900}")
    if "mysql" in image:
        return LogLine("stderr", f"{iso} {h % 40} [Note] [MY-010914] [Server] Aborted connection {h % 5000} "
                                 f"to db: 'ghost' (Got an error reading communication packets)")
    if "ghost" in image:
        return LogLine("stdout", f'[{ts:%Y-%m-%d %H:%M:%S}] INFO "GET /ghost/api/content{path}" {status} {ms}ms')
    if "redis" in image:
        return LogLine("stdout", f"1:M {ts:%d %b %Y %H:%M:%S}.{h % 1000:03d} * {1 + h % 100} changes in 300 seconds. "
                                 "Saving...")
    if "mailpit" in image:
        return LogLine("stdout", f'time="{ts:%Y/%m/%d %H:%M:%S}" level=info msg="[smtpd] accepted message '
                                 f'from order-{h % 900}@shop.example"')
    if "worker" in image:
        if c.state == "exited" and i >= 0:
            return LogLine("stderr", f"{iso} ERROR job failed: dial tcp 10.0.0.5:6379: connect: connection refused")
        return LogLine("stdout", f"{iso} INFO  processed job=send-receipt id={h % 9000} in {ms}ms")
    if "caddy" in image:
        return LogLine("stdout", json.dumps({"level": "info", "ts": round(ts.timestamp(), 3),
                                             "logger": "http.log.access", "msg": "handled request",
                                             "request": {"method": "GET", "uri": path}, "status": status}))
    level = "WARN " if h % 13 == 0 else ("ERROR" if status == 500 else "INFO ")
    extra = f" slow query took {200 + ms}ms" if level == "WARN " else ""
    return LogLine("stderr" if level == "ERROR" else "stdout",
                   f"{iso} {level} http method=GET path={path} status={status} duration={ms}ms{extra}")


# ---------------------------------------------------------------- backend


class FixtureBackend:
    """Serves a snapshot document; implements the read, detail and mutable backend protocols."""

    demo = True  # no real daemon behind it: exec is unavailable

    def __init__(self, source: str | Path | SnapshotDocument | None = None, *, latency: float = 0.0,
                 jitter: bool = True, slow: dict[str, float] | None = None, action_latency: float = 0.0,
                 vm_latency: float = 0.0, log_interval: float = 0.8) -> None:
        if source is None:
            source = find_demo_snapshot()
        data = loads_as(Path(source).read_text(), SnapshotDocument) if isinstance(source, str | Path) else source
        self.generated_at = datetime.fromisoformat(str(data.get("generated_at", "2026-09-12T20:31:00Z"))
                                                   .replace("Z", "+00:00"))
        self._snaps = {s.key: s for s in load_document(data)}
        self.latency = latency
        self.slow = dict(slow or {})  # first fetch of these keys takes this long (one deliberate loader)
        self.jitter = jitter
        self.action_latency = action_latency
        self.vm_latency = vm_latency
        self.log_interval = log_interval
        self.rounds: dict[str, int] = {}
        self.fetch_log: list[str] = []
        self.calls: list[tuple[str, ...]] = []
        self.overrides: dict[str, TargetSnapshot] = {}
        self.fail_next: dict[str, str] = {}
        self._stashed: dict[str, TargetSnapshot] = {}

    @property
    def keys(self) -> list[str]:
        return list(self._snaps)

    async def storage(self, target: Target) -> DiskUsage:
        return {"LayersSize": 2_400_000_000, "Images": [{"Id": "demo"}],
                "Containers": [{"SizeRw": 12_000_000}],
                "Volumes": [{"Name": "database", "UsageData": {"Size": 640_000_000, "RefCount": 1}}],
                "BuildCache": [{"Size": 320_000_000, "InUse": False}]}

    async def discover(self) -> list[Target]:
        return [self._current(k).target for k in self._snaps]

    def _current(self, key: str) -> TargetSnapshot:
        return self.overrides.get(key) or self._snaps[key]

    def set_snapshot(self, snap: TargetSnapshot) -> None:
        """Test hook: replace what a target returns (e.g. VM stopped elsewhere)."""
        self.overrides[snap.key] = snap
        self._snaps.setdefault(snap.key, snap)

    async def fetch(self, target: Target) -> TargetSnapshot:
        self.fetch_log.append(target.key)
        delay = self.slow.pop(target.key, None)
        if delay is None:
            delay = self.latency
        if delay:
            await asyncio.sleep(delay)
        base = self._current(target.key)
        n = self.rounds[target.key] = self.rounds.get(target.key, 0) + 1
        containers = tuple(self.jittered(c, n) for c in base.containers)
        return replace(base, containers=containers, fetched_at=time.time())

    def jittered(self, c: Container, round_: int) -> Container:
        if not self.jitter or c.stats is None:
            return c
        return c.with_stats(jitter_stats(c.id, c.stats, round_))

    def history(self, key: str, cid: str, n: int) -> list[float]:
        """Plausible past CPU samples (oldest first) so demo sparklines start populated."""
        base = self._current(key).container(cid)
        if base is None or base.stats is None or base.stats.cpu_percent is None:
            return []
        return [jitter_stats(cid, base.stats, r).cpu_percent or 0.0 for r in range(-n + 1, 1)]

    def mem_history(self, key: str, cid: str, n: int) -> list[int]:
        base = self._current(key).container(cid)
        if base is None or base.stats is None:
            return []
        return [jitter_stats(cid, base.stats, r).mem_bytes for r in range(-n + 1, 1)]

    # -- detail reads

    async def logs(self, target: Target, cid: str, *, tail: int = 200, follow: bool = False) -> AsyncIterator[LogLine]:
        c = self._current(target.key).container(cid) or self._snaps[target.key].container(cid)
        if c is None:
            raise EngineError(f"No such container: {cid}")
        start = self.generated_at - timedelta(seconds=2 * tail)
        for i in range(tail):
            yield _log_line(c, i - tail, start + timedelta(seconds=2 * i))
        if not follow or not c.running:
            return
        i = 0
        while True:
            await asyncio.sleep(self.log_interval)
            i += 1
            yield _log_line(c, i, self.generated_at + timedelta(seconds=2 * i))

    async def inspect(self, target: Target, cid: str) -> ContainerInspect:
        c = self._current(target.key).container(cid)
        if c is None:
            raise EngineError(f"No such container: {cid}")
        h = zlib.crc32(c.id.encode())
        network = f"{c.project}_default" if c.project else "bridge"
        env = ["TZ=UTC", "APP_ENV=production", f"DATABASE_URL=postgres://app:s3cret@db:5432/{c.project or 'app'}",
               "LOG_LEVEL=info"]
        return {
            "Id": c.id + "0" * 52,
            "Created": (self.generated_at - timedelta(days=5)).isoformat().replace("+00:00", "Z"),
            "Path": "docker-entrypoint.sh",
            "Args": [c.service or c.name],
            "State": {"Status": c.state, "ExitCode": c.exit_code or 0,
                      "StartedAt": (self.generated_at - timedelta(hours=2)).isoformat().replace("+00:00", "Z")},
            "Config": {"Image": c.image, "Tty": False, "Env": env, "Cmd": [c.service or c.name],
                       "WorkingDir": "/app", "Labels": {"com.docker.compose.project": c.project}},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped" if c.project else "no"}},
            "Mounts": ([{"Type": "volume", "Name": f"{c.project}_{c.service}-data", "Destination": "/data"}]
                       if c.project else []),
            "NetworkSettings": {"Networks": {network: {"IPAddress": f"172.18.0.{2 + h % 200}"}}},
        }

    # -- mutations

    async def _mutate(self, op: str, target: Target, cid: str | None = None) -> TargetSnapshot:
        ensure_writable(target)
        self.calls.append((op, target.key) + ((cid,) if cid else ()))
        if self.action_latency:
            await asyncio.sleep(self.action_latency)
        if op in self.fail_next:
            raise EngineError(self.fail_next.pop(op))
        return self._current(target.key)

    def _replace_container(self, snap: TargetSnapshot, cid: str, new: Container | None) -> None:
        containers = tuple(new if c.id == cid else c for c in snap.containers if new is not None or c.id != cid)
        self.overrides[snap.key] = replace(snap, containers=containers)

    def _started(self, key: str, c: Container) -> Container:
        original = self._snaps[key].container(c.id)
        stats = original.stats if original and original.stats else Stats(0.3, 12 << 20, 4 << 30)
        return replace(c, state="running", status="Up Less than a second", exit_code=None, stats=stats,
                       health=None if c.health is None else "starting")

    async def start(self, target: Target, cid: str) -> None:
        snap = await self._mutate("start", target, cid)
        c = snap.container(cid)
        if c is not None:
            self._replace_container(snap, cid, self._started(target.key, c))

    async def restart(self, target: Target, cid: str) -> None:
        snap = await self._mutate("restart", target, cid)
        c = snap.container(cid)
        if c is not None:
            self._replace_container(snap, cid, self._started(target.key, c))

    async def stop(self, target: Target, cid: str) -> None:
        snap = await self._mutate("stop", target, cid)
        c = snap.container(cid)
        if c is not None:
            self._replace_container(snap, cid, replace(c, state="exited", status="Exited (0) Less than a second ago",
                                                       exit_code=0, stats=None, health=None))

    async def remove(self, target: Target, cid: str) -> None:
        snap = await self._mutate("remove", target, cid)
        self._replace_container(snap, cid, None)

    async def prune_dangling_images(self, target: Target) -> int:
        snap = await self._mutate("prune", target)
        dangling: list[Image] = [i for i in snap.images if i.dangling]
        self.overrides[target.key] = replace(snap, images=tuple(i for i in snap.images if not i.dangling))
        return sum(i.size_bytes for i in dangling)

    async def _vm(self, op: str, target: Target, running: bool) -> None:
        ensure_writable(target)
        self.calls.append((op, target.key))
        if self.vm_latency:
            await asyncio.sleep(self.vm_latency)
        if op in self.fail_next:
            raise EngineError(self.fail_next.pop(op))
        snap = self._current(target.key)
        if snap.target.vm is None:
            raise ValueError(f"{target.name} is not a Lima VM")
        vm = replace(snap.target.vm, status="Running" if running else "Stopped")
        new_target = replace(snap.target, vm=vm)
        if running:
            stashed = self._stashed.pop(target.key, None)
            self.overrides[target.key] = (replace(stashed, target=new_target) if stashed is not None
                                          else TargetSnapshot(new_target, DaemonState.OK))
        else:
            if snap.state is DaemonState.OK:
                self._stashed[target.key] = snap
            self.overrides[target.key] = TargetSnapshot(new_target, DaemonState.VM_STOPPED)

    async def vm_start(self, target: Target) -> None:
        await self._vm("vm_start", target, running=True)

    async def vm_stop(self, target: Target) -> None:
        await self._vm("vm_stop", target, running=False)

    def exec_argv(self, target: Target, cid: str) -> list[str] | None:
        ensure_writable(target)
        return None  # a snapshot has no shell to open
