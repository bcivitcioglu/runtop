"""CPU / memory math for Engine stats samples (spec/SPEC.md "Stats")."""

from __future__ import annotations

from runtop.data.models import Stats
from runtop.data.wire import CpuStats, CpuUsage, MemoryStats, StatsSample


def cpu_percent(sample: StatsSample, prev_cpu: CpuStats | None) -> float | None:
    """CPU% of ``sample`` relative to ``prev_cpu`` (a ``cpu_stats`` / ``precpu_stats`` object).

    ``None`` when there is no usable previous sample (first one-shot sample).
    """
    cur: CpuStats = sample.get("cpu_stats") or {}
    prev: CpuStats = prev_cpu or {}
    cur_usage: CpuUsage = cur.get("cpu_usage") or {}
    prev_usage: CpuUsage = prev.get("cpu_usage") or {}
    sys_cur, sys_prev = cur.get("system_cpu_usage"), prev.get("system_cpu_usage")
    if sys_cur is None or sys_prev is None:
        return None
    cpu_delta = cur_usage.get("total_usage", 0) - prev_usage.get("total_usage", 0)
    sys_delta = sys_cur - sys_prev
    ncpu = cur.get("online_cpus") or len(cur_usage.get("percpu_usage") or []) or 1
    if sys_delta > 0 and cpu_delta >= 0:
        return round(cpu_delta / sys_delta * ncpu * 100, 4)
    return None


def mem_usage(sample: StatsSample) -> tuple[int, int]:
    """``(mem_bytes, mem_limit_bytes)``: usage minus inactive file cache (cgroup v2, then v1)."""
    ms: MemoryStats = sample.get("memory_stats") or {}
    st: dict[str, int] = ms.get("stats") or {}
    cache = st.get("inactive_file", st.get("total_inactive_file", 0))
    return max(0, ms.get("usage", 0) - cache), ms.get("limit", 0)


def precpu_filled(sample: StatsSample) -> bool:
    """True when the daemon filled ``precpu_stats`` (``stream=false`` without one-shot)."""
    precpu: CpuStats = sample.get("precpu_stats") or {}
    return precpu.get("system_cpu_usage") is not None


def compute(sample: StatsSample, prev_cpu: CpuStats | None = None) -> Stats:
    """Normalize one sample. ``prev_cpu`` is the previous one-shot ``cpu_stats`` kept by the caller;
    it is ignored when the daemon already filled ``precpu_stats``."""
    base = sample.get("precpu_stats") if precpu_filled(sample) else prev_cpu
    mem, limit = mem_usage(sample)
    return Stats(cpu_percent=cpu_percent(sample, base), mem_bytes=mem, mem_limit_bytes=limit)
