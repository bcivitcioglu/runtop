"""Left sidebar: machines + remote contexts, then the Containers / Images sections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Unpack

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option
from typing_extensions import override

from runtop.data.models import DaemonState, Target, TargetKind
from runtop.state.store import Section
from runtop.widgets.kwargs import WidgetKwargs
from runtop.widgets.styles import fg


@dataclass(frozen=True, slots=True)
class TargetStatus:
    target: Target
    state: DaemonState | None  # None = never fetched
    running: int | None = None
    fetching: bool = False
    pending: str | None = None


class _SideList(OptionList):
    """OptionList that hands focus to its sibling at the edges, like one continuous list."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    COMPONENT_CLASSES = {"side--header", "side--ok", "side--warn", "side--bad", "side--idle", "side--muted",
                         "side--badge"}

    DEFAULT_CSS = """
    _SideList {
        height: auto;
        border: none;
        padding: 0;
        background: transparent;
        scrollbar-size-vertical: 1;
        &:focus { border: none; background-tint: transparent; }
        & > .option-list--option { padding: 0 1; }
        & > .option-list--option-highlighted { background: $foreground 9%; color: $foreground; text-style: none; }
        &:focus > .option-list--option-highlighted { background: $primary 32%; color: $foreground; text-style: none; }
        & > .option-list--option-hover { background: $foreground 5%; }
        & > .option-list--option-disabled { color: $foreground-muted; }
        & > .side--header { color: $foreground-muted; text-style: bold; }
        & > .side--ok { color: $success; }
        & > .side--warn { color: $warning; }
        & > .side--bad { color: $error; }
        & > .side--idle { color: $foreground-muted; }
        & > .side--muted { color: $foreground-muted; }
        & > .side--badge { color: $foreground-muted; }
    }
    """

    class EdgeReached(Message):
        def __init__(self, source: _SideList, direction: int) -> None:
            super().__init__()
            self.source = source
            self.direction = direction

    def _enabled_indices(self) -> list[int]:
        return [i for i in range(self.option_count) if not self.get_option_at_index(i).disabled]

    @override
    def action_cursor_down(self) -> None:
        idx = self._enabled_indices()
        if idx and self.highlighted is not None and self.highlighted >= idx[-1]:
            self.post_message(self.EdgeReached(self, +1))
            return
        super().action_cursor_down()

    @override
    def action_cursor_up(self) -> None:
        idx = self._enabled_indices()
        if idx and self.highlighted is not None and self.highlighted <= idx[0]:
            self.post_message(self.EdgeReached(self, -1))
            return
        super().action_cursor_up()

    def focus_edge(self, direction: int) -> None:
        idx = self._enabled_indices()
        if idx:
            self.highlighted = idx[0] if direction > 0 else idx[-1]
        self.focus()

    def s(self, name: str) -> Style:
        return fg(self, name)


def _row(left: Text, right: Text | None = None) -> Table:
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    grid.add_row(left, right or Text(""))
    return grid


class TargetList(_SideList):
    class TargetHighlighted(Message):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def __init__(self, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self._statuses: list[TargetStatus] = []
        self._selected: str | None = None
        self._syncing = False

    @override
    def on_mount(self) -> None:
        self.watch(self.app, "theme", lambda _: self._render_options(), init=False)

    def set_targets(self, statuses: list[TargetStatus], selected: str | None) -> None:
        self._statuses = statuses
        self._selected = selected
        self._render_options()

    def _prompt(self, st: TargetStatus) -> Table:
        t = st.target
        if t.kind is TargetKind.CONTEXT:
            if st.state is DaemonState.OK:
                dot, cls = "●", "side--ok"
            elif st.state is DaemonState.UNREACHABLE:
                dot, cls = "●", "side--bad"
            else:
                dot, cls = "○", "side--idle"
        else:
            running = t.vm.running if t.vm else st.state is DaemonState.OK
            if not running:
                dot, cls = "○", "side--idle"
            elif st.state is DaemonState.UNREACHABLE:
                dot, cls = "●", "side--bad"
            elif st.state is DaemonState.NO_DOCKER_SOCKET:
                dot, cls = "●", "side--warn"
            else:
                dot, cls = "●", "side--ok"
        left = Text.assemble((dot + " ", self.s(cls)), t.name)
        right = Text()
        if st.pending:
            dot, cls = "◐", "side--warn"
            left = Text.assemble((dot + " ", self.s(cls)), t.name)
            right.append(f"{st.pending}…", self.s("side--warn"))
        elif st.state is DaemonState.UNREACHABLE:
            right.append("offline", self.s("side--bad"))
        elif t.vm is not None and not t.vm.running:
            right.append("stopped", self.s("side--muted"))
        elif st.state is DaemonState.NO_DOCKER_SOCKET:
            right.append("no docker", self.s("side--muted"))
        elif st.running is not None:
            right.append(f"{st.running} up", self.s("side--muted"))
        return _row(left, right)

    def _render_options(self) -> None:
        self._syncing = True
        try:
            highlighted_key = self._selected
            machines = [s for s in self._statuses if s.target.kind is not TargetKind.CONTEXT]
            remotes = [s for s in self._statuses if s.target.kind is TargetKind.CONTEXT]
            options: list[Option] = []
            if machines:
                options.append(Option(Text("MACHINES", style=self.s("side--header")), id="h:machines", disabled=True))
                options += [Option(self._prompt(s), id=s.target.key) for s in machines]
            if remotes:
                if machines:
                    options.append(Option(Text(""), id="h:gap", disabled=True))
                header = Text.assemble(("REMOTE", self.s("side--header")), ("  read-only", self.s("side--muted")))
                options.append(Option(header, id="h:remote", disabled=True))
                options += [Option(self._prompt(s), id=s.target.key) for s in remotes]
            ids = [o.id for o in options]
            if ids == [self.get_option_at_index(i).id for i in range(self.option_count)]:
                for o in options:
                    if o.id:
                        self.replace_option_prompt(o.id, o.prompt)
            else:
                self.clear_options()
                self.add_options(options)
            if highlighted_key is not None:
                try:
                    index = self.get_option_index(highlighted_key)
                except Exception:
                    index = None
                if index is not None and index != self.highlighted:
                    self.highlighted = index
        finally:
            self._syncing = False

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if self._syncing or event.option.id is None or event.option.id.startswith("h:"):
            return
        if event.option.id != self._selected:
            self._selected = event.option.id
            self.post_message(self.TargetHighlighted(event.option.id))


SECTION_GLYPHS = {Section.CONTAINERS: "▦", Section.IMAGES: "◇"}
SECTION_TITLES = {Section.CONTAINERS: "Containers", Section.IMAGES: "Images"}


class SectionList(_SideList):
    class SectionHighlighted(Message):
        def __init__(self, section: Section) -> None:
            super().__init__()
            self.section = section

    def __init__(self, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self._counts: dict[Section, int | None] = {}
        self._section = Section.CONTAINERS
        self._syncing = False

    @override
    def on_mount(self) -> None:
        self.watch(self.app, "theme", lambda _: self._render_options(), init=False)
        self._render_options()

    def set_counts(self, counts: dict[Section, int | None], section: Section) -> None:
        if counts == self._counts and section == self._section and self.option_count:
            return
        self._counts, self._section = counts, section
        self._render_options()

    def _render_options(self) -> None:
        self._syncing = True
        try:
            self.clear_options()
            opts = [Option(Text("DOCKER", style=self.s("side--header")), id="h:docker", disabled=True)]
            for sec in Section:
                n = self._counts.get(sec)
                left = Text.assemble((SECTION_GLYPHS[sec] + " ", self.s("side--muted")), SECTION_TITLES[sec])
                opts.append(Option(_row(left, Text("" if n is None else str(n), style=self.s("side--muted"))),
                                   id=f"s:{sec}"))
            self.add_options(opts)
            self.highlighted = self.get_option_index(f"s:{self._section}")
        finally:
            self._syncing = False

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if self._syncing or not event.option.id or not event.option.id.startswith("s:"):
            return
        sec = Section(event.option.id.removeprefix("s:"))
        if sec != self._section:
            self._section = sec
            self.post_message(self.SectionHighlighted(sec))


class Sidebar(Vertical):
    DEFAULT_CSS = """
    Sidebar {
        width: 28;
        height: 1fr;
        padding: 1 0 0 0;
        background: $background;
        & > SectionList { margin-bottom: 1; }
        & > TargetList { height: auto; max-height: 1fr; }
    }
    """

    @override
    def compose(self) -> ComposeResult:
        yield SectionList(id="sections")
        yield TargetList(id="targets")

    @on(_SideList.EdgeReached)
    def _edge(self, event: _SideList.EdgeReached) -> None:
        event.stop()
        lists: list[_SideList] = [self.query_one(SectionList), self.query_one(TargetList)]
        i = lists.index(event.source)
        j = i + event.direction
        if 0 <= j < len(lists):
            lists[j].focus_edge(event.direction)
