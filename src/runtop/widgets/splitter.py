"""Draggable column splitter: drag to resize, double-click to reset."""

from __future__ import annotations

from typing import Unpack

from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from typing_extensions import override

from runtop.widgets.kwargs import WidgetKwargs


class Splitter(Static):
    """A one-cell vertical rule that resizes ``target`` (the column on ``side``)."""

    DEFAULT_CSS = """
    Splitter {
        width: 1;
        height: 1fr;
        color: $foreground 12%;
        background: transparent;
        &:hover, &.-dragging { color: $primary; background: $primary 15%; }
    }
    """

    dragging = reactive(False)

    class Resized(Message):
        def __init__(self, splitter: Splitter, width: int) -> None:
            super().__init__()
            self.splitter = splitter
            self.width = width

    def __init__(self, target: str, side: str = "left", *, default: int, minimum: int = 16,
                 maximum: int = 80, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self.target_id = target
        self.side = side
        self.default = default
        self.minimum = minimum
        self.maximum = maximum

    @override
    def render(self) -> str:
        return "\n".join("│" for _ in range(max(1, self.size.height)))

    @property
    def target(self) -> Widget:
        return self.screen.query_one(f"#{self.target_id}")

    def watch_dragging(self, value: bool) -> None:
        self.set_class(value, "-dragging")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.dragging = True
        self.capture_mouse()
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self.dragging:
            self.dragging = False
            self.release_mouse()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self.dragging:
            return
        region = self.target.region
        if self.side == "left":
            width = event.screen_x - region.x
        else:
            width = region.right - event.screen_x - 1
        self.set_width(width)
        event.stop()

    def on_click(self, event: events.Click) -> None:
        if event.chain >= 2:
            self.set_width(self.default)
        event.stop()

    def set_width(self, width: int) -> int:
        width = max(self.minimum, min(self.maximum, int(width)))
        self.target.styles.width = width
        self.post_message(self.Resized(self, width))
        return width
