"""Opt-in JSONL archives with one writer, bounded files and a directory-wide budget."""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

MIB = 1024 * 1024
MAX_RECORD = 64 * 1024
_ARCHIVE = re.compile(r"runtop-[0-9]{20}-[a-f0-9]{32}\.jsonl\Z")


@dataclass(frozen=True)
class RecordingPolicy:
    max_bytes: int = 64 * MIB
    file_bytes: int = 8 * MIB
    keep_days: float = 7

    def validate(self) -> None:
        if not MAX_RECORD <= self.file_bytes <= self.max_bytes <= 1024 * 1024 * MIB:
            raise ValueError("File size must be at least 64 KiB and no larger than the total budget (maximum 1 TiB)")
        if not 0 < self.keep_days <= 3650:
            raise ValueError("Retention must be between 0 and 3650 days")


@dataclass(frozen=True)
class Record:
    time: str
    target: str
    container: str
    name: str
    project: str
    service: str
    stream: str
    text: str
    truncated: bool = False
    version: int = 1

    @classmethod
    def now(cls, target: str, container: str, name: str, project: str, service: str,
            stream: str, text: str) -> Record:
        return cls(datetime.now(UTC).isoformat(), target, container, name, project, service, stream, text)

    def encode(self) -> bytes:
        data = asdict(self)
        raw = (json.dumps(data, ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_RECORD:
            # Bound even pathological escapes, names and multibyte output.
            for key, value in data.items():
                if isinstance(value, str):
                    data[key] = value[:MAX_RECORD // 16]
            data["truncated"] = True
            raw = (json.dumps(data, ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_RECORD:
            raise ValueError("Log record metadata exceeds the record limit")
        return raw


def archive_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    files = []
    for p in directory.iterdir():
        if _ARCHIVE.fullmatch(p.name):
            if p.is_symlink():
                raise ValueError(f"Archive must not be a symlink: {p.name}")
            if p.is_file():
                files.append(p)
    return sorted(files)


def usage(directory: Path) -> dict[str, int]:
    files = archive_files(directory)
    sizes = []
    for p in files:
        try:
            sizes.append(p.stat().st_size)
        except FileNotFoundError:  # the writer may rotate while a reader checks usage
            pass
    return {"files": len(sizes), "bytes": sum(sizes)}


class ArchiveWriter:
    """Call from a worker thread. Only exact archive filenames are ever removed."""

    def __init__(self, directory: Path, policy: RecordingPolicy) -> None:
        policy.validate()
        self.directory, self.policy = directory.expanduser().resolve(), policy
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / ".runtop-record.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock: BinaryIO = os.fdopen(fd, "rb+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError("Another recording or export is using this folder") from None
        self.file: BinaryIO | None = None
        self.path: Path | None = None
        self.size = 0
        self.written = 0
        self.truncated = 0
        self._last_prune = 0.0
        try:
            self.prune()
        except BaseException:
            self.close()
            raise

    def prune(self, reserve: int = 0) -> None:
        entries = []
        now = time.time()
        for p in archive_files(self.directory):
            st = p.stat()
            if p != self.path and now - st.st_mtime > self.policy.keep_days * 86400:
                p.unlink()
            else:
                entries.append((p, st.st_size))
        total = sum(size for _, size in entries)
        for p, size in entries:
            if total + reserve <= self.policy.max_bytes:
                break
            if p != self.path:
                p.unlink()
                total -= size
        if total + reserve > self.policy.max_bytes:
            raise OSError("Archive budget exhausted")
        self._last_prune = time.monotonic()

    def write(self, record: Record) -> None:
        raw = record.encode()
        if self.file is not None and self.size + len(raw) > self.policy.file_bytes:
            self.file.close()
            self.file = None
            self.path = None
        # A scan per batch/rotation, with the next file's complete budget reserved.
        if self.file is None:
            self.prune(self.policy.file_bytes)
            self.path = self.directory / f"runtop-{time.time_ns():020d}-{uuid.uuid4().hex}.jsonl"
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            self.file = os.fdopen(fd, "wb", buffering=0)
            self.size = 0
        elif time.monotonic() - self._last_prune > 30:
            self.prune()
        self.file.write(raw)
        self.size += len(raw)
        self.written += 1
        self.truncated += int(b'"truncated": true' in raw)

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
        if not self.lock.closed:
            self.lock.close()


def records(directory: Path) -> Iterator[Record]:
    """Stream records in file order; tolerate a trailing write or interrupted last line."""
    for path in archive_files(directory):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        with os.fdopen(fd, "rb") as file:
            while raw := file.readline(MAX_RECORD + 1):
                if len(raw) > MAX_RECORD:
                    raise ValueError(f"Oversized archive record: {path.name}")
                if not raw.endswith(b"\n"):
                    continue
                try:
                    d = json.loads(raw)
                    if not isinstance(d, dict) or d.get("version") != 1:
                        continue
                    keys = ("time", "target", "container", "name", "project", "service", "stream", "text")
                    if not all(isinstance(d.get(k), str) for k in keys):
                        continue
                    yield Record(*(d[k] for k in keys), truncated=d.get("truncated") is True)
                except (ValueError, TypeError):
                    continue


def search(directory: Path, query: str = "", *, ignore_case: bool = False, container: str = "",
           project: str = "", since: str = "", limit: int = 200) -> Iterator[Record]:
    if not 1 <= limit <= 10000:
        raise ValueError("Result limit must be between 1 and 10000")
    after = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    if after is not None and after.tzinfo is None:
        raise ValueError("--since needs a timezone, for example 2026-01-01T12:00:00Z")
    needle = query.casefold() if ignore_case else query
    count = 0
    for record in records(directory):
        if container and container not in (record.container, record.name):
            continue
        if project and record.project != project:
            continue
        if after is not None:
            try:
                if datetime.fromisoformat(record.time.replace("Z", "+00:00")) < after:
                    continue
            except (ValueError, TypeError):
                continue
        haystack = record.text.casefold() if ignore_case else record.text
        if needle not in haystack:
            continue
        yield record
        count += 1
        if count >= limit:
            break
