"""Typed JSON shapes: the ``runtop.snapshot/v1`` document plus the Engine API / docker CLI /
``limactl`` payloads runtop reads.

Wire payloads are ``total=False``: daemons omit fields freely, so readers use ``.get`` with a
default. The snapshot document is what runtop itself writes, so every key is present.
"""

from __future__ import annotations

import json
from typing import NotRequired, TypedDict, TypeVar, cast

T = TypeVar("T")


def loads_as(raw: str | bytes, shape: type[T]) -> T:
    """``json.loads`` typed as the documented ``shape`` (not validated: daemons are trusted to follow
    their API; readers still ``.get`` every optional field). ``shape`` is only read by the type checker.
    Raises ``json.JSONDecodeError``."""
    # cast: json.loads is typed ``Any``; this is the one place wire JSON gets its static shape.
    return cast(T, json.loads(raw))


# ---------------------------------------------------------------- runtop.snapshot/v1


class StatsDict(TypedDict):
    cpu_percent: float | None
    mem_bytes: int
    mem_limit_bytes: int


class _ContainerBase(TypedDict):
    id: str
    name: str
    image: str
    state: str
    status: str
    health: str | None
    exit_code: int | None
    project: str
    service: str
    ports: list[str]


class ContainerDict(_ContainerBase, total=False):
    stats: StatsDict | None  # omitted by ``Container.to_dict(include_stats=False)``


class ImageDict(TypedDict):
    id: str
    ref: str
    size_bytes: int
    containers: int | None
    dangling: bool


class VMDict(TypedDict):
    status: str
    vm_type: str
    arch: str
    cpus: int
    memory_bytes: int
    disk_bytes: int
    disk_used_bytes: int | None


class TargetSnapshotDict(TypedDict):
    key: str
    kind: str
    name: str
    read_only: bool
    endpoint: str
    state: str
    error: str | None
    vm: VMDict | None
    containers: list[ContainerDict]
    images: list[ImageDict]
    images_error: NotRequired[str]
    stale: NotRequired[bool]


class _SnapshotDocumentBase(TypedDict):
    schema: str
    targets: list[TargetSnapshotDict]


class SnapshotDocument(_SnapshotDocumentBase, total=False):
    generated_at: str  # always written; readers fall back when a hand-made document omits it


# ---------------------------------------------------------------- Docker Engine API


class EnginePort(TypedDict, total=False):
    IP: str
    PrivatePort: int
    PublicPort: int
    Type: str


class EngineContainer(TypedDict, total=False):
    """One entry of ``GET /containers/json``."""

    Id: str
    Names: list[str]
    Image: str
    ImageID: str
    State: str
    Status: str
    Labels: dict[str, str] | None
    Ports: list[EnginePort] | None


class EngineImage(TypedDict, total=False):
    """One entry of ``GET /images/json``."""

    Id: str
    RepoTags: list[str] | None
    RepoDigests: list[str] | None
    Size: int
    Containers: int


class EngineVersion(TypedDict, total=False):
    ApiVersion: str
    Version: str
    Os: str
    Arch: str


class CpuUsage(TypedDict, total=False):
    total_usage: int
    percpu_usage: list[int] | None


class CpuStats(TypedDict, total=False):
    cpu_usage: CpuUsage
    system_cpu_usage: int
    online_cpus: int


class MemoryStats(TypedDict, total=False):
    usage: int
    limit: int
    stats: dict[str, int]


class StatsSample(TypedDict, total=False):
    """``GET /containers/{id}/stats?stream=false``."""

    cpu_stats: CpuStats
    precpu_stats: CpuStats
    memory_stats: MemoryStats


class HealthCheck(TypedDict, total=False):
    ExitCode: int
    Output: str


class HealthState(TypedDict, total=False):
    Status: str
    FailingStreak: int
    Log: list[HealthCheck]


class InspectState(TypedDict, total=False):
    OOMKilled: bool
    Error: str
    Health: HealthState
    Status: str
    ExitCode: int
    StartedAt: str


class InspectConfig(TypedDict, total=False):
    Image: str
    Tty: bool
    Env: list[str] | None
    Cmd: list[str] | None
    WorkingDir: str
    Labels: dict[str, str] | None


class RestartPolicy(TypedDict, total=False):
    Name: str


class InspectHostConfig(TypedDict, total=False):
    RestartPolicy: RestartPolicy


class InspectMount(TypedDict, total=False):
    Type: str
    Name: str
    Source: str
    Destination: str


class EndpointSettings(TypedDict, total=False):
    IPAddress: str


class InspectNetworkSettings(TypedDict, total=False):
    Networks: dict[str, EndpointSettings | None] | None


class ContainerInspect(TypedDict, total=False):
    """``GET /containers/{id}/json`` (and ``docker inspect``), limited to the fields runtop shows."""

    Id: str
    RestartCount: int
    Created: str
    Path: str
    Args: list[str]
    State: InspectState
    Config: InspectConfig | None
    HostConfig: InspectHostConfig | None
    Mounts: list[InspectMount] | None
    NetworkSettings: InspectNetworkSettings | None


class PruneReport(TypedDict, total=False):
    SpaceReclaimed: int


class EngineErrorBody(TypedDict, total=False):
    message: str


# ---------------------------------------------------------------- docker CLI (``--format json``)


class PsLine(TypedDict, total=False):
    ID: str
    Names: str
    Image: str
    State: str
    Status: str
    Labels: str | None
    Ports: str | None


class ImagesLine(TypedDict, total=False):
    ID: str
    Repository: str
    Tag: str
    Size: str
    Containers: str | int


class ColimaLine(TypedDict, total=False):
    name: str
    status: str
    arch: str
    cpus: int
    memory: int
    disk: int
    runtime: str


class ContextLine(TypedDict, total=False):
    Name: str
    DockerEndpoint: str


# ---------------------------------------------------------------- limactl list --format json


class LimaInstanceLine(TypedDict, total=False):
    name: str
    status: str
    dir: str
    vmType: str
    arch: str
    cpus: int
    memory: int
    disk: int
