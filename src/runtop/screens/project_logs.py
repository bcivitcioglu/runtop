"""Bounded, interleaved logs from the existing members of a Compose project."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static
from typing_extensions import override

from runtop.data.backend import LogsBackend
from runtop.data.engine import LogLine
from runtop.data.models import Container, Target
from runtop.widgets.log_view import LogView


async def project_lines(backend: LogsBackend, target: Target,
                        containers: tuple[Container, ...]) -> AsyncGenerator[LogLine, None]:
    """One bounded queue provides backpressure; closing the view joins every reader."""
    queue: asyncio.Queue[LogLine | None] = asyncio.Queue(maxsize=500)

    async def read(c: Container) -> None:
        try:
            async for line in backend.logs(target, c.id, tail=200, follow=c.running):
                await queue.put(LogLine(line.stream, f"[{c.service or c.name}] {line.text}"))
        except Exception as e:
            await queue.put(LogLine("stderr", f"[{c.service or c.name}] logs unavailable: {e}"))
        finally:
            # Cancellation must not block on a full queue whose consumer has closed.
            task = asyncio.current_task()
            if task is not None and not task.cancelling():
                await queue.put(None)

    tasks = [asyncio.create_task(read(c)) for c in containers]
    try:
        remaining = len(tasks)
        while remaining:
            item = await queue.get()
            if item is None:
                remaining -= 1
            else:
                yield item
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class ProjectLogsScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close")]
    DEFAULT_CSS = """
    ProjectLogsScreen { background: $background; }
    ProjectLogsScreen > Static { height: 1; padding: 0 1; color: $primary; }
    ProjectLogsScreen > LogView { height: 1fr; padding: 1; }
    """

    def __init__(self, backend: LogsBackend, target: Target, project: str, containers: tuple[Container, ...]) -> None:
        super().__init__()
        self.backend, self.target, self.project, self.containers = backend, target, project, containers

    @override
    def compose(self) -> ComposeResult:
        yield Static(f"{self.project} · {self.target.name} · {len(self.containers)} services · esc back", markup=False)
        yield LogView()

    def on_mount(self) -> None:
        self.query_one(LogView).show(f"{self.target.key}/{self.project}",
                                    lambda: project_lines(self.backend, self.target, self.containers))

    def action_close(self) -> None:
        self.query_one(LogView).stop()
        self.dismiss()
