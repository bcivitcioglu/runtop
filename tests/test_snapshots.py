"""SVG snapshots of the main states (``uv run pytest --snapshot-update`` after reviewing changes).

Determinism: every screenshot waits on concrete app state, never on wall-clock pauses. The initial
target's first fetch must land before a test switches targets — otherwise the switch cancels it and
the sidebar misses that target's "N up" count (what flaked on slow CI runners).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from textual.pilot import Pilot

from runtop.app import RuntopApp
from runtop.data.models import DaemonState
from runtop.widgets import topbar

from .helpers import make_app, wait_for

TIMEOUT = 15.0  # generous for slow runners; every wait returns as soon as its condition holds
LONG_LIVED_GROUPS = {"logs"}  # follow streams never finish on their own


def settled(app: RuntopApp, key: str | None = None) -> Callable[[], bool]:
    """The target (default: the current selection) is selected, fetched, rendered and focused."""

    def ready() -> bool:
        store = app.store
        if not store.targets_loaded or store.selected is None:
            return False
        if key is not None and store.selected != key:
            return False
        if store.state(store.selected) is DaemonState.LOADING or store.fetching or store.pending:
            return False
        if any(not w.is_finished for w in app.workers if w.group not in LONG_LIVED_GROUPS):
            return False
        return app.focused is not None

    return ready


async def idle(pilot: Pilot[None], app: RuntopApp, key: str | None = None) -> None:
    assert await wait_for(pilot, settled(app, key), timeout=TIMEOUT), "app did not settle"
    await pilot.wait_for_scheduled_animations()
    await pilot.pause()  # let the last store change reach the compositor


def run_before(app: RuntopApp, key: str, *keys: str) -> Callable[[Pilot[None]], Awaitable[None]]:
    async def go(pilot: Pilot[None]) -> None:
        await idle(pilot, app)  # the initial target's first fetch completes before any switch
        if key != app.store.selected:
            app.poller.select(key)
        await idle(pilot, app, key)
        if keys:
            await pilot.press(*keys)
            await idle(pilot, app, key)

    return go


@pytest.mark.parametrize(("name", "key", "keys", "size", "theme"), [
    ("containers", "lima:docker", (), (160, 45), "runtop"),
    ("containers_collapsed", "lima:docker", ("down", "down", "left"), (160, 45), "runtop"),
    ("images", "lima:docker", ("shift+tab", "up", "right", "tab"), (160, 45), "runtop"),
    ("vm_stopped", "lima:ci", (), (160, 45), "runtop"),
    ("no_docker_socket", "lima:k3s", (), (160, 45), "runtop"),
    ("remote_read_only", "ctx:prod-eu", (), (160, 45), "runtop"),
    ("unreachable", "ctx:staging", (), (160, 45), "runtop"),
    ("narrow", "lima:docker", (), (100, 30), "runtop"),
    ("light", "lima:docker", (), (160, 45), "runtop-light"),
])
def test_snapshot(snap_compare: Callable[..., bool], monkeypatch: pytest.MonkeyPatch, name: str, key: str,
                  keys: tuple[str, ...], size: tuple[int, int], theme: str) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    # "updated just now" is wall-clock relative: a starved runner could otherwise render "updated 6s ago".
    monkeypatch.setattr(topbar, "ago", lambda _seconds: "just now")
    # make_app's FixtureBackend has no simulated latency, no deliberate slow loader and no stats jitter.
    app = make_app(theme=theme)
    assert snap_compare(app, terminal_size=size, run_before=run_before(app, key, *keys))


@pytest.mark.parametrize("view", ["storage", "project_confirmation", "archives"])
def test_workflow_snapshot(snap_compare: Callable[..., bool], monkeypatch: pytest.MonkeyPatch, view: str) -> None:
    from runtop.widgets.container_tree import ContainerTree

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(topbar, "ago", lambda _seconds: "just now")
    app = make_app()

    async def before(pilot: Pilot[None]) -> None:
        await idle(pilot, app)
        if view == "archives":
            await pilot.press("L")
        elif view == "storage":
            await pilot.press("D")
        else:
            tree = app.screen.query_one(ContainerTree)
            tree.select_key("group:shop")
            tree.focus()
            await pilot.press("x")
        await pilot.pause()
        assert await wait_for(pilot, lambda: all(w.is_finished for w in app.workers))
        await pilot.pause()

    assert snap_compare(app, terminal_size=(110, 32), run_before=before)
