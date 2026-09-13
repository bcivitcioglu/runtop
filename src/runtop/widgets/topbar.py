"""Top bar (wordmark, breadcrumb, refresh indicator) and the READ-ONLY strip."""

from __future__ import annotations

import time
from typing import Unpack

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static
from typing_extensions import override

from runtop.widgets.kwargs import WidgetKwargs
from runtop.widgets.styles import fg

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def ago(seconds: float) -> str:
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"


class Breadcrumb(Static):
    COMPONENT_CLASSES = {"crumb--brand", "crumb--sep", "crumb--muted"}
    DEFAULT_CSS = """
    Breadcrumb {
        width: 1fr; height: 1; padding: 0 1;
        & > .crumb--brand { color: $primary; text-style: bold; }
        & > .crumb--sep { color: $foreground-muted; }
        & > .crumb--muted { color: $foreground-muted; }
    }
    """

    parts: reactive[tuple[str, ...]] = reactive(())

    @override
    def render(self) -> Text:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("◆ runtop", fg(self, "crumb--brand"))
        for i, part in enumerate(self.parts):
            text.append("  ›  ", fg(self, "crumb--sep"))
            text.append(part, Style(bold=i == len(self.parts) - 1) if i == len(self.parts) - 1 else
                        fg(self, "crumb--muted"))
        return text


class RefreshIndicator(Static):
    COMPONENT_CLASSES = {"refresh--spin", "refresh--muted", "refresh--chip"}
    DEFAULT_CSS = """
    RefreshIndicator {
        width: auto; height: 1; padding: 0 1;
        & > .refresh--spin { color: $primary; }
        & > .refresh--muted { color: $foreground-muted; }
        & > .refresh--chip { color: $foreground; background: $foreground 10%; }
    }
    """

    SPIN_DELAY = 0.4  # quick polls stay silent; only slow fetches show the spinner

    fetching = reactive(False, layout=True)
    fetched_at = reactive(0.0, layout=True)
    state_label = reactive("", layout=True)
    chips: reactive[tuple[str, ...]] = reactive((), layout=True)
    frame = reactive(0)

    def __init__(self, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self._fetch_started = 0.0

    def on_mount(self) -> None:
        self.set_interval(0.12, self._spin)
        self.set_interval(1.0, self.refresh)

    def watch_fetching(self, fetching: bool) -> None:
        if fetching:
            self._fetch_started = time.monotonic()

    @property
    def spinning(self) -> bool:
        return self.fetching and (not self.fetched_at or time.monotonic() - self._fetch_started >= self.SPIN_DELAY)

    def _spin(self) -> None:
        if self.fetching:
            self.frame += 1

    @override
    def render(self) -> Text:
        text = Text(no_wrap=True, justify="right")
        for chip in self.chips:
            text.append(f" {chip} ", fg(self, "refresh--chip", keep_bg=True))
            text.append("  ")
        if self.spinning:
            text.append(SPINNER[self.frame % len(SPINNER)] + " ", fg(self, "refresh--spin"))
            text.append("refreshing", fg(self, "refresh--muted"))
        elif self.state_label:
            text.append(self.state_label, fg(self, "refresh--muted"))
            if self.fetched_at and self.state_label == "stale":
                text.append(f" · last success {ago(time.time() - self.fetched_at)}", fg(self, "refresh--muted"))
        elif self.fetched_at:
            text.append("● ", fg(self, "refresh--muted"))
            text.append(f"updated {ago(time.time() - self.fetched_at)}", fg(self, "refresh--muted"))
        return text


class TopBar(Horizontal):
    DEFAULT_CSS = """
    TopBar {
        height: 1;
        background: $background;
        dock: top;
    }
    """

    @override
    def compose(self) -> ComposeResult:
        yield Breadcrumb(id="crumb")
        yield RefreshIndicator(id="refresh")


class ReadOnlyStrip(Static):
    COMPONENT_CLASSES = {"ro--label", "ro--text"}
    DEFAULT_CSS = """
    ReadOnlyStrip {
        height: 1;
        width: 1fr;
        padding: 0 1;
        background: $error 22%;
        display: none;
        & > .ro--label { color: $text-error; text-style: bold; }
        & > .ro--text { color: $foreground; }
    }
    ReadOnlyStrip.-visible { display: block; }
    """

    endpoint = reactive("")
    name_ = reactive("")

    def show_for(self, name: str | None, endpoint: str = "") -> None:
        self.set_class(name is not None, "-visible")
        if name is not None:
            self.name_, self.endpoint = name, endpoint

    @override
    def render(self) -> Text:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("■ READ-ONLY", fg(self, "ro--label"))
        text.append(f"   {self.name_} · {self.endpoint} · browse and read logs; actions are disabled",
                    fg(self, "ro--text"))
        return text
