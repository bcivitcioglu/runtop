"""runtop Textual app: wires backend → store → poller → main screen."""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from rich.console import RenderableType
from textual import events
from textual.app import App, SuspendNotSupported
from textual.binding import Binding
from typing_extensions import override

from runtop import config as configmod
from runtop.commands import ActionsProvider, ContainersProvider, TargetsProvider
from runtop.data.backend import ExecBackend, FixtureBackend, LiveBackend, ReadBackend
from runtop.data.capture import Capture
from runtop.data.format import human_bytes
from runtop.data.models import Container
from runtop.screens.confirm import ConfirmScreen
from runtop.screens.help import HelpScreen
from runtop.screens.main import MainScreen
from runtop.screens.overlay import DetailOverlay
from runtop.state.actions import ActionSpec, Disabled, Subject
from runtop.state.poller import Poller
from runtop.state.store import Store
from runtop.themes import CUSTOM_THEMES, next_theme

if TYPE_CHECKING:
    from runtop.widgets.detail import DetailPane


class RuntopApp(App[None]):
    TITLE = "runtop"
    CSS_PATH = "app.tcss"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (90, "-medium"), (120, "-wide")]
    BINDINGS = [
        Binding("question_mark", "show_help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
        Binding("t", "cycle_theme", "Theme", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]
    COMMANDS = App.COMMANDS | {TargetsProvider, ContainersProvider, ActionsProvider}
    COMMAND_PALETTE_BINDING = "ctrl+p"

    def __init__(self, backend: ReadBackend, *, target: str | None = None, quit_after: float | None = None,
                 local_interval: float = 2.0, remote_interval: float = 15.0, discover_interval: float = 3.0,
                 theme: str = "runtop", poll: bool = True, config: configmod.Config | None = None) -> None:
        super().__init__()
        self.config = config
        if config is not None:
            theme = config.theme
            target = target or config.last_target
        self.capture = Capture()
        self.backend = backend
        self.store = Store()
        self.poller = Poller(self, self.store, backend, self._store_changed, local_interval=local_interval,
                             remote_interval=remote_interval, discover_interval=discover_interval,
                             preferred=target)
        self.quit_after = quit_after
        self.poll = poll
        self._ui_ready = False
        self._pending_exit: tuple[None, int, RenderableType | None] | None = None
        self.collapsed: dict[str, set[str]] = {k: set(v) for k, v in (config.collapsed if config else {}).items()}
        self.cursors: dict[str, str] = {}
        self.exec_runner: Callable[[list[str]], int] = lambda argv: subprocess.run(argv, check=False).returncode
        for t in CUSTOM_THEMES:
            self.register_theme(t)
        self.theme = theme if theme in self.available_themes else "runtop"

    @classmethod
    def from_args(cls, *, demo: bool = False, snapshot: str | None = None, target: str | None = None,
                  quit_after: float | None = None) -> RuntopApp:
        if demo or snapshot:
            # Short simulated latency; one deliberate loading moment on the first remote connection.
            backend = FixtureBackend(snapshot, latency=0.12, slow={"ctx:prod-eu": 1.1}, action_latency=0.7,
                                     vm_latency=2.0, log_interval=0.6)
            return cls(backend, target=target, quit_after=quit_after, local_interval=1.0, remote_interval=6.0,
                       discover_interval=3.0)
        return cls(LiveBackend(), target=target, quit_after=quit_after, config=configmod.load())

    @override
    def get_default_screen(self) -> MainScreen:
        return MainScreen()

    @property
    def main(self) -> MainScreen | None:
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen):
                return screen
        return None

    def on_mount(self) -> None:
        if self.quit_after:
            self.set_timer(self.quit_after, self.exit)
        if self.poll:
            self.poller.start()

    def mark_ui_ready(self) -> None:
        """Called by MainScreen once its widget tree has mounted and rendered."""
        self._ui_ready = True
        if self._pending_exit is not None:
            result, return_code, message = self._pending_exit
            self._pending_exit = None
            super().exit(result, return_code, message)

    @override
    def exit(self, result: None = None, return_code: int = 0, message: RenderableType | None = None) -> None:
        # Exiting while the first screen is still mounting tears widgets down under Textual's
        # own pending Mount handlers (e.g. Tabs validating a tab that was already removed),
        # which crashes on the way out. Hold the exit until the UI is up, capped at 2 s.
        if self._ui_ready or not self.is_running:
            super().exit(result, return_code, message)
            return
        if self._pending_exit is None:
            self._pending_exit = (result, return_code, message)
            self.set_timer(2.0, self.mark_ui_ready)

    def _store_changed(self, key: str | None) -> None:
        screen = self.main
        if screen is not None:
            screen.sync(key)

    def on_app_blur(self, _: events.AppBlur) -> None:
        self.poller.blurred = True

    def on_app_focus(self, _: events.AppFocus) -> None:
        self.poller.blurred = False

    @property
    def is_demo(self) -> bool:
        return bool(getattr(self.backend, "demo", False))

    # ------------------------------------------------------------------ actions

    def current_subject(self) -> Subject:
        screen = self.main
        return screen.current_subject() if screen is not None else Subject()

    def subject_for(self, key: str | None, *, container: Container | None = None, focus: str = "middle") -> Subject:
        store = self.store
        return Subject(target=store.target(key), snapshot=store.snapshot(key), container=container, focus=focus,
                       demo=self.is_demo, pending=frozenset(store.pending))

    def refuse(self, spec: ActionSpec, subject: Subject, reason: str) -> None:
        self.notify(reason, title=spec.label(subject), severity="warning", timeout=3)

    def trigger(self, spec: ActionSpec, subject: Subject) -> None:
        """Run an action for the subject captured *now* (key press, button or palette)."""
        avail = spec.available(subject)
        if isinstance(avail, Disabled):
            self.refuse(spec, subject, avail.reason)
            return
        if spec.plan.method == "exec":
            self.run_exec(subject)
            return
        if spec.confirm is not None:
            title, body, label = spec.confirm(subject)

            def confirmed(ok: bool | None) -> None:
                if ok:
                    self._start_action(spec, subject)

            self.push_screen(ConfirmScreen(title, body, label, destructive=spec.destructive), confirmed)
            return
        self._start_action(spec, subject)

    @staticmethod
    def _pending_key(spec: ActionSpec, subject: Subject) -> str:
        assert subject.target is not None
        if spec.scope == "container" and subject.container is not None:
            return f"{subject.target.key}/{subject.container.id}"
        return subject.target.key

    def _start_action(self, spec: ActionSpec, subject: Subject) -> None:
        target = self.store.target(subject.key)
        if target is None or subject.target is None or target.endpoint != subject.target.endpoint:
            self.refuse(spec, subject, "target changed — choose the action again")
            return
        fresh = replace(subject, target=target, snapshot=self.store.snapshot(subject.key),
                        pending=frozenset(self.store.pending))
        available = spec.available(fresh)
        if isinstance(available, Disabled):
            self.refuse(spec, subject, available.reason)
            return
        pkey = self._pending_key(spec, subject)
        self.store.pending[pkey] = spec.plan.pending
        self._store_changed(subject.key)
        self.run_worker(partial(self._perform, spec, subject, pkey), group="actions", exit_on_error=False)

    async def _perform(self, spec: ActionSpec, subject: Subject, pkey: str) -> None:
        target = subject.target
        assert target is not None
        name = subject.container.name if spec.scope == "container" and subject.container else target.name
        # the registry names backend methods by string; backends without one refuse below
        method: Callable[..., Awaitable[object]] | None = getattr(self.backend, spec.plan.method, None)
        try:
            if method is None:
                raise RuntimeError("this backend cannot do that")
            if spec.scope == "project" and subject.group:
                results = []
                for c in subject.group.containers:
                    if (spec.plan.method == "start" and c.running) or (spec.plan.method == "stop" and not c.running):
                        results.append(f"{c.name}: already {c.state}")
                        continue
                    try:
                        await method(target, c.id)
                        results.append(f"{c.name}: {spec.plan.done.lower()}")
                    except Exception as e:
                        results.append(f"{c.name}: failed: {e}")
                self.notify("\n".join(results), title=f"{subject.group.label} · {target.name}", timeout=15)
                return
            args = (target, subject.container.id) if spec.scope == "container" and subject.container else (target,)
            result = await method(*args)
            message = f"{spec.plan.done} {name}"
            if spec.id == "images.prune":
                reclaimed = result if isinstance(result, int) else 0
                message = f"Reclaimed {human_bytes(reclaimed)} of dangling images"
            self.notify(message, title=target.name, timeout=3)
        except Exception as e:
            self.notify(str(e).strip() or type(e).__name__, title=f"{spec.label(subject)} failed", severity="error",
                        timeout=8)
        finally:
            self.store.pending.pop(pkey, None)
            if spec.scope == "machine":
                self.poller.discover_now()
            if target.key == self.store.selected:
                self.poller.fetch_now(force=True)
            self._store_changed(target.key)

    def run_exec(self, subject: Subject) -> None:
        """Open a shell in the container's own target, with the TUI suspended."""
        target, container = subject.target, subject.container
        assert target is not None and container is not None
        backend = self.backend
        argv = backend.exec_argv(target, container.id) if isinstance(backend, ExecBackend) else None
        if not argv:
            self.notify("exec is not available for this machine", severity="warning")
            return
        try:
            with self.suspend():
                code = self.exec_runner(argv)
        except SuspendNotSupported:
            code = self.exec_runner(argv)
        suffix = f" (exit {code})" if code not in (0, None) else ""
        self.notify(f"Shell in {container.name} closed{suffix}", title=target.name, timeout=3)
        if target.key == self.store.selected:
            self.poller.fetch_now(force=True)

    # ------------------------------------------------------------------ navigation helpers (palette)

    def go_to_target(self, key: str) -> None:
        self.poller.select(key)

    def go_to_container(self, key: str, cid: str) -> None:
        screen = self.main
        if screen is None:
            return
        if key != self.store.selected:
            self.poller.select(key)
        self.call_after_refresh(lambda: screen.jump_to_container(cid))

    def detail_overlay(self) -> DetailPane | None:
        for screen in self.screen_stack:
            if isinstance(screen, DetailOverlay) and screen.is_mounted:
                from runtop.widgets.detail import DetailPane

                return screen.query_one(DetailPane)
        return None

    def open_detail_overlay(self) -> None:
        if any(isinstance(s, DetailOverlay) for s in self.screen_stack):
            return
        self.push_screen(DetailOverlay(), lambda _: self._store_changed(self.store.selected))
        self.call_after_refresh(lambda: self._store_changed(self.store.selected))

    def action_show_help(self) -> None:
        if not any(isinstance(s, HelpScreen) for s in self.screen_stack):
            self.push_screen(HelpScreen())

    def action_refresh_now(self) -> None:
        self.poller.refresh()

    def action_cycle_theme(self) -> None:
        self.theme = next_theme(self.theme, set(self.available_themes))
        self.notify(f"Theme: {self.theme}", timeout=1.5)

    def persist(self) -> None:
        """Write preferences (live mode only)."""
        if self.config is None:
            return
        cfg = self.config
        cfg.theme = self.theme
        cfg.last_target = self.store.selected or cfg.last_target
        cfg.collapsed = {k: sorted(v) for k, v in self.collapsed.items() if v}
        screen = self.main
        if screen is not None and screen.is_mounted:
            cfg.sidebar_width, cfg.detail_width = screen.column_widths()
            cfg.section, cfg.sort = screen.section.value, screen.sort
        configmod.save(cfg)

    @override
    async def action_quit(self) -> None:
        self.persist()
        await super().action_quit()

    async def on_unmount(self) -> None:
        await self.capture.stop()
        if isinstance(self.backend, LiveBackend):
            await self.backend.aclose()
