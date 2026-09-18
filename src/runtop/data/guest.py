"""Guest vitals for a Lima VM, read over ``limactl shell``.

A VM is worth watching even when it runs no containers: a CI runner, a build box or a
plain dev VM all pin CPU without Docker ever hearing about it. Everything here comes from
one shell round trip per probe (:data:`VITALS_SCRIPT`), parsed by :func:`parse_vitals`.

CPU is a counter, not a gauge: ``/proc/stat`` reports cumulative jiffies, so a percentage
needs two samples. :class:`GuestProbe` keeps the previous one per instance and diffs, the
same way the Engine client turns container CPU counters into a rate. The first probe after
selecting a VM therefore has no ``cpu_percent`` — load average covers that gap.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace

from runtop.data.remote import RemoteError, Runner, subprocess_runner

# One round trip. Tagged sections so a missing tool degrades to a gap, not a parse failure.
VITALS_SCRIPT = (
    "echo '#up'; cat /proc/uptime; "
    "echo '#load'; cat /proc/loadavg; "
    "echo '#cpu'; grep '^cpu ' /proc/stat; "
    "echo '#mem'; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; "
    "echo '#disk'; df -kP /; "
    "echo '#proc'; ps -eo pcpu=,pmem=,args= --sort=-pcpu 2>/dev/null | head -n 5"
)

TOP_PROCS = 5
INTERPRETERS = frozenset({"node", "python", "python3", "sh", "bash", "ruby", "perl", "java", "dotnet"})


class GuestError(RuntimeError):
    """The guest could not be probed (VM down, limactl missing, shell refused)."""


@dataclass(frozen=True, slots=True)
class CpuSample:
    """Cumulative jiffies from ``/proc/stat``. Only differences mean anything."""

    busy: int
    total: int

    def rate(self, prev: CpuSample) -> float | None:
        """Busy share since ``prev``, or None when the counters did not advance."""
        span = self.total - prev.total
        if span <= 0:
            return None
        return max(0.0, min(100.0, (self.busy - prev.busy) / span * 100.0))


@dataclass(frozen=True, slots=True)
class GuestProc:
    cpu_percent: float
    mem_percent: float
    command: str


@dataclass(frozen=True, slots=True)
class GuestVitals:
    """What the guest reports. Every field is optional: a VM may lack any given tool."""

    uptime_seconds: float | None = None
    load: tuple[float, float, float] | None = None
    mem_used_bytes: int | None = None
    mem_total_bytes: int | None = None
    disk_used_bytes: int | None = None
    disk_total_bytes: int | None = None
    procs: tuple[GuestProc, ...] = ()
    cpu: CpuSample | None = None  # raw counters; the probe turns these into cpu_percent
    cpu_percent: float | None = None
    error: str | None = None

    @property
    def empty(self) -> bool:
        return (self.load is None and self.mem_total_bytes is None and self.disk_total_bytes is None
                and not self.procs)


def short_command(argv: str) -> str:
    """``/…/node /…/index.cjs`` → ``node index.cjs``. Keeps a leading verb, drops paths."""
    parts = argv.split()
    if not parts:
        return ""
    if parts[0].startswith("["):  # kernel thread: [kworker/1:3-events] has no path to strip
        return argv[:60]
    head = os.path.basename(parts[0]) or parts[0]
    if len(parts) > 1 and not parts[1].startswith("-"):
        tail = os.path.basename(parts[1]) or parts[1]
        if head in INTERPRETERS or not tail.startswith("/"):
            head = f"{head} {tail}"
    return head[:60]


def _floats(line: str, n: int) -> tuple[float, ...] | None:
    fields = line.split()[:n]
    if len(fields) < n:
        return None
    try:
        return tuple(float(f) for f in fields)
    except ValueError:
        return None


def _parse_cpu(line: str) -> CpuSample | None:
    fields = line.split()[1:]
    if len(fields) < 5:
        return None
    try:
        values = [int(f) for f in fields]
    except ValueError:
        return None
    total = sum(values)
    idle = values[3] + values[4]  # idle + iowait
    return CpuSample(busy=total - idle, total=total)


def _parse_proc(line: str) -> GuestProc | None:
    fields = line.split(None, 2)
    if len(fields) < 3:
        return None
    try:
        cpu, mem = float(fields[0]), float(fields[1])
    except ValueError:
        return None
    command = short_command(fields[2])
    return GuestProc(cpu, mem, command) if command else None


def parse_vitals(text: str) -> GuestVitals:
    """Parse :data:`VITALS_SCRIPT` output. Unknown or missing sections are skipped."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            current = sections.setdefault(line[1:].strip(), [])
            continue
        if current is not None and line.strip():
            current.append(line)
    vitals = GuestVitals()
    if (up := sections.get("up")) and (seconds := _floats(up[0], 1)):
        vitals = replace(vitals, uptime_seconds=seconds[0])
    if (load := sections.get("load")) and (three := _floats(load[0], 3)):
        vitals = replace(vitals, load=(three[0], three[1], three[2]))
    if cpu := sections.get("cpu"):
        vitals = replace(vitals, cpu=_parse_cpu(cpu[0]))
    total_kb = avail_kb = None
    for line in sections.get("mem", []):
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields or not fields[0].isdigit():
            continue
        if key == "MemTotal":
            total_kb = int(fields[0])
        elif key == "MemAvailable":
            avail_kb = int(fields[0])
    if total_kb is not None:
        used = (total_kb - avail_kb) if avail_kb is not None else None
        vitals = replace(vitals, mem_total_bytes=total_kb * 1024,
                         mem_used_bytes=used * 1024 if used is not None else None)
    for line in sections.get("disk", []):
        fields = line.split()
        # Filesystem 1024-blocks Used Available Capacity Mounted-on
        if len(fields) >= 6 and fields[1].isdigit() and fields[2].isdigit():
            vitals = replace(vitals, disk_total_bytes=int(fields[1]) * 1024,
                             disk_used_bytes=int(fields[2]) * 1024)
            break
    procs = [p for p in (_parse_proc(line) for line in sections.get("proc", [])) if p is not None]
    return replace(vitals, procs=tuple(procs[:TOP_PROCS]))


def shell_argv(name: str) -> list[str]:
    """``limactl shell`` argv for one instance. Refuses a name that could be a flag."""
    if not name or name.startswith("-") or "/" in name or name in (".", ".."):
        raise GuestError(f"unsafe instance name {name!r}")
    return ["limactl", "shell", "--workdir", "/", name, "sh", "-c", VITALS_SCRIPT]


class GuestProbe:
    """Throttled per-instance vitals. ``limactl shell`` is an ssh round trip, so it is
    measured at most every ``interval`` seconds and never concurrently for one instance.
    """

    def __init__(self, runner: Runner = subprocess_runner, *, interval: float = 5.0,
                 timeout: float = 8.0) -> None:
        self.interval = interval
        self.timeout = timeout
        self._runner = runner
        self._cache: dict[str, tuple[float, GuestVitals]] = {}
        self._prev: dict[str, CpuSample] = {}
        self._busy: set[str] = set()

    def cached(self, name: str) -> GuestVitals | None:
        hit = self._cache.get(name)
        return hit[1] if hit else None

    def forget(self, name: str) -> None:
        """Drop the CPU baseline so a restarted VM does not diff across its reboot."""
        self._prev.pop(name, None)
        self._cache.pop(name, None)

    async def measure(self, name: str) -> GuestVitals | None:
        """Fresh vitals, or the cached ones while within ``interval`` / already in flight."""
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit and now - hit[0] < self.interval:
            return hit[1]
        if name in self._busy:
            return hit[1] if hit else None
        self._busy.add(name)
        try:
            vitals = await self._probe(name)
        finally:
            self._busy.discard(name)
        self._cache[name] = (time.monotonic(), vitals)
        return vitals

    async def _probe(self, name: str) -> GuestVitals:
        try:
            result = await self._runner(shell_argv(name), self.timeout)
        except GuestError as e:
            return GuestVitals(error=str(e))
        except (RemoteError, TimeoutError) as e:
            return GuestVitals(error=_short(e) or "guest probe failed")
        if result.returncode != 0:
            text = (result.stderr or result.stdout).decode(errors="replace").strip()
            return GuestVitals(error=text.splitlines()[-1][:200] if text else "guest probe failed")
        vitals = parse_vitals(result.stdout.decode(errors="replace"))
        sample = vitals.cpu
        if sample is not None:
            prev = self._prev.get(name)
            self._prev[name] = sample
            # A counter that went backwards means the VM rebooted: drop the stale baseline.
            if prev is not None and sample.total >= prev.total:
                vitals = replace(vitals, cpu_percent=sample.rate(prev))
        return vitals


def _short(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    return text.splitlines()[-1][:200]


__all__ = ["VITALS_SCRIPT", "GuestError", "GuestProbe", "GuestProc", "GuestVitals", "parse_vitals",
           "shell_argv", "short_command"]
