"""On-demand Docker disk accounting. Never fetched by the monitoring heartbeat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class UsageData(TypedDict, total=False):
    Size: int
    RefCount: int


class DiskItem(TypedDict, total=False):
    Id: str
    Name: str
    Size: int
    SizeRw: int
    InUse: bool
    UsageData: UsageData | None


class DiskUsage(TypedDict, total=False):
    LayersSize: int
    Images: list[DiskItem] | None
    Containers: list[DiskItem] | None
    Volumes: list[DiskItem] | None
    BuildCache: list[DiskItem] | None


@dataclass(frozen=True)
class StorageRow:
    category: str
    count: int
    size_bytes: int | None
    note: str


def summarize(data: DiskUsage) -> list[StorageRow]:
    images = data.get("Images") or []
    containers = data.get("Containers") or []
    volumes = data.get("Volumes") or []
    cache = data.get("BuildCache") or []
    rw = [c.get("SizeRw", -1) for c in containers]
    sizes = [(v.get("UsageData") or {}).get("Size", -1) for v in volumes]
    cache_sizes = [c.get("Size", -1) for c in cache]
    layers = data.get("LayersSize", -1)
    return [
        StorageRow("Image layers", len(images), layers if layers >= 0 else None, "shared layers counted once"),
        StorageRow("Containers", len(containers), sum(rw) if all(n >= 0 for n in rw) else None,
                   "writable layers; excludes logs and mounts"),
        StorageRow("Volumes", len(volumes), sum(sizes) if all(n >= 0 for n in sizes) else None,
                   "persistent application data; never auto-deleted"),
        StorageRow("Build cache", len(cache), sum(cache_sizes) if all(n >= 0 for n in cache_sizes) else None,
                   "logical record sizes; may share data"),
    ]
