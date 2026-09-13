"""Read, export and record application logs without opening the TUI."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import sys
from dataclasses import asdict
from functools import partial
from pathlib import Path

from runtop.data.backend import FixtureBackend, LiveBackend, LogsBackend
from runtop.data.capture import Capture, disk_call, open_archive
from runtop.data.models import Container, DaemonState, Target
from runtop.data.recording import MIB, Record, RecordingPolicy, search, usage


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runtop logs", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("record", "export", "search", "status"):
        s = sub.add_parser(command)
        s.add_argument("--directory", required=True, type=Path, help="user-selected archive folder")
        if command in ("search", "status"):
            s.add_argument("--json", action="store_true", help="machine-readable output")
        if command == "search":
            s.add_argument("query", nargs="?", default="", help="literal text to find")
            s.add_argument("-i", "--ignore-case", action="store_true")
            s.add_argument("--container", default="", help="exact container ID or name")
            s.add_argument("--project", default="")
            s.add_argument("--since", default="", help="ISO timestamp with timezone")
            s.add_argument("--limit", type=int, default=200)
        if command in ("record", "export"):
            s.add_argument("--target", required=True, help="target key, e.g. lima:docker")
            scope = s.add_mutually_exclusive_group(required=True)
            scope.add_argument("--container", action="append", default=[], help="ID or name; repeat for several")
            scope.add_argument("--project", help="existing Compose project")
            s.add_argument("--max-mib", type=int, default=64, help="total archive budget")
            s.add_argument("--file-mib", type=int, default=8, help="rotate at this file size")
            s.add_argument("--keep-days", type=float, default=7)
            s.add_argument("--tail", type=int, default=0 if command == "record" else 200)
            s.add_argument("--demo", action="store_true", help="synthetic logs; no daemon")
            if command == "record":
                s.add_argument("--duration", type=float, default=0, help="seconds; 0 records until Ctrl+C")
    return p


async def select(backend: LiveBackend | FixtureBackend, key: str, names: list[str],
                 project: str | None) -> tuple[Target, tuple[Container, ...]]:
    targets = await backend.discover()
    target = next((t for t in targets if t.key == key), None)
    if target is None:
        raise ValueError(f"Unknown target {key!r}")
    snap = await backend.fetch(target, with_stats=False, with_images=False) if isinstance(backend, LiveBackend) \
        else await backend.fetch(target)
    if snap.state is not DaemonState.OK or snap.stale:
        raise ValueError(snap.error or f"Target unavailable: {snap.state}")
    if project is not None:
        containers = tuple(c for c in snap.containers if c.project == project)
    else:
        chosen = {}
        for name in names:
            matches = [c for c in snap.containers if c.id == name or c.name == name]
            if len(matches) != 1:
                raise ValueError(f"Container {name!r} is missing or ambiguous")
            chosen[matches[0].id] = matches[0]
        containers = tuple(chosen.values())
    if not containers:
        raise ValueError("No containers match the requested scope")
    return target, containers


async def export_logs(backend: LogsBackend, target: Target, containers: tuple[Container, ...], directory: Path,
                      policy: RecordingPolicy, tail: int = 200) -> int:
    writer = await open_archive(directory, policy)
    try:
        for c in containers:
            async for line in backend.logs(target, c.id, tail=tail, follow=False):
                record = Record.now(target.key, c.id, c.name, c.project, c.service, line.stream, line.text)
                await disk_call(partial(writer.write, record))
        return writer.written
    finally:
        await disk_call(writer.close)


async def capture_command(args: argparse.Namespace) -> int:
    if args.tail < 0 or args.tail > 10000:
        raise ValueError("--tail must be between 0 and 10000")
    policy = RecordingPolicy(args.max_mib * MIB, args.file_mib * MIB, args.keep_days)
    policy.validate()
    backend = FixtureBackend(log_interval=0.02) if args.demo else LiveBackend()
    capture = Capture()
    try:
        target, containers = await select(backend, args.target, args.container, args.project)
        if args.command == "export":
            count = await export_logs(backend, target, containers, args.directory, policy, args.tail)
            print(json.dumps({"records": count, "directory": str(args.directory)}))
            return 0
        if not math.isfinite(args.duration) or args.duration < 0:
            raise ValueError("--duration must be a finite nonnegative number")
        await capture.start(backend, target, containers, args.directory, policy, tail=args.tail)
        print(f"Recording {capture.scope} to {capture.directory}; Ctrl+C stops", file=sys.stderr)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        waiter = asyncio.create_task(stop.wait())
        try:
            assert capture.task is not None
            await asyncio.wait({capture.task, waiter}, timeout=args.duration or None,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            await capture.stop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
        print(json.dumps({"records": capture.written, "truncated": capture.truncated,
                          "error": capture.error}), file=sys.stderr)
        return 1 if capture.error else 0
    finally:
        await capture.stop()
        if isinstance(backend, LiveBackend):
            await backend.aclose()


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            result = usage(args.directory.expanduser())
            print(json.dumps(result) if args.json else f"{result['files']} files · {result['bytes']} bytes")
            return 0
        if args.command == "search":
            found = False
            for record in search(args.directory.expanduser(), args.query, ignore_case=args.ignore_case,
                                 container=args.container, project=args.project, since=args.since, limit=args.limit):
                found = True
                if args.json:
                    print(json.dumps(asdict(record), ensure_ascii=False))
                else:
                    # Escape control characters so an archive cannot send terminal commands.
                    text = json.dumps(record.text, ensure_ascii=False)[1:-1]
                    name = json.dumps(record.name, ensure_ascii=False)[1:-1]
                    timestamp = json.dumps(record.time, ensure_ascii=False)[1:-1]
                    stream = json.dumps(record.stream, ensure_ascii=False)[1:-1]
                    print(f"{timestamp} [{name}/{stream}] {text}")
            return 0 if found else 1
        return asyncio.run(capture_command(args))
    except (OSError, ValueError) as e:
        print(f"runtop logs: {e}", file=sys.stderr)
        return 2
