from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runtop.cli import dump
from runtop.data.backend import FixtureBackend, LiveBackend, MutableBackend, ReadBackend, jitter_stats
from runtop.data.models import DaemonState, Stats, Target, TargetKind, VMInfo
from runtop.data.remote import RunResult
from runtop.data.wire import SnapshotDocument

from .conftest import text


async def test_fixture_backend_serves_demo_and_jitters_deterministically() -> None:
    a, b = FixtureBackend(), FixtureBackend()
    assert isinstance(a, ReadBackend) and isinstance(a, MutableBackend)
    targets = await a.discover()
    assert [t.key for t in targets][:2] == ["lima:docker", "lima:ci"]
    docker = targets[0]
    r1 = [c.stats for c in (await a.fetch(docker)).containers]
    r2 = [c.stats for c in (await a.fetch(docker)).containers]
    assert r1 != r2  # sparklines move
    assert [c.stats for c in (await b.fetch(docker)).containers] == r1  # but deterministically
    cpus = [s.cpu_percent for s in r1 if s and s.cpu_percent is not None]
    assert all(c > 0 for c in cpus)


def test_jitter_stays_near_base() -> None:
    base = Stats(10.0, 100_000_000, 4 << 30)
    vals = [jitter_stats("abc", base, r) for r in range(200)]
    assert all(v.cpu_percent is not None and 0 < v.cpu_percent < 20 for v in vals)
    assert all(95_000_000 < v.mem_bytes < 105_000_000 for v in vals)
    assert FixtureBackend(jitter=False).jitter is False


async def test_fixture_history_is_populated() -> None:
    fb = FixtureBackend()
    hist = fb.history("lima:docker", "a1f3c9e2b7d4", 30)
    assert len(hist) == 30 and all(h > 0 for h in hist)
    assert fb.history("lima:docker", "d8b1f4c7e2a9", 30) == []  # exited: no stats


def lima_target(tmp_path: Path, status: str = "Running", sock: bool = True) -> Target:
    d = tmp_path / "vm"
    (d / "sock").mkdir(parents=True)
    if sock:
        (d / "sock" / "docker.sock").write_text("")
    return Target("lima:vm", TargetKind.LIMA, "vm", False, f"unix://{d}/sock/docker.sock",
                  VMInfo(status, "vz", "aarch64", 2, 1, 1), dir=str(d))


async def test_live_backend_vm_states_without_daemons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_du(path: str, timeout: float = 20.0) -> int:
        return 4096

    monkeypatch.setattr("runtop.data.lima.du_bytes", no_du)
    lb = LiveBackend()
    stopped = await lb.fetch(lima_target(tmp_path, status="Stopped"))
    assert stopped.state is DaemonState.VM_STOPPED and stopped.containers == ()
    nosock = await lb.fetch(lima_target(tmp_path / "b", sock=False), with_disk=True)
    assert nosock.state is DaemonState.NO_DOCKER_SOCKET
    assert nosock.target.vm is not None and nosock.target.vm.disk_used_bytes == 4096
    dead = await lb.fetch(lima_target(tmp_path / "c"))  # a plain file is not a socket
    assert dead.state is DaemonState.UNREACHABLE and dead.error


async def test_live_backend_remote_via_runner() -> None:
    calls: list[list[str]] = []

    async def runner(argv: list[str], timeout: float) -> RunResult:
        calls.append(argv)
        if argv[3] == "ps":
            return RunResult(0, text("cli/docker_ps.jsonl").encode(), b"")
        return RunResult(0, text("cli/docker_images.jsonl").encode(), b"")

    lb = LiveBackend(runner)
    t = Target("ctx:prod-eu", TargetKind.CONTEXT, "prod-eu", True, "ssh://x")
    snap = await lb.fetch(t)
    assert snap.state is DaemonState.OK and len(snap.containers) == 5 and len(snap.images) == 5
    assert all(c.stats is None for c in snap.containers)
    assert {a[3] for a in calls} == {"ps", "images"}

    async def failing(argv: list[str], timeout: float) -> RunResult:
        return RunResult(255, b"", b"ssh: connect to host staging port 22: Operation timed out\n")

    bad = await LiveBackend(failing).fetch(t)
    assert bad.state is DaemonState.UNREACHABLE and bad.error is not None and "timed out" in bad.error


async def test_dump_demo(capsys: pytest.CaptureFixture[str], demo_doc: SnapshotDocument) -> None:
    assert await dump("ctx:prod-eu", demo=True, snapshot=None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "runtop.snapshot/v1"
    assert out["targets"] == [t for t in demo_doc["targets"] if t["key"] == "ctx:prod-eu"]
    assert await dump("lima:nope", demo=True, snapshot=None) == 2
    assert "unknown target" in capsys.readouterr().err


def test_env_isolated() -> None:
    assert "RUNTOP_LIVE" not in os.environ or os.environ["RUNTOP_LIVE"]


def test_host_socket_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtop.data.backend import host_socket

    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert host_socket() == str(sock)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{sock}")
    assert host_socket() == str(sock)
