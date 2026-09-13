"""In-memory app state: targets, per-target snapshot cache, stats history."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from enum import StrEnum

from runtop.data.models import DaemonState, Target, TargetKind, TargetSnapshot

HISTORY_LEN = 120


class Section(StrEnum):
    CONTAINERS = "containers"
    IMAGES = "images"


class Store:
    def __init__(self) -> None:
        self.targets: list[Target] = []
        self.targets_loaded = False
        self.discover_error: str | None = None
        self.snapshots: dict[str, TargetSnapshot] = {}
        self.cpu_hist: dict[tuple[str, str], deque[float]] = {}
        self.mem_hist: dict[tuple[str, str], deque[int]] = {}
        self.selected: str | None = None
        self.fetching: set[str] = set()
        self.seeded: set[str] = set()
        # in-flight actions: target key or "<target key>/<container id>" → verb ("stopping", "starting"…)
        self.pending: dict[str, str] = {}

    # -- targets

    def target(self, key: str | None) -> Target | None:
        return next((t for t in self.targets if t.key == key), None) if key else None

    def set_targets(self, targets: list[Target], preferred: str | None = None) -> bool:
        """Replace the target list. Returns True when the selection changed."""
        old_endpoints = {t.key: t.endpoint for t in self.targets}
        self.targets = targets
        self.targets_loaded = True
        self.discover_error = None
        keys = {t.key for t in targets}
        for key in list(self.snapshots):
            current = self.target(key)
            if key not in keys or (current is not None and old_endpoints.get(key) != current.endpoint):
                del self.snapshots[key]
                for hist in (self.cpu_hist, self.mem_hist):
                    for item in list(hist):
                        if item[0] == key:
                            del hist[item]
        self.seeded.intersection_update(keys)
        self.fetching.intersection_update(keys)
        for hist in (self.cpu_hist, self.mem_hist):
            for item in list(hist):
                if item[0] not in keys:
                    del hist[item]
        before = self.selected
        if self.selected not in keys:
            self.selected = None
            if preferred in keys:
                self.selected = preferred
            elif targets:
                self.selected = self.default_target().key
        return self.selected != before

    def default_target(self) -> Target:
        """First running Lima VM named ``docker``, else first running VM, else the first target."""
        running = [t for t in self.targets if t.vm is not None and t.vm.running]
        for t in running:
            if t.name == "docker":
                return t
        return running[0] if running else self.targets[0]

    def fail_discovery(self, error: str) -> None:
        self.targets_loaded = True
        self.discover_error = error

    # -- snapshots

    def apply(self, snap: TargetSnapshot) -> None:
        key = snap.key
        cached = self.snapshots.get(key)
        if snap.state is DaemonState.UNREACHABLE and cached is not None and cached.state is DaemonState.OK:
            snap = replace(cached, error=snap.error, stale=True)
        if snap.images_error and cached is not None and cached.state is DaemonState.OK:
            snap = replace(snap, images=cached.images)
        if not snap.stats_sampled and cached is not None and cached.state is DaemonState.OK:
            previous = {c.id: c.stats for c in cached.containers if c.running}
            snap = replace(snap, containers=tuple(c.with_stats(previous.get(c.id)) if c.running else c
                                                   for c in snap.containers))
        self.snapshots[key] = snap
        # Keep the freshest VM facts (e.g. disk usage) on the target list too.
        for i, t in enumerate(self.targets):
            if t.key == key and snap.target.vm is not None and t.vm is not None:
                self.targets[i] = replace(t, vm=replace(t.vm, disk_used_bytes=snap.target.vm.disk_used_bytes))
        if snap.stale:
            return
        live = set()
        for c in snap.containers:
            live.add(c.id)
            if c.stats is None or not snap.stats_sampled:
                continue
            if c.stats.cpu_percent is not None:
                self.cpu_hist.setdefault((key, c.id), deque(maxlen=HISTORY_LEN)).append(c.stats.cpu_percent)
            self.mem_hist.setdefault((key, c.id), deque(maxlen=HISTORY_LEN)).append(c.stats.mem_bytes)
        if snap.state is DaemonState.OK:
            for hist in (self.cpu_hist, self.mem_hist):
                for k in [k for k in hist if k[0] == key and k[1] not in live]:
                    del hist[k]

    def pending_for(self, key: str, cid: str | None = None) -> str | None:
        return self.pending.get(f"{key}/{cid}" if cid else key)

    def seed_mem(self, key: str, cid: str, values: list[int]) -> None:
        dq = self.mem_hist.setdefault((key, cid), deque(maxlen=HISTORY_LEN))
        existing = list(dq)
        dq.clear()
        dq.extend([*values, *existing])

    def mem_history(self, key: str, cid: str) -> tuple[int, ...]:
        return tuple(self.mem_hist.get((key, cid), ()))

    def seed_cpu(self, key: str, cid: str, values: list[float]) -> None:
        dq = self.cpu_hist.setdefault((key, cid), deque(maxlen=HISTORY_LEN))
        existing = list(dq)
        dq.clear()
        dq.extend([*values, *existing])

    def snapshot(self, key: str | None) -> TargetSnapshot | None:
        """Cached snapshot, corrected by the latest target facts.

        A VM that ``limactl list`` now reports as stopped shows as stopped immediately,
        even if the cached snapshot predates it.
        """
        if key is None:
            return None
        snap = self.snapshots.get(key)
        target = self.target(key)
        if (target is not None and target.kind in (TargetKind.LIMA, TargetKind.COLIMA)
                and target.vm is not None and not target.vm.running):
            return TargetSnapshot(target, DaemonState.VM_STOPPED, fetched_at=snap.fetched_at if snap else 0.0)
        if snap is not None and target is not None and snap.target.vm != target.vm and target.vm is not None:
            snap = replace(snap, target=target)
        return snap

    def state(self, key: str | None) -> DaemonState:
        snap = self.snapshot(key)
        return snap.state if snap else DaemonState.LOADING

    def cpu_history(self, key: str, cid: str) -> tuple[float, ...]:
        return tuple(self.cpu_hist.get((key, cid), ()))
