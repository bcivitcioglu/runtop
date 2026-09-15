from __future__ import annotations

from textual.widgets import ContentSwitcher

from runtop.state.store import Section
from runtop.widgets.sidebar import SectionSelector, TargetList

from .helpers import main_screen, make_app, screen_text, settle, wait_for


async def test_view_selector_is_independent_of_machine_selection() -> None:
    app = make_app()
    async with app.run_test(size=(160, 45)) as pilot:
        assert await wait_for(pilot, lambda: app.store.selected is not None and not app.store.fetching)
        await settle(pilot)
        selected = app.store.selected
        await pilot.press("shift+tab", "up")
        selector = app.query_one(SectionSelector)
        assert app.focused is selector
        assert main_screen(app).section is Section.CONTAINERS  # focus alone never switches views
        await pilot.press("right")
        assert main_screen(app).section is Section.IMAGES
        assert app.store.selected == selected
        assert app.query_one(ContentSwitcher).current == "images"
        await pilot.press("down")
        assert isinstance(app.focused, TargetList)
        await pilot.press("down")
        assert await wait_for(pilot, lambda: app.store.selected != selected)
        assert main_screen(app).section is Section.IMAGES  # view choice applies to the next machine too
        assert "stopped" in screen_text(app).lower()


async def test_view_selector_clicks_and_unknown_counts() -> None:
    app = make_app()
    async with app.run_test(size=(160, 45)) as pilot:
        assert await wait_for(pilot, lambda: app.store.selected is not None and not app.store.fetching)
        await settle(pilot)
        selected = app.store.selected
        await pilot.click("#sections", offset=(20, 2))
        assert main_screen(app).section is Section.IMAGES
        await pilot.click("#sections", offset=(5, 2))
        assert main_screen(app).section is Section.CONTAINERS
        assert app.store.selected == selected
        app.poller.select("lima:ci")
        assert await wait_for(pilot, lambda: not app.store.fetching)
        selector = app.query_one(SectionSelector)
        assert selector._counts == {Section.CONTAINERS: None, Section.IMAGES: None}
        await pilot.click("#sections", offset=(20, 2))
        assert main_screen(app).section is Section.IMAGES
        assert app.store.selected == "lima:ci"


async def test_view_selector_stacks_in_a_resized_sidebar() -> None:
    app = make_app()
    async with app.run_test(size=(160, 45)) as pilot:
        assert await wait_for(pilot, lambda: app.store.selected is not None and not app.store.fetching)
        app.query_one("#sidebar").styles.width = 18
        await pilot.pause()
        selector = app.query_one(SectionSelector)
        assert selector.stacked
        assert "Containers" in screen_text(app) and "Images" in screen_text(app)
        await pilot.click("#sections", offset=(8, 5))
        assert main_screen(app).section is Section.IMAGES
        await pilot.press("space")
        assert main_screen(app).section is Section.CONTAINERS
