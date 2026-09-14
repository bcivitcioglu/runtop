"""Logs tab: a follow-able, searchable log with level highlighting and a jump-to-bottom affordance."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Self, Unpack

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.strip import Strip
from textual.widgets import Button, Input, Label, Log, Switch
from typing_extensions import override

from runtop.data.engine import LogLine
from runtop.data.format import strip_non_sgr
from runtop.widgets.kwargs import WidgetKwargs
from runtop.widgets.styles import fg

STDERR_MARK = "⁣"  # invisible separator: marks stderr lines inside Log's plain line store
_ANY_ESCAPE = re.compile(r"\x1b(?:\[[0-9;?]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_LEVELS = [
    (re.compile(r"\b(ERROR|ERR|FATAL|PANIC|CRITICAL|error|fatal|panic)\b|level=error"), "log--error"),
    (re.compile(r"\b(WARN|WARNING|warn|warning)\b|level=warn"), "log--warn"),
    (re.compile(r"\b(INFO|LOG|NOTICE|\[Note\]|\[System\])\b|level=info"), "log--info"),
    (re.compile(r"\b(DEBUG|TRACE|debug)\b"), "log--muted"),
]
_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?(?: UTC)?"  # ISO-ish
    r"|\[[^\]]{8,30}\]"  # [bracketed]
    r"|\d+:[A-Z] \d{2} \w{3} \d{4} [\d:.]+)"  # redis
)


_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[T ](?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)?Z?(?: UTC)?")


class LimaLog(Log):
    """``Log`` that keeps SGR colours, marks stderr, and highlights levels and search matches."""

    COMPONENT_CLASSES = {"log--error", "log--warn", "log--info", "log--muted", "log--match", "log--stderr"}
    DEFAULT_CSS = """
    LimaLog {
        background: $background;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        & > .log--error { color: $error; }
        & > .log--warn { color: $warning; }
        & > .log--info { color: $primary; }
        & > .log--muted { color: $foreground-muted; }
        & > .log--match { background: $warning 35%; color: $foreground; }
        & > .log--stderr { color: $error; }
    }
    """

    def __init__(self, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(max_lines=5000, auto_scroll=True, **kwargs)
        self.query_text = ""
        self.compact_timestamps = True  # narrow pane: show the time, not the date

    @override
    def write_lines(self, lines: Iterable[str], scroll_end: bool | None = None) -> Self:
        super().write_lines(lines, scroll_end)
        # Log refreshes new lines at their indexes from before pruning. At max_lines those rows lie past
        # the end, so a following view would stop repainting: repaint the viewport instead.
        if self.max_lines is not None and self.line_count >= self.max_lines:
            self.refresh()
        return self

    @override
    def on_mount(self) -> None:
        self.watch(self.app, "theme", lambda _: self.repaint(), init=False)

    @work(thread=True, exit_on_error=False)
    @override
    def _update_size(self, updates: int, lines: list[str]) -> None:
        # Log measures new lines in a thread and reports back through self.app. When the app
        # is shutting down while a stream is still writing, that lookup raises
        # NoActiveAppError (and call_from_thread RuntimeError), which Textual's version
        # turns into a crash on quit. Same work, but it gives up quietly once the app is gone.
        if not lines:
            return
        max_length = max(cell_len(self._process_line(line)) for line in lines)
        try:
            self.app.call_from_thread(self._update_maximum_width, updates, max_length)
        except RuntimeError:  # includes NoActiveAppError
            return

    def repaint(self) -> None:
        self._render_line_cache.clear()
        self.refresh()

    @override
    @classmethod
    def _process_line(cls, line: str) -> str:  # used for width measurement
        return _ANY_ESCAPE.sub("", line.removeprefix(STDERR_MARK)).expandtabs()

    def set_query(self, query: str) -> None:
        self.query_text = query
        self.repaint()

    def matches(self) -> list[int]:
        q = self.query_text.lower()
        if not q:
            return []
        return [i for i, line in enumerate(self._lines) if q in self._process_line(line).lower()]

    @override
    def _render_line_strip(self, y: int, rich_style: Style) -> Strip:
        selection = self.text_selection
        if y in self._render_line_cache and selection is None:
            return self._render_line_cache[y]
        raw = self._lines[y]
        stderr = raw.startswith(STDERR_MARK)
        body = strip_non_sgr(raw.removeprefix(STDERR_MARK)).expandtabs()
        iso = _ISO_PREFIX.match(body)
        if iso and self.compact_timestamps:
            body = iso.group("time") + body[iso.end():]  # "2026-09-12T20:30:10.432Z INFO" → "20:30:10 INFO"
        text = Text.from_ansi(body, no_wrap=True, end="")
        text.stylize_before(rich_style)
        plain = text.plain
        ts = _TIMESTAMP.match(plain)
        if iso and self.compact_timestamps:
            text.stylize(fg(self, "log--muted"), 0, len(iso.group("time")))
        elif ts:
            text.stylize(fg(self, "log--muted"), 0, ts.end())
        for pattern, cls in _LEVELS:
            m = pattern.search(plain)
            if m:
                text.stylize(fg(self, cls), m.start(), m.end())
                break
        gutter = Text("▎" if stderr else " ", style=fg(self, "log--stderr") if stderr else Style(), end="")
        q = self.query_text.lower()
        if q:
            match_style = fg(self, "log--match", keep_bg=True)
            lower = plain.lower()
            start = lower.find(q)
            while start != -1:
                text.stylize(match_style, start, start + len(q))
                start = lower.find(q, start + len(q))
        line_text = Text.assemble(gutter, text, no_wrap=True, end="")
        if selection is not None and (span := selection.get_span(y - self._clear_y)) is not None:
            start, end = span
            if end == -1:
                end = len(line_text)
            line_text.stylize(self.screen.get_component_rich_style("screen--selection"), start, end)
        strip = Strip(line_text.render(self.app.console), line_text.cell_len)
        if selection is None:
            self._render_line_cache[y] = strip
        return strip


LogSource = Callable[[], AsyncIterator[LogLine]]


class LogView(Vertical):
    """Toolbar (search · matches · follow) over a :class:`LimaLog`; streams from a ``LogSource``."""

    BINDINGS = [
        Binding("f", "toggle_follow", "Follow", show=False),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "prev_match", "Prev match", show=False),
        Binding("slash", "search", "Search logs", show=False),
        Binding("G,end", "jump_bottom", "Bottom", show=False),
    ]

    DEFAULT_CSS = """
    LogView {
        height: 1fr;
        & > #log-toolbar { height: 1; margin: 0 0 1 0; }
        #log-search {
            width: 1fr; height: 1; border: none; padding: 0 1; background: $foreground 7%;
            &:focus { background: $foreground 12%; }
        }
        #log-matches { width: auto; min-width: 8; padding: 0 1; color: $foreground-muted; }
        #log-follow-label { width: auto; padding: 0 1 0 2; color: $foreground-muted; }
        #log-follow { height: 1; width: auto; border: none; padding: 0; background: transparent; }
        #log-state { height: 1; color: $foreground-muted; padding: 0 1; }
        #log-jump {
            dock: bottom; offset: 0 -1; width: auto; min-width: 0; height: 1; margin: 0 2 0 0;
            border: none; padding: 0 1; background: $primary; color: $text; display: none;
        }
        #log-jump.-visible { display: block; }
    }
    """

    def __init__(self, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self.source_key: str | None = None
        self.follow = True
        self._match_index = -1
        self.lines_received = 0

    @override
    def compose(self) -> ComposeResult:
        with Horizontal(id="log-toolbar"):
            yield Input(placeholder="search logs  (/)", id="log-search")
            yield Label("", id="log-matches")
            yield Label("follow", id="log-follow-label")
            yield Switch(value=True, id="log-follow")
        yield Label("", id="log-state")
        yield LimaLog(id="log")
        yield Button("↓ jump to bottom", id="log-jump", compact=True)

    @property
    def log_widget(self) -> LimaLog:
        return self.query_one(LimaLog)

    def on_mount(self) -> None:
        self.set_interval(0.25, self._check_scroll)

    # -- streaming

    def show(self, key: str | None, source: LogSource | None, *, placeholder: str = "") -> None:
        """Stream logs for ``key`` (e.g. "lima:docker/abc123"); same key = keep streaming."""
        if key == self.source_key:
            return
        self.source_key = key
        log = self.log_widget
        log.clear()
        self.lines_received = 0
        self._set_state(placeholder if source is None else "loading logs…")
        if source is None:
            self.workers.cancel_group(self, "logs")
            return
        self._stream(key, source)

    def stop(self) -> None:
        self.source_key = None
        self.workers.cancel_group(self, "logs")

    def _set_state(self, text: str) -> None:
        state = self.query_one("#log-state", Label)
        state.update(text)
        state.display = bool(text)

    @work(group="logs", exclusive=True, exit_on_error=False)
    async def _stream(self, key: str | None, source: LogSource) -> None:
        log = self.log_widget
        buf: list[str] = []
        last_flush = time.monotonic()

        def flush() -> None:
            nonlocal last_flush
            if buf:
                log.write_lines(buf, scroll_end=self.follow)
                buf.clear()
                self._update_matches()
            last_flush = time.monotonic()

        async def flush_periodically() -> None:
            while True:
                await asyncio.sleep(0.05)
                if key != self.source_key:
                    return
                flush()

        flusher = asyncio.create_task(flush_periodically())
        try:
            async for line in source():
                if key != self.source_key:
                    return
                if not self.lines_received:
                    self._set_state("")
                self.lines_received += 1
                buf.append((STDERR_MARK if line.stream == "stderr" else "") + line.text)
                if self.lines_received == 1 or len(buf) >= 500 or time.monotonic() - last_flush > 0.05:
                    flush()
                    await asyncio.sleep(0)
            flush()
            self._set_state("stream ended · reopen Logs to reconnect" if self.lines_received else "no log output yet")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # show, don't crash
            flush()
            self._set_state(f"logs unavailable: {str(e).strip() or type(e).__name__}")
        finally:
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

    # -- toolbar

    @on(Switch.Changed, "#log-follow")
    def _follow_changed(self, event: Switch.Changed) -> None:
        self.follow = event.value
        self.log_widget.auto_scroll = event.value
        if event.value:
            self.log_widget.scroll_end(animate=False)

    def action_toggle_follow(self) -> None:
        switch = self.query_one("#log-follow", Switch)
        switch.value = not switch.value

    def action_search(self) -> None:
        self.query_one("#log-search", Input).focus()

    @on(Input.Changed, "#log-search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.log_widget.set_query(event.value)
        self._match_index = -1
        self._update_matches()

    @on(Input.Submitted, "#log-search")
    def _search_submitted(self) -> None:
        self.action_next_match()
        self.log_widget.focus()

    def _update_matches(self) -> None:
        q = self.log_widget.query_text
        label = self.query_one("#log-matches", Label)
        if not q:
            label.update("")
            return
        n = len(self.log_widget.matches())
        pos = f"{self._match_index + 1}/" if self._match_index >= 0 else ""
        label.update(f"{pos}{n} match{'es' if n != 1 else ''}")

    def _goto_match(self, step: int) -> None:
        matches = self.log_widget.matches()
        if not matches:
            return
        self._match_index = (self._match_index + step) % len(matches)
        switch = self.query_one("#log-follow", Switch)
        switch.value = False
        line = matches[self._match_index]
        self.log_widget.scroll_to(y=max(0, line - self.log_widget.size.height // 2), animate=False)
        self._update_matches()

    def action_next_match(self) -> None:
        self._goto_match(+1)

    def action_prev_match(self) -> None:
        self._goto_match(-1)

    def action_jump_bottom(self) -> None:
        self.query_one("#log-follow", Switch).value = True
        self.log_widget.scroll_end(animate=False)

    @on(Button.Pressed, "#log-jump")
    def _jump(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_jump_bottom()

    def _check_scroll(self) -> None:
        log = self.log_widget
        away = log.line_count > log.size.height and not log.is_vertical_scroll_end
        self.query_one("#log-jump", Button).set_class(away, "-visible")
