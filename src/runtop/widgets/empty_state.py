"""One designed placeholder per non-content pane state (never a bare empty list)."""

from __future__ import annotations

from typing import Unpack

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.widget import Widget
from textual.widgets import LoadingIndicator, Static
from typing_extensions import override

from runtop.state.viewmodel import PaneKind, PaneState
from runtop.widgets.kwargs import WidgetKwargs

GLYPHS = {  # (glyph, caption) — plain glyphs that exist in common monospace fonts
    PaneKind.LOADING: ("", ""),
    PaneKind.VM_STOPPED: ("■", "STOPPED"),
    PaneKind.NO_DOCKER_SOCKET: ("×", "NO DOCKER"),
    PaneKind.UNREACHABLE: ("!", "OFFLINE"),
    PaneKind.NO_TARGETS: ("◇", "NO MACHINES"),
    PaneKind.EMPTY: ("□", "EMPTY"),
    PaneKind.NO_MATCH: ("?", "NO MATCH"),
}


def _centered(widget: Widget) -> Widget:
    """The ``Center`` row a placeholder part sits in (hidden together with it)."""
    parent = widget.parent
    assert isinstance(parent, Widget)
    return parent


class EmptyState(Vertical):
    DEFAULT_CSS = """
    EmptyState {
        align: center middle;
        height: 1fr;
        width: 1fr;
        background: transparent;
        & > Center { height: auto; }
        #es-glyph {
            width: 17; height: 5; content-align: center middle; text-align: center; margin-bottom: 1;
            color: $foreground-muted; text-style: bold;
            border: round $foreground 25%;
        }
        #es-spinner { width: 12; height: 1; color: $primary; margin-bottom: 1; min-height: 1; background: transparent; }
        #es-title { width: auto; max-width: 60; text-style: bold; color: $foreground; margin-top: 0; }
        #es-body { width: auto; max-width: 52; color: $foreground-muted; text-align: center; margin-top: 1; }
        #es-hint {
            width: auto; max-width: 60; margin-top: 1; padding: 0 1;
            color: $foreground; background: $foreground 8%;
        }
        &.-tone-warning #es-glyph { color: $warning; border: round $warning 50%; }
        &.-tone-error #es-glyph { color: $error; border: round $error 50%; }
        &.-tone-error #es-body { color: $text-error; }
        &.-tone-primary #es-glyph { color: $primary; }
    }
    """

    def __init__(self, state: PaneState | None = None, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self._state = state or PaneState(PaneKind.LOADING)

    @override
    def compose(self) -> ComposeResult:
        with Center():
            yield Static(id="es-glyph")
        with Center():
            yield LoadingIndicator(id="es-spinner")
        with Center():
            yield Static(id="es-title")
        with Center():
            yield Static(id="es-body")
        with Center():
            yield Static(id="es-hint")

    def on_mount(self) -> None:
        self.show(self._state, force=True)

    @property
    def state(self) -> PaneState:
        return self._state

    def show(self, state: PaneState, force: bool = False) -> None:
        if state == self._state and not force:
            return
        self._state = state
        for tone in ("muted", "warning", "error", "primary"):
            self.set_class(state.tone == tone, f"-tone-{tone}")
        loading = state.kind is PaneKind.LOADING
        glyph = self.query_one("#es-glyph", Static)
        mark, caption = GLYPHS.get(state.kind, ("", ""))
        glyph.update(f"{mark}\n\n{caption}" if caption else mark)
        _centered(glyph).display = not loading
        _centered(self.query_one("#es-spinner")).display = loading
        self.query_one("#es-title", Static).update(state.title)
        body = self.query_one("#es-body", Static)
        body.update(state.body)
        _centered(body).display = bool(state.body)
        hint = self.query_one("#es-hint", Static)
        hint.update(state.hint)
        _centered(hint).display = bool(state.hint)
