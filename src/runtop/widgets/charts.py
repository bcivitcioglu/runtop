"""Eighth-block charts and bars (pure text builders + a small chart widget)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Unpack

from rich.style import Style
from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget
from typing_extensions import override

from runtop.widgets.kwargs import WidgetKwargs
from runtop.widgets.styles import fg

EIGHTHS = " ▁▂▃▄▅▆▇█"
HBAR = " ▏▎▍▌▋▊▉█"


def chart_rows(values: list[float] | tuple[float, ...], width: int, height: int, top: float) -> list[str]:
    """``height`` rows (top first) of a right-aligned column chart; missing history is blank."""
    width, height = max(0, width), max(1, height)
    recent = list(values)[-width:]
    vals: list[float | None] = [None] * (width - len(recent))
    vals += recent
    top = top if top > 0 else 1.0
    rows: list[str] = []
    for r in range(height - 1, -1, -1):
        row: list[str] = []
        for v in vals:
            if v is None:
                row.append(" ")
                continue
            eighths = max(0.0, min(1.0, v / top)) * height * 8
            n = round(max(0.0, min(8.0, eighths - r * 8)))
            if r == 0 and n == 0:
                n = 1  # keep a baseline so zero reads as "measured, idle"
            row.append(EIGHTHS[n])
        rows.append("".join(row))
    return rows


def nice_top(peak: float, floor: float) -> float:
    """Round a chart ceiling up to 1/2/5 × 10ⁿ so the axis label reads cleanly."""
    peak = max(peak, floor)
    mag = 1.0
    while mag * 10 <= peak:
        mag *= 10
    while mag > peak:
        mag /= 10
    for step in (1, 2, 5, 10):
        if step * mag >= peak:
            return step * mag
    return 10 * mag  # pragma: no cover


def bar_text(fraction: float, width: int, fill: Style, track: Style) -> Text:
    """A smooth horizontal meter using eighth blocks, with a solid track."""
    fraction = max(0.0, min(1.0, fraction))
    eighths = round(fraction * width * 8)
    full, part = divmod(eighths, 8)
    text = Text(no_wrap=True)
    text.append("█" * full, fill)
    if part and full < width:
        text.append(HBAR[part], fill + Style(bgcolor=track.color))
        full += 1
    text.append("█" * max(0, width - full), track)
    return text


class BlockChart(Widget):
    """A labelled multi-row chart. ``values`` newest last; ``fmt`` formats the axis ceiling."""

    COMPONENT_CLASSES = {"chart--bar", "chart--axis", "chart--grid"}
    DEFAULT_CSS = """
    BlockChart {
        height: 5;
        & > .chart--bar { color: $primary; }
        & > .chart--axis { color: $foreground-muted; }
        & > .chart--grid { color: $foreground 10%; }
    }
    """

    values: reactive[tuple[float, ...]] = reactive((), always_update=True)

    def __init__(self, *, floor: float = 5.0, fmt: Callable[[float], str] = lambda v: f"{v:g}",
                 zero_based: bool = True, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self.floor = floor
        self.fmt = fmt
        self.zero_based = zero_based

    @override
    def render(self) -> Text:
        width, height = self.size.width, self.size.height
        label_w = 7
        vals = self.values
        bottom = 0.0
        if self.zero_based or not vals:
            top = nice_top(max(vals) if vals else 0.0, self.floor)
        else:
            # Memory barely moves: chart the band it lives in instead of a solid wall.
            lo, hi = min(vals), max(vals)
            spread = max(hi - lo, self.floor)
            bottom = max(0.0, lo - spread)
            top = hi + spread * 0.25
        rows = chart_rows([v - bottom for v in vals], max(1, width - label_w), height, top - bottom)
        bar, axis, grid = fg(self, "chart--bar"), fg(self, "chart--axis"), fg(self, "chart--grid")
        text = Text(no_wrap=True, overflow="crop", end="")
        for i, row in enumerate(rows):
            label = self.fmt(top) if i == 0 else ((self.fmt(bottom) if bottom else "0") if i == height - 1 else "")
            text.append(label.rjust(label_w - 1)[: label_w - 1] + " ", axis)
            if row.strip() == "" and i < height - 1:
                text.append("┈" * len(row) if i == 0 else row, grid)
            else:
                text.append(row, bar)
            if i < len(rows) - 1:
                text.append("\n")
        return text
