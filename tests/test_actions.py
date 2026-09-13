"""A4: action registry, confirm modal, toasts, pending state, VM ops, exec, read-only layers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Never, TypedDict, Unpack

import pytest
from textual.pilot import Pilot
from typing_extensions import override

from runtop.app import RuntopApp
from runtop.data.backend import FixtureBackend, LiveBackend
from runtop.data.models import Container, DaemonState, Image, Target, TargetKind, TargetSnapshot, VMInfo
from runtop.data.remote import ALLOWED_VERBS, ReadOnlyViolation, RunResult
from runtop.screens.confirm import ConfirmScreen
from runtop.state.actions import (
    BY_ID,
    READ_ONLY_REASON,
    REGISTRY,
    ActionSpec,
    Availability,
    Disabled,
    Subject,
    resolve,
)
from runtop.widgets.container_tree import ContainerTree
from runtop.widgets.detail import ActionBar, ActionButton, DetailPane

from .helpers import (
    container_named,
    demo_backend,
    main_screen,
    make_app,
    record_notes,
    screen_text,
    settle,
    snapshot,
    wait_for,
)

SIZE = (160, 45)
LIMA = Target("lima:docker", TargetKind.LIMA, "docker", False, "unix:///x/sock/docker.sock",
              VMInfo("Running", "vz", "aarch64", 4, 1, 1))
CTX = Target("ctx:prod", TargetKind.CONTEXT, "prod", True, "ssh://prod")
RUNNING = Container("aaaaaaaaaaaa", "web", "nginx", "running", "Up 1 hour")
EXITED = Container("bbbbbbbbbbbb", "job", "alpine", "exited", "Exited (0) 1 hour ago", exit_code=0)
DANGLING = Image("cccccccccccc", "<none>:<none>", 10, 0, True)


class SubjectOptions(TypedDict, total=False):
    images: tuple[Image, ...]
    focus: str
    demo: bool
    pending: Iterable[str]


def subject(target: Target = LIMA, container: Container | None = RUNNING, **kw: Unpack[SubjectOptions]) -> Subject:
    snap = TargetSnapshot(target, DaemonState.OK, containers=(RUNNING, EXITED), images=kw.get("images", ()))
    return Subject(target=target, snapshot=snap, container=container, focus=kw.get("focus", "middle"),
                   demo=kw.get("demo", False), pending=frozenset(kw.get("pending", ())))


def reason(avail: Availability) -> str | None:
    return avail.reason if isinstance(avail, Disabled) else None


def resolved(key: str, s: Subject) -> ActionSpec:
    spec = resolve(key, s)
    assert spec is not None
    return spec


# ---------------------------------------------------------------- registry matrix


def test_every_action_is_refused_on_read_only_targets() -> None:
    for spec in REGISTRY:
        for c in (RUNNING, EXITED):
            avail = spec.available(subject(CTX, c, images=(DANGLING,)))
            assert isinstance(avail, Disabled) and avail.reason in (READ_ONLY_REASON,), spec.id


@pytest.mark.parametrize(("action", "container", "kwargs", "want"), [
    ("container.start", RUNNING, {}, "already running"),
    ("container.start", EXITED, {}, None),
    ("container.stop", EXITED, {}, "not running"),
    ("container.stop", RUNNING, {}, None),
    ("container.restart", EXITED, {}, None),
    ("container.remove", None, {}, "select a container"),
    ("container.exec", EXITED, {}, "container is not running"),
    ("container.exec", RUNNING, {"demo": True}, "exec needs a real daemon (demo mode)"),
    ("container.exec", RUNNING, {}, None),
    ("container.stop", RUNNING, {"pending": ["lima:docker/aaaaaaaaaaaa"]},
     "an action is already running on this container"),
    ("images.prune", None, {}, "no dangling images"),
    ("images.prune", None, {"images": (DANGLING,)}, None),
    ("machine.start", None, {}, "already running"),
    ("machine.stop", None, {}, None),
    ("machine.stop", None, {"pending": ["lima:docker"]}, "docker is busy"),
])
def test_availability_matrix(action: str, container: Container | None, kwargs: SubjectOptions,
                             want: str | None) -> None:
    avail = BY_ID[action].available(subject(container=container, **kwargs))
    assert reason(avail) == want


def test_machine_actions_need_a_lima_vm() -> None:
    host = Target("host", TargetKind.HOST, "host", False, "unix:///var/run/docker.sock")
    assert reason(BY_ID["machine.stop"].available(subject(host))) == "not a Lima VM"


def test_keys_resolve_by_focus() -> None:
    assert resolved("x", subject(focus="middle")).id == "container.stop"
    assert resolved("x", subject(focus="sidebar")).id == "machine.stop"
    assert resolved("s", subject(focus="detail")).id == "container.start"
    assert resolved("p", subject(focus="sidebar")).id == "images.prune"
    assert resolve("z", subject()) is None
    assert resolved("x", subject(CTX, focus="sidebar")).id == "container.stop"  # contexts have no machine actions


def test_confirm_texts() -> None:
    confirm_remove, confirm_prune = BY_ID["container.remove"].confirm, BY_ID["images.prune"].confirm
    assert confirm_remove is not None and confirm_prune is not None
    title, body, label = confirm_remove(subject())
    assert title == "Remove web?" and "killed" in body and label == "Remove"
    title, body, _ = confirm_prune(subject(images=(DANGLING,)))
    assert "1 untagged listed" in body and "Shared layers" in body


# ---------------------------------------------------------------- in the app


async def ready(pilot: Pilot[None], app: RuntopApp) -> None:
    assert await wait_for(pilot, lambda: app.screen.query_one(ContainerTree).last_line > 0
                          and isinstance(app.focused, ContainerTree))


async def select(pilot: Pilot[None], app: RuntopApp, name: str) -> Container:
    c = container_named(app, name)
    assert app.screen.query_one(ContainerTree).select_key(f"c:{c.id}")
    await settle(pilot)
    return c


async def test_stop_key_shows_pending_then_toast_and_new_state() -> None:
    backend = demo_backend(action_latency=0.3)
    app = make_app(backend)
    notes = record_notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        web = await select(pilot, app, "shop-web-1")
        await pilot.press("x")
        assert await wait_for(pilot, lambda: "stopping…" in screen_text(app), timeout=1)
        assert await wait_for(pilot, lambda: container_named(app, "shop-web-1").state == "exited")
        assert ("stop", "lima:docker", web.id) in backend.calls
        assert ("Stopped shop-web-1", "docker", "information") in notes
        assert app.store.pending == {}
        await pilot.press("s")  # the same key row now offers Start
        assert await wait_for(pilot, lambda: container_named(app, "shop-web-1").state == "running")


async def test_action_binds_to_captured_target_not_current_selection() -> None:
    backend = demo_backend(action_latency=0.4)
    app = make_app(backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        api = await select(pilot, app, "shop-api-1")
        await pilot.press("R")
        app.poller.select("ctx:prod-eu")  # switch away while the restart is in flight
        assert await wait_for(pilot, lambda: ("restart", "lima:docker", api.id) in backend.calls)
        await pilot.pause(0.6)
        assert all(call[1] == "lima:docker" for call in backend.calls)


async def test_remove_confirm_n_cancels_y_removes() -> None:
    backend = demo_backend()
    app = make_app(backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        worker = await select(pilot, app, "shop-worker-1")
        await pilot.press("X")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "Remove shop-worker-1?" in screen_text(app)
        await pilot.press("n")
        assert await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
        assert backend.calls == []
        await pilot.press("X")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("enter")  # destructive dialogs focus Cancel: Enter is safe
        assert await wait_for(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
        assert backend.calls == []
        await pilot.press("X")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        assert await wait_for(pilot, lambda: ("remove", "lima:docker", worker.id) in backend.calls)
        assert await wait_for(pilot, lambda: all(c.id != worker.id for c in snapshot(app, "lima:docker").containers))


async def test_prune_confirms_and_reports_reclaimed_space() -> None:
    backend = demo_backend()
    app = make_app(backend)
    notes = record_notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await pilot.press("p")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "2 untagged listed" in screen_text(app)
        await pilot.press("y")
        assert await wait_for(pilot, lambda: any("Reclaimed 729M" in n[0] for n in notes))
        assert await wait_for(pilot, lambda: not any(i.dangling for i in snapshot(app, "lima:docker").images))
        await pilot.press("p")
        await settle(pilot)
        assert not isinstance(app.screen, ConfirmScreen)
        assert notes[-1][0] == "no dangling images"


async def test_action_failure_is_an_error_toast_and_clears_pending() -> None:
    backend = demo_backend()
    backend.fail_next["stop"] = "container is marked for removal"
    app = make_app(backend)
    notes = record_notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await select(pilot, app, "redis")
        await pilot.press("x")
        assert await wait_for(pilot, lambda: any(n[2] == "error" for n in notes))
        assert notes[-1] == ("container is marked for removal", "Stop redis failed", "error")
        assert app.store.pending == {}


async def test_vm_stop_from_sidebar_confirms_shows_pending_and_start_again() -> None:
    backend = demo_backend(vm_latency=0.4)
    app = make_app(backend, discover_interval=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await pilot.press("shift+tab")  # sidebar, on docker
        await pilot.press("x")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "Stop docker?" in screen_text(app)
        await pilot.press("y")
        assert await wait_for(pilot, lambda: "stopping…" in screen_text(app), timeout=1)
        assert await wait_for(pilot, lambda: "docker is stopped" in screen_text(app), timeout=4)
        assert ("vm_stop", "lima:docker") in backend.calls
        app.screen.query_one("#targets").focus()
        await settle(pilot)
        await pilot.press("s")  # starting needs no confirmation
        assert await wait_for(pilot, lambda: ("vm_start", "lima:docker") in backend.calls)
        assert await wait_for(pilot, lambda: app.store.state("lima:docker") is DaemonState.OK, timeout=4)


class ExecBackend(FixtureBackend):
    demo = False

    @override
    def exec_argv(self, target: Target, cid: str) -> list[str] | None:
        return ["docker", "--host", target.endpoint, "exec", "-it", cid, "sh"]


async def test_exec_suspends_and_runs_argv_for_the_containers_own_target() -> None:
    backend = ExecBackend(jitter=False)
    app = make_app(backend)
    ran: list[list[str]] = []
    app.exec_runner = lambda argv: ran.append(argv) or 0
    notes = record_notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        db = await select(pilot, app, "shop-db-1")
        await pilot.press("e")
        assert await wait_for(pilot, lambda: bool(ran))
        assert ran == [["docker", "--host", "unix:///Users/demo/.lima/docker/sock/docker.sock", "exec", "-it", db.id,
                        "sh"]]
        assert any("Shell in shop-db-1 closed" in n[0] for n in notes)


async def test_exec_is_refused_in_demo_mode_with_reason() -> None:
    app = make_app()
    app.exec_runner = lambda argv: pytest.fail("must not exec")
    notes = record_notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await pilot.press("e")
        assert await wait_for(pilot, lambda: bool(notes))
        assert notes[-1][0] == "exec needs a real daemon (demo mode)"


async def test_footer_gating_dims_unavailable_actions() -> None:
    app = make_app(target="ctx:prod-eu")
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        screen = main_screen(app)
        for key in "sxRXep":
            assert screen.check_action("act", (key,)) is None, key
        app.poller.select("lima:docker")
        assert await wait_for(pilot, lambda: app.store.state("lima:docker") is DaemonState.OK)
        await settle(pilot)
        assert screen.check_action("act", ("x",)) is True  # running container selected
        assert screen.check_action("act", ("s",)) is None
        app.poller.select("ctx:prod-eu")
        await settle(pilot)
        await pilot.press("shift+tab")
        assert all(screen.check_action("act", (key,)) is None for key in "sxRXep")


async def test_read_only_every_key_and_button_is_refused() -> None:
    backend = demo_backend()
    app = make_app(backend, target="ctx:prod-eu")
    notes = record_notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        for focus in ("middle", "sidebar"):
            if focus == "sidebar":
                await pilot.press("shift+tab")
            for key in "sxRXep":
                await pilot.press(key)
                await settle(pilot, 0.02, 2)
                assert not isinstance(app.screen, ConfirmScreen)
        pane = app.screen.query_one("#detail", DetailPane)
        app.screen.query_one(ContainerTree).focus()
        await settle(pilot)
        for bar in pane.query(ActionBar):
            for button in bar.query(ActionButton):
                assert button.disabled or not button.display
                bar.post_message(ActionBar.Requested(button.action_id))
        for spec in REGISTRY:  # and straight through the trigger API
            app.trigger(spec, app.current_subject())
        await settle(pilot)
        assert backend.calls == []
        assert notes and all(n[0] == READ_ONLY_REASON for n in notes)
        assert "READ-ONLY" in screen_text(app)


async def test_read_only_layers_below_the_registry_record_only_allowlisted_argv(
        monkeypatch: pytest.MonkeyPatch) -> None:
    argvs: list[list[str]] = []

    async def runner(argv: list[str], timeout: float) -> RunResult:
        argvs.append(argv)
        if argv[1] == "context":
            return RunResult(0, b'{"Name":"prod","DockerEndpoint":"ssh://prod"}\n', b"")
        return RunResult(0, b"", b"")

    backend = LiveBackend(runner)
    for method, args in [("start", ("abc",)), ("stop", ("abc",)), ("restart", ("abc",)), ("remove", ("abc",)),
                         ("prune_dangling_images", ()), ("vm_start", ()), ("vm_stop", ())]:
        with pytest.raises(ReadOnlyViolation):
            await getattr(backend, method)(CTX, *args)
    with pytest.raises(ReadOnlyViolation):
        backend.exec_argv(CTX, "abc")

    spawned: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kw: object) -> Never:
        spawned.append(argv)
        raise RuntimeError("stop here")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await backend.fetch(CTX)
    await backend.inspect(CTX, "abc")
    with pytest.raises(RuntimeError):
        async for _ in backend.logs(CTX, "abc"):
            pass
    for argv in argvs + [list(a) for a in spawned]:
        assert argv[:3] == ["docker", "--context", "prod"] and argv[3] in ALLOWED_VERBS, argv
    assert {a[3] for a in argvs} == {"ps", "images", "inspect"} and spawned[0][3] == "logs"
