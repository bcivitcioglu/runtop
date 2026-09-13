from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from runtop.data.backend import LiveBackend
from runtop.data.engine import EngineClient
from runtop.data.models import Container, DaemonState, Stats, Target, TargetKind, TargetSnapshot
from runtop.state.store import Store


async def test_every_round_yields_containers_before_slow_images_and_stats(tmp_path: Path) -> None:
    sock = tmp_path / "socket"
    sock.touch()
    target = Target("host", TargetKind.HOST, "host", False, f"unix://{sock}")
    images, stats = asyncio.Event(), asyncio.Event()

    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/_ping":
            return httpx.Response(200, text="OK")
        if req.url.path == "/version":
            return httpx.Response(200, json={"ApiVersion": "1.51"})
        if req.url.path.endswith("/containers/json"):
            return httpx.Response(200, json=[{"Id": "abc", "Names": ["/api"], "State": "running"}])
        if req.url.path.endswith("/images/json"):
            await images.wait()
            return httpx.Response(200, json=[])
        await stats.wait()
        return httpx.Response(200, json={})

    backend = LiveBackend(include_contexts=False)
    backend._engines[str(sock)] = EngineClient(transport=httpx.MockTransport(handler))
    try:
        for _ in range(2):
            images.clear()
            stats.clear()
            stream = backend.fetch_stream(target)
            first = await asyncio.wait_for(anext(stream), 0.5)
            assert first.containers[0].name == "api" and not first.images_loaded and not first.stats_sampled
            stats.set()
            second = await asyncio.wait_for(anext(stream), 0.5)
            assert second.stats_sampled and not second.images_loaded
            images.set()
            third = await asyncio.wait_for(anext(stream), 0.5)
            assert third.images_loaded and not third.stats_sampled
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
    finally:
        await backend.aclose()


def test_partial_updates_preserve_stats_without_duplicating_history() -> None:
    t = Target("host", TargetKind.HOST, "host", False, "unix:///socket")
    c = Container("abc", "api", "image", "running", "Up", stats=Stats(20, 100, 1000))
    store = Store()
    store.set_targets([t])
    snap = TargetSnapshot(t, DaemonState.OK, containers=(c,))
    store.apply(snap)
    store.apply(replace(snap, containers=(c.with_stats(None),), stats_sampled=False))
    assert store.snapshots[t.key].containers[0].stats == c.stats
    assert list(store.cpu_hist[(t.key, c.id)]) == [20]
    store.apply(replace(snap, containers=(replace(c, state="exited", stats=None),), stats_sampled=False))
    assert store.snapshots[t.key].containers[0].stats is None
