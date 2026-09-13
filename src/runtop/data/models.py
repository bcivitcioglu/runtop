"""Frozen data model shared by backends, store and widgets.

Field names and JSON shapes follow ``runtop.snapshot/v1`` in ``spec/SPEC.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum

from runtop.data.wire import (
    ContainerDict,
    ImageDict,
    SnapshotDocument,
    StatsDict,
    TargetSnapshotDict,
    VMDict,
)

SCHEMA = "runtop.snapshot/v1"


class DaemonState(StrEnum):
    LOADING = "loading"
    OK = "ok"
    VM_STOPPED = "vm_stopped"
    NO_DOCKER_SOCKET = "no_docker_socket"
    UNREACHABLE = "unreachable"
    NO_TARGETS = "no_targets"


class TargetKind(StrEnum):
    LIMA = "lima"
    HOST = "host"
    COLIMA = "colima"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class Stats:
    cpu_percent: float | None
    mem_bytes: int
    mem_limit_bytes: int

    def to_dict(self) -> StatsDict:
        return {"cpu_percent": self.cpu_percent, "mem_bytes": self.mem_bytes,
                "mem_limit_bytes": self.mem_limit_bytes}

    @classmethod
    def from_dict(cls, d: StatsDict | None) -> Stats | None:
        if d is None:
            return None
        return cls(d.get("cpu_percent"), int(d.get("mem_bytes") or 0), int(d.get("mem_limit_bytes") or 0))


@dataclass(frozen=True, slots=True)
class Container:
    id: str
    name: str
    image: str
    state: str
    status: str
    health: str | None = None
    exit_code: int | None = None
    project: str = ""
    service: str = ""
    ports: tuple[str, ...] = ()
    stats: Stats | None = None
    image_id: str = field(default="", compare=False)

    @property
    def running(self) -> bool:
        return self.state == "running"

    def with_stats(self, stats: Stats | None) -> Container:
        return replace(self, stats=stats)

    def to_dict(self, *, include_stats: bool = True) -> ContainerDict:
        d: ContainerDict = {
            "id": self.id, "name": self.name, "image": self.image, "state": self.state,
            "status": self.status, "health": self.health, "exit_code": self.exit_code,
            "project": self.project, "service": self.service, "ports": list(self.ports),
        }
        if include_stats:
            d["stats"] = self.stats.to_dict() if self.stats else None
        return d

    @classmethod
    def from_dict(cls, d: ContainerDict) -> Container:
        return cls(
            id=d["id"], name=d.get("name", ""), image=d.get("image", ""), state=d.get("state", ""),
            status=d.get("status", ""), health=d.get("health"), exit_code=d.get("exit_code"),
            project=d.get("project", ""), service=d.get("service", ""),
            ports=tuple(d.get("ports") or ()), stats=Stats.from_dict(d.get("stats")),
        )


@dataclass(frozen=True, slots=True)
class Image:
    id: str
    ref: str
    size_bytes: int
    containers: int | None
    dangling: bool

    def to_dict(self) -> ImageDict:
        return {"id": self.id, "ref": self.ref, "size_bytes": self.size_bytes,
                "containers": self.containers, "dangling": self.dangling}

    @classmethod
    def from_dict(cls, d: ImageDict) -> Image:
        return cls(d["id"], d.get("ref", "<none>:<none>"), int(d.get("size_bytes") or 0),
                   d.get("containers"), bool(d.get("dangling")))


@dataclass(frozen=True, slots=True)
class VMInfo:
    status: str
    vm_type: str
    arch: str
    cpus: int
    memory_bytes: int
    disk_bytes: int
    disk_used_bytes: int | None = None

    @property
    def running(self) -> bool:
        return self.status == "Running"

    def to_dict(self) -> VMDict:
        return {"status": self.status, "vm_type": self.vm_type, "arch": self.arch, "cpus": self.cpus,
                "memory_bytes": self.memory_bytes, "disk_bytes": self.disk_bytes,
                "disk_used_bytes": self.disk_used_bytes}

    @classmethod
    def from_dict(cls, d: VMDict | None) -> VMInfo | None:
        if d is None:
            return None
        return cls(d.get("status", ""), d.get("vm_type", ""), d.get("arch", ""), int(d.get("cpus") or 0),
                   int(d.get("memory_bytes") or 0), int(d.get("disk_bytes") or 0), d.get("disk_used_bytes"))


@dataclass(frozen=True, slots=True)
class Target:
    """Something runtop can show: a Lima VM, the host socket, or a docker context."""

    key: str
    kind: TargetKind
    name: str
    read_only: bool
    endpoint: str
    vm: VMInfo | None = None
    dir: str | None = field(default=None, compare=False)  # lima instance dir (not serialized)

    @property
    def is_remote(self) -> bool:
        return self.kind is TargetKind.CONTEXT

    @property
    def socket_path(self) -> str | None:
        return self.endpoint.removeprefix("unix://") if self.endpoint.startswith("unix://") else None


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    target: Target
    state: DaemonState
    error: str | None = None
    containers: tuple[Container, ...] = ()
    images: tuple[Image, ...] = ()
    fetched_at: float = field(default=0.0, compare=False)
    stats_sampled: bool = True  # internal: only fresh samples extend history
    images_loaded: bool = True  # False while images are loading or unavailable
    images_error: str | None = None
    stale: bool = False  # retained last successful snapshot; actions are disabled

    @property
    def key(self) -> str:
        return self.target.key

    def container(self, cid: str) -> Container | None:
        return next((c for c in self.containers if c.id == cid), None)

    def to_dict(self) -> TargetSnapshotDict:
        t = self.target
        ok = self.state is DaemonState.OK
        result: TargetSnapshotDict = {
            "key": t.key, "kind": str(t.kind), "name": t.name, "read_only": t.read_only,
            "endpoint": t.endpoint, "state": str(self.state), "error": self.error,
            "vm": t.vm.to_dict() if t.vm else None,
            "containers": [c.to_dict() for c in self.containers] if ok else [],
            "images": [i.to_dict() for i in self.images] if ok else [],
        }
        if self.images_error:
            result["images_error"] = self.images_error
        if self.stale:
            result["stale"] = True
        return result

    @classmethod
    def from_dict(cls, d: TargetSnapshotDict) -> TargetSnapshot:
        target = Target(
            key=d["key"], kind=TargetKind(d["kind"]), name=d.get("name", d["key"]),
            read_only=bool(d.get("read_only")) or d["kind"] == TargetKind.CONTEXT,
            endpoint=d.get("endpoint", ""), vm=VMInfo.from_dict(d.get("vm")),
        )
        return cls(
            target=target, state=DaemonState(d.get("state", "loading")), error=d.get("error"),
            containers=tuple(Container.from_dict(c) for c in d.get("containers") or ()),
            images=tuple(Image.from_dict(i) for i in d.get("images") or ()),
            images_error=d.get("images_error"), images_loaded=not bool(d.get("images_error")),
            stale=d.get("stale", False),
        )


def snapshot_document(snaps: list[TargetSnapshot], generated_at: str) -> SnapshotDocument:
    return {"schema": SCHEMA, "generated_at": generated_at, "targets": [s.to_dict() for s in snaps]}


def dumps_document(snaps: list[TargetSnapshot], generated_at: str) -> str:
    return json.dumps(snapshot_document(snaps, generated_at), indent=2) + "\n"


def load_document(data: SnapshotDocument) -> list[TargetSnapshot]:
    schema = data.get("schema")
    if schema != SCHEMA:
        raise ValueError(f"unsupported snapshot schema {schema!r} (want {SCHEMA})")
    return [TargetSnapshot.from_dict(t) for t in data.get("targets") or ()]
