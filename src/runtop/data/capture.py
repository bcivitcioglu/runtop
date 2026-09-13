"""Foreground recording sessions. Disk work never runs on Textual's event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TypeVar

from runtop.data.backend import LogsBackend
from runtop.data.models import Container, Target
from runtop.data.recording import MAX_RECORD, ArchiveWriter, Record, RecordingPolicy

T = TypeVar("T")


async def disk_call(fn: Callable[[], T]) -> T:
    """Join an in-flight disk write before cancellation can close its file."""
    task = asyncio.create_task(asyncio.to_thread(fn))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def open_archive(directory: Path, policy: RecordingPolicy) -> ArchiveWriter:
    task = asyncio.create_task(asyncio.to_thread(ArchiveWriter, directory, policy))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        writer = await task
        await asyncio.to_thread(writer.close)
        raise


class Capture:
    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.directory: Path | None = None
        self.error = ""
        self.written = 0
        self.truncated = 0
        self.scope = ""
        self._ready: asyncio.Future[None] | None = None

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, backend: LogsBackend, target: Target, containers: tuple[Container, ...],
                    directory: Path, policy: RecordingPolicy, *, tail: int = 0) -> None:
        if self.active:
            raise ValueError("A recording is already active; stop it before starting another")
        if not containers:
            raise ValueError("Select a container or a Compose project to record")
        if len(containers) > 128:
            raise ValueError("A recording can follow at most 128 containers")
        policy.validate()
        self.directory = directory.expanduser().resolve()
        self.error, self.written, self.truncated = "", 0, 0
        self.scope = f"{target.name} · {len(containers)} container(s)"
        self._ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._run(backend, target, containers, policy, tail))
        await self._ready

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self, backend: LogsBackend, target: Target, containers: tuple[Container, ...],
                   policy: RecordingPolicy, tail: int) -> None:
        queue: asyncio.Queue[Record | None] = asyncio.Queue(maxsize=256)
        writer: ArchiveWriter | None = None
        readers: list[asyncio.Task[None]] = []

        async def read(c: Container) -> None:
            try:
                async for line in backend.logs(target, c.id, tail=tail, follow=c.running):
                    record = Record.now(target.key, c.id, c.name, c.project, c.service, line.stream,
                                        line.text[:MAX_RECORD])
                    if len(line.text) > MAX_RECORD:
                        record = replace(record, truncated=True)
                    try:
                        queue.put_nowait(record)
                    except asyncio.QueueFull:
                        self.error = "Recording stopped: disk cannot keep up (queue full)"
                        if self.task is not None:
                            self.task.cancel()
                        return
            except Exception as e:
                self.error = f"{c.name}: {e}"
            finally:
                task = asyncio.current_task()
                if task is not None and not task.cancelling():
                    await queue.put(None)

        def write_batch(batch: list[Record]) -> None:
            assert writer is not None
            for record in batch:
                writer.write(record)
            self.written, self.truncated = writer.written, writer.truncated

        try:
            assert self.directory is not None
            # Let open complete before signalling readiness or closing during cancellation.
            writer = await open_archive(self.directory, policy)
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)
            readers = [asyncio.create_task(read(c)) for c in containers]
            remaining = len(readers)
            while remaining or not queue.empty():
                try:
                    first = await asyncio.wait_for(queue.get(), 1.0)
                except TimeoutError:
                    await disk_call(writer.prune)
                    continue
                batch = []
                for item in [first]:
                    if item is None:
                        remaining -= 1
                    else:
                        batch.append(item)
                while not queue.empty() and len(batch) < 100:
                    item = queue.get_nowait()
                    if item is None:
                        remaining -= 1
                    else:
                        batch.append(item)
                await disk_call(partial(write_batch, batch))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error = str(e) or type(e).__name__
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if writer is not None:
                try:
                    if not self.error:
                        batch = []
                        while not queue.empty():
                            item = queue.get_nowait()
                            if item is not None:
                                batch.append(item)
                        await disk_call(partial(write_batch, batch))
                except Exception as e:
                    self.error = str(e)
                finally:
                    await disk_call(writer.close)
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(ValueError(self.error or "Recording cancelled"))
