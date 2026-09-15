"""Scriptable listings using the shared snapshot contract."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime

from runtop.data.backend import FixtureBackend, LiveBackend
from runtop.data.models import DaemonState, TargetSnapshot, dumps_document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runtop ps", description=__doc__)
    parser.add_argument("-a", "--all", action="store_true", help="include non-running containers")
    parser.add_argument("-t", "--target", help="select a target by key or name")
    parser.add_argument("--contexts", action="store_true", help="include remote targets")
    parser.add_argument("--no-contexts", action="store_true", help="skip context discovery in live mode")
    parser.add_argument("--images", action="store_true", help="print images; JSON retains the snapshot schema")
    parser.add_argument("--stats", action="store_true", help="request local CPU and memory samples")
    parser.add_argument("--json", action="store_true", help="write snapshot JSON to standard output")
    parser.add_argument("--demo", action="store_true", help="use bundled data without contacting engines")
    parser.add_argument("--snapshot", help="read a saved snapshot file")
    return parser


def _human(n: int) -> str:
    for size, suffix in ((1 << 30, "G"), (1 << 20, "M"), (1 << 10, "K")):
        if n >= size:
            return f"{n / size:.1f}{suffix}"
    return f"{n}B"


async def listing(args: argparse.Namespace) -> int:
    fixture = bool(args.demo or args.snapshot)
    backend = FixtureBackend(args.snapshot, jitter=False) if fixture else LiveBackend(
        include_contexts=not args.no_contexts)
    try:
        targets = await backend.discover()
        if args.target:
            targets = [t for t in targets if args.target in (t.key, t.name)]
            if not targets:
                print(f"unknown target: {args.target}", file=sys.stderr)
                return 2
        elif not args.contexts:
            targets = [t for t in targets if not t.is_remote]
        failed = bool(getattr(backend, "discovery_error", None))
        snapshots: list[TargetSnapshot] = []
        for target in targets:
            if isinstance(backend, LiveBackend):
                if args.stats and not target.is_remote:
                    await backend.fetch(target, with_images=False)
                    await asyncio.sleep(0.5)
                snap = await backend.fetch(target, with_stats=args.stats)
            else:
                snap = await backend.fetch(target)
            if snap.state is DaemonState.UNREACHABLE or snap.images_error:
                failed = True
                print(f"{target.key}: {snap.error or snap.images_error}", file=sys.stderr)
            if not args.all:
                snap = replace(snap, containers=tuple(c for c in snap.containers if c.running))
            snapshots.append(snap)
        if args.json:
            generated = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            print(dumps_document(snapshots, generated), end="")
        else:
            _table(snapshots, images=args.images, stats=args.stats)
        return 1 if not targets else 3 if failed else 0
    except Exception as error:
        print(f"runtop: {error}", file=sys.stderr)
        return 1
    finally:
        if isinstance(backend, LiveBackend):
            await backend.aclose()


def _table(snapshots: list[TargetSnapshot], *, images: bool, stats: bool) -> None:
    if images:
        print("TARGET\tIMAGE\tID\tSIZE")
    else:
        print("TARGET\tNAME\tID\tIMAGE\tSTATUS\tPORTS" + ("\tCPU\tMEM" if stats else ""))
    for snap in snapshots:
        if images:
            for image in snap.images:
                print(f"{snap.key}\t{image.ref}\t{image.id}\t{_human(image.size_bytes)}")
        else:
            for container in snap.containers:
                metrics = ""
                if stats:
                    sample = container.stats
                    cpu = f"{sample.cpu_percent:.1f}%" if sample and sample.cpu_percent is not None else "—"
                    mem = _human(sample.mem_bytes) if sample else "—"
                    metrics = f"\t{cpu}\t{mem}"
                print(f"{snap.key}\t{container.name}\t{container.id}\t{container.image}\t"
                      f"{container.status}\t{','.join(container.ports)}{metrics}")


def main(argv: list[str]) -> int:
    return asyncio.run(listing(_parser().parse_args(argv)))
