from __future__ import annotations

import pytest

from runtop.data.remote import (
    ALLOWED_VERBS,
    ReadOnlyViolation,
    RemoteClient,
    RemoteError,
    RunResult,
    build_argv,
    label_value,
    list_contexts,
    normalize_cli_ports,
    parse_context_ls,
    parse_docker_size,
    parse_images,
    parse_ps,
)

from .conftest import ExpectedParsers, load, load_json, text


def test_ps_matches_expected() -> None:
    assert [c.to_dict(include_stats=False) for c in parse_ps(text("cli/docker_ps.jsonl"))] == load_json(
        "expected/remote_containers.json")


def test_images_match_expected() -> None:
    got = [i.to_dict() for i in parse_images(text("cli/docker_images.jsonl"))]
    assert got == load_json("expected/remote_images.json")


def test_parsers_match_expected() -> None:
    exp = load("expected/parsers.json", ExpectedParsers)
    assert {s: normalize_cli_ports(s) for s in exp["remote_ports"]} == exp["remote_ports"]
    assert {s: parse_docker_size(s) for s in exp["docker_size"]} == exp["docker_size"]
    assert {s: label_value(s, "com.docker.compose.project") for s in exp["label_value"]} == exp["label_value"]


def test_images_containers_na_is_null() -> None:
    line = '{"Containers": "N/A", "ID": "abc", "Repository": "<none>", "Size": "1kB", "Tag": "<none>"}'
    (img,) = parse_images(line)
    assert (img.containers, img.dangling, img.ref, img.size_bytes) == (None, True, "<none>:<none>", 1000)


def test_context_ls_keeps_local_and_remote_endpoints() -> None:
    lines = "\n".join([
        '{"Name":"default","DockerEndpoint":"unix:///var/run/docker.sock"}',
        '{"Name":"zeta","DockerEndpoint":"tcp://10.0.0.2:2376"}',
        '{"Name":"prod-eu","DockerEndpoint":"ssh://deploy@prod-eu.example.com"}',
        "not json",
    ])
    targets = parse_context_ls(lines)
    assert [(t.key, t.read_only, t.kind.value) for t in targets] == [
        ("local:default", False, "host"), ("ctx:prod-eu", True, "context"), ("ctx:zeta", True, "context")]


class RecordingRunner:
    def __init__(self, results: dict[str, RunResult | BaseException] | None = None) -> None:
        self.argvs: list[list[str]] = []
        self.results = results or {}

    async def __call__(self, argv: list[str], timeout: float) -> RunResult:
        self.argvs.append(argv)
        verb = argv[3] if len(argv) > 3 and argv[1] == "--context" else argv[1]
        res = self.results.get(verb, RunResult(0, b"", b""))
        if isinstance(res, BaseException):
            raise res
        return res


@pytest.mark.parametrize("verb", ["rm", "stop", "start", "restart", "kill", "exec", "run", "system", "image",
                                  "container", "compose", "prune", "pull", "cp", "update", "Ps", ""])
def test_mutating_verbs_raise_before_spawning(verb: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        build_argv("prod-eu", verb, "x")


async def test_client_run_refuses_without_spawning() -> None:
    runner = RecordingRunner()
    client = RemoteClient("prod-eu", runner)
    with pytest.raises(ReadOnlyViolation):
        await client._run("rm", "-f", "api-1")
    assert runner.argvs == []


def test_client_has_no_mutating_methods() -> None:
    public = {n for n in dir(RemoteClient) if not n.startswith("_")}
    assert public <= {"containers", "images", "inspect", "logs", "version"}


async def test_client_argv_is_allowlisted() -> None:
    runner = RecordingRunner({
        "ps": RunResult(0, text("cli/docker_ps.jsonl").encode(), b""),
        "images": RunResult(0, text("cli/docker_images.jsonl").encode(), b""),
        "inspect": RunResult(0, b'[{"Id": "x"}]', b""),
        "version": RunResult(0, b"29.8.0\n", b""),
    })
    client = RemoteClient("prod-eu", runner)
    assert len(await client.containers()) == 5
    assert len(await client.images()) == 5
    assert (await client.inspect("api-1"))["Id"] == "x"
    assert await client.version() == "29.8.0"
    for argv in runner.argvs:
        assert argv[:3] == ["docker", "--context", "prod-eu"]
        assert argv[3] in ALLOWED_VERBS


async def test_client_errors_carry_stderr() -> None:
    runner = RecordingRunner({"ps": RunResult(1, b"", b"error during connect\nssh: connect to host: timed out\n")})
    with pytest.raises(RemoteError, match="timed out"):
        await RemoteClient("staging", runner).containers()


async def test_list_contexts_failure_is_visible() -> None:
    with pytest.raises(RemoteError, match="boom"):
        await list_contexts(RecordingRunner({"context": RunResult(1, b"", b"boom")}))
    with pytest.raises(RemoteError, match="docker not found"):
        await list_contexts(RecordingRunner({"context": RemoteError("docker not found")}))
