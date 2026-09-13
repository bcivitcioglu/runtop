"""Polling: one heartbeat, only the selected target is fetched.

* Overlapping rounds are skipped (an in-flight fetch is never duplicated).
* Selecting a target bumps ``generation``, cancels the fetch worker group and drops any
  result that still arrives for an older generation.
* Remote contexts poll slowly (each call is an ssh round trip); everything slows while the
  terminal is blurred.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from runtop.data.backend import HistoryBackend, MemHistoryBackend, ReadBackend, StreamingBackend
from runtop.data.models import DaemonState, TargetSnapshot
from runtop.state.store import Store

if TYPE_CHECKING:
    from textual.app import App
    from textual.worker import Worker

FETCH_GROUP = "runtop-fetch"
DISCOVER_GROUP = "runtop-discover"


class Poller:
    def __init__(self, app: App[None], store: Store, backend: ReadBackend, on_change: Callable[[str | None], None], *,
                 local_interval: float = 2.0, remote_interval: float = 15.0, discover_interval: float = 3.0,
                 blur_factor: float = 4.0, preferred: str | None = None) -> None:
        self.app = app
        self.store = store
        self.backend = backend
        self.on_change = on_change
        self.local_interval = local_interval
        self.remote_interval = remote_interval
        self.discover_interval = discover_interval
        self.blur_factor = blur_factor
        self.preferred = preferred
        self.generation = 0
        self.blurred = False
        self._fetch_worker: Worker[object] | None = None
        self._discover_worker: Worker[object] | None = None
        self._last_fetch: dict[str, float] = {}
        self._last_discover = 0.0
        self.dropped = 0  # stale results discarded (tests)

    # -- lifecycle

    def start(self, heartbeat: float = 0.25) -> None:
        self.discover_now()
        self.app.set_interval(heartbeat, self.tick, name="runtop-poll")

    def tick(self) -> None:
        now = time.monotonic()
        factor = self.blur_factor if self.blurred else 1.0
        if now - self._last_discover >= self.discover_interval * factor:
            self.discover_now()
        key = self.store.selected
        target = self.store.target(key)
        if key is None or target is None:
            return
        interval = (self.remote_interval if target.is_remote else self.local_interval) * factor
        if now - self._last_fetch.get(key, 0.0) >= interval:
            self.fetch_now()

    # -- discovery

    def discover_now(self) -> None:
        if not self.app.is_running or getattr(self.app, "_exit", False):
            return
        if self._discover_worker is not None and self._discover_worker.is_running:
            return
        self._last_discover = time.monotonic()
        self._discover_worker = self.app.run_worker(self._discover, group=DISCOVER_GROUP, exit_on_error=False)

    async def _discover(self) -> None:
        try:
            targets = await self.backend.discover()
        except Exception as e:  # never leave the UI on a loader
            self.store.fail_discovery(str(e) or type(e).__name__)
            self.on_change(None)
            return
        before_error = self.store.discover_error
        before = [(t.key, t.vm) for t in self.store.targets]
        first = not self.store.targets_loaded
        changed_selection = self.store.set_targets(targets, self.preferred if first else None)
        self.store.discover_error = getattr(self.backend, "discovery_error", None)
        if changed_selection:
            self.generation += 1
            self.fetch_now()
        if (first or changed_selection or before_error != self.store.discover_error
                or before != [(t.key, t.vm) for t in targets]):
            self.on_change(None)

    # -- fetching

    def select(self, key: str) -> None:
        """Switch target: cached data shows instantly, a fresh fetch starts now."""
        if key == self.store.selected or self.store.target(key) is None:
            return
        self.store.selected = key
        self.generation += 1
        self.app.workers.cancel_group(self.app, FETCH_GROUP)
        self._fetch_worker = None
        self.store.fetching.clear()
        self.on_change(key)
        self.fetch_now()

    def refresh(self) -> None:
        self.discover_now()
        self.fetch_now(force=True)

    def fetch_now(self, *, force: bool = False) -> None:
        key = self.store.selected
        target = self.store.target(key)
        if key is None or target is None or not self.app.is_running or getattr(self.app, "_exit", False):
            return
        if self._fetch_worker is not None and self._fetch_worker.is_running:
            if not force:
                return  # skip overlapping round
            self.app.workers.cancel_group(self.app, FETCH_GROUP)
        self._last_fetch[key] = time.monotonic()
        self.store.fetching.add(key)
        # pass a callable, not a coroutine: a worker cancelled before it starts never leaks one
        self._fetch_worker = self.app.run_worker(partial(self._fetch, key, self.generation), group=FETCH_GROUP,
                                                 exit_on_error=False)
        self.on_change(key)

    async def _fetch(self, key: str, generation: int) -> None:
        target = self.store.target(key)
        if target is None:
            return
        backend = self.backend
        try:
            if isinstance(backend, StreamingBackend):
                async for snap in backend.fetch_stream(target):
                    if not self._accept(key, generation, snap):
                        return
            else:
                self._accept(key, generation, await backend.fetch(target))
        except Exception as e:  # backend bugs become a visible state, not a crash
            self._accept(key, generation, TargetSnapshot(target, DaemonState.UNREACHABLE,
                                                         error=str(e) or type(e).__name__, fetched_at=time.time()))
        finally:
            if generation == self.generation:
                self.store.fetching.discard(key)
                self.on_change(key)

    def _accept(self, key: str, generation: int, snap: TargetSnapshot) -> bool:
        """Apply one (possibly partial) result. False when it is stale and was dropped."""
        target = self.store.target(key)
        if (generation != self.generation or key != self.store.selected or target is None
                or target.endpoint != snap.target.endpoint):
            self.dropped += 1
            return False
        if not snap.images_loaded and not snap.images_error:
            cached = self.store.snapshots.get(key)
            if cached is not None and cached.images_loaded and cached.state is DaemonState.OK:
                snap = replace(snap, images=cached.images, images_loaded=True)  # keep last images meanwhile
        self._last_fetch[key] = time.monotonic()
        backend = self.backend
        if key not in self.store.seeded and isinstance(backend, HistoryBackend):
            self.store.seeded.add(key)
            for c in snap.containers:
                if c.stats is not None:
                    self.store.seed_cpu(key, c.id, backend.history(key, c.id, 40))
                    if isinstance(backend, MemHistoryBackend):
                        self.store.seed_mem(key, c.id, backend.mem_history(key, c.id, 40))
        self.store.apply(snap)
        self.on_change(key)
        return True
