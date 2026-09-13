"""Confirm dialog for destructive actions: a small card over a dimmed app."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from typing_extensions import override


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Confirm", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
        Binding("left,right,tab", "toggle_focus", "Switch", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
        background: $background 70%;
    }
    ConfirmScreen > #dialog {
        width: 60; max-width: 90%; height: auto;
        padding: 1 3;
        background: $panel;
        border: round $foreground 18%;
    }
    ConfirmScreen.-destructive > #dialog { border: round $error 55%; }
    #confirm-scroll { height: auto; max-height: 12; }
    #confirm-title { text-style: bold; color: $foreground; }
    #confirm-body { color: $foreground-muted; margin: 1 0; }
    #confirm-hint { color: $foreground-muted; width: 1fr; height: 1; content-align: left middle; }
    #confirm-buttons { height: 1; margin-top: 1; }
    #confirm-buttons > Button {
        min-width: 0; height: 1; border: none; padding: 0 2; margin-left: 1; text-style: none;
        background: $foreground 10%; color: $foreground;
        &:hover { background: $foreground 18%; }
        &:focus { background: $foreground 22%; text-style: bold; }
    }
    #confirm-buttons > #confirm-ok { background: $primary 70%; color: $text; }
    ConfirmScreen.-destructive #confirm-buttons > #confirm-ok { background: $error 75%; color: $text; }
    #confirm-buttons > #confirm-ok:focus { text-style: bold reverse; }
    """

    def __init__(self, title: str, body: str, confirm_label: str = "Confirm", *, destructive: bool = True) -> None:
        super().__init__(classes="-destructive" if destructive else "")
        self.title_text = title
        self.body = body
        self.confirm_label = confirm_label

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.title_text, id="confirm-title", markup=False)
            with VerticalScroll(id="confirm-scroll"):
                yield Static(self.body, id="confirm-body", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Static("y confirm  ·  n / esc cancel", id="confirm-hint")
                yield Button("Cancel", id="confirm-cancel", compact=True)
                yield Button(self.confirm_label, id="confirm-ok", compact=True)

    def on_mount(self) -> None:
        # Enter presses the focused button: Cancel for destructive dialogs (safe default), else Confirm.
        default = "#confirm-cancel" if self.has_class("-destructive") else "#confirm-ok"
        self.query_one(default, Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-ok")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_toggle_focus(self) -> None:
        ok, cancel = self.query_one("#confirm-ok", Button), self.query_one("#confirm-cancel", Button)
        (cancel if self.focused is ok else ok).focus()
