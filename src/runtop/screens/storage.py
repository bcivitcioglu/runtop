"""A deliberate, on-demand storage inspection; no background disk scans."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from typing_extensions import override

from runtop.data.backend import StorageBackend
from runtop.data.format import human_bytes_long
from runtop.data.models import Target
from runtop.data.storage import summarize


class StorageScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,D,q", "close", "Close"), Binding("r", "reload", "Refresh")]
    DEFAULT_CSS = """
    StorageScreen { align: center middle; background: $background 70%; }
    StorageScreen > VerticalScroll {
        width: 90%; max-width: 90; height: 85%; border: round $primary; padding: 1 2;
        background: $panel;
    }
    StorageScreen Static { height: auto; margin-bottom: 1; }
    """

    def __init__(self, target: Target, backend: StorageBackend) -> None:
        super().__init__()
        self.target, self.backend = target, backend

    @override
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(f"Storage · {self.target.name}", markup=False)
            yield Static("Loading Docker disk usage…", id="storage-body", markup=False)
            yield Static("These categories are not additive. Docker usage excludes guest OS files, bind mounts, "
                         "and some logs. Deleting Docker data may not immediately shrink the VM disk file. "
                         "This view does not delete anything.", markup=False)
            yield Button("Close (esc)", id="storage-close")

    def on_mount(self) -> None:
        self.action_reload()

    @work(exclusive=True, exit_on_error=False)
    async def action_reload(self) -> None:
        body = self.query_one("#storage-body", Static)
        body.update("Loading Docker disk usage…")
        try:
            data = await self.backend.storage(self.target)
            lines = []
            for row in summarize(data):
                size = human_bytes_long(row.size_bytes) if row.size_bytes is not None else "unknown"
                noun = "entry" if row.count == 1 else "entries"
                lines.append(f"{row.category} · {row.count} {noun} · {size}\n  {row.note}")
            body.update("\n\n".join(lines))
        except Exception as e:
            body.update(f"Storage unavailable: {e}\nPress r to retry.")

    def on_button_pressed(self) -> None:
        self.action_close()

    def action_close(self) -> None:
        self.dismiss()
