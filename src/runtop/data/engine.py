"""Docker Engine API over a unix socket (httpx), with normalization per spec/SPEC.md."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TypedDict, Unpack

import httpx

from runtop.data import stats as statsmath
from runtop.data.models import Container, Image, Stats
from runtop.data.storage import DiskUsage
from runtop.data.wire import (
    ContainerInspect,
    CpuStats,
    EngineContainer,
    EngineErrorBody,
    EngineImage,
    EngineVersion,
    PruneReport,
    StatsSample,
    loads_as,
)

# ---------------------------------------------------------------- normalization


def health_from_status(status: str) -> str | None:
    if "(healthy)" in status:
        return "healthy"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(health: starting)" in status:
        return "starting"
    return None


_EXIT = re.compile(r"Exited \((-?\d+)\)")


def exit_code_from_status(status: str) -> int | None:
    m = _EXIT.match(status)
    return int(m.group(1)) if m else None


def port_sort_key(p: str) -> tuple[int, int]:
    if "->" in p:
        pub, priv = p.split("->", 1)
        return int(pub), int(priv)
    return int(p), int(p)


def normalize_container(c: EngineContainer) -> Container:
    ports: list[str] = []
    for p in c.get("Ports") or ():
        if p.get("PublicPort"):
            s = f"{p['PublicPort']}->{p['PrivatePort']}"
        elif p.get("PrivatePort"):
            s = str(p["PrivatePort"])
        else:
            continue
        if s not in ports:
            ports.append(s)
    labels = c.get("Labels") or {}
    status = c.get("Status", "")
    return Container(
        id=c["Id"][:12],
        name=",".join(sorted(n.lstrip("/") for n in c.get("Names") or ())),
        image=c.get("Image", ""),
        image_id=c.get("ImageID", "").removeprefix("sha256:")[:12],
        state=c.get("State", "").lower(),
        status=status,
        health=health_from_status(status),
        exit_code=exit_code_from_status(status),
        project=labels.get("com.docker.compose.project", ""),
        service=labels.get("com.docker.compose.service", ""),
        ports=tuple(sorted(ports, key=port_sort_key)),
    )


def normalize_image(i: EngineImage) -> Image:
    tags = [t for t in i.get("RepoTags") or () if t != "<none>:<none>"]
    if tags:
        ref = tags[0].split("@sha256:")[0]
    elif digests := i.get("RepoDigests"):
        repo, _, digest = digests[0].partition("@")
        ref = f"{repo}@{digest.removeprefix('sha256:')[:12]}"
    else:
        ref = "<none>:<none>"
    n = i.get("Containers")
    return Image(
        id=i["Id"].removeprefix("sha256:")[:12],
        ref=ref,
        size_bytes=int(i.get("Size") or 0),
        containers=None if n is None or n < 0 else int(n),
        dangling=not tags,
    )


def sort_containers(cs: list[Container]) -> list[Container]:
    return sorted(cs, key=lambda c: (c.project, c.name))


def sort_images(images: list[Image]) -> list[Image]:
    return sorted(images, key=lambda i: -i.size_bytes)


# ---------------------------------------------------------------- log demux


@dataclass(frozen=True, slots=True)
class LogLine:
    stream: str  # "stdout" | "stderr"
    text: str


class LogDecoder:
    """Incremental decoder for Engine log bodies.

    Multiplexed (non-TTY) bodies carry 8-byte headers ``[stream,0,0,0,size BE u32]``; frames
    may split lines. TTY bodies are raw bytes (reported as stdout).
    """

    def __init__(self, tty: bool) -> None:
        self.tty = tty
        self._raw = b""
        self._remaining = 0
        self._stream = 1
        self._lines: dict[int, bytes] = {1: b"", 2: b""}

    def _split(self, stream: int, chunk: bytes) -> list[LogLine]:
        buf = self._lines.get(stream, b"") + chunk
        *done, rest = buf.split(b"\n")
        name = "stderr" if stream == 2 else "stdout"
        out = []
        for part in done:
            while len(part) > 65536:
                out.append(LogLine(name, part[:65536].decode(errors="replace") + " [continued]"))
                part = part[65536:]
            out.append(LogLine(name, part.decode(errors="replace").removesuffix("\r")))
        while len(rest) > 65536:
            out.append(LogLine(name, rest[:65536].decode(errors="replace") + " [continued]"))
            rest = rest[65536:]
        self._lines[stream] = rest
        return out

    def feed(self, data: bytes) -> list[LogLine]:
        if self.tty:
            return self._split(1, data)
        self._raw += data
        out: list[LogLine] = []
        while self._raw:
            if not self._remaining:
                if len(self._raw) < 8:
                    break
                self._remaining = int.from_bytes(self._raw[4:8], "big")
                self._stream = self._raw[0]
                self._raw = self._raw[8:]
                if not self._remaining:
                    continue
            n = min(len(self._raw), self._remaining)
            out += self._split(self._stream, self._raw[:n])
            self._raw = self._raw[n:]
            self._remaining -= n
        return out

    def flush(self) -> list[LogLine]:
        out = []
        for stream, rest in self._lines.items():
            if rest:
                out.append(LogLine("stderr" if stream == 2 else "stdout",
                                   rest.decode(errors="replace").removesuffix("\r")))
                self._lines[stream] = b""
        return out


def demux(raw: bytes, tty: bool = False) -> list[LogLine]:
    d = LogDecoder(tty)
    return d.feed(raw) + d.flush()


# ---------------------------------------------------------------- client


class EngineError(RuntimeError):
    pass


class RequestOptions(TypedDict, total=False):
    """The httpx request options runtop passes (omitted keys keep the client defaults)."""

    params: dict[str, str]
    timeout: float


class EngineClient:
    """Async Engine API client. Use as ``async with EngineClient(sock) as eng: ...``.

    ``transport`` may be an ``httpx.MockTransport`` for tests.
    """

    STATS_CONCURRENCY = 8
    STATS_TIMEOUT = 4.0

    def __init__(self, socket_path: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 8.0) -> None:
        if transport is None:
            if socket_path is None:
                raise ValueError("socket_path or transport required")
            transport = httpx.AsyncHTTPTransport(uds=socket_path)
        self.socket_path = socket_path
        self._http = httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=timeout)
        self._prefix: str | None = None
        self.api_version: str | None = None

    async def __aenter__(self) -> EngineClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing

    async def _raw(self, method: str, path: str, **kw: Unpack[RequestOptions]) -> httpx.Response:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise EngineError(str(e) or type(e).__name__) from e
        if r.status_code >= 400:
            try:
                body = loads_as(r.content, EngineErrorBody)
                msg = body.get("message", r.text)
            except (json.JSONDecodeError, AttributeError):
                msg = r.text
            raise EngineError(f"{method} {path}: {r.status_code} {msg}".strip())
        return r

    async def negotiate(self) -> str:
        """``GET /version`` once; all later calls are prefixed ``/v<ApiVersion>``."""
        if self._prefix is None:
            data = loads_as((await self._raw("GET", "/version")).content, EngineVersion)
            self.api_version = str(data.get("ApiVersion") or "")
            self._prefix = f"/v{self.api_version}" if self.api_version else ""
        return self._prefix

    async def _api(self, method: str, path: str, **kw: Unpack[RequestOptions]) -> httpx.Response:
        return await self._raw(method, await self.negotiate() + path, **kw)

    # -- reads

    async def ping(self) -> bool:
        r = await self._raw("GET", "/_ping")
        return r.text.strip() == "OK"

    async def version(self) -> EngineVersion:
        await self.negotiate()
        return loads_as((await self._raw("GET", "/version")).content, EngineVersion)

    async def containers(self) -> list[Container]:
        data = loads_as((await self._api("GET", "/containers/json", params={"all": "1"})).content,
                        list[EngineContainer])
        return sort_containers([normalize_container(c) for c in data])

    async def images(self) -> list[Image]:
        data = loads_as((await self._api("GET", "/images/json")).content, list[EngineImage])
        return sort_images([normalize_image(i) for i in data])

    async def disk_usage(self) -> DiskUsage:
        return loads_as((await self._api("GET", "/system/df", timeout=30)).content, DiskUsage)

    async def inspect(self, cid: str) -> ContainerInspect:
        return loads_as((await self._api("GET", f"/containers/{cid}/json")).content, ContainerInspect)

    async def stats_sample(self, cid: str, *, one_shot: bool = True) -> StatsSample:
        params = {"stream": "false"}
        if one_shot:
            params["one-shot"] = "true"
        return loads_as((await self._api("GET", f"/containers/{cid}/stats", params=params)).content, StatsSample)

    async def fill_stats(self, containers: list[Container], prev: dict[str, CpuStats],
                         *, one_shot: bool = True) -> list[Container]:
        """Attach stats to running containers (max 8 concurrent, 4 s each).

        ``prev`` maps container id → previous ``cpu_stats`` and is updated in place, so
        callers keep it per target across polls.
        """
        sem = asyncio.Semaphore(self.STATS_CONCURRENCY)

        async def one(c: Container) -> Container:
            if not c.running:
                return c
            async with sem:
                try:
                    sample = await asyncio.wait_for(self.stats_sample(c.id, one_shot=one_shot),
                                                    self.STATS_TIMEOUT)
                except (TimeoutError, EngineError, json.JSONDecodeError):
                    return c
            st: Stats = statsmath.compute(sample, prev.get(c.id))
            cpu_stats = sample.get("cpu_stats")
            if cpu_stats:
                prev[c.id] = cpu_stats
            return c.with_stats(st)

        tasks = [asyncio.create_task(one(c)) for c in containers]
        try:
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=self.STATS_TIMEOUT)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            out = [task.result() if not task.cancelled() and task.exception() is None else c
                   for task, c in zip(tasks, containers, strict=True)]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        live = {c.id for c in containers if c.running}
        for cid in list(prev):
            if cid not in live:
                del prev[cid]
        return list(out)

    async def logs(self, cid: str, *, tail: int = 200, follow: bool = False, timestamps: bool = False,
                   tty: bool | None = None) -> AsyncIterator[LogLine]:
        if tty is None:
            config = (await self.inspect(cid)).get("Config") or {}
            tty = bool(config.get("Tty"))
        params = {"stdout": "1", "stderr": "1", "tail": str(tail)}
        if follow:
            params["follow"] = "1"
        if timestamps:
            params["timestamps"] = "1"
        decoder = LogDecoder(tty)
        path = await self.negotiate() + f"/containers/{cid}/logs"
        timeout = httpx.Timeout(8.0, read=None) if follow else self._http.timeout
        try:
            async with self._http.stream("GET", path, params=params, timeout=timeout) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise EngineError(f"logs {cid}: {r.status_code} {r.text}")
                async for chunk in r.aiter_bytes():
                    for line in decoder.feed(chunk):
                        yield line
        except httpx.HTTPError as e:
            raise EngineError(str(e) or type(e).__name__) from e
        for line in decoder.flush():
            yield line

    # -- mutations (local targets only; remotes have no such methods)

    async def start(self, cid: str) -> None:
        await self._api("POST", f"/containers/{cid}/start")

    async def stop(self, cid: str, t: int = 10) -> None:
        await self._api("POST", f"/containers/{cid}/stop", params={"t": str(t)}, timeout=t + 15)

    async def restart(self, cid: str, t: int = 10) -> None:
        await self._api("POST", f"/containers/{cid}/restart", params={"t": str(t)}, timeout=t + 15)

    async def remove(self, cid: str) -> None:
        await self._api("DELETE", f"/containers/{cid}", params={"force": "1"})

    async def prune_dangling_images(self) -> int:
        r = await self._api("POST", "/images/prune", params={"filters": json.dumps({"dangling": ["true"]})},
                            timeout=120)
        report = loads_as(r.content, PruneReport)
        return int(report.get("SpaceReclaimed") or 0)
