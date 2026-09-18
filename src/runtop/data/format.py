"""Human formatting helpers (pure, no Textual imports)."""

from __future__ import annotations

import re

SPARK_CHARS = "▁▂▃▄▅▆▇█"
_UNITS = ("B", "K", "M", "G", "T")


def human_bytes(n: int | float | None, *, dash: str = "–") -> str:
    """Compact binary size: ``512B`` ``9.0K`` ``164M`` ``1.2G``."""
    if n is None:
        return dash
    v = float(n)
    for unit in _UNITS:
        if abs(v) < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(v)}B"
            return f"{v:.1f}{unit}" if abs(v) < 10 else f"{v:.0f}{unit}"
        v /= 1024
    return f"{v:.0f}T"  # pragma: no cover


def human_bytes_long(n: int | float | None, *, dash: str = "–") -> str:
    """Detail-pane size: ``164.0 MiB`` ``4.00 GiB``."""
    if n is None:
        return dash
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(v) < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(v)} B"
            return f"{v:.2f} {unit}" if abs(v) < 10 else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TiB"  # pragma: no cover


def percent(p: float | None, *, dash: str = "–") -> str:
    if p is None:
        return dash
    if p >= 100:
        return f"{p:.0f}%"
    return f"{p:.1f}%"


def sparkline(values: list[float] | tuple[float, ...], width: int, *, floor: float = 5.0, levels: int = 8) -> str:
    """Last ``width`` values as eighth-blocks, left-padded with spaces.

    Scale is ``max(values, floor)`` so an idle container stays flat instead of
    magnifying noise into spikes. ``levels`` < 8 caps the bar height so stacked
    list rows never touch.
    """
    if width <= 0:
        return ""
    vals = [max(0.0, v) for v in list(values)[-width:]]
    if not vals:
        return " " * width
    top = max(max(vals), floor)
    steps = max(1, min(len(SPARK_CHARS), levels)) - 1
    chars = "".join(SPARK_CHARS[min(steps, round(v / top * steps))] for v in vals)
    return chars.rjust(width)


_UP = re.compile(r"^Up (?P<dur>.+?)(?: \((?:healthy|unhealthy|health: starting)\))?$")
_EXITED = re.compile(r"^Exited \((?P<code>-?\d+)\) (?P<ago>.+?) ago$")
_DURATION = [
    (re.compile(r"^less than a second$", re.I), "<1s"),
    (re.compile(r"^about a minute$", re.I), "1m"),
    (re.compile(r"^about an hour$", re.I), "1h"),
]
_UNIT = {"second": "s", "minute": "m", "hour": "h", "day": "d", "week": "w", "month": "mo", "year": "y"}


def short_duration(text: str) -> str:
    """``2 hours`` → ``2h``, ``About an hour`` → ``1h``; unknown text returned as-is."""
    t = text.strip()
    for pat, out in _DURATION:
        if pat.match(t):
            return out
    m = re.match(r"^(\d+) (second|minute|hour|day|week|month|year)s?$", t, re.I)
    if m:
        return f"{m.group(1)}{_UNIT[m.group(2).lower()]}"
    return t


def human_uptime(seconds: float | None, *, dash: str = "–") -> str:
    """Elapsed seconds as ``42s`` ``18m`` ``3h 07m`` ``5d 02h``."""
    if seconds is None or seconds < 0:
        return dash
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, hours, days = total // 60, total // 3600, total // 86400
    if minutes < 60:
        return f"{minutes}m"
    if hours < 24:
        return f"{hours}h {minutes % 60:02d}m"
    return f"{days}d {hours % 24:02d}h"


def short_status(state: str, status: str) -> str:
    """Compact status for list rows: ``up 2h``, ``exited 1``, ``created``."""
    m = _UP.match(status)
    if state == "running" and m:
        return f"up {short_duration(m.group('dur'))}"
    m = _EXITED.match(status)
    if m:
        return f"exited {m.group('code')}"
    return state or status.lower()


_NON_SGR = re.compile(r"\x1b(?:\[[0-9;?]*[@-ln-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def strip_non_sgr(text: str) -> str:
    """Remove escape sequences except SGR colour codes (``ESC[…m``)."""
    return _NON_SGR.sub("", text)


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + ellipsis


_HOME = re.compile(r"^(?:/Users|/home)/[^/]+/")


def tilde(path: str) -> str:
    """``/Users/x/.lima/docker`` → ``~/.lima/docker`` (display only)."""
    return _HOME.sub("~/", path)
