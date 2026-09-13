"""Keyboard reference (``?``), generated from the action registry plus navigation keys."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static
from typing_extensions import override

from runtop.state.actions import REGISTRY
from runtop.widgets.styles import fg

NAVIGATION = [
    ("↑ ↓  j k", "move"),
    ("← →  h l", "collapse · expand"),
    ("tab", "next column"),
    ("enter", "toggle group · logs"),
    ("[  ]", "previous / next machine"),
    ("1  2  3", "Info · Logs · Stats"),
    ("i", "details (narrow)"),
    ("D", "Docker storage (on demand)"),
    ("L", "record, export and search log archives"),
    ("y", "copy selected ID"),
    ("l", "project logs"),
]
VIEW = [
    ("/", "filter · search logs"),
    ("n  N", "next / previous match"),
    ("f", "follow logs"),
    ("o", "sort: name · cpu · memory"),
    ("r", "refresh now"),
    ("t", "next theme"),
    ("ctrl+p", "command palette"),
    ("q", "quit"),
]


class HelpScreen(ModalScreen[None]):
    COMPONENT_CLASSES = {"help--key", "help--head", "help--muted"}
    BINDINGS = [Binding("escape,question_mark,q", "dismiss_help", "Close", show=False)]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: $background 70%; }
    HelpScreen > Vertical {
        width: 104; max-width: 96%; height: auto; max-height: 92%;
        padding: 1 3; background: $panel; border: round $foreground 18%;
    }
    HelpScreen #help-title { text-style: bold; margin-bottom: 1; }
    HelpScreen VerticalScroll { height: auto; max-height: 34; }
    HelpScreen .help--key { color: $primary; text-style: bold; }
    HelpScreen .help--head { color: $foreground-muted; text-style: bold; }
    HelpScreen .help--muted { color: $foreground-muted; }
    """

    @override
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Keyboard", id="help-title")
            with VerticalScroll():
                yield Static(id="help-body")
            yield Static("esc to close", classes="help--muted")

    def on_mount(self) -> None:
        key, head, muted = fg(self, "help--key"), fg(self, "help--head"), fg(self, "help--muted")

        def section(title: str, rows: list[tuple[str, str]]) -> Table:
            t = Table.grid(padding=(0, 1))
            t.add_column(no_wrap=True, min_width=max(len(k) for k, _ in rows))
            t.add_column(no_wrap=True, overflow="ellipsis")
            t.add_row(Text(title, style=head), "")
            for k, desc in rows:
                t.add_row(Text(k, style=key), desc)
            return t

        actions: dict[str, str] = {}
        for spec in REGISTRY:
            title = spec.title.lower().replace(" machine", "")
            actions[spec.key] = f"{actions[spec.key]} · {title} machine" if spec.key in actions else title
        grid = Table.grid(padding=(0, 3), expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(section("NAVIGATE", NAVIGATION), section("VIEW", VIEW),
                     section("ACTIONS", list(actions.items())))
        grid.add_row("", "", "")
        grid.add_row(Text("Machine actions apply when the sidebar has focus.", style=muted),
                     Text("Remote contexts are read-only: every action is refused with a reason.", style=muted), "")
        self.query_one("#help-body", Static).update(grid)

    def action_dismiss_help(self) -> None:
        self.dismiss(None)
