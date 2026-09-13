"""Detail pane as an overlay for narrow terminals (``i``)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from typing_extensions import override

from runtop.widgets.detail import DetailPane


class DetailOverlay(ModalScreen[None]):
    BINDINGS = [
        Binding("escape,i", "close", "Close", show=True),
        Binding("1", "tab('tab-info')", "Info", show=False),
        Binding("2", "tab('tab-logs')", "Logs", show=False),
        Binding("3", "tab('tab-stats')", "Stats", show=False),
    ]
    DEFAULT_CSS = """
    DetailOverlay { align: right top; background: $background 55%; }
    DetailOverlay > DetailPane {
        width: 90%; max-width: 72; height: 100%;
        border-left: tall $primary 40%;
    }
    """

    @override
    def compose(self) -> ComposeResult:
        yield DetailPane(id="detail-overlay")

    def action_close(self) -> None:
        self.dismiss(None)

    def action_tab(self, tab: str) -> None:
        self.query_one(DetailPane).set_tab(tab)
