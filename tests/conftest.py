from __future__ import annotations

import json
import pathlib
from typing import TypedDict, TypeVar

import pytest

from runtop.data.wire import SnapshotDocument, StatsDict, loads_as

ROOT = pathlib.Path(__file__).resolve().parent.parent
FX = ROOT / "spec" / "fixtures"

T = TypeVar("T")

class LogLineDict(TypedDict):
    stream: str
    text: str


class ExpectedLogs(TypedDict):
    mux: list[LogLineDict]
    tty: list[str]


class ExpectedStats(TypedDict):
    oneshot_first: StatsDict
    oneshot_second_vs_first: StatsDict
    stream_false: StatsDict


class DemuxEdgeCases(TypedDict):
    blank_lines_and_unterminated_tail: list[LogLineDict]
    tty_blank_lines: list[str]


class ExpectedParsers(TypedDict):
    remote_ports: dict[str, list[str]]
    docker_size: dict[str, int]
    label_value: dict[str, str]
    demux_edge_cases: DemuxEdgeCases


def load(name: str, shape: type[T]) -> T:
    """A fixture JSON file, typed as ``shape`` (e.g. ``list[EngineContainer]``)."""
    return loads_as((FX / name).read_text(), shape)


def load_json(name: str) -> object:
    """A fixture JSON file, untyped: only compared against or served as a mock HTTP body."""
    data: object = json.loads((FX / name).read_text())
    return data


def text(name: str) -> str:
    return (FX / name).read_text()


def raw(name: str) -> bytes:
    return (FX / name).read_bytes()


@pytest.fixture
def demo_doc() -> SnapshotDocument:
    return load("snapshots/demo.json", SnapshotDocument)
