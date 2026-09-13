from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from runtop.data.backend import FixtureBackend
from runtop.data.capture import Capture
from runtop.data.recording import (
    MAX_RECORD,
    ArchiveWriter,
    Record,
    RecordingPolicy,
    archive_files,
    records,
    search,
    usage,
)
from runtop.logs_cli import main
from runtop.screens.archive import ArchiveScreen

from .helpers import make_app, wait_for


def line(text: str = "hello") -> Record:
    return Record.now("lima:docker", "abc123", "api", "shop", "api", "stdout", text)


def test_rotation_budget_retention_and_unrelated_files(tmp_path: Path) -> None:
    policy = RecordingPolicy(2 * MAX_RECORD, MAX_RECORD, 1)
    unrelated = tmp_path / "application.jsonl"
    unrelated.write_text("keep me")
    writer = ArchiveWriter(tmp_path, policy)
    for i in range(100):
        writer.write(line(f"{i}:" + "x" * 12000))
        assert usage(tmp_path)["bytes"] <= policy.max_bytes
        assert all(p.stat().st_size <= policy.file_bytes for p in archive_files(tmp_path))
    writer.close()
    kept = list(records(tmp_path))
    assert kept and kept[-1].text.startswith("99:") and len(kept) < 100
    for p in archive_files(tmp_path):
        expired = time.time() - 172800
        os.utime(p, (expired, expired))
    next_writer = ArchiveWriter(tmp_path, policy)
    next_writer.close()
    assert not archive_files(tmp_path)
    assert unrelated.read_text() == "keep me"


def test_one_writer_per_directory_and_private_files(tmp_path: Path) -> None:
    first = ArchiveWriter(tmp_path, RecordingPolicy())
    try:
        with pytest.raises(ValueError, match="Another recording"):
            ArchiveWriter(tmp_path, RecordingPolicy())
        first.write(line())
        assert archive_files(tmp_path)[0].stat().st_mode & 0o777 == 0o600
    finally:
        first.close()
    ArchiveWriter(tmp_path, RecordingPolicy()).close()


def test_symlinks_are_never_followed_or_removed(tmp_path: Path) -> None:
    victim = tmp_path / "precious"
    victim.write_text("keep")
    alias = tmp_path / ("runtop-" + "0" * 20 + "-" + "a" * 32 + ".jsonl")
    alias.symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        ArchiveWriter(tmp_path, RecordingPolicy())
    assert victim.read_text() == "keep" and alias.is_symlink()
    alias.unlink()
    (tmp_path / ".runtop-record.lock").unlink()
    (tmp_path / ".runtop-record.lock").symlink_to(victim)
    with pytest.raises(OSError):
        ArchiveWriter(tmp_path, RecordingPolicy())
    assert victim.read_text() == "keep"


def test_search_filters_partial_lines_and_oversized_text(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path, RecordingPolicy())
    writer.write(replace(line("Error: database unavailable"), time="2026-01-02T12:00:00+00:00"))
    writer.write(line("a" * (MAX_RECORD * 2)))
    writer.close()
    matches = list(search(tmp_path, "error", ignore_case=True, project="shop", container="api",
                          since="2026-01-01T00:00:00Z"))
    assert len(matches) == 1
    assert list(records(tmp_path))[-1].truncated
    assert not list(search(tmp_path, "Error", container="other"))
    with archive_files(tmp_path)[-1].open("ab") as file:
        file.write(b'{"version":1')
    assert len(list(records(tmp_path))) == 2
    with pytest.raises(ValueError, match="timezone"):
        list(search(tmp_path, since="2026-01-01"))
    with pytest.raises(ValueError, match="limit"):
        list(search(tmp_path, limit=0))


async def test_capture_close_flushes_and_releases_lock(tmp_path: Path) -> None:
    backend = FixtureBackend(log_interval=0.005)
    target = (await backend.discover())[0]
    snap = await backend.fetch(target)
    capture = Capture()
    await capture.start(backend, target, snap.containers[:2], tmp_path, RecordingPolicy())
    for _ in range(100):
        if capture.written >= 2:
            break
        await asyncio.sleep(0.01)
    assert capture.active and capture.written >= 2
    await capture.stop()
    assert not capture.active and not capture.error
    assert len(list(records(tmp_path))) == capture.written
    ArchiveWriter(tmp_path, RecordingPolicy()).close()


async def test_failed_capture_can_be_restarted(tmp_path: Path) -> None:
    backend = FixtureBackend()
    target = (await backend.discover())[0]
    snap = await backend.fetch(target)
    capture = Capture()
    locked = ArchiveWriter(tmp_path, RecordingPolicy())
    with pytest.raises(ValueError, match="Another recording"):
        await capture.start(backend, target, snap.containers[:1], tmp_path, RecordingPolicy())
    locked.close()
    await capture.start(backend, target, snap.containers[:1], tmp_path, RecordingPolicy())
    await capture.stop()
    assert not capture.active


def test_cli_export_search_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = str(tmp_path)
    assert main(["export", "--demo", "--target", "lima:docker", "--project", "shop",
                 "--tail", "3", "--directory", directory]) == 0
    assert json.loads(capsys.readouterr().out)["records"] > 0
    assert main(["search", "--directory", directory, "--json", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "shop"
    assert main(["search", "not-present-text", "--directory", directory]) == 1
    assert main(["status", "--directory", directory, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["bytes"] > 0
    assert main(["export", "--demo", "--target", "lima:docker", "--container", "missing",
                 "--directory", directory]) == 2


async def test_archive_screen_record_search_and_app_shutdown(tmp_path: Path) -> None:
    from textual.widgets import Input, Static

    app = make_app()
    async with app.run_test(size=(100, 36)) as pilot:
        assert await wait_for(pilot, lambda: bool(app.store.snapshots))
        target = app.store.target(app.store.selected)
        assert target is not None
        snap = app.store.snapshot(target.key)
        assert snap is not None
        app.push_screen(ArchiveScreen(target, snap.containers[:1]))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ArchiveScreen)
        screen.query_one("#archive-directory", Input).value = str(tmp_path)
        screen.begin(False)
        assert await wait_for(pilot, lambda: app.capture.written > 0)
        screen.find()
        assert await wait_for(pilot, lambda: "matches" in str(screen.query_one("#archive-results", Static).content))
        screen.action_close()
        await pilot.pause()
        assert app.capture.active  # survives closing the controls and changing selection
    assert not app.capture.active
    ArchiveWriter(tmp_path, RecordingPolicy()).close()


async def test_full_queue_stops_capture_with_visible_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import AsyncIterator

    from runtop.data.engine import LogLine
    from runtop.data.models import Target

    backend = FixtureBackend()
    target = (await backend.discover())[0]
    snap = await backend.fetch(target)

    async def burst(target: Target, cid: str, *, tail: int = 0, follow: bool = False) -> AsyncIterator[LogLine]:
        for i in range(10000):
            yield LogLine("stdout", str(i))

    monkeypatch.setattr(backend, "logs", burst)
    capture = Capture()
    await capture.start(backend, target, snap.containers[:1], tmp_path, RecordingPolicy())
    assert capture.task is not None
    await asyncio.wait_for(asyncio.shield(capture.task), 2)
    assert not capture.active and "queue full" in capture.error
    ArchiveWriter(tmp_path, RecordingPolicy()).close()


async def test_cancelling_disk_call_joins_the_write() -> None:
    import threading

    from runtop.data.capture import disk_call

    started, release, completed = threading.Event(), threading.Event(), threading.Event()

    def write() -> None:
        started.set()
        release.wait(2)
        completed.set()

    task = asyncio.create_task(disk_call(write))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
