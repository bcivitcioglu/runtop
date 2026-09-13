"""Small persisted preferences (live mode only; ``--demo`` and tests never touch disk)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Config:
    log_directory: str = ""
    log_max_mib: int = 64
    log_file_mib: int = 8
    log_keep_days: float = 7
    theme: str = "runtop"
    sidebar_width: int | None = None
    detail_width: int | None = None
    last_target: str | None = None
    collapsed: dict[str, list[str]] = field(default_factory=dict)


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "runtop" / "config.json"


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return Config()
    if not isinstance(data, dict):
        return Config()
    cfg = Config()
    if isinstance(data.get("theme"), str):
        cfg.theme = data["theme"]
    for name in ("sidebar_width", "detail_width"):
        value = data.get(name)
        low, high = (18, 48) if name == "sidebar_width" else (30, 90)
        if type(value) is int:
            setattr(cfg, name, max(low, min(high, value)))
    if isinstance(data.get("last_target"), str):
        cfg.last_target = data["last_target"]
    if isinstance(data.get("collapsed"), dict):
        cfg.collapsed = {k: [str(x) for x in v] for k, v in data["collapsed"].items() if isinstance(v, list)}
    if isinstance(data.get("log_directory"), str):
        cfg.log_directory = data["log_directory"]
    total, file, days = data.get("log_max_mib"), data.get("log_file_mib"), data.get("log_keep_days")
    if (type(total) is int and type(file) is int and type(days) in (int, float)
            and 1 <= file <= total <= 1048576 and 0 < days <= 3650):
        cfg.log_max_mib, cfg.log_file_mib, cfg.log_keep_days = total, file, float(days)
    return cfg


def save(cfg: Config, path: Path | None = None) -> bool:
    path = path or config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(cfg), indent=2) + "\n")
        tmp.replace(path)
    except OSError:
        return False
    return True
