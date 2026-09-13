from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, Unpack

import pytest
from typing_extensions import override

from runtop.app import RuntopApp
from runtop.data.backend import FixtureBackend, ReadBackend
from runtop.data.models import Container, DaemonState, Target, TargetSnapshot
from runtop.data.wire import SnapshotDocument
from runtop.screens.main import MainScreen
from runtop.widgets.container_tree import ContainerTree, Row

if TYPE_CHECKING:
    from textual.app import App
    from textual.notifications import SeverityLevel
    from textual.pilot import Pilot

    from runtop.config import Config


class AppOptions(TypedDict, total=False):
    target: str | None
    quit_after: float | None
    local_interval: float
    remote_interval: float
    discover_interval: float
    theme: str
    poll: bool
    config: Config | None


class FixtureOptions(TypedDict, total=False):
    source: str | Path | SnapshotDocument | None
    latency: float
    jitter: bool
    slow: dict[str, float] | None
    action_latency: float
    vm_latency: float
    log_interval: float


def make_app(backend: ReadBackend | None = None, **kw: Unpack[AppOptions]) -> RuntopApp:
    kw.setdefault("local_interval", 60.0)
    kw.setdefault("remote_interval", 60.0)
    kw.setdefault("discover_interval", 60.0)
    return RuntopApp(backend or FixtureBackend(jitter=False), **kw)


def screen_text(app: App[None]) -> str:
    strips = app.screen._compositor.render_full_update().strips
    return "\n".join("".join(s.text for s in line).rstrip() for line in strips)


def main_screen(app: RuntopApp) -> MainScreen:
    screen = app.main
    assert screen is not None
    return screen


def tree(app: App[None]) -> ContainerTree:
    return app.screen.query_one(ContainerTree)


def selected_row(t: ContainerTree) -> Row:
    row = t.selected_row
    assert row is not None
    return row


def selected_container(t: ContainerTree) -> Container:
    c = selected_row(t).container
    assert c is not None
    return c


def snapshot(app: RuntopApp, key: str) -> TargetSnapshot:
    snap = app.store.snapshot(key)
    assert snap is not None
    return snap


async def settle(pilot: Pilot[None], delay: float = 0.05, rounds: int = 3) -> None:
    for _ in range(rounds):
        await pilot.pause(delay)


async def wait_for(pilot: Pilot[None], predicate: Callable[[], object], timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await pilot.pause(0.05)
    return bool(predicate())


class GatedBackend(FixtureBackend):
    """Fetches for keys in ``gates`` block until the gate event is set."""

    def __init__(self) -> None:
        super().__init__(jitter=False)
        self.gates: dict[str, asyncio.Event] = {}
        self.completed: list[str] = []
        self.cancelled: list[str] = []

    @override
    async def fetch(self, target: Target) -> TargetSnapshot:
        gate = self.gates.get(target.key)
        try:
            if gate is not None:
                await gate.wait()
            snap = await super().fetch(target)
        except asyncio.CancelledError:
            self.cancelled.append(target.key)
            raise
        self.completed.append(target.key)
        return snap


def stopped(snap: TargetSnapshot) -> TargetSnapshot:
    assert snap.target.vm is not None
    vm = replace(snap.target.vm, status="Stopped")
    return TargetSnapshot(replace(snap.target, vm=vm), DaemonState.VM_STOPPED)


def record_notes(app: RuntopApp) -> list[tuple[str, str, str]]:
    """Capture toasts (run_test disables real notifications)."""
    notes: list[tuple[str, str, str]] = []
    original = app.notify

    def notify(message: str, *, title: str = "", severity: SeverityLevel = "information",
               timeout: float | None = None, markup: bool = True) -> None:
        notes.append((str(message), str(title), str(severity)))
        return original(message, title=title, severity=severity, timeout=timeout, markup=markup)

    pytest.MonkeyPatch().setattr(app, "notify", notify)  # per-test app: nothing to undo
    return notes


def demo_backend(**kw: Unpack[FixtureOptions]) -> FixtureBackend:
    kw.setdefault("jitter", False)
    return FixtureBackend(**kw)


def container_named(app: RuntopApp, name: str, key: str = "lima:docker") -> Container:
    snap = snapshot(app, key)
    return next(c for c in snap.containers if c.name == name)
