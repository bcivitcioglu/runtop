from __future__ import annotations

import pytest

from runtop.data.format import human_uptime
from runtop.data.guest import (
    CpuSample,
    GuestError,
    GuestProbe,
    GuestVitals,
    parse_vitals,
    shell_argv,
    short_command,
)
from runtop.data.remote import RemoteError, RunResult

# Captured from a Lima VM running a GitHub Actions job, trimmed to 3 processes.
SAMPLE = """#up
688.76 1584.82
#load
3.48 2.90 1.62 1/244 145218
#cpu
cpu  98513 81 14600 158482 952 0 3359 0 0 0
#mem
MemTotal:        6053276 kB
MemAvailable:    5286868 kB
#disk
Filesystem     1024-blocks    Used Available Capacity Mounted on
/dev/vda1         59848952 9949240  49883328      17% /
#proc
14.4  3.2 /home/runner/actions-runner/externals/node24/bin/node /home/runner/_work/index.cjs
10.9  2.0 /home/runner/actions-runner/bin/Runner.Worker spawnclient 143 158
 0.2  1.7 /usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock
"""


def test_parses_a_real_guest_sample() -> None:
    v = parse_vitals(SAMPLE)
    assert v.uptime_seconds == pytest.approx(688.76)
    assert v.load == (3.48, 2.90, 1.62)
    assert v.mem_total_bytes == 6053276 * 1024
    assert v.mem_used_bytes == (6053276 - 5286868) * 1024
    assert v.disk_total_bytes == 59848952 * 1024
    assert v.disk_used_bytes == 9949240 * 1024
    assert v.cpu == CpuSample(busy=98513 + 81 + 14600 + 3359, total=275987)  # idle+iowait excluded
    assert [p.command for p in v.procs] == ["node index.cjs", "Runner.Worker spawnclient", "dockerd"]
    assert v.procs[0].cpu_percent == 14.4 and v.procs[0].mem_percent == 3.2
    assert not v.empty


@pytest.mark.parametrize(("argv", "expected"), [
    ("/usr/local/bin/node /srv/app/index.cjs", "node index.cjs"),
    ("/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock", "dockerd"),
    ("/usr/local/bin/lima-guestagent daemon --vsock-port 2222", "lima-guestagent daemon"),
    ("[kworker/1:3-events]", "[kworker/1:3-events]"),
    ("", ""),
])
def test_short_command_keeps_the_verb_and_drops_paths(argv: str, expected: str) -> None:
    assert short_command(argv) == expected


def test_parse_degrades_when_a_tool_is_missing() -> None:
    v = parse_vitals("#up\n42.0 80.0\n#load\n#proc\n")
    assert v.uptime_seconds == 42.0
    assert v.load is None and v.mem_total_bytes is None and v.disk_total_bytes is None
    assert v.procs == ()
    assert parse_vitals("").empty
    assert parse_vitals("garbage\nwithout tags\n").empty


def test_parse_ignores_unparseable_numbers() -> None:
    v = parse_vitals("#cpu\ncpu x y z\n#mem\nMemTotal:  notanumber kB\n#proc\nx y z\n")
    assert v.cpu is None and v.mem_total_bytes is None and v.procs == ()


def test_mem_without_available_reports_total_only() -> None:
    v = parse_vitals("#mem\nMemTotal:        1024 kB\n")
    assert v.mem_total_bytes == 1024 * 1024 and v.mem_used_bytes is None


def test_cpu_rate_is_a_share_of_the_elapsed_jiffies() -> None:
    first, second = CpuSample(busy=100, total=400), CpuSample(busy=200, total=600)
    assert second.rate(first) == pytest.approx(50.0)
    assert second.rate(second) is None  # no time passed
    assert CpuSample(busy=0, total=500).rate(first) == 0.0  # clamped, never negative


def test_shell_argv_refuses_a_name_that_could_be_a_flag() -> None:
    assert shell_argv("build-ci")[:2] == ["limactl", "shell"]
    assert "build-ci" in shell_argv("build-ci")
    for bad in ("--rm", "-x", "a/b", "..", ""):
        with pytest.raises(GuestError):
            shell_argv(bad)


async def test_probe_needs_two_samples_for_cpu_then_caches() -> None:
    calls = 0

    async def runner(argv: list[str], timeout: float) -> RunResult:
        nonlocal calls
        calls += 1
        busy, total = 100 * calls, 400 * calls
        return RunResult(0, f"#cpu\ncpu  {busy} 0 0 {total - busy} 0\n#load\n1.0 1.0 1.0\n".encode(), b"")

    probe = GuestProbe(runner, interval=0.0)
    first = await probe.measure("ci")
    assert first is not None and first.cpu_percent is None  # nothing to diff against yet
    second = await probe.measure("ci")
    assert second is not None and second.cpu_percent == pytest.approx(25.0)
    assert calls == 2
    probe.interval = 60.0
    assert await probe.measure("ci") == second and calls == 2  # throttled
    assert probe.cached("ci") == second
    probe.forget("ci")
    assert probe.cached("ci") is None


async def test_probe_surfaces_failures_instead_of_raising() -> None:
    async def failing(argv: list[str], timeout: float) -> RunResult:
        return RunResult(1, b"", b"limactl: instance \"ci\" is stopped\n")

    vitals = await GuestProbe(failing, interval=0.0).measure("ci")
    assert vitals is not None and vitals.error == 'limactl: instance "ci" is stopped'

    async def exploding(argv: list[str], timeout: float) -> RunResult:
        raise RemoteError("limactl not found")

    vitals = await GuestProbe(exploding, interval=0.0).measure("ci")
    assert vitals is not None and vitals.error == "limactl not found"

    async def timing_out(argv: list[str], timeout: float) -> RunResult:
        raise TimeoutError

    vitals = await GuestProbe(timing_out, interval=0.0).measure("ci")
    assert vitals is not None and vitals.error and vitals.empty


async def test_probe_drops_a_baseline_that_went_backwards() -> None:
    counters = iter([(900, 1000), (10, 20)])  # second reading is post-reboot

    async def runner(argv: list[str], timeout: float) -> RunResult:
        busy, total = next(counters)
        return RunResult(0, f"#cpu\ncpu  {busy} 0 0 {total - busy} 0\n".encode(), b"")

    probe = GuestProbe(runner, interval=0.0)
    await probe.measure("ci")
    after = await probe.measure("ci")
    assert after is not None and after.cpu_percent is None  # no nonsense rate across the reboot


def test_unsafe_name_error_is_reported_not_raised() -> None:
    async def never_called(argv: list[str], timeout: float) -> RunResult:  # pragma: no cover
        raise AssertionError("should not spawn a process")

    probe = GuestProbe(never_called, interval=0.0)
    vitals = GuestVitals(error="unsafe instance name '--rm'")
    assert probe.cached("--rm") is None
    assert vitals.empty and vitals.error


@pytest.mark.parametrize(("seconds", "expected"), [
    (None, "–"), (-1.0, "–"), (0.0, "0s"), (42.0, "42s"), (60.0, "1m"), (1080.0, "18m"),
    (11220.0, "3h 07m"), (439200.0, "5d 02h"),
])
def test_human_uptime(seconds: float | None, expected: str) -> None:
    assert human_uptime(seconds) == expected
