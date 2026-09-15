"""Bundled, offline documentation for humans and automation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypedDict, cast

from runtop import __version__


class Topic(TypedDict):
    id: str
    title: str
    content: str


class Manual(TypedDict):
    schema: str
    topics: list[Topic]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runtop docs", description=__doc__)
    parser.add_argument("topic", nargs="?", default="all", help="topic ID, or all (default)")
    parser.add_argument("--list", action="store_true", help="list available topic IDs and titles")
    parser.add_argument("--json", action="store_true", help="emit runtop.docs/v1 JSON for agents")
    args = parser.parse_args(argv)
    path = Path(__file__).parent / "resources/manual.json"
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / "spec/manual.json"
    manual = cast(Manual, json.loads(path.read_text()))
    topics = [t for t in manual["topics"] if args.topic == "all" or t["id"] == args.topic]
    if not topics:
        parser.error(f"unknown topic {args.topic!r}; use runtop docs --list")
    if args.json:
        entries = [{k: v for k, v in t.items() if not args.list or k != "content"} for t in topics]
        print(json.dumps({"schema": "runtop.docs/v1", "version": __version__, "edition": "full",
                          "format": "markdown", "topics": entries}, ensure_ascii=False, indent=2))
    elif args.list:
        for topic in topics:
            print(f"{topic['id']}\t{topic['title']}")
    else:
        print("\n\n".join(t["content"] for t in topics))
    return 0
