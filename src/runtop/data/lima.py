"""Lima instances via ``limactl list --format json`` (one JSON object per line)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass

from runtop.data.models import Target, TargetKind, VMInfo
from runtop.data.wire import LimaInstanceLine, loads_as


@dataclass(frozen=True, slots=True)
class Instance:
    name: str
    status: str
    dir: str
    vm_type: str
    arch: str
    cpus: int
    memory_bytes: int
    disk_bytes: int

    @property
    def socket_path(self) -> str:
        return os.path.join(self.dir, "sock", "docker.sock")

    @property
    def running(self) -> bool:
        return self.status == "Running"

    def to_target(self, disk_used_bytes: int | None = None) -> Target:
        return Target(
            key=f"lima:{self.name}", kind=TargetKind.LIMA, name=self.name, read_only=False,
            endpoint=f"unix://{self.socket_path}", dir=self.dir,
            vm=VMInfo(self.status, self.vm_type, self.arch, self.cpus, self.memory_bytes,
                      self.disk_bytes, disk_used_bytes),
        )


class LimaError(RuntimeError):
    pass


def parse_list(text: str) -> list[Instance]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        d = loads_as(line, LimaInstanceLine)
        out.append(Instance(
            name=d.get("name", ""), status=d.get("status", ""), dir=d.get("dir", ""),
            vm_type=d.get("vmType", ""), arch=d.get("arch", ""), cpus=int(d.get("cpus") or 0),
            memory_bytes=int(d.get("memory") or 0), disk_bytes=int(d.get("disk") or 0),
        ))
    return out


def available() -> bool:
    return shutil.which("limactl") is not None


async def list_instances(timeout: float = 10.0) -> list[Instance]:
    """Run ``limactl list``. Raises :class:`LimaError` (never hangs: bounded by ``timeout``)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "limactl", "list", "--format", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise LimaError("limactl not found (brew install lima)") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    except TimeoutError as e:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise LimaError("limactl list timed out") from e
    if proc.returncode != 0:
        raise LimaError(err.decode(errors="replace").strip() or f"limactl list exited {proc.returncode}")
    try:
        return parse_list(out.decode(errors="replace"))
    except (json.JSONDecodeError, ValueError) as e:
        raise LimaError(f"cannot parse limactl output: {e}") from e


class DiskUsage:
    """``du -sk <dir>`` × 1024, measured at most every ``interval`` seconds per dir."""

    def __init__(self, interval: float = 30.0) -> None:
        self.interval = interval
        self._cache: dict[str, tuple[float, int | None]] = {}

    def cached(self, path: str) -> int | None:
        hit = self._cache.get(path)
        return hit[1] if hit else None

    async def measure(self, path: str) -> int | None:
        now = time.monotonic()
        hit = self._cache.get(path)
        if hit and now - hit[0] < self.interval:
            return hit[1]
        value = await du_bytes(path)
        self._cache[path] = (now, value if value is not None else (hit[1] if hit else None))
        return self._cache[path][1]


def parse_du(text: str) -> int | None:
    fields = text.split()
    if not fields:
        return None
    try:
        return int(fields[0]) * 1024
    except ValueError:
        return None


async def du_bytes(path: str, timeout: float = 20.0) -> int | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "du", "-sk", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    except TimeoutError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        return None
    return parse_du(out.decode(errors="replace"))
