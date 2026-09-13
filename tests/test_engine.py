from __future__ import annotations

import json
from typing import Never

import httpx
import pytest

from runtop.data.engine import (
    EngineClient,
    EngineError,
    LogDecoder,
    LogLine,
    demux,
    normalize_container,
    normalize_image,
    sort_containers,
    sort_images,
)
from runtop.data.models import Stats
from runtop.data.stats import compute, cpu_percent, mem_usage
from runtop.data.wire import ContainerDict, CpuStats, EngineContainer, EngineImage, ImageDict, StatsSample

from .conftest import ExpectedLogs, ExpectedParsers, ExpectedStats, load, load_json, raw, text


def frame(stream: int, body: bytes) -> bytes:
    """One multiplexed log frame: ``[stream,0,0,0,size BE u32] + body``."""
    return bytes([stream, 0, 0, 0]) + len(body).to_bytes(4, "big") + body


def test_containers_match_expected() -> None:
    got = sort_containers([normalize_container(c) for c in load("engine/containers_json.json", list[EngineContainer])])
    assert [c.to_dict(include_stats=False) for c in got] == load("expected/containers.json", list[ContainerDict])


def test_images_match_expected() -> None:
    got = sort_images([normalize_image(i) for i in load("engine/images_json.json", list[EngineImage])])
    assert [i.to_dict() for i in got] == load("expected/images.json", list[ImageDict])


def test_image_ref_rules() -> None:
    base: EngineImage = {"Id": "sha256:" + "ab" * 32, "Size": 1, "Containers": -1}
    assert normalize_image({**base, "RepoTags": ["a:1@sha256:dead"]}).ref == "a:1"
    img = normalize_image({**base, "RepoTags": ["<none>:<none>"], "RepoDigests": ["repo@sha256:" + "c" * 64]})
    assert (img.ref, img.dangling, img.containers) == ("repo@" + "c" * 12, True, None)
    assert normalize_image({**base, "RepoTags": None}).ref == "<none>:<none>"


def test_stats_match_expected() -> None:
    exp = load("expected/stats.json", ExpectedStats)
    s1, s2, sf = (load(f"engine/{name}.json", StatsSample)
                  for name in ("stats_oneshot_1", "stats_oneshot_2", "stats_streamfalse"))

    def as_dict(st: Stats) -> dict[str, object]:
        return dict(st.to_dict())

    assert as_dict(compute(s1)) == exp["oneshot_first"]
    assert as_dict(compute(s2, s1.get("cpu_stats"))) == exp["oneshot_second_vs_first"]
    assert as_dict(compute(sf)) == exp["stream_false"]
    # precpu filled by the daemon wins over a caller-kept sample
    assert compute(sf, s1.get("cpu_stats")).cpu_percent == exp["stream_false"]["cpu_percent"]


def test_cpu_uses_online_cpus_then_percpu_then_one() -> None:
    def sample(online: int | None, percpu: list[int] | None) -> StatsSample:
        cur: CpuStats = {"cpu_usage": {"total_usage": 200, "percpu_usage": percpu}, "system_cpu_usage": 1000}
        if online is not None:
            cur["online_cpus"] = online
        return {"cpu_stats": cur}

    prev: CpuStats = {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 0}
    assert cpu_percent(sample(4, [1, 2]), prev) == 40.0
    assert cpu_percent(sample(None, [1, 2]), prev) == 20.0
    assert cpu_percent(sample(None, []), prev) == 10.0
    assert cpu_percent(sample(4, None), {"cpu_usage": {"total_usage": 300}, "system_cpu_usage": 0}) is None


def test_mem_subtracts_v1_cache_and_clamps() -> None:
    assert mem_usage({"memory_stats": {"usage": 100, "limit": 9, "stats": {"total_inactive_file": 30}}}) == (70, 9)
    assert mem_usage({"memory_stats": {"usage": 10, "limit": 9, "stats": {"inactive_file": 30}}}) == (0, 9)


def test_demux_matches_expected() -> None:
    exp = load("expected/logs.json", ExpectedLogs)
    assert [{"stream": ln.stream, "text": ln.text} for ln in demux(raw("engine/logs_mux.bin"))] == exp["mux"]
    assert [ln.text for ln in demux(raw("engine/logs_tty.bin"), tty=True)] == exp["tty"]


def test_demux_handles_arbitrary_chunking() -> None:
    data = raw("engine/logs_mux.bin")
    for size in (1, 3, 7, 13):
        d = LogDecoder(tty=False)
        lines: list[LogLine] = []
        for i in range(0, len(data), size):
            lines += d.feed(data[i : i + size])
        lines += d.flush()
        assert lines == demux(data)


def test_demux_frame_splits_line_and_flushes_partial() -> None:
    lines = demux(frame(1, b"hel") + frame(1, b"lo\r\nwor") + frame(2, b"oops\n") + frame(1, b"ld"))
    assert [(ln.stream, ln.text) for ln in lines] == [("stdout", "hello"), ("stderr", "oops"), ("stdout", "world")]


# ------------------------------------------------------------------ client vs MockTransport


def engine_transport(calls: list[str], *, ping_ok: bool = True) -> httpx.MockTransport:
    stats_seq = [load_json("engine/stats_oneshot_1.json"), load_json("engine/stats_oneshot_2.json")]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}?{request.url.query.decode()}")
        if path == "/_ping":
            return httpx.Response(200 if ping_ok else 500, text=text("engine/ping.txt"))
        if path == "/version":
            return httpx.Response(200, json=load_json("engine/version.json"))
        if not path.startswith("/v1.56/"):
            return httpx.Response(400, json={"message": "unversioned call"})
        rest = path.removeprefix("/v1.56")
        if rest == "/containers/json":
            return httpx.Response(200, json=load_json("engine/containers_json.json"))
        if rest == "/images/json":
            return httpx.Response(200, json=load_json("engine/images_json.json"))
        if rest.endswith("/stats"):
            cid = rest.split("/")[2]
            if cid.startswith("efc9bc5455f8"):
                return httpx.Response(200, json=stats_seq.pop(0) if len(stats_seq) > 1 else stats_seq[0])
            return httpx.Response(404, json={"message": "no such container"})
        if rest == "/containers/9f2f1ea22598/json":
            return httpx.Response(200, json=load_json("engine/inspect_web.json"))
        if rest == "/containers/72965dd89a60/json":
            return httpx.Response(200, json=load_json("engine/inspect_tty.json"))
        if rest == "/containers/9f2f1ea22598/logs":
            return httpx.Response(200, content=raw("engine/logs_mux.bin"))
        if rest == "/containers/72965dd89a60/logs":
            return httpx.Response(200, content=raw("engine/logs_tty.bin"))
        return httpx.Response(404, json={"message": f"unhandled {rest}"})

    return httpx.MockTransport(handler)


async def test_client_negotiates_version_and_lists() -> None:
    calls: list[str] = []
    async with EngineClient(transport=engine_transport(calls)) as eng:
        assert await eng.ping()
        cs = await eng.containers()
        imgs = await eng.images()
    assert eng.api_version == "1.56"
    assert [c.to_dict(include_stats=False) for c in cs] == load_json("expected/containers.json")
    assert [i.to_dict() for i in imgs] == load_json("expected/images.json")
    assert calls.count("GET /version?") == 1
    assert "GET /v1.56/containers/json?all=1" in calls


async def test_client_fill_stats_keeps_previous_sample() -> None:
    calls: list[str] = []
    prev: dict[str, CpuStats] = {}
    exp = load("expected/stats.json", ExpectedStats)
    async with EngineClient(transport=engine_transport(calls)) as eng:
        cs = await eng.containers()
        first = {c.name: c for c in await eng.fill_stats(cs, prev)}
        second = {c.name: c for c in await eng.fill_stats(cs, prev)}
    first_stats, second_stats = first["lsfx-api"].stats, second["lsfx-api"].stats
    assert first_stats is not None and second_stats is not None
    assert first_stats.to_dict() == exp["oneshot_first"]
    assert second_stats.to_dict() == exp["oneshot_second_vs_first"]
    assert first["lsfx-worker"].stats is None  # not running: no stats call
    assert first["lsfx-web"].stats is None  # stats error → null, not a crash
    assert not any("/containers/c1d9d3114110" in c for c in calls)
    assert all("one-shot=true" in c for c in calls if c.split("?")[0].endswith("/stats"))
    assert set(prev) == {"efc9bc5455f8"}


async def test_client_logs_mux_and_tty() -> None:
    exp = load("expected/logs.json", ExpectedLogs)
    async with EngineClient(transport=engine_transport([])) as eng:
        mux = [(ln.stream, ln.text) async for ln in eng.logs("9f2f1ea22598")]
        tty = [ln.text async for ln in eng.logs("72965dd89a60")]
    assert mux == [(d["stream"], d["text"]) for d in exp["mux"]]
    assert tty == exp["tty"]


async def test_client_errors_are_engine_errors() -> None:
    async with EngineClient(transport=engine_transport([], ping_ok=False)) as eng:
        with pytest.raises(EngineError):
            await eng.ping()
        with pytest.raises(EngineError, match="unhandled"):
            await eng.inspect("nope")

    def boom(request: httpx.Request) -> Never:
        raise httpx.ConnectError("connection refused")

    async with EngineClient(transport=httpx.MockTransport(boom)) as eng:
        with pytest.raises(EngineError, match="refused"):
            await eng.containers()


async def test_mutations_use_documented_endpoints() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"ApiVersion": "1.44"})
        seen.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        return httpx.Response(200, json={"SpaceReclaimed": 42} if "prune" in request.url.path else {})

    async with EngineClient(transport=httpx.MockTransport(handler)) as eng:
        await eng.start("abc")
        await eng.stop("abc")
        await eng.restart("abc")
        await eng.remove("abc")
        assert await eng.prune_dangling_images() == 42
    assert seen[:4] == [
        "POST /v1.44/containers/abc/start?", "POST /v1.44/containers/abc/stop?t=10",
        "POST /v1.44/containers/abc/restart?t=10", "DELETE /v1.44/containers/abc?force=1",
    ]
    assert seen[4].startswith("POST /v1.44/images/prune?filters=")
    assert json.loads(httpx.URL("http://x/?" + seen[4].split("?", 1)[1]).params["filters"]) == {"dangling": ["true"]}


def test_demux_edge_cases_match_expected() -> None:
    exp = load("expected/parsers.json", ExpectedParsers)["demux_edge_cases"]
    got = demux(frame(1, b"a\n\nb\nend") + frame(2, b"err\n"))
    assert [{"stream": ln.stream, "text": ln.text} for ln in got] == exp["blank_lines_and_unterminated_tail"]
    assert [ln.text for ln in demux(b"one\r\n\ntwo\nlast", tty=True)] == exp["tty_blank_lines"]


def test_first_oneshot_sample_is_stats_with_null_cpu() -> None:
    st = compute(load("engine/stats_oneshot_1.json", StatsSample))
    assert st is not None and st.cpu_percent is None and st.mem_bytes > 0


def test_long_output_and_incomplete_large_frames_are_bounded() -> None:
    decoder = LogDecoder(tty=False)
    header = bytes([1, 0, 0, 0]) + (256 * 1024 * 1024).to_bytes(4, "big")
    lines = decoder.feed(header + b"a" * (128 * 1024 + 1))
    assert len(lines) == 2 and all(line.text.endswith(" [continued]") for line in lines)
    assert len(decoder._raw) == 0 and len(decoder._lines[1]) == 1
    tty = LogDecoder(tty=True)
    for _ in range(5):
        tty.feed(b"a" * 32768)
        assert len(tty._lines[1]) <= 65536


def test_mux_fragmentation_preserves_order_and_content() -> None:
    body = raw("engine/logs_mux.bin")
    expected = demux(body)
    for chunk_size in (1, 7, 8, 11, 256):
        decoder = LogDecoder(tty=False)
        result = []
        for i in range(0, len(body), chunk_size):
            result.extend(decoder.feed(body[i:i + chunk_size]))
        result.extend(decoder.flush())
        assert result == expected
