from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from textual.app import App, ComposeResult
from typing_extensions import override

from runtop.config import Config, load
from runtop.data.backend import FixtureBackend, LiveBackend, parse_colima
from runtop.data.engine import EngineClient, LogLine
from runtop.data.lima import Instance
from runtop.data.models import Container, DaemonState, Target, TargetKind, TargetSnapshot
from runtop.data.remote import RunResult
from runtop.data.storage import DiskUsage, summarize
from runtop.data.wire import loads_as
from runtop.screens.confirm import ConfirmScreen
from runtop.screens.project_logs import project_lines
from runtop.screens.storage import StorageScreen
from runtop.state.actions import BY_ID, Disabled, Subject
from runtop.state.store import Store
from runtop.widgets.log_view import LogView

from .helpers import main_screen, make_app, screen_text, tree, wait_for


@pytest.mark.parametrize("raw", ["[]", "null", "true", "42", '"hello"', "{"])
def test_invalid_config_shape_uses_defaults(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(raw)
    assert load(path) == Config()


def test_config_widths_are_bounded(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"sidebar_width":-300,"detail_width":90000}')
    assert (load(path).sidebar_width, load(path).detail_width) == (18, 90)


class LogApp(App[None]):
    @override
    def compose(self) -> ComposeResult:
        yield LogView()


async def test_quiet_log_burst_flushes_without_another_line_and_closes_source() -> None:
    closed = asyncio.Event()

    async def source() -> AsyncIterator[LogLine]:
        try:
            yield LogLine("stdout", "first")
            yield LogLine("stderr", "last of burst")
            await asyncio.Event().wait()
        finally:
            closed.set()

    app = LogApp()
    async with app.run_test() as pilot:
        view = app.query_one(LogView)
        view.show("quiet", source)
        assert await wait_for(pilot, lambda: view.log_widget.line_count == 2)
        view.stop()
        await asyncio.wait_for(closed.wait(), 1)


async def test_discovery_keeps_host_colima_and_remote_and_deduplicates_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLIMA_HOME", str(tmp_path))
    monkeypatch.setattr("runtop.data.backend.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("runtop.data.backend.lima.available", lambda: True)
    inst = Instance("vm", "Stopped", "/fake/vm", "vz", "aarch64", 2, 1024, 4096)
    monkeypatch.setattr("runtop.data.backend.lima.list_instances", AsyncMock(return_value=[inst]))
    monkeypatch.setattr("runtop.data.backend.host_socket", lambda: "/fake/host.sock")

    async def runner(argv: list[str], timeout: float) -> RunResult:
        if argv[0] == "colima":
            return RunResult(0, b'{"name":"default","status":"Stopped","runtime":"docker"}', b"")
        return RunResult(0, (json.dumps({"Name": "colima", "DockerEndpoint": f"unix://{tmp_path}/default/docker.sock"})
                             + '\n{"Name":"prod","DockerEndpoint":"ssh://prod"}').encode(), b"")

    backend = LiveBackend(runner)
    targets = await backend.discover()
    assert [t.key for t in targets] == ["lima:vm", "colima:default", "host", "ctx:prod"]
    assert targets[-1].read_only


async def test_colima_actions_use_profile_and_backend_refuses_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLIMA_HOME", "/profiles")
    target = parse_colima('{"name":"work","status":"Stopped","runtime":"docker"}')[0]
    calls = []

    async def runner(argv: list[str], timeout: float) -> RunResult:
        calls.append(argv)
        return RunResult(0, b"", b"")

    backend = LiveBackend(runner)
    await backend.vm_start(target)
    assert calls == [["colima", "start", "--profile", "work"]]
    with pytest.raises(PermissionError):
        await backend.vm_stop(replace(target, read_only=True))
    assert len(calls) == 1
    assert parse_colima('{"name":"other","runtime":"containerd"}') == []


async def test_image_failure_retains_containers_and_marks_partial() -> None:
    async def runner(argv: list[str], timeout: float) -> RunResult:
        if argv[3] == "ps":
            return RunResult(0, b'{"ID":"123","Names":"api","State":"running"}', b"")
        await asyncio.sleep(0.01)
        return RunResult(1, b"", b"images denied")

    target = Target("ctx:prod", TargetKind.CONTEXT, "prod", True, "ssh://prod")
    snap = await LiveBackend(runner).fetch(target)
    assert snap.state is DaemonState.OK and len(snap.containers) == 1
    assert snap.images_error == "images denied" and not snap.images_loaded
    assert snap.to_dict()["images_error"] == "images denied"


def test_stale_cache_does_not_append_history_or_allow_actions() -> None:
    target = Target("host", TargetKind.HOST, "host", False, "unix:///socket")
    c = Container("abc", "api", "image", "running", "Up")
    store = Store()
    store.set_targets([target])
    store.apply(TargetSnapshot(target, DaemonState.OK, containers=(c,), fetched_at=10))
    store.apply(TargetSnapshot(target, DaemonState.UNREACHABLE, error="offline", fetched_at=20))
    snap = store.snapshot(target.key)
    assert snap is not None and snap.stale and snap.containers == (c,) and snap.fetched_at == 10
    assert isinstance(BY_ID["container.stop"].available(Subject(target=target, snapshot=snap, container=c)), Disabled)
    store.apply(TargetSnapshot(target, DaemonState.OK, containers=(c,), fetched_at=30))
    assert not store.snapshots[target.key].stale


async def test_stats_round_deadline_includes_queued_containers() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/version":
            return httpx.Response(200, json={"ApiVersion": "1.51"})
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    async with EngineClient(transport=httpx.MockTransport(handler)) as eng:
        eng.STATS_TIMEOUT = 0.03
        containers = [Container(str(i), "test", "image", "running", "Up") for i in range(32)]
        result = await asyncio.wait_for(eng.fill_stats(containers, {}), 0.3)
        assert len(result) == 32 and all(c.stats is None for c in result)


def test_shared_storage_accounting_fixture() -> None:
    data = loads_as(Path("spec/fixtures/engine/disk_usage.json").read_text(), DiskUsage)
    rows = summarize(data)
    assert [(r.category, r.count, r.size_bytes) for r in rows] == [
        ("Image layers", 2, 1000), ("Containers", 2, 30), ("Volumes", 2, None), ("Build cache", 2, 90)]


async def test_storage_inspection_is_explicit_and_closes_in_narrow_mode() -> None:
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        assert await wait_for(pilot, lambda: app.store.selected and app.store.snapshots)
        await pilot.press("D")
        assert await wait_for(pilot, lambda: isinstance(app.screen, StorageScreen) and "Volumes" in screen_text(app))
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: not isinstance(app.screen, StorageScreen))


async def test_project_action_confirms_bound_members() -> None:
    backend = FixtureBackend(jitter=False)
    app = make_app(backend)
    async with app.run_test(size=(160, 45)) as pilot:
        assert await wait_for(pilot, lambda: app.store.snapshots and tree(app).last_line > 0)
        tree(app).select_key("group:shop")
        tree(app).focus()
        await pilot.pause()
        main_screen(app).action_act("x")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert isinstance(app.screen, ConfirmScreen)
        assert "Stop shop" in app.screen.title_text
        await pilot.press("y")
        assert await wait_for(pilot, lambda: not app.store.pending and any(op[0] == "stop" for op in backend.calls))
        assert all(op[1] == "lima:docker" for op in backend.calls)


async def test_project_log_cancel_joins_all_readers() -> None:
    class Reader:
        def __init__(self) -> None:
            self.closed = 0

        async def logs(self, target: Target, cid: str, *, tail: int = 200,
                       follow: bool = False) -> AsyncIterator[LogLine]:
            try:
                yield LogLine("stdout", cid)
                await asyncio.Event().wait()
            finally:
                self.closed += 1

    reader = Reader()
    target = Target("host", TargetKind.HOST, "host", False, "unix:///socket")
    containers = tuple(Container(str(i), f"service-{i}", "img", "running", "Up") for i in range(2))
    stream = project_lines(reader, target, containers)
    lines = [await anext(stream), await anext(stream)]
    assert all(line.text.startswith("[service-") for line in lines)
    await stream.aclose()
    assert reader.closed == 2
