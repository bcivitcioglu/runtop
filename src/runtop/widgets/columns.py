"""Fixed-width column layout shared by the container list header and its rows."""

from __future__ import annotations

from dataclasses import dataclass

GAP = 2
DOT = 2  # "● "
INDENT = 2  # children sit under their group's name


@dataclass(frozen=True, slots=True)
class Columns:
    width: int
    name: int
    status: int
    cpu: int
    spark: int
    mem: int
    ports: int
    image: int

    @property
    def stats(self) -> bool:
        return self.cpu > 0


def layout(width: int, *, stats: bool = True) -> Columns:
    """Columns for a row ``width`` cells wide (excluding indent + dot).

    Priorities as width shrinks: ports go first (they're in the detail pane), then the sparkline
    narrows, then IMAGE goes, then status and sparkline. CPU and MEM always stay. At 150+ terminal
    columns (≈ 76 row cells with both side panes) IMAGE is visible.
    """
    status, cpu, spark, mem = 9, 6, 8, 6
    if not stats:
        cpu = spark = mem = 0
    ports, image = 12, 16
    min_name, comfy_name = 12, 18

    def fixed() -> int:
        return sum(c + GAP for c in (status, cpu, spark, mem, ports, image) if c)

    if width - fixed() < comfy_name:
        ports = 0
    if spark and width - fixed() < comfy_name:
        spark = 6
    if width - fixed() < min_name:
        image = 0
    if width - fixed() < min_name:
        status = 0
    if width - fixed() < min_name:
        spark = 0
    if image:
        # hand spare room to IMAGE (up to 36) once the name is comfortable, then to ports if they were dropped
        room = width - fixed() - comfy_name
        grow = max(0, min(36 - image, room))
        image += grow
    name = max(4, width - fixed())
    return Columns(width, name, status, cpu, spark, mem, ports, image)
