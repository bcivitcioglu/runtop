"""A5: command palette providers, help screen."""

from __future__ import annotations

from textual.command import CommandPalette
from textual.pilot import Pilot

from runtop.app import RuntopApp
from runtop.screens.help import HelpScreen
from runtop.widgets.container_tree import ContainerTree

from .helpers import container_named, demo_backend, make_app, record_notes, screen_text, settle, wait_for

SIZE = (160, 45)


async def ready(pilot: Pilot[None], app: RuntopApp) -> None:
    assert await wait_for(pilot, lambda: app.screen.query_one(ContainerTree).last_line > 0 and app.focused)


async def palette(pilot: Pilot[None], app: RuntopApp, query: str) -> None:
    await pilot.press("ctrl+p")
    assert await wait_for(pilot, lambda: isinstance(app.screen, CommandPalette))
    for ch in query:
        await pilot.press("space" if ch == " " else ch)
    await pilot.pause(0.6)


async def test_palette_stop_web_runs_the_action() -> None:
    backend = demo_backend()
    app = make_app(backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await palette(pilot, app, "stop web")
        assert "Stop shop-web-1" in screen_text(app)
        await pilot.press("enter")
        web = container_named(app, "shop-web-1")
        assert await wait_for(pilot, lambda: ("stop", "lima:docker", web.id) in backend.calls)


async def test_palette_switches_target_by_name() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await palette(pilot, app, "prod")
        assert "Switch to prod-eu" in screen_text(app)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.store.selected == "ctx:prod-eu")


async def test_palette_jumps_to_container() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await palette(pilot, app, "mailpit")
        assert "mailpit · standalone · running" in screen_text(app)
        await pilot.press("enter")
        tree = app.screen.query_one(ContainerTree)
        assert await wait_for(pilot, lambda: tree.selected_row and tree.selected_row.container
                              and tree.selected_row.container.name == "mailpit")


async def test_palette_lists_unavailable_actions_with_reason() -> None:
    backend = demo_backend()
    app = make_app(backend, target="ctx:prod-eu")
    notes = record_notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await palette(pilot, app, "remove caddy")
        assert "unavailable: read-only context" in screen_text(app)
        await pilot.press("enter")
        await settle(pilot)
        assert backend.calls == [] and notes[-1][0] == "read-only context — view only"


async def test_help_screen_opens_and_closes() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await pilot.press("question_mark")
        assert await wait_for(pilot, lambda: isinstance(app.screen, HelpScreen))
        assert await wait_for(pilot, lambda: "prune dangling images" in screen_text(app))
        text = screen_text(app)
        assert "Keyboard" in text and "command palette" in text and "stop project" in text and "stop machine" in text
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: not isinstance(app.screen, HelpScreen))
