"""Recording controls and bounded archive search, shared with the headless CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Log, Static
from typing_extensions import override

from runtop.appref import runtop_app
from runtop.data.backend import LogsBackend
from runtop.data.models import Container, Target
from runtop.data.recording import MIB, Record, RecordingPolicy, search, usage
from runtop.logs_cli import export_logs


class ArchiveScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close")]
    DEFAULT_CSS = """
    ArchiveScreen { align: center middle; background: $background 75%; }
    ArchiveScreen > VerticalScroll {
        width: 95%; max-width: 120; height: 95%; background: $panel;
        border: round $primary; padding: 1 2;
    }
    ArchiveScreen Static { height: auto; margin-bottom: 0; }
    ArchiveScreen Input { height: 1; border: none; padding: 0 1; margin-bottom: 1; background: $background; }
    ArchiveScreen Horizontal Input { width: 1fr; min-width: 0; }
    ArchiveScreen Horizontal { height: auto; }
    ArchiveScreen #archive-policy Input { width: 1fr; }
    ArchiveScreen Button { width: 1fr; min-width: 0; padding: 0 1; margin-right: 1; }
    ArchiveScreen Log { height: 8; min-height: 5; margin-top: 1; background: $background; }
    """

    def __init__(self, target: Target | None, containers: tuple[Container, ...]) -> None:
        super().__init__()
        self.target, self.containers = target, containers
        self.busy = False

    @override
    def compose(self) -> ComposeResult:
        cfg = runtop_app(self.app).config
        with VerticalScroll():
            yield Static("Log archives", markup=False)
            names = ", ".join(c.name for c in self.containers)
            scope = (f"{self.target.name} · {names}" if self.target and names
                     else "Select a container or project to record")
            yield Static(scope, id="archive-scope", markup=False)
            yield Input(value=cfg.log_directory if cfg else "", placeholder="Archive folder (required)",
                        id="archive-directory")
            yield Static("Total MiB · File MiB · Retention days", markup=False)
            with Horizontal(id="archive-policy"):
                yield Input(str(cfg.log_max_mib if cfg else 64), id="archive-max", type="integer")
                yield Input(str(cfg.log_file_mib if cfg else 8), id="archive-file", type="integer")
                yield Input(str(cfg.log_keep_days if cfg else 7), id="archive-days", type="number")
            yield Static("Recording is off until started and stops when runtop closes. Rotation removes only "
                         "runtop archive files in this folder. Retention runs while recording. "
                         "Saved application output may contain secrets.", markup=False)
            with Horizontal():
                yield Button("Record", id="archive-start", variant="primary")
                yield Button("Stop", id="archive-stop")
                yield Button("Export tail", id="archive-export")
                yield Button("Close", id="archive-close")
            yield Static("", id="archive-state", markup=False)
            yield Input(placeholder="Search saved logs (literal text, case insensitive) · Enter", id="archive-query")
            with Horizontal():
                yield Input(placeholder="Container ID or name (optional)", id="archive-container")
                yield Input(placeholder="Project (optional)", id="archive-project")
            yield Button("Search", id="archive-search")
            yield Static("", id="archive-results", markup=False)
            yield Log(id="archive-output", max_lines=500, auto_scroll=False)

    def on_mount(self) -> None:
        self.set_interval(0.5, self._status)
        self._status()

    def _status(self) -> None:
        app = runtop_app(self.app)
        capture = app.capture
        self.query_one("#archive-start", Button).disabled = self.busy or capture.active or not self.containers
        self.query_one("#archive-export", Button).disabled = self.busy or capture.active or not self.containers
        self.query_one("#archive-stop", Button).disabled = not capture.active
        if capture.active or capture.written or capture.error:
            text = ("● Recording" if capture.active else "Recording stopped")
            text += f" · {capture.written} records · {capture.truncated} truncated · {capture.directory}"
            if capture.error:
                text += f"\n{capture.error}"
            self.query_one("#archive-state", Static).update(text)

    def _directory(self) -> Path:
        value = self.query_one("#archive-directory", Input).value.strip()
        if not value:
            raise ValueError("Choose a folder for the archives")
        return Path(value).expanduser()

    def _policy(self) -> RecordingPolicy:
        policy = RecordingPolicy(int(self.query_one("#archive-max", Input).value) * MIB,
                                 int(self.query_one("#archive-file", Input).value) * MIB,
                                 float(self.query_one("#archive-days", Input).value))
        policy.validate()
        return policy

    def _remember(self, directory: Path, policy: RecordingPolicy) -> None:
        cfg = runtop_app(self.app).config
        if cfg is not None:
            cfg.log_directory = str(directory)
            cfg.log_max_mib, cfg.log_file_mib = policy.max_bytes // MIB, policy.file_bytes // MIB
            cfg.log_keep_days = policy.keep_days

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "archive-close":
            self.action_close()
        elif event.button.id == "archive-search":
            self.find()
        elif event.button.id == "archive-stop":
            self.stop_recording()
        elif event.button.id in ("archive-start", "archive-export"):
            self.begin(event.button.id == "archive-export")

    @work(group="archive-capture", exclusive=True, exit_on_error=False)
    async def begin(self, export: bool) -> None:
        self.busy = True
        self._status()
        try:
            app = runtop_app(self.app)
            directory, policy = self._directory(), self._policy()
            if self.target is None or not isinstance(app.backend, LogsBackend):
                raise ValueError("Log capture is unavailable")
            current = app.store.target(self.target.key)
            if current is None or current.endpoint != self.target.endpoint:
                raise ValueError("Target changed; reopen log archives")
            self._remember(directory, policy)
            if export:
                count = await export_logs(app.backend, self.target, self.containers, directory, policy)
                self.query_one("#archive-state", Static).update(f"Exported {count} records to {directory}")
            else:
                await app.capture.start(app.backend, self.target, self.containers, directory, policy)
        except (OSError, ValueError, RuntimeError) as e:
            self.query_one("#archive-state", Static).update(str(e))
        finally:
            self.busy = False
            self._status()

    @work(group="archive-stop", exclusive=True, exit_on_error=False)
    async def stop_recording(self) -> None:
        await runtop_app(self.app).capture.stop()
        self._status()

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.find()

    @work(group="archive-search", exclusive=True, exit_on_error=False)
    async def find(self) -> None:
        result = self.query_one("#archive-results", Static)
        result.update("Searching…")
        try:
            directory = self._directory()
            query = self.query_one("#archive-query", Input).value
            container = self.query_one("#archive-container", Input).value
            project = self.query_one("#archive-project", Input).value

            def read() -> tuple[list[Record], dict[str, int]]:
                return list(search(directory, query, ignore_case=True, container=container,
                                   project=project, limit=500)), usage(directory)

            rows, size = await asyncio.to_thread(read)
            log = self.query_one("#archive-output", Log)
            log.clear()
            log.write_lines(json.dumps(f"{r.time} [{r.name}/{r.stream}] {r.text}", ensure_ascii=False)[1:-1]
                            for r in rows)
            result.update(f"{len(rows)} matches (limit 500) · {size['files']} files · {size['bytes'] / MIB:.2f} MiB")
        except (OSError, ValueError) as e:
            result.update(str(e))

    def action_close(self) -> None:
        if self.busy:
            self.notify("Wait for the export to finish", severity="warning")
            return
        self.dismiss()
