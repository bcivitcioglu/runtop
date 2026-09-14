"""A3: detail pane (Info · Logs · Stats), charts, log view, progressive remote loading, overlay."""

from __future__ import annotations

import asyncio

from rich.style import Style
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from typing_extensions import override

from runtop.app import RuntopApp
from runtop.data.backend import LiveBackend
from runtop.data.engine import LogLine
from runtop.data.models import Container, DaemonState, Target, TargetKind
from runtop.data.remote import RunResult
from runtop.screens.overlay import DetailOverlay
from runtop.state.store import Section
from runtop.widgets.charts import BlockChart, bar_text, chart_rows, nice_top
from runtop.widgets.container_tree import ContainerTree
from runtop.widgets.detail import DetailPane, StatsView, mask_env, uptime_text
from runtop.widgets.log_view import STDERR_MARK, LimaLog, LogView

from .conftest import text as fixture_text
from .helpers import container_named, demo_backend, main_screen, make_app, screen_text, settle, snapshot, wait_for

SIZE = (160, 45)


def pane(app: RuntopApp) -> DetailPane:
    return app.screen.query_one("#detail", DetailPane)


async def ready(pilot: Pilot[None], app: RuntopApp) -> None:
    assert await wait_for(pilot, lambda: app.screen.query_one(ContainerTree).last_line > 0 and app.focused)


async def select(pilot: Pilot[None], app: RuntopApp, name: str) -> Container:
    c = container_named(app, name)
    app.screen.query_one(ContainerTree).select_key(f"c:{c.id}")
    await settle(pilot)
    return c


def test_chart_and_bar_builders() -> None:
    rows = chart_rows([0, 5, 10], 4, 2, 10)
    assert rows == ["   █", " ▁██"]
    assert chart_rows([], 3, 1, 1) == ["   "]
    assert (nice_top(3.2, 1), nice_top(0.1, 5), nice_top(730, 1)) == (5, 5, 1000)
    bar = bar_text(0.5, 10, Style(color="blue"), Style(color="grey50"))
    assert bar.cell_len == 10 and bar.plain.count("█") == 10


def test_detail_helpers() -> None:
    assert uptime_text(Container("a", "a", "i", "running", "Up 2 hours (healthy)")) == "up 2 hours"
    assert uptime_text(Container("a", "a", "i", "exited", "Exited (1) 14 minutes ago")) == "exited 14 minutes ago"
    assert mask_env(["A=secret", "EMPTY=", "FLAG"]) == [("A", "••••••"), ("EMPTY", ""), ("FLAG", "")]


async def test_container_card_info_tab_masks_env() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await select(pilot, app, "shop-api-1")
        assert await wait_for(pilot, lambda: "DATABASE_URL" in screen_text(app))
        text = screen_text(app)
        for want in ("CONTAINER", "shop-api-1", "running", "shop/api:dev", "up 2 hours", "healthy", "Stop", "Restart",
                     "Info", "Logs", "Stats", "3000 → 3000", "unless-stopped", "shop_default", "env values are masked"):
            assert want in text, want
        assert "s3cret" not in text


async def test_logs_tab_tails_follows_and_highlights_stderr() -> None:
    backend = demo_backend(log_interval=0.05)
    app = make_app(backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await select(pilot, app, "shop-db-1")
        await pilot.press("2")
        log = pane(app).log_view.log_widget
        assert await wait_for(pilot, lambda: log.line_count >= 200)
        assert await wait_for(pilot, lambda: log.line_count >= 205)  # follow keeps appending
        assert all(line.startswith(STDERR_MARK) for line in log._lines[:5])  # postgres logs to stderr
        assert any("LOG:" in line for line in log._lines[:5])


async def test_enter_on_container_opens_logs_focused() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await select(pilot, app, "shop-web-1")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: isinstance(app.focused, LimaLog))
        assert pane(app).active_tab == "tab-logs"


async def test_no_leaked_log_workers_after_many_selection_changes() -> None:
    backend = demo_backend(log_interval=0.02)
    app = make_app(backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await pilot.press("2")
        tree = app.screen.query_one(ContainerTree)
        for i in range(50):
            await pilot.press("down" if (i // 12) % 2 == 0 else "up")
        await settle(pilot, 0.1)
        running = [w for w in app.workers if w.group == "logs" and w.is_running]
        assert len(running) <= 1
        assert tree.selected_row is not None


async def test_log_search_counts_and_jumps() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await select(pilot, app, "shop-web-1")
        await pilot.press("2")
        view = pane(app).query_one(LogView)
        assert await wait_for(pilot, lambda: view.log_widget.line_count >= 200)
        view.query_one("#log-search").focus()
        await pilot.press(*"404")
        await settle(pilot)
        matches = view.log_widget.matches()
        assert matches and "match" in str(view.query_one("#log-matches").render())
        await pilot.press("enter")  # next match, focus returns to the log
        await pilot.press("n")
        assert view._match_index == 1 % len(matches)
        assert view.follow is False


def test_log_render_keeps_sgr_and_strips_other_escapes() -> None:
    class A(App[None]):
        @override
        def compose(self) -> ComposeResult:
            yield LimaLog()

    async def go() -> None:
        app = A()
        async with app.run_test() as pilot:
            log = app.query_one(LimaLog)
            log.write_line("\x1b[2Jplain \x1b[32mgreen\x1b[0m ERROR boom")
            await pilot.pause()
            strip = log._render_line_strip(0, Style())
            assert strip.text.strip() == "plain green ERROR boom"
            assert any(seg.style and seg.style.color and seg.text == "green" for seg in strip)

    asyncio.run(go())


async def test_log_keeps_repainting_after_max_lines_prunes() -> None:
    class A(App[None]):
        @override
        def compose(self) -> ComposeResult:
            yield LimaLog()

    app = A()
    async with app.run_test(size=(60, 12)) as pilot:
        log = app.query_one(LimaLog)
        cap = log.max_lines
        assert cap is not None
        log.write_lines([f"line {i}" for i in range(cap)])
        await pilot.pause()
        assert f"line {cap - 1}" in screen_text(app)
        for i in range(cap, cap + 3):  # every write past the cap prunes the oldest line
            log.write_lines([f"line {i}"])
            await pilot.pause()
        assert f"line {cap + 2}" in screen_text(app)


async def test_stats_tab_charts_history_and_remote_message() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        await select(pilot, app, "blog-mysql-1")
        await pilot.press("3")
        await settle(pilot)
        stats = pane(app).query_one(StatsView)
        assert len(stats.query_one("#st-cpu", BlockChart).values) >= 40  # seeded demo history
        text = screen_text(app)
        assert "CPU" in text and "MEMORY" in text and "sampled every" in text
        app.poller.select("ctx:prod-eu")
        assert await wait_for(pilot, lambda: app.store.state("ctx:prod-eu") is DaemonState.OK)
        await settle(pilot)
        assert "aren't polled on remote contexts" in screen_text(app)


async def test_group_image_and_machine_cards() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot, app)
        app.screen.query_one(ContainerTree).select_key("group:shop")
        await settle(pilot)
        text = screen_text(app)
        assert "COMPOSE PROJECT" in text and "SERVICE" in text and "worker  exited" in text
        await pilot.press("shift+tab")
        await settle(pilot)
        text = screen_text(app)
        assert "MACHINE" in text and "21.4 GiB" in text and "DISK CAP" in text
        main_screen(app).section = Section.IMAGES
        main_screen(app).sync()
        await settle(pilot)
        app.screen.query_one("#images").focus()
        await settle(pilot)
        text = screen_text(app)
        assert "IMAGE" in text and "mysql:8.4" in text and "layers may be shared" in text


async def test_narrow_i_opens_detail_overlay_and_escape_closes() -> None:
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await ready(pilot, app)
        await select(pilot, app, "shop-api-1")
        await pilot.press("i")
        assert await wait_for(pilot, lambda: isinstance(app.screen, DetailOverlay))
        assert await wait_for(pilot, lambda: "shop-api-1" in screen_text(app) and "CONTAINER" in screen_text(app))
        await pilot.press("2")
        assert app.screen.query_one(DetailPane).active_tab == "tab-logs"
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: not isinstance(app.screen, DetailOverlay))


async def test_progressive_remote_fetch_shows_containers_before_images() -> None:
    release = asyncio.Event()

    async def runner(argv: list[str], timeout: float) -> RunResult:
        if argv[1] == "context":
            return RunResult(0, b'{"Name":"slow","DockerEndpoint":"ssh://slow"}\n', b"")
        if argv[3] == "ps":
            return RunResult(0, fixture_text("cli/docker_ps.jsonl").encode(), b"")
        await release.wait()
        return RunResult(0, fixture_text("cli/docker_images.jsonl").encode(), b"")

    class Remote(LiveBackend):
        @override
        async def discover(self) -> list[Target]:
            return [Target("ctx:slow", TargetKind.CONTEXT, "slow", True, "ssh://slow")]

    app = make_app(Remote(runner), target="ctx:slow")
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: app.screen.query_one(ContainerTree).last_line > 0)
        snap = snapshot(app, "ctx:slow")
        assert snap.state is DaemonState.OK and not snap.images_loaded
        main_screen(app).section = Section.IMAGES
        main_screen(app).sync()
        await settle(pilot)
        assert "Loading images from slow" in screen_text(app) and "No images" not in screen_text(app)
        release.set()
        assert await wait_for(pilot, lambda: snapshot(app, "ctx:slow").images_loaded)
        await settle(pilot)
        assert "REPOSITORY" in screen_text(app)


def test_logline_type_is_shared() -> None:
    assert LogLine("stdout", "x").text == "x"
