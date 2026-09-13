from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtop.data import format as fmt
from runtop.data.lima import DiskUsage, parse_du, parse_list
from runtop.data.models import SCHEMA, DaemonState, TargetKind, dumps_document, load_document
from runtop.data.wire import SnapshotDocument

from .conftest import text


def test_limactl_list_parse() -> None:
    insts = parse_list(text("cli/limactl_list.jsonl"))
    assert [(i.name, i.running) for i in insts] == [("docker", True), ("acme-ci", False), ("orbit-ci", False)]
    t = insts[0].to_target(123)
    assert t.key == "lima:docker" and t.kind is TargetKind.LIMA and not t.read_only
    assert t.endpoint == "unix:///Users/demo/.lima/docker/sock/docker.sock"
    assert t.socket_path == "/Users/demo/.lima/docker/sock/docker.sock"
    assert t.vm is not None
    assert (t.vm.cpus, t.vm.memory_bytes, t.vm.disk_bytes, t.vm.disk_used_bytes) == (4, 4294967296, 107374182400, 123)


def test_du_parse() -> None:
    assert parse_du("22436892\t/Users/demo/.lima/docker\n") == 22436892 * 1024
    assert parse_du("") is None
    assert parse_du("du: cannot read") is None


async def test_disk_usage_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake(path: str, timeout: float = 20.0) -> int:
        calls.append(path)
        return 2048

    monkeypatch.setattr("runtop.data.lima.du_bytes", fake)
    d = DiskUsage(interval=30)
    assert await d.measure("/x") == 2048
    assert await d.measure("/x") == 2048
    assert calls == ["/x"]


def test_demo_document_round_trips(demo_doc: SnapshotDocument) -> None:
    snaps = load_document(demo_doc)
    assert [s.key for s in snaps] == ["lima:docker", "lima:ci", "lima:k3s", "ctx:prod-eu", "ctx:staging"]
    assert snaps[3].target.read_only and snaps[4].state is DaemonState.UNREACHABLE
    assert json.loads(dumps_document(snaps, demo_doc["generated_at"])) == demo_doc


def test_bad_schema_rejected() -> None:
    with pytest.raises(ValueError):
        load_document({"schema": "other/v9", "targets": []})
    assert SCHEMA == "runtop.snapshot/v1"


@pytest.mark.parametrize(("n", "want"), [(0, "0B"), (512, "512B"), (9 * 1024, "9.0K"), (171966464, "164M"),
                                          (4294967296, "4.0G"), (None, "–")])
def test_human_bytes(n: int | None, want: str) -> None:
    assert fmt.human_bytes(n) == want


def test_human_bytes_long_and_percent() -> None:
    assert fmt.human_bytes_long(171966464) == "164.0 MiB"
    assert fmt.human_bytes_long(4294967296) == "4.00 GiB"
    assert (fmt.percent(None), fmt.percent(7.44), fmt.percent(250.2)) == ("–", "7.4%", "250%")


def test_sparkline() -> None:
    assert fmt.sparkline([], 4) == "    "
    assert fmt.sparkline([0, 50, 100], 3) == "▁▅█"
    assert fmt.sparkline([0.1, 0.2], 4) == "  ▁▁"  # floor keeps idle flat
    assert len(fmt.sparkline(list(range(100)), 8)) == 8


@pytest.mark.parametrize(("state", "status", "want"), [
    ("running", "Up 2 hours (healthy)", "up 2h"),
    ("running", "Up About an hour", "up 1h"),
    ("running", "Up Less than a second", "up <1s"),
    ("running", "Up 5 days", "up 5d"),
    ("exited", "Exited (1) 14 minutes ago", "exited 1"),
    ("exited", "Exited (137) 2 weeks ago", "exited 137"),
    ("created", "Created", "created"),
    ("paused", "Up 3 minutes (Paused)", "paused"),
])
def test_short_status(state: str, status: str, want: str) -> None:
    assert fmt.short_status(state, status) == want


def test_strip_non_sgr() -> None:
    s = "\x1b[2J\x1b[Hhi \x1b[32mgreen\x1b[0m\x1b]0;title\x07!"
    assert fmt.strip_non_sgr(s) == "hi \x1b[32mgreen\x1b[0m!"


def test_config_round_trip_and_bad_files(tmp_path: Path) -> None:
    from runtop.config import Config, load, save

    path = tmp_path / "runtop" / "config.json"
    assert load(path) == Config()
    cfg = Config(theme="nord", sidebar_width=30, detail_width=50, last_target="lima:docker",
                 collapsed={"lima:docker": ["group:shop"]})
    assert save(cfg, path)
    assert load(path) == cfg
    path.write_text("{not json")
    assert load(path) == Config()
    path.write_text('{"theme": 3, "sidebar_width": "wide", "collapsed": {"x": "nope"}}')
    assert load(path) == Config()
