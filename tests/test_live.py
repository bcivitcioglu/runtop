"""Live smoke tests against the real Lima ``docker`` VM. ``RUNTOP_LIVE=1 uv run pytest -m live``."""

from __future__ import annotations

import os
import pathlib
from typing import Never

import pytest

from runtop.data.backend import LiveBackend
from runtop.data.engine import EngineClient
from runtop.data.models import DaemonState
from runtop.data.remote import ReadOnlyViolation, RemoteClient

SOCK = pathlib.Path.home() / ".lima" / "docker" / "sock" / "docker.sock"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUNTOP_LIVE") != "1" or not SOCK.exists(),
                       reason="set RUNTOP_LIVE=1 with ~/.lima/docker/sock/docker.sock present"),
]


async def test_live_engine_lists() -> None:
    async with EngineClient(str(SOCK)) as eng:
        assert await eng.ping()
        await eng.containers()
        imgs = await eng.images()
        assert eng.api_version
        assert all(i.size_bytes >= 0 for i in imgs)


async def test_live_backend_discovers_and_fetches_docker_vm() -> None:
    lb = LiveBackend(include_contexts=False)
    targets = await lb.discover()
    docker = next(t for t in targets if t.key == "lima:docker")
    snap = await lb.fetch(docker, one_shot=False)
    await lb.aclose()
    assert snap.state is DaemonState.OK
    for c in snap.containers:
        if c.running and c.stats:
            assert c.stats.mem_bytes > 0


async def test_live_mutating_verb_on_context_raises_without_spawning() -> None:
    spawned: list[list[str]] = []

    async def runner(argv: list[str], timeout: float) -> Never:
        spawned.append(argv)
        raise AssertionError("must not spawn")

    for verb in ("rm", "stop", "restart", "exec", "system"):
        with pytest.raises(ReadOnlyViolation):
            await RemoteClient("prod-context", runner)._run(verb, "x")
    assert spawned == []
