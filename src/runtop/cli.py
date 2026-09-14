"""runtop: a TUI Docker workspace in the style of a desktop app, for Lima VMs."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from runtop import __version__


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runtop", description=__doc__)
    p.add_argument("--version", action="version", version=f"runtop {__version__} (full)")
    p.add_argument("--edition", action="version", version="full")
    p.add_argument("--doctor", action="store_true", help="diagnose discovery and daemon connectivity")
    p.add_argument("--demo", action="store_true", help="use the bundled demo snapshot instead of real daemons")
    p.add_argument("--snapshot", metavar="FILE", help="use a runtop.snapshot/v1 file instead of real daemons")
    p.add_argument("--target", metavar="KEY", help="select this target on start (e.g. lima:docker, ctx:prod)")
    p.add_argument("--dump", metavar="KEY", help="print the normalized snapshot JSON for one target and exit")
    p.add_argument("--keys", metavar="TOKENS", help='scripted keys for recordings, e.g. "down enter wait:2 tab"')
    p.add_argument("--quit-after", metavar="SECS", type=float, help="exit after N seconds (recordings)")
    return p


async def dump(key: str, *, demo: bool, snapshot: str | None) -> int:
    from runtop.data.backend import FixtureBackend, LiveBackend
    from runtop.data.models import dumps_document

    backend: FixtureBackend | LiveBackend
    backend = FixtureBackend(snapshot, jitter=False) if demo or snapshot else LiveBackend()
    try:
        targets = await backend.discover()
        target = next((t for t in targets if t.key == key), None)
        if target is None:
            known = ", ".join(t.key for t in targets) or "none"
            print(f"runtop: unknown target {key!r} (known: {known})", file=sys.stderr)
            return 2
        if isinstance(backend, LiveBackend):
            # stream=false without one-shot: the daemon fills precpu_stats, so CPU% is ready now.
            snap = await backend.fetch(target, one_shot=False)
        else:
            snap = await backend.fetch(target)
    except Exception as e:  # CLI boundary
        print(f"runtop: {e}", file=sys.stderr)
        return 1
    finally:
        if isinstance(backend, LiveBackend):
            await backend.aclose()
    generated = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sys.stdout.write(dumps_document([snap], generated))
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "ps":
        from runtop.ps_cli import main as ps_main

        raise SystemExit(ps_main(argv[1:]))
    if argv and argv[0] == "logs":
        from runtop.logs_cli import main as logs_main

        raise SystemExit(logs_main(argv[1:]))
    args = _parser().parse_args(argv)
    if args.doctor:
        from runtop.data.doctor import diagnose

        raise SystemExit(asyncio.run(diagnose()))
    if args.dump:
        raise SystemExit(asyncio.run(dump(args.dump, demo=args.demo, snapshot=args.snapshot)))

    from runtop.app import RuntopApp  # deferred: keep --version and --dump light

    app = RuntopApp.from_args(
        demo=args.demo, snapshot=args.snapshot, target=args.target, quit_after=args.quit_after,
    )
    auto_pilot = None
    if args.keys:
        from runtop.keys import autopilot, parse_keys

        auto_pilot = autopilot(parse_keys(args.keys))
    app.run(auto_pilot=auto_pilot)
