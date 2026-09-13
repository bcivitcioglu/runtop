"""Remote docker contexts through the docker CLI — hard read-only.

Every call is ``docker --context <name> <verb> …`` with ``verb`` in :data:`ALLOWED_VERBS`.
Anything else raises :class:`ReadOnlyViolation` before a process is spawned. The client
exposes no mutating methods at all.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from runtop.data.engine import exit_code_from_status, health_from_status, port_sort_key
from runtop.data.models import Container, Image, Target, TargetKind
from runtop.data.wire import ContainerInspect, ContextLine, ImagesLine, PsLine, loads_as

ALLOWED_VERBS = frozenset({"ps", "images", "logs", "inspect", "stats", "version"})


class ReadOnlyViolation(PermissionError):
    """A non-allowlisted docker verb was requested for a read-only context."""


class RemoteError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[list[str], float], Awaitable[RunResult]]


async def subprocess_runner(argv: list[str], timeout: float) -> RunResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise RemoteError(f"{argv[0]} not found") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return RunResult(proc.returncode or 0, out, err)


def build_argv(context: str, verb: str, *args: str) -> list[str]:
    """The only way an argv for a context is built. Refuses non-allowlisted verbs."""
    if verb not in ALLOWED_VERBS:
        raise ReadOnlyViolation(f"docker {verb!r} is not allowed on read-only context {context!r}")
    if not context or context.startswith("-"):
        raise ValueError(f"bad context name {context!r}")
    return ["docker", "--context", context, verb, *args]


# ---------------------------------------------------------------- parsers


def label_value(labels: str, key: str) -> str:
    for kv in labels.split(","):
        k, _, v = kv.strip().partition("=")
        if k == key:
            return v
    return ""


_PAIR = re.compile(r"(\d+)->(\d+)")
_BARE = re.compile(r"(?:^|,)\s*(\d+)/(?:tcp|udp)")


def normalize_cli_ports(s: str) -> list[str]:
    out: list[str] = []
    for pub, priv in _PAIR.findall(s):
        p = f"{pub}->{priv}"
        if p not in out:
            out.append(p)
    covered = {side for p in out for side in p.split("->")}
    for bare in _BARE.findall(s):
        if bare not in out and bare not in covered:
            out.append(bare)
    return sorted(out, key=port_sort_key)


_SIZE_SUFFIXES = (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("kB", 10**3), ("KB", 10**3), ("B", 1))


def parse_docker_size(s: str) -> int:
    s = s.strip()
    for suf, mult in _SIZE_SUFFIXES:
        if s.endswith(suf):
            try:
                return int(float(s[: -len(suf)]) * mult)
            except ValueError:
                return 0
    return 0


_Line = TypeVar("_Line")


def _jsonl(text: str, shape: type[_Line]) -> list[_Line]:
    out: list[_Line] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(loads_as(line, shape))
            except json.JSONDecodeError:
                continue
    return out


def parse_ps(text: str) -> list[Container]:
    cs = []
    for p in _jsonl(text, PsLine):
        status = p.get("Status", "")
        labels = p.get("Labels", "") or ""
        cs.append(Container(
            id=p.get("ID", "")[:12], name=p.get("Names", ""), image=p.get("Image", ""),
            state=p.get("State", "").lower(), status=status,
            health=health_from_status(status), exit_code=exit_code_from_status(status),
            project=label_value(labels, "com.docker.compose.project"),
            service=label_value(labels, "com.docker.compose.service"),
            ports=tuple(normalize_cli_ports(p.get("Ports", "") or "")),
        ))
    return sorted(cs, key=lambda c: (c.project, c.name))


def parse_images(text: str) -> list[Image]:
    imgs = []
    for i in _jsonl(text, ImagesLine):
        repo = i.get("Repository", "")
        n = str(i.get("Containers", "")).strip()
        containers = int(n) if n.lstrip("-").isdigit() and int(n) >= 0 else None
        imgs.append(Image(
            id=i.get("ID", "")[:12],
            ref="<none>:<none>" if repo == "<none>" else f"{repo}:{i.get('Tag', '')}",
            size_bytes=parse_docker_size(i.get("Size", "")),
            containers=containers, dangling=repo == "<none>",
        ))
    return sorted(imgs, key=lambda i: -i.size_bytes)


def parse_context_ls(text: str) -> list[Target]:
    """Docker contexts with ``ssh://`` or ``tcp://`` endpoints → read-only targets, by name."""
    out = []
    for e in _jsonl(text, ContextLine):
        name, ep = e.get("Name", ""), e.get("DockerEndpoint", "")
        if not name:
            continue
        if ep.startswith("unix://"):
            out.append(Target(key=f"local:{name}", kind=TargetKind.HOST, name=name, read_only=False, endpoint=ep))
        elif ep.startswith(("ssh://", "tcp://")):
            out.append(Target(key=f"ctx:{name}", kind=TargetKind.CONTEXT, name=name, read_only=True, endpoint=ep))
    return sorted(out, key=lambda t: t.name)


async def list_contexts(runner: Runner = subprocess_runner, timeout: float = 10.0) -> list[Target]:
    """Discovery (not a per-context call, so outside the verb allowlist). Errors → ``[]``."""
    try:
        res = await runner(["docker", "context", "ls", "--format", "json"], timeout)
    except (RemoteError, TimeoutError) as e:
        raise RemoteError(f"docker context discovery: {e}") from e
    if res.returncode != 0:
        raise RemoteError(f"docker context discovery: {_err_text(res)}")
    return parse_context_ls(res.stdout.decode(errors="replace"))


# ---------------------------------------------------------------- client


def _err_text(res: RunResult) -> str:
    text = res.stderr.decode(errors="replace").strip() or res.stdout.decode(errors="replace").strip()
    return text.splitlines()[-1] if text else f"docker exited {res.returncode}"


class RemoteClient:
    """Read-only client for one docker context. Deliberately has no mutating methods."""

    def __init__(self, context: str, runner: Runner = subprocess_runner, timeout: float = 15.0) -> None:
        self.context = context
        self._runner = runner
        self.timeout = timeout

    async def _run(self, verb: str, *args: str) -> str:
        argv = build_argv(self.context, verb, *args)  # raises before spawning
        try:
            res = await self._runner(argv, self.timeout)
        except TimeoutError as e:
            raise RemoteError(f"docker {verb} on {self.context} timed out after {self.timeout:.0f}s") from e
        if res.returncode != 0:
            raise RemoteError(_err_text(res))
        return res.stdout.decode(errors="replace")

    async def version(self) -> str:
        return (await self._run("version", "--format", "{{.Server.Version}}")).strip()

    async def containers(self) -> list[Container]:
        return parse_ps(await self._run("ps", "-a", "--format", "json"))

    async def images(self) -> list[Image]:
        return parse_images(await self._run("images", "--format", "json"))

    async def inspect(self, name: str) -> ContainerInspect:
        out = (await self._run("inspect", "--", name)).strip()
        data = loads_as(out, list[ContainerInspect]) if out else []
        return data[0] if data else {}

    async def logs(self, name: str, *, tail: int = 200, follow: bool = False) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(stream, line)`` from stdout **and** stderr."""
        args = ["--tail", str(tail)] + (["-f"] if follow else []) + ["--", name]
        argv = build_argv(self.context, "logs", *args)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=500)

        async def pump(reader: asyncio.StreamReader | None, stream: str) -> None:
            if reader is None:
                return
            async for raw in reader:
                await queue.put((stream, raw.decode(errors="replace").rstrip("\n").removesuffix("\r")))

        async def run() -> None:
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(pump(proc.stdout, "stdout"))
                    group.create_task(pump(proc.stderr, "stderr"))
            finally:
                current = asyncio.current_task()
                if current is not None and not current.cancelling():
                    await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while (item := await queue.get()) is not None:
                yield item
            await task
            code = await proc.wait()
            if code:
                raise RemoteError(f"docker logs exited {code}")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
