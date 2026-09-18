"""Backends: where targets and snapshots come from.

* :class:`LiveBackend` — ``limactl`` + Engine API sockets + read-only docker contexts.
* :class:`FixtureBackend` — a ``runtop.snapshot/v1`` document (``--demo`` and tests), with
  deterministic stats jitter so sparklines move in recordings.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Protocol, runtime_checkable

from runtop.data import lima
from runtop.data.engine import EngineClient, EngineError, LogLine
from runtop.data.guest import GuestProbe, GuestVitals
from runtop.data.models import (
    DaemonState,
    Target,
    TargetKind,
    TargetSnapshot,
    VMInfo,
)
from runtop.data.remote import (
    RemoteClient,
    RemoteError,
    Runner,
    RunResult,
    list_contexts,
    subprocess_runner,
)
from runtop.data.safety import EXEC_SHELL, ensure_writable
from runtop.data.storage import DiskUsage
from runtop.data.wire import ColimaLine, ContainerInspect, CpuStats, loads_as


class DiscoveryError(RuntimeError):
    """Listing targets failed (e.g. limactl missing). Never leaves the UI on a loader."""


@runtime_checkable
class ReadBackend(Protocol):
    async def discover(self) -> list[Target]: ...

    async def fetch(self, target: Target) -> TargetSnapshot: ...


@runtime_checkable
class StreamingBackend(Protocol):
    """Progressive fetch (remote contexts yield containers before images)."""

    def fetch_stream(self, target: Target) -> AsyncIterator[TargetSnapshot]: ...


@runtime_checkable
class HistoryBackend(Protocol):
    """Past CPU samples so sparklines start populated (demo)."""

    def history(self, key: str, cid: str, n: int) -> list[float]: ...


@runtime_checkable
class MemHistoryBackend(Protocol):
    def mem_history(self, key: str, cid: str, n: int) -> list[int]: ...


@runtime_checkable
class LogsBackend(Protocol):
    def logs(self, target: Target, cid: str, *, tail: int = 200, follow: bool = False) -> AsyncIterator[LogLine]: ...


@runtime_checkable
class InspectBackend(Protocol):
    async def inspect(self, target: Target, cid: str) -> ContainerInspect: ...


@runtime_checkable
class DetailBackend(LogsBackend, InspectBackend, Protocol):
    """Per-container reads used by the detail pane (A3)."""


@runtime_checkable
class StorageBackend(Protocol):
    async def storage(self, target: Target) -> DiskUsage: ...


@runtime_checkable
class VitalsBackend(Protocol):
    """Guest-level vitals for a VM, independent of whether it speaks Docker."""

    async def vitals(self, target: Target) -> GuestVitals | None: ...

    def cached_vitals(self, target: Target) -> GuestVitals | None: ...


@runtime_checkable
class ExecBackend(Protocol):
    def exec_argv(self, target: Target, cid: str) -> list[str] | None: ...


@runtime_checkable
class MutableBackend(Protocol):
    """Container / VM mutations (A4). Every method refuses read-only targets."""

    async def start(self, target: Target, cid: str) -> None: ...

    async def stop(self, target: Target, cid: str) -> None: ...

    async def restart(self, target: Target, cid: str) -> None: ...

    async def remove(self, target: Target, cid: str) -> None: ...

    async def prune_dangling_images(self, target: Target) -> int: ...

    async def vm_start(self, target: Target) -> None: ...

    async def vm_stop(self, target: Target) -> None: ...

    def exec_argv(self, target: Target, cid: str) -> list[str] | None: ...




def _short_error(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    return text.splitlines()[-1][:300]


# ---------------------------------------------------------------- live


class LiveBackend:
    """Real daemons. One instance per app; keeps per-target previous CPU samples."""

    def __init__(self, runner: Runner = subprocess_runner, *, include_contexts: bool = True) -> None:
        self._context_cache: tuple[float, list[Target], str | None] | None = None
        self.discovery_error: str | None = None
        self._runner = runner
        self.include_contexts = include_contexts
        self._prev_cpu: dict[str, dict[str, CpuStats]] = {}
        self._disk = lima.DiskUsage()
        self._engines: dict[str, EngineClient] = {}
        self._guest = GuestProbe(runner)

    async def discover(self) -> list[Target]:
        self.discovery_error = None
        errors: list[str] = []

        async def vms() -> list[Target]:
            if not lima.available():
                return []
            try:
                return [inst.to_target(self._disk.cached(inst.dir)) for inst in await lima.list_instances()]
            except lima.LimaError as e:
                errors.append(str(e))
                return []

        async def colima() -> list[Target]:
            if not shutil.which("colima"):
                return []
            try:
                result = await self._runner(["colima", "list", "--json"], 10.0)
                if result.returncode:
                    raise DiscoveryError(result.stderr.decode(errors="replace").strip())
                return parse_colima(result.stdout.decode(errors="replace"))
            except (RemoteError, TimeoutError, ValueError, DiscoveryError) as e:
                errors.append(f"colima: {_short_error(e)}")
                return []

        async def contexts() -> list[Target]:
            if not self.include_contexts or not shutil.which("docker"):
                return []
            cached = self._context_cache
            if cached and time.monotonic() - cached[0] < 30:
                if cached[2]:
                    errors.append(cached[2])
                return cached[1]
            try:
                result = await list_contexts(self._runner)
                self._context_cache = (time.monotonic(), result, None)
                return result
            except RemoteError as e:
                errors.append(str(e))
                previous = cached[1] if cached else []
                self._context_cache = (time.monotonic(), previous, str(e))
                return previous

        local, profiles, ctxs = await asyncio.gather(vms(), colima(), contexts())
        candidates = [*local, *profiles]
        if host := host_socket():
            candidates.append(Target("host", TargetKind.HOST, "host", False, f"unix://{host}"))
        candidates += ctxs
        targets: list[Target] = []
        sockets: set[str] = set()
        for target in candidates:
            sock = target.socket_path
            canonical = os.path.realpath(sock) if sock else None
            if canonical and canonical in sockets:
                continue
            if canonical:
                sockets.add(canonical)
            targets.append(target)
        self.discovery_error = "; ".join(errors) or None
        if errors and not targets:
            raise DiscoveryError(self.discovery_error)
        return targets

    def _engine(self, sock: str) -> EngineClient:
        eng = self._engines.get(sock)
        if eng is None:
            eng = self._engines[sock] = EngineClient(sock)
        return eng

    async def aclose(self) -> None:
        for eng in self._engines.values():
            await eng.aclose()
        self._engines.clear()

    async def fetch(self, target: Target, *, with_stats: bool = True, one_shot: bool = True,
                    with_disk: bool = False, with_images: bool = True) -> TargetSnapshot:
        now = time.time()
        if target.kind is TargetKind.CONTEXT:
            return await self._fetch_remote(target, now)
        if target.vm is not None and target.dir:
            used = (await self._disk.measure(target.dir) if target.vm.running and with_disk
                    else self._disk.cached(target.dir))
            target = replace(target, vm=replace(target.vm, disk_used_bytes=used))
        if target.vm is not None and not target.vm.running:
            return TargetSnapshot(target, DaemonState.VM_STOPPED, fetched_at=now)
        sock = target.socket_path
        if not sock or not os.path.exists(sock):
            return TargetSnapshot(target, DaemonState.NO_DOCKER_SOCKET, fetched_at=now)
        eng = self._engine(sock)
        try:
            containers = await eng.containers()
            images_error = None
            try:
                images = await eng.images() if with_images else []
            except (EngineError, json.JSONDecodeError) as e:
                images, images_error = [], _short_error(e)
            if with_stats:
                prev = self._prev_cpu.setdefault(target.key, {})
                containers = await eng.fill_stats(containers, prev, one_shot=one_shot)
        except (EngineError, json.JSONDecodeError) as e:
            return TargetSnapshot(target, DaemonState.UNREACHABLE, error=_short_error(e), fetched_at=now)
        return TargetSnapshot(target, DaemonState.OK, containers=tuple(containers), images=tuple(images),
                              fetched_at=time.time(), images_error=images_error,
                              images_loaded=with_images and images_error is None)

    async def _fetch_remote(self, target: Target, now: float) -> TargetSnapshot:
        snap: TargetSnapshot | None = None
        async for partial in self.fetch_stream(target):
            snap = partial
        return snap or TargetSnapshot(target, DaemonState.UNREACHABLE, error="no result", fetched_at=now)

    async def fetch_stream(self, target: Target) -> AsyncIterator[TargetSnapshot]:
        """Yield containers first on every refresh; images and stats complete independently."""
        if target.kind is not TargetKind.CONTEXT:
            snap = await self.fetch(target, with_stats=False, with_images=False)
            snap = replace(snap, stats_sampled=False)
            yield snap
            if snap.state is not DaemonState.OK or not target.socket_path:
                return
            eng = self._engine(target.socket_path)
            images = asyncio.create_task(eng.images())
            prev = self._prev_cpu.setdefault(target.endpoint, {})
            stats = asyncio.create_task(eng.fill_stats(list(snap.containers), prev))
            pending: set[asyncio.Task[object]] = {images, stats}
            try:
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    sampled = stats in done
                    if sampled:
                        snap = replace(snap, containers=tuple(stats.result()))
                    if images in done:
                        try:
                            snap = replace(snap, images=tuple(images.result()), images_loaded=True)
                        except (EngineError, json.JSONDecodeError) as e:
                            snap = replace(snap, images_error=_short_error(e), images_loaded=False)
                    yield replace(snap, stats_sampled=sampled, fetched_at=time.time())
            finally:
                for task in (images, stats):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(images, stats, return_exceptions=True)
            return
        now = time.time()
        client = RemoteClient(target.name, self._runner)
        ps = asyncio.ensure_future(client.containers())
        images = asyncio.ensure_future(client.images())
        try:
            try:
                containers = await ps
            except (RemoteError, TimeoutError) as e:
                images.cancel()
                yield TargetSnapshot(target, DaemonState.UNREACHABLE, error=_short_error(e), fetched_at=now)
                return
            if not images.done():
                yield TargetSnapshot(target, DaemonState.OK, containers=tuple(containers), images_loaded=False,
                                     fetched_at=time.time())
            try:
                imgs = await images
            except (RemoteError, TimeoutError) as e:
                yield TargetSnapshot(target, DaemonState.OK, containers=tuple(containers),
                                     images_loaded=False, images_error=_short_error(e), fetched_at=time.time())
                return
            yield TargetSnapshot(target, DaemonState.OK, containers=tuple(containers), images=tuple(imgs),
                                 fetched_at=time.time())
        finally:
            for task in (ps, images):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ps, images, return_exceptions=True)

    # -- guest vitals

    @staticmethod
    def _probeable(target: Target) -> bool:
        """Only Lima instances: ``limactl shell`` is the one guest entry runtop owns."""
        return target.kind is TargetKind.LIMA and target.vm is not None and target.vm.running

    async def vitals(self, target: Target) -> GuestVitals | None:
        return await self._guest.measure(target.name) if self._probeable(target) else None

    def cached_vitals(self, target: Target) -> GuestVitals | None:
        return self._guest.cached(target.name) if self._probeable(target) else None

    async def storage(self, target: Target) -> DiskUsage:
        if target.is_remote:
            raise EngineError("storage inspection is available on local Docker sockets")
        if not target.socket_path:
            raise EngineError("no Docker socket")
        return await self._engine(target.socket_path).disk_usage()

    # -- detail reads

    async def logs(self, target: Target, cid: str, *, tail: int = 200, follow: bool = False) -> AsyncIterator[LogLine]:
        if target.kind is TargetKind.CONTEXT:
            async for stream, text in RemoteClient(target.name, self._runner).logs(cid, tail=tail, follow=follow):
                yield LogLine(stream, text)
            return
        sock = target.socket_path
        if not sock:
            return
        # A dedicated client per stream: a follow request holds its connection open.
        async with EngineClient(sock) as eng:
            async for line in eng.logs(cid, tail=tail, follow=follow, timestamps=True):
                yield line

    async def inspect(self, target: Target, cid: str) -> ContainerInspect:
        if target.kind is TargetKind.CONTEXT:
            return await RemoteClient(target.name, self._runner).inspect(cid)
        sock = target.socket_path
        if not sock:
            return {}
        return await self._engine(sock).inspect(cid)

    # -- mutations (A4)

    def _local_engine(self, target: Target) -> EngineClient:
        ensure_writable(target)
        sock = target.socket_path
        if not sock:
            raise EngineError(f"{target.name} has no docker socket")
        return self._engine(sock)

    async def start(self, target: Target, cid: str) -> None:
        await self._local_engine(target).start(cid)

    async def stop(self, target: Target, cid: str) -> None:
        await self._local_engine(target).stop(cid)

    async def restart(self, target: Target, cid: str) -> None:
        await self._local_engine(target).restart(cid)

    async def remove(self, target: Target, cid: str) -> None:
        await self._local_engine(target).remove(cid)

    async def prune_dangling_images(self, target: Target) -> int:
        return await self._local_engine(target).prune_dangling_images()

    async def _limactl(self, verb: str, target: Target) -> None:
        ensure_writable(target)
        if target.kind not in (TargetKind.LIMA, TargetKind.COLIMA):
            raise ValueError(f"{target.name} is not a managed VM")
        argv = (["colima", verb, "--profile", target.name] if target.kind is TargetKind.COLIMA
                else ["limactl", verb, target.name])
        res: RunResult = await self._runner(argv, 600.0)
        if res.returncode != 0:
            text = (res.stderr or res.stdout).decode(errors="replace").strip()
            raise lima.LimaError(text.splitlines()[-1] if text else f"limactl {verb} exited {res.returncode}")
        self._guest.forget(target.name)  # counters restart with the VM

    async def vm_start(self, target: Target) -> None:
        await self._limactl("start", target)

    async def vm_stop(self, target: Target) -> None:
        await self._limactl("stop", target)

    def exec_argv(self, target: Target, cid: str) -> list[str] | None:
        ensure_writable(target)
        sock = target.socket_path
        if not sock:
            return None
        return ["docker", "--host", f"unix://{sock}", "exec", "-it", cid, "sh", "-c", EXEC_SHELL]


def parse_colima(text: str) -> list[Target]:
    """Colima's JSONL includes stopped profiles; lifecycle stays with Colima."""
    root = os.environ.get("COLIMA_HOME") or os.path.expanduser("~/.colima")
    targets = []
    for line in text.splitlines():
        if not line.strip():
            continue
        data = loads_as(line, ColimaLine)
        name = data.get("name", "")
        if not name or name.startswith("-") or "/" in name or name in (".", ".."):
            continue
        # containerd / incus profiles do not expose a Docker Engine.
        if not data.get("runtime", "").startswith("docker"):
            continue
        targets.append(Target(f"colima:{name}", TargetKind.COLIMA, name, False,
                              f"unix://{root}/{name}/docker.sock",
                              VMInfo(data.get("status", ""), "", data.get("arch", ""),
                                     data.get("cpus", 0), data.get("memory", 0), data.get("disk", 0))))
    return targets


def host_socket() -> str | None:
    dh = os.environ.get("DOCKER_HOST", "")
    if dh:
        return dh.removeprefix("unix://") if dh.startswith("unix://") else None
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    candidates: list[str] = [os.path.join(runtime, "docker.sock")] if runtime else []
    for candidate in [*candidates, "/var/run/docker.sock"]:
        if os.path.exists(candidate):
            return candidate
    return None


from runtop.data.demo import FixtureBackend, find_demo_snapshot, jitter_stats  # noqa: E402  re-export

__all__ = ["DetailBackend", "DiscoveryError", "ExecBackend", "FixtureBackend", "HistoryBackend", "InspectBackend",
           "LiveBackend", "LogsBackend", "MemHistoryBackend", "MutableBackend", "ReadBackend", "StorageBackend",
           "StreamingBackend", "VitalsBackend", "find_demo_snapshot", "host_socket", "jitter_stats"]
