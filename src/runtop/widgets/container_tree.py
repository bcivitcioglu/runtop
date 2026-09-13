"""Grouped container list: a Tree whose labels are fixed-width, theme-coloured column rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Unpack

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.widgets import Tree
from textual.widgets.tree import TreeNode
from typing_extensions import override

from runtop.data.format import human_bytes, percent, short_status, sparkline, truncate
from runtop.data.models import Container
from runtop.state.viewmodel import Group, display_name
from runtop.widgets.columns import DOT, GAP, INDENT, Columns, layout
from runtop.widgets.kwargs import WidgetKwargs
from runtop.widgets.styles import fg


@dataclass(frozen=True, slots=True)
class Row:
    kind: str  # "group" | "container"
    key: str
    group: Group
    container: Container | None = None
    hist: tuple[float, ...] = field(default=(), compare=False)


STATE_CLASS = {
    "running": "ctree--ok",
    "restarting": "ctree--warn",
    "paused": "ctree--warn",
    "created": "ctree--idle",
    "exited": "ctree--idle",
    "dead": "ctree--bad",
    "removing": "ctree--warn",
}


def dot_for(c: Container) -> tuple[str, str]:
    """Glyph + component class for a container's status dot."""
    if c.running:
        if c.health == "unhealthy":
            return "●", "ctree--bad"
        if c.health == "starting":
            return "●", "ctree--warn"
        return "●", "ctree--ok"
    if c.state == "exited" and c.exit_code not in (None, 0):
        return "●", "ctree--bad"
    if c.state in ("paused", "restarting"):
        return "◐", "ctree--warn"
    return "○", "ctree--idle"


def ports_text(ports: tuple[str, ...], width: int) -> str:
    if not ports or width <= 0:
        return ""
    shown = [p.replace("->", "→") for p in ports]
    text = ", ".join(shown)
    if len(text) <= width:
        return text
    first = shown[0]
    more = f" +{len(shown) - 1}"
    return truncate(first, width - len(more)) + more if width > len(more) + 3 else truncate(first, width)


def sum_histories(hists: list[tuple[float, ...]]) -> tuple[float, ...]:
    if not hists:
        return ()
    n = max(len(h) for h in hists)
    out = [0.0] * n
    for h in hists:
        for i, v in enumerate(h):
            out[n - len(h) + i] += v
    return tuple(out)


class ContainerTree(Tree[Row]):
    """Compose projects as collapsible groups; each row is exactly as wide as the widget."""

    ICON_NODE = "▸ "
    ICON_NODE_EXPANDED = "▾ "

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("left,h", "collapse_or_parent", "Collapse", show=False),
        Binding("right,l", "expand_or_next", "Expand", show=False),
        Binding("home,g", "scroll_home_cursor", "Top", show=False),
        Binding("end,G", "scroll_end_cursor", "Bottom", show=False),
    ]

    COMPONENT_CLASSES = {
        "ctree--ok", "ctree--warn", "ctree--bad", "ctree--idle", "ctree--muted", "ctree--dim",
        "ctree--spark", "ctree--group", "ctree--standalone", "ctree--icon",
    }

    DEFAULT_CSS = """
    ContainerTree {
        background: transparent;
        padding: 0 1 0 0;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
        & > .tree--cursor { background: $foreground 9%; color: $foreground; text-style: none; }
        &:focus > .tree--cursor { background: $primary 32%; color: $foreground; text-style: none; }
        &:focus { background-tint: transparent; }
        & > .tree--highlight { background: $foreground 5%; }
        & > .tree--highlight-line { background: $foreground 5%; }
        & > .tree--guides, & > .tree--guides-hover, & > .tree--guides-selected { color: transparent; }
        & > .ctree--ok { color: $success; }
        & > .ctree--warn { color: $warning; }
        & > .ctree--bad { color: $error; }
        & > .ctree--idle { color: $foreground-muted; }
        & > .ctree--muted { color: $foreground-muted; }
        & > .ctree--dim { color: $foreground-muted; }
        & > .ctree--spark { color: $primary; }
        & > .ctree--group { text-style: bold; color: $foreground; }
        & > .ctree--standalone { text-style: bold; color: $foreground-muted; }
        & > .ctree--icon { color: $foreground-muted; }
    }
    """

    def __init__(self, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__("containers", **kwargs)
        self.show_root = False
        self.show_guides = False
        self.guide_depth = INDENT
        self.auto_expand = False
        self.collapsed: set[str] = set()
        self.stats_columns = True
        self.pending: dict[str, str] = {}  # container id → in-flight verb
        self._shape: list[tuple[str, tuple[str, ...]]] = []

    @override
    def on_mount(self) -> None:
        self.watch(self.app, "theme", lambda _: self._invalidate(), init=False)

    # -- data

    @property
    def selected_row(self) -> Row | None:
        node = self.cursor_node
        return node.data if node is not None else None

    def set_groups(self, groups: list[Group], histories: dict[str, tuple[float, ...]], *, stats: bool = True,
                   cursor_key: str | None = None) -> None:
        """Update in place when the shape is unchanged, else rebuild keeping collapse + cursor.

        ``cursor_key`` forces a rebuild with the cursor on that row (``""`` = first container),
        used when switching targets.
        """
        shape = [(g.key, tuple(c.id for c in g.containers)) for g in groups]
        stats_changed = stats != self.stats_columns
        self.stats_columns = stats
        if shape == self._shape and not stats_changed and cursor_key is None:
            by_key = {n.data.key: n for n in self._all_nodes() if n.data is not None}
            for g in groups:
                node = by_key.get(g.key)
                if node is None:
                    continue
                node.data = Row("group", g.key, g, hist=sum_histories([histories.get(c.id, ()) for c in g.containers]))
                node.refresh()
                for c in g.containers:
                    child = by_key.get(f"c:{c.id}")
                    if child is not None:
                        child.data = Row("container", f"c:{c.id}", g, c, histories.get(c.id, ()))
                        child.refresh()
            return
        cursor_line = self.cursor_line
        if cursor_key is None:
            cursor_key = self.selected_row.key if self.selected_row else None
        elif cursor_key == "":
            cursor_key, cursor_line = None, -1
        self._shape = shape
        self.clear()
        for g in groups:
            ghist = sum_histories([histories.get(c.id, ()) for c in g.containers])
            gnode = self.root.add(g.label, data=Row("group", g.key, g, hist=ghist),
                                  expand=g.key not in self.collapsed)
            for c in g.containers:
                gnode.add_leaf(c.name, data=Row("container", f"c:{c.id}", g, c, histories.get(c.id, ())))
        self._restore_cursor(cursor_key, cursor_line)

    def _all_nodes(self) -> list[TreeNode[Row]]:
        out: list[TreeNode[Row]] = []
        stack = list(self.root.children)
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(n.children)
        return out

    def _restore_cursor(self, key: str | None, line: int) -> None:
        self._tree_lines  # noqa: B018 - force line build so node.line is valid
        target = None
        if key is not None:
            target = next((n for n in self._all_nodes() if n.data and n.data.key == key), None)
            if target is not None and target.line < 0 and target.parent is not None:
                target = target.parent  # hidden inside a collapsed group
        if target is not None and target.line >= 0:
            self.move_cursor(target)
        elif self.last_line >= 0:
            self.cursor_line = max(0, min(line if line >= 0 else self._first_container_line(), self.last_line))

    def _first_container_line(self) -> int:
        for i, tl in enumerate(self._tree_lines):
            if tl.node.data is not None and tl.node.data.kind == "container":
                return i
        return 0

    def select_key(self, key: str) -> bool:
        self._tree_lines  # noqa: B018
        node = next((n for n in self._all_nodes() if n.data and n.data.key == key), None)
        if node is None:
            return False
        if node.parent is not None and node.parent is not self.root and not node.parent.is_expanded:
            node.parent.expand()
            self._tree_lines  # noqa: B018
        self.move_cursor(node)
        return True

    # -- actions

    def action_collapse_or_parent(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            self.move_cursor(node.parent)

    def action_expand_or_next(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand and not node.is_expanded:
            node.expand()

    def action_scroll_home_cursor(self) -> None:
        self.cursor_line = 0

    def action_scroll_end_cursor(self) -> None:
        self.cursor_line = self.last_line

    def _on_tree_node_collapsed(self, event: Tree.NodeCollapsed[Row]) -> None:
        if event.node.data:
            self.collapsed.add(event.node.data.key)

    def _on_tree_node_expanded(self, event: Tree.NodeExpanded[Row]) -> None:
        if event.node.data:
            self.collapsed.discard(event.node.data.key)

    # -- rendering

    def _style(self, name: str) -> Style:
        return fg(self, name)

    def columns(self) -> Columns:
        width = self.scrollable_content_region.width - INDENT - DOT
        return layout(width, stats=self.stats_columns)

    @override
    def render_label(self, node: TreeNode[Row], base_style: Style, style: Style) -> Text:
        row = node.data
        if row is None:
            return Text(str(node.label))
        cols = self.columns()
        text = self._group_row(node, row, cols) if row.kind == "group" else self._container_row(row, cols)
        text.stylize_before(style)
        return text

    @override
    def get_label_width(self, node: TreeNode[Row]) -> int:
        return max(0, self.scrollable_content_region.width - (0 if node.data and node.data.kind == "group" else INDENT))

    def _cells(self, text: Text, cols: Columns, *, status: tuple[str, Style], cpu: str, spark: tuple[float, ...],
               mem: str, ports: str, image: str, dim: bool) -> None:
        muted = self._style("ctree--muted")
        num = muted if dim else Style()
        if cols.status:
            text.append(" " * GAP)
            text.append(status[0][: cols.status].ljust(cols.status), status[1])
        if cols.cpu:
            text.append(" " * GAP)
            text.append(cpu.rjust(cols.cpu), num)
        if cols.spark:
            text.append(" " * GAP)
            line = sparkline(spark, cols.spark, levels=4) if spark else ""
            text.append(line.ljust(cols.spark), self._style("ctree--spark") + (muted if dim else Style()))
        if cols.mem:
            text.append(" " * GAP)
            text.append(mem.rjust(cols.mem), num)
        if cols.ports:
            text.append(" " * GAP)
            text.append(ports.ljust(cols.ports), muted)
        if cols.image:
            text.append(" " * GAP)
            text.append(truncate(image, cols.image).ljust(cols.image), muted)

    def _group_row(self, node: TreeNode[Row], row: Row, cols: Columns) -> Text:
        g = row.group
        icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
        text = Text(no_wrap=True, end="")
        text.append(icon, self._style("ctree--icon") + Style(meta={"toggle": True}))
        name_w = cols.name + DOT
        label_style = self._style("ctree--standalone" if not g.project else "ctree--group")
        text.append(truncate(g.label, name_w - 1).ljust(name_w), label_style)
        counts = f"{g.running}/{g.total} up"
        status_style = self._style("ctree--ok" if g.running == g.total else "ctree--muted")
        self._cells(text, cols, status=(counts, status_style), cpu=percent(g.cpu) if g.running else "",
                    spark=row.hist if g.running else (), mem=human_bytes(g.mem) if g.mem is not None else "",
                    ports="", image="", dim=False)
        return text

    def _container_row(self, row: Row, cols: Columns) -> Text:
        c = row.container
        assert c is not None
        dim = not c.running
        glyph, dot_class = dot_for(c)
        text = Text(no_wrap=True, end="")
        text.append(glyph + " ", self._style(dot_class))
        name = display_name(c, row.group)
        text.append(truncate(name, cols.name - 1).ljust(cols.name), self._style("ctree--dim") if dim else Style())
        st = c.stats
        status = short_status(c.state, c.status)
        status_class = "ctree--bad" if dot_class == "ctree--bad" and not c.running else "ctree--muted"
        if c.running and c.health == "unhealthy":
            status, status_class = "unhealthy", "ctree--bad"
        if c.id in self.pending:
            status, status_class = f"{self.pending[c.id]}…", "ctree--warn"
        self._cells(
            text, cols, status=(status, self._style(status_class)),
            cpu=percent(st.cpu_percent) if st else ("–" if c.running else ""),
            spark=row.hist if c.running else (),
            mem=human_bytes(st.mem_bytes) if st else ("–" if c.running else ""),
            ports=ports_text(c.ports, cols.ports), image=c.image, dim=dim,
        )
        return text


def header_text(cols: Columns, muted: Style) -> Text:
    """Column titles aligned with :class:`ContainerTree` rows."""
    text = Text(no_wrap=True, end="", style=muted)
    text.append(" " * (INDENT + DOT))
    text.append("NAME".ljust(cols.name))
    for title, width, right in (("STATUS", cols.status, False), ("CPU", cols.cpu, True), ("", cols.spark, False),
                                ("MEM", cols.mem, True), ("PORTS", cols.ports, False), ("IMAGE", cols.image, False)):
        if width:
            text.append(" " * GAP)
            text.append(title.rjust(width) if right else title.ljust(width))
    return text
