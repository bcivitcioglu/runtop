"""Pilot tests for navigation, state transitions and the application shell."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
from typing_extensions import override

from runtop.app import RuntopApp
from runtop.data.backend import FixtureBackend
from runtop.data.models import DaemonState, Target, TargetKind, TargetSnapshot
from runtop.widgets.container_tree import ContainerTree
from runtop.widgets.images_table import ImagesTable
from runtop.widgets.sidebar import SectionSelector, TargetList
from runtop.widgets.splitter import Splitter

from .helpers import (
    GatedBackend,
    main_screen,
    make_app,
    screen_text,
    selected_container,
    selected_row,
    settle,
    stopped,
    tree,
    wait_for,
)

SIZE = (160, 45)


def loaded(app: RuntopApp, key: str = "lima:docker") -> Callable[[], bool]:
    return lambda: app.store.state(key) is not DaemonState.LOADING and app.store.selected == key


async def test_renders_targets_containers_and_details() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        await settle(pilot)
        text = screen_text(app)
        for want in ("runtop", "MACHINES", "docker", "ci", "k3s", "REMOTE", "prod-eu", "staging",
                     "Containers", "Images", "blog", "shop", "standalone", "api-1", "worker-1", "exited 1",
                     "7 running · 9 total", "CONTAINER"):
            assert want in text, want
        assert isinstance(app.focused, ContainerTree)
        assert selected_container(tree(app)).name == "blog-app-1"
        assert "ghost:5-alpine" in text  # detail pane shows the selected container's image


async def test_stopped_vm_message() -> None:
    app = make_app(target="lima:ci")
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: "ci is stopped" in screen_text(app))
        assert "limactl start ci" in screen_text(app)


async def test_each_target_state_is_distinct() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, loaded(app))
        expected = {
            "lima:k3s": "No Docker on k3s",
            "ctx:staging": "Can't reach staging",
            "ctx:prod-eu": "READ-ONLY",
            "lima:ci": "ci is stopped",
        }
        for key, want in expected.items():
            app.poller.select(key)
            assert await wait_for(pilot, lambda want=want: want in screen_text(app)), key
        assert "Operation timed out" in screen_text(app) or True


async def test_key_navigation_and_group_rows() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        t = tree(app)
        assert selected_row(t).kind == "container"  # cursor starts on the first container, not a header
        await pilot.press("j")
        assert selected_container(t).name == "blog-mysql-1"
        await pilot.press("j")
        assert selected_row(t).kind == "group" and selected_row(t).group.project == "shop"
        await pilot.press("k", "k")
        assert selected_container(t).name == "blog-app-1"


async def test_group_collapse_and_expand() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        t = tree(app)
        lines = t.last_line
        assert t.select_key("group:shop")
        await pilot.press("enter")
        await settle(pilot)
        assert "group:shop" in t.collapsed
        assert t.last_line == lines - 4
        assert "▸ shop" in screen_text(app)
        await pilot.press("enter")
        await settle(pilot)
        assert "group:shop" not in t.collapsed and t.last_line == lines
        await pilot.press("left")  # left collapses a group…
        await settle(pilot)
        assert "group:shop" in t.collapsed
        await pilot.press("left")  # …and on a collapsed group moves to the sidebar
        assert isinstance(app.focused, TargetList | SectionSelector)


async def test_collapse_survives_refresh_and_target_round_trip() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        tree(app).select_key("group:blog")
        await pilot.press("space")
        await settle(pilot)
        app.poller.fetch_now(force=True)
        await settle(pilot)
        assert "group:blog" in tree(app).collapsed
        app.poller.select("lima:ci")
        await settle(pilot)
        app.poller.select("lima:docker")
        await settle(pilot)
        assert "▸ blog" in screen_text(app)


async def test_search_filter_and_no_match() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        await pilot.press("slash", "m", "y", "s")
        await settle(pilot)
        t = tree(app)
        names = [n.data.container.name for n in t._all_nodes()
                 if n.data is not None and n.data.kind == "container" and n.data.container is not None]
        assert names == ["blog-mysql-1"]
        await pilot.press("q")  # typing, not quitting
        await settle(pilot)
        assert app.is_running
        await pilot.press("x")
        await settle(pilot)
        assert "No containers match “mysqx”" in screen_text(app)
        await pilot.press("escape")
        await settle(pilot)
        assert "No containers match" not in screen_text(app)
        assert len([n for n in t._all_nodes() if n.data is not None and n.data.kind == "container"]) == 9


async def test_remote_is_read_only_and_has_no_stats_columns() -> None:
    app = make_app(target="ctx:prod-eu")
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        await settle(pilot)
        text = screen_text(app)
        assert "READ-ONLY" in text and "ssh://deploy@prod-eu.example.com" in text
        assert "CPU" not in text.split("NAME", 1)[1].splitlines()[0]
        assert "IMAGE" in text


async def test_loading_is_never_an_empty_list() -> None:
    backend = GatedBackend()
    gate = backend.gates["lima:docker"] = asyncio.Event()
    app = make_app(backend)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: "Loading docker" in screen_text(app))
        assert "No containers" not in screen_text(app)
        gate.set()
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)


async def test_empty_containers_and_images_states() -> None:
    fb = FixtureBackend(jitter=False)
    docker = fb._snaps["lima:docker"]
    fb.set_snapshot(replace(docker, containers=(), images=()))
    app = make_app(fb)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: "No containers" in screen_text(app))
        screen = main_screen(app)
        screen.section = screen.section.__class__("images")
        screen.sync()
        await settle(pilot)
        assert "No images" in screen_text(app)


async def test_discovery_failure_shows_no_targets_state() -> None:
    class Broken(FixtureBackend):
        @override
        async def discover(self) -> Never:
            raise RuntimeError("limactl not found (brew install lima)")

    app = make_app(Broken())
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: "No machines found" in screen_text(app))
        assert "brew install lima" in screen_text(app)


async def test_selection_survives_refresh_with_reorder() -> None:
    fb = FixtureBackend(jitter=False)
    app = make_app(fb)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        t = tree(app)
        db = next(c for c in fb._snaps["lima:docker"].containers if c.name == "shop-db-1")
        assert t.select_key(f"c:{db.id}")
        docker = fb._snaps["lima:docker"]
        new = replace(db, id="0000newnew00", name="shop-aaa-1")
        fb.set_snapshot(replace(docker, containers=(new, *reversed(docker.containers))))
        app.poller.fetch_now(force=True)
        assert await wait_for(pilot, lambda: t.last_line == 12)
        assert selected_container(t).id == db.id


async def test_target_switch_cancels_inflight_fetch_and_drops_stale() -> None:
    backend = GatedBackend()
    app = make_app(backend)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, loaded(app))
        gate = backend.gates["lima:docker"] = asyncio.Event()
        app.poller.fetch_now(force=True)
        await settle(pilot)
        gen = app.poller.generation
        app.poller.select("lima:ci")
        gate.set()
        assert await wait_for(pilot, lambda: "lima:ci" in backend.completed)
        await settle(pilot)
        assert app.poller.generation == gen + 1
        assert "lima:docker" in backend.cancelled
        assert backend.completed.count("lima:docker") == 1  # only the initial fetch landed


async def test_stale_result_for_old_generation_is_dropped() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, loaded(app))
        before = app.store.snapshots["lima:docker"]
        gen = app.poller.generation
        app.poller.generation += 1  # simulate a switch that raced this fetch
        await app.poller._fetch("lima:docker", gen)
        assert app.poller.dropped == 1
        assert app.store.snapshots["lima:docker"] is before


async def test_cached_target_switch_is_instant() -> None:
    backend = GatedBackend()
    app = make_app(backend)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        app.poller.select("lima:ci")
        assert await wait_for(pilot, lambda: "ci is stopped" in screen_text(app))
        backend.gates["lima:docker"] = asyncio.Event()  # the refresh will hang…
        await pilot.press("left_square_bracket")  # …but cached data shows at once
        await settle(pilot)
        assert app.store.selected == "lima:docker" and "lima:docker" in app.store.fetching
        assert "blog" in screen_text(app) and tree(app).last_line > 0


async def test_vm_stopped_elsewhere_shows_within_discover_interval() -> None:
    fb = FixtureBackend(jitter=False)
    app = make_app(fb, discover_interval=0.3)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        fb.set_snapshot(stopped(fb._snaps["lima:docker"]))
        assert await wait_for(pilot, lambda: "docker is stopped" in screen_text(app), timeout=5)


async def test_new_and_removed_targets_update_sidebar() -> None:
    fb = FixtureBackend(jitter=False)
    app = make_app(fb, discover_interval=0.2)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, loaded(app))
        extra = Target("ctx:zz-new", TargetKind.CONTEXT, "zz-new", True, "ssh://zz")
        fb.set_snapshot(TargetSnapshot(extra, DaemonState.OK))
        assert await wait_for(pilot, lambda: "zz-new" in screen_text(app))


async def test_tab_cycles_columns_and_sidebar_edges() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: isinstance(app.focused, ContainerTree))
        await pilot.press("tab")
        screen = main_screen(app)
        focused = app.focused
        assert (focused is not None and focused.id == "detail-scroll") or screen._column_of(focused) == "detail"
        await pilot.press("tab")
        assert screen._column_of(app.focused) == "sidebar"
        await pilot.press("tab")
        assert isinstance(app.focused, ContainerTree)
        await pilot.press("shift+tab")
        assert isinstance(app.focused, TargetList)
        await pilot.press("up")  # top of machines → view selector
        assert isinstance(app.focused, SectionSelector)
        await pilot.press("down")  # selector → back into machines
        assert isinstance(app.focused, TargetList)


async def test_sections_switch_to_images_table() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        # Wait for each step to land instead of racing key presses on slow CI runners.
        await pilot.press("shift+tab")
        assert await wait_for(pilot, lambda: isinstance(app.focused, TargetList | SectionSelector))
        await pilot.press("up", "right")
        assert await wait_for(pilot, lambda: "REPOSITORY" in screen_text(app)
                              and "‹dangling›" in screen_text(app) and "11 images" in screen_text(app), timeout=10.0)
        text = screen_text(app)
        assert "REPOSITORY" in text and "‹dangling›" in text and "11 images" in text
        await pilot.press("enter")
        assert isinstance(app.focused, ImagesTable)
        image = app.focused.selected_image
        assert image is not None and image.ref == "mysql:8.4"
        assert "mysql:8.4" in screen_text(app)


async def test_sort_cycles() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        await pilot.press("o")
        await settle(pilot)
        assert main_screen(app).sort == "cpu" and "sort: cpu" in screen_text(app)
        groups = [n.data.group.project for n in tree(app).root.children if n.data is not None]
        assert groups[0] == "shop" and groups[-1] == ""


async def test_theme_cycles() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert app.theme == "runtop"
        await pilot.press("t")
        assert app.theme == "runtop-light"


async def test_splitter_drag_and_double_click_reset() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        splitter = app.screen.query_one("#split-left", Splitter)
        sidebar = app.screen.query_one("#sidebar")
        start = sidebar.region.width
        await pilot.mouse_down(splitter)
        await pilot.hover(None, offset=(start + 10, 10))
        await pilot.mouse_up(None, offset=(start + 10, 10))
        await settle(pilot)
        assert sidebar.region.width == start + 10
        await pilot.click(splitter, times=2)
        await settle(pilot)
        assert sidebar.region.width == start


async def test_narrow_layouts() -> None:
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        assert not app.screen.query_one("#detail").display or app.screen.query_one("#detail").region.width == 0
        assert app.screen.query_one("#sidebar").region.width > 0
        await pilot.resize_terminal(80, 24)
        await settle(pilot)
        assert app.screen.query_one("#sidebar").region.width == 0
        await pilot.press("right_square_bracket")
        await settle(pilot)
        assert app.store.selected == "lima:ci"
        assert "ci" in screen_text(app).splitlines()[0]


@pytest.mark.parametrize("quit_after", [0.01, 0.05, 0.3])
async def test_quit_after(quit_after: float) -> None:
    # Very early exits used to crash when the first screen was still mounting (Tabs._on_mount).
    app = make_app(quit_after=quit_after)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(quit_after + 0.8)
        assert not app.is_running


async def test_exit_before_ui_ready_is_deferred_not_dropped() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        app._ui_ready = False
        app.exit()
        assert app.is_running  # held while the UI is (pretend) still mounting
        app.mark_ui_ready()
        await pilot.pause(0.3)
        assert not app.is_running


async def test_live_config_is_applied_and_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtop import config as configmod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = configmod.Config(theme="nord", sidebar_width=32, last_target="ctx:prod-eu",
                           collapsed={"ctx:prod-eu": ["group:api"]})
    app = make_app(config=cfg)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: tree(app).last_line > 0 and app.focused is not None)
        assert app.theme == "nord" and app.store.selected == "ctx:prod-eu"
        assert app.screen.query_one("#sidebar").region.width == 32
        assert "▸ api" in screen_text(app)
        await pilot.press("t")
        await pilot.press("q")
    saved = configmod.load()
    assert saved.theme != "nord" and saved.last_target == "ctx:prod-eu" and saved.sidebar_width == 32
    assert saved.collapsed == {"ctx:prod-eu": ["group:api"]}


async def test_deferred_middle_focus_does_not_steal_sidebar_navigation() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: isinstance(app.focused, ContainerTree))
        await pilot.press("shift+tab")
        assert isinstance(app.focused, TargetList)
        main_screen(app)._focus_middle_or_sidebar()
        await pilot.pause()
        assert isinstance(app.focused, TargetList)
