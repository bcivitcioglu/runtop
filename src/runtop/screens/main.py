"""The main 3-column screen: sidebar · grouped containers / images · detail."""

from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Static, Tree
from typing_extensions import override

from runtop.appref import runtop_app
from runtop.data.backend import LogsBackend, StorageBackend
from runtop.data.format import human_bytes
from runtop.data.models import DaemonState, TargetKind, TargetSnapshot
from runtop.screens.archive import ArchiveScreen
from runtop.screens.project_logs import ProjectLogsScreen
from runtop.screens.storage import StorageScreen
from runtop.state.actions import ACTION_KEYS, BY_ID, Disabled, Subject, resolve
from runtop.state.store import Section
from runtop.state.viewmodel import SORTS, PaneKind, build_groups, filter_images, state_to_pane
from runtop.widgets.container_tree import ContainerTree, Row, header_text
from runtop.widgets.detail import ActionBar, DetailPane, DetailSubject
from runtop.widgets.empty_state import EmptyState
from runtop.widgets.footer import RuntopFooter
from runtop.widgets.images_table import ImagesTable
from runtop.widgets.kwargs import WidgetKwargs
from runtop.widgets.sidebar import SectionSelector, Sidebar, TargetList, TargetStatus
from runtop.widgets.splitter import Splitter
from runtop.widgets.styles import fg
from runtop.widgets.topbar import Breadcrumb, ReadOnlyStrip, RefreshIndicator, TopBar

if TYPE_CHECKING:
    from textual.dom import DOMNode

    from runtop.app import RuntopApp

SIDEBAR_WIDTH = 26
DETAIL_WIDTH = 52
COLUMNS = ("sidebar", "middle", "detail")


class ColumnHeader(Static):
    """Column titles aligned to the container rows (re-rendered on resize)."""

    COMPONENT_CLASSES = {"colhead--text"}
    DEFAULT_CSS = """
    ColumnHeader {
        height: 1; padding: 0 1 0 0; background: transparent;
        & > .colhead--text { color: $foreground-muted; }
    }
    """

    def __init__(self, tree: ContainerTree, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self._ctree = tree

    @override
    def render(self) -> Text:
        return header_text(self._ctree.columns(), fg(self, "colhead--text"))


class MiddleTitle(Static):
    COMPONENT_CLASSES = {"mtitle--muted"}
    DEFAULT_CSS = """
    MiddleTitle {
        width: 1fr; height: 1;
        & > .mtitle--muted { color: $foreground-muted; }
    }
    """

    parts: reactive[tuple[str, str]] = reactive(("", ""))

    @override
    def render(self) -> Text:
        title, meta = self.parts
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(title, "bold")
        if meta:
            text.append("   " + meta, fg(self, "mtitle--muted"))
        return text


class MainScreen(Screen[None]):
    AUTO_FOCUS = ""
    BINDINGS = [
        Binding("tab", "next_column", "Pane", show=False, priority=True),
        Binding("shift+tab", "prev_column", "Pane", show=False, priority=True),
        Binding("slash", "filter", "Filter"),
        Binding("s", "act('s')", "Start"),
        Binding("x", "act('x')", "Stop"),
        Binding("R", "act('R')", "Restart"),
        Binding("X", "act('X')", "Remove"),
        Binding("e", "act('e')", "Shell"),
        Binding("p", "act('p')", "Prune"),
        Binding("right_square_bracket", "next_target", "Machine", key_display="[ ]"),
        Binding("left_square_bracket", "prev_target", "Prev machine", show=False),
        Binding("o", "sort", "Sort", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("1", "tab('tab-info')", "Info", show=False),
        Binding("2", "tab('tab-logs')", "Logs", show=False),
        Binding("3", "tab('tab-stats')", "Stats", show=False),
        Binding("i", "toggle_detail", "Details"),
        Binding("L", "archives", "Archives", show=False),
        Binding("D", "storage", "Storage", show=False),
        Binding("l", "project_logs", "Project logs", show=False),
        Binding("y", "copy_id", "Copy ID", show=False),
        Binding("escape", "escape", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.section = Section.CONTAINERS
        self.query_text = ""
        self.sort = "name"
        self._synced_key: str | None = None
        self._ready = False

    @property
    def lapp(self) -> RuntopApp:
        return runtop_app(self.app)

    # ------------------------------------------------------------------ layout

    @override
    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        yield ReadOnlyStrip(id="readonly")
        yield Static("", id="data-warning", markup=False)
        yield Static("", id="recording-status", markup=False)
        with Horizontal(id="columns"):
            yield Sidebar(id="sidebar")
            yield Splitter("sidebar", "left", default=SIDEBAR_WIDTH, minimum=18, maximum=48, id="split-left")
            with Vertical(id="middle"):
                with Horizontal(id="middle-head"):
                    yield MiddleTitle(id="middle-title")
                    yield Input(placeholder="filter…", id="filter")
                yield ActionBar("images", id="image-actions")
                tree = ContainerTree(id="tree")
                yield ColumnHeader(tree, id="colhead")
                with ContentSwitcher(initial="empty", id="switch"):
                    yield tree
                    yield ImagesTable(id="images")
                    yield EmptyState(id="empty")
            yield Splitter("detail", "right", default=DETAIL_WIDTH, minimum=30, maximum=90, id="split-right")
            yield DetailPane(id="detail")
        yield RuntopFooter(id="footer")

    def on_mount(self) -> None:
        self.set_interval(0.5, self._recording_status)
        self._recording_status()
        cfg = self.lapp.config
        self.query_one("#sidebar").styles.width = (cfg.sidebar_width if cfg and cfg.sidebar_width else SIDEBAR_WIDTH)
        self.query_one("#detail").styles.width = (cfg.detail_width if cfg and cfg.detail_width else DETAIL_WIDTH)
        self.query_one("#filter").display = False
        self._ready = True
        self.sync()
        self.call_after_refresh(self.lapp.mark_ui_ready)

    def column_widths(self) -> tuple[int | None, int | None]:
        def width(cid: str) -> int | None:
            w = self.query_one(f"#{cid}").styles.width
            return int(w.value) if w is not None and w.unit.name == "CELLS" else None

        return width("sidebar"), width("detail")

    def on_resize(self) -> None:
        self.query_one(ColumnHeader).refresh()

    # ------------------------------------------------------------------ sync from store

    def sync(self, key: str | None = None) -> None:
        if not self._ready:
            return
        store = self.lapp.store
        selected = store.selected
        target = store.target(selected)
        snap = store.snapshot(selected)

        warning = self.query_one("#data-warning", Static)
        problems = [store.discover_error] if store.discover_error else []
        if snap is not None:
            if snap.stale:
                problems.append(f"STALE · showing last successful data · {snap.error} · r retry")
            if snap.images_error:
                problems.append(f"Images unavailable · {snap.images_error} · r retry")
        warning.update(" | ".join(problems))
        warning.display = bool(problems)
        warning.styles.height = "auto"

        # sidebar
        statuses = []
        for t in store.targets:
            s = store.snapshot(t.key)
            running = sum(1 for c in s.containers if c.running) if s and s.state is DaemonState.OK else None
            statuses.append(TargetStatus(t, s.state if s else None, running, t.key in store.fetching,
                                         store.pending.get(t.key)))
        self.query_one(TargetList).set_targets(statuses, selected)
        ok = snap is not None and snap.state is DaemonState.OK
        counts = {
            Section.CONTAINERS: len(snap.containers) if ok and snap else None,
            Section.IMAGES: len(snap.images) if ok and snap and snap.images_loaded else None,
        }
        self.query_one(SectionSelector).set_counts(counts, self.section)

        # top bar + read-only strip
        crumbs = (target.name, "Containers" if self.section is Section.CONTAINERS else "Images") if target else ()
        self.query_one(Breadcrumb).parts = crumbs
        refresh = self.query_one(RefreshIndicator)
        refresh.fetching = selected in store.fetching
        refresh.fetched_at = snap.fetched_at if snap else 0.0
        refresh.state_label = ("stale" if snap and snap.stale else "offline"
                               if snap and snap.state is not DaemonState.OK else "partial"
                               if snap and snap.images_error else "")
        chips = []
        if self.sort != "name" and self.section is Section.CONTAINERS:
            chips.append(f"sort: {self.sort}")
        if self.query_text:
            chips.append(f"/{self.query_text}")
        refresh.chips = tuple(chips)
        self.query_one(ReadOnlyStrip).show_for(target.name if target and target.read_only else None,
                                               target.endpoint if target else "")

        # middle
        pane = state_to_pane(targets_loaded=store.targets_loaded, discover_error=store.discover_error,
                             target=target, snapshot=snap, section=self.section, query=self.query_text)
        switch = self.query_one("#switch", ContentSwitcher)
        tree = self.query_one(ContainerTree)
        images = self.query_one(ImagesTable)
        colhead = self.query_one(ColumnHeader)
        self.query_one(MiddleTitle).parts = self._title_parts(snap)
        if pane.kind is PaneKind.CONTENT and snap is not None and selected is not None:
            if self.section is Section.CONTAINERS:
                groups = build_groups(snap.containers, self.query_text, self.sort)
                hist = {c.id: store.cpu_history(selected, c.id) for c in snap.containers}
                cursor_key = None
                if self._synced_key != selected:
                    tree.collapsed = set(self.lapp.collapsed.get(selected, set()))
                    cursor_key = self.lapp.cursors.get(selected, "")
                prefix = f"{selected}/"
                tree.pending = {k[len(prefix):]: v for k, v in store.pending.items() if k.startswith(prefix)}
                tree.set_groups(groups, hist, stats=target is not None and target.kind is not TargetKind.CONTEXT,
                                cursor_key=cursor_key)
                self.lapp.collapsed[selected] = tree.collapsed
                self._switch_to(switch, "tree")
                colhead.display = True
                colhead.refresh()
            else:
                images.set_images(filter_images(snap.images, self.query_text))
                self._switch_to(switch, "images")
                colhead.display = False
        else:
            self.query_one(EmptyState).show(pane)
            self._switch_to(switch, "empty")
            colhead.display = False
        bar = self.query_one("#image-actions", ActionBar)
        bar.display = self.section is Section.IMAGES
        bar.update_subject(self.current_subject())
        self._synced_key = selected
        self._update_detail()
        self.refresh_bindings()  # footer gating follows pending actions and state changes

    def _switch_to(self, switch: ContentSwitcher, name: str) -> None:
        loading = self.query_one(EmptyState).state.kind is PaneKind.LOADING
        if switch.current == name:
            if self.focused is None and not (name == "empty" and loading):
                self.call_after_refresh(self._focus_middle_or_sidebar)
            return
        had_focus = self.focused is not None and self._column_of(self.focused) == "middle"
        switch.current = name
        if had_focus or (self.focused is None and not (name == "empty" and loading)):
            self.call_after_refresh(self._focus_middle_or_sidebar)
            # the first focus after a ContentSwitcher flip can be swallowed; confirm shortly after
            self.set_timer(0.15, self._ensure_focus)

    def _ensure_focus(self) -> None:
        if self.focused is None:
            self._focus_middle_or_sidebar()

    def _focus_middle_or_sidebar(self) -> None:
        # A deferred refresh must not undo navigation made since it was scheduled.
        if self.focused is not None and self._column_of(self.focused) != "middle":
            return
        widget = self._middle_widget()
        if widget is not None:
            widget.focus()
        elif self.focused is None or self._column_of(self.focused) == "middle":
            self.query_one(TargetList).focus()

    def _title_parts(self, snap: TargetSnapshot | None) -> tuple[str, str]:
        section = "Containers" if self.section is Section.CONTAINERS else "Images"
        meta = ""
        if snap is not None and snap.state is DaemonState.OK:
            if self.section is Section.CONTAINERS:
                running = sum(1 for c in snap.containers if c.running)
                meta = f"{running} running · {len(snap.containers)} total"
            else:
                dangling = sum(1 for i in snap.images if i.dangling)
                meta = f"{len(snap.images)} images · {human_bytes(sum(i.size_bytes for i in snap.images))}"
                if dangling:
                    meta += f" · {dangling} dangling"
        if self.query_text and snap is not None:
            matched = (sum(g.total for g in build_groups(snap.containers, self.query_text, self.sort))
                       if self.section is Section.CONTAINERS else len(filter_images(snap.images, self.query_text)))
            meta = f"{matched} matches · /{self.query_text} · esc clear"
        return section, meta

    def action_archives(self) -> None:
        subject = self.current_subject()
        containers = ((subject.container,) if subject.container else
                      subject.group.containers if subject.group else ())
        self.app.push_screen(ArchiveScreen(subject.target, tuple(containers)))

    def _recording_status(self) -> None:
        capture = self.lapp.capture
        state = self.query_one("#recording-status", Static)
        state.display = capture.active or bool(capture.error)
        label = "● Recording" if capture.active else "Recording stopped"
        state.update(f"{label} · {capture.written} records · {capture.directory} · L archives"
                     + (f" · {capture.error}" if capture.error else ""))

    def action_project_logs(self) -> None:
        sub = self.current_subject()
        backend = self.lapp.backend
        if sub.target and sub.group and not sub.container and sub.group.project and isinstance(backend, LogsBackend):
            self.app.push_screen(ProjectLogsScreen(backend, sub.target, sub.group.project, sub.group.containers))

    def action_storage(self) -> None:
        target = self.lapp.store.target(self.lapp.store.selected)
        backend = self.lapp.backend
        if target is None or not isinstance(backend, StorageBackend):
            return
        self.app.push_screen(StorageScreen(target, backend))

    def action_copy_id(self) -> None:
        sub = self.current_subject()
        value = sub.container.id if sub.container else sub.image.id if sub.image else ""
        if value:
            self.app.copy_to_clipboard(value)
            self.app.notify("Copied ID (requires terminal clipboard support)")

    # ------------------------------------------------------------------ detail

    def _middle_widget(self) -> Widget | None:
        current = self.query_one("#switch", ContentSwitcher).current
        if current == "tree":
            return self.query_one(ContainerTree)
        if current == "images":
            return self.query_one(ImagesTable)
        return None

    def _update_detail(self) -> None:
        store = self.lapp.store
        snap = store.snapshot(store.selected)
        focus_col = self._column_of(self.focused) if self.focused else None
        current = self.query_one("#switch", ContentSwitcher).current
        target = store.target(store.selected)
        if snap is None and target is not None:
            snap = TargetSnapshot(target, DaemonState.LOADING)
        subject = DetailSubject("target", snap) if snap else DetailSubject("none")
        if focus_col != "sidebar" and snap is not None and snap.state is DaemonState.OK:
            if current == "tree":
                row: Row | None = self.query_one(ContainerTree).selected_row
                if row is not None and row.kind == "container" and row.container is not None:
                    subject = DetailSubject("container", snap, container=snap.container(row.container.id)
                                            or row.container)
                elif row is not None:
                    subject = DetailSubject("group", snap, group=row.group)
            elif current == "images":
                img = self.query_one(ImagesTable).selected_image
                if img is not None:
                    subject = DetailSubject("image", snap, image=img)
        action_subject = self.current_subject()
        self.query_one("#detail", DetailPane).show(subject, action_subject)
        overlay = self.lapp.detail_overlay()
        if overlay is not None:
            overlay.show(subject, action_subject)

    # ------------------------------------------------------------------ action subject

    def current_subject(self) -> Subject:
        store = self.lapp.store
        key = store.selected
        target = store.target(key)
        snap = store.snapshot(key)
        focus = self._column_of(self.focused) or "middle"
        if self.lapp.detail_overlay() is not None:
            focus = "detail"
        container = group = image = None
        current = self.query_one("#switch", ContentSwitcher).current
        if focus != "sidebar" and snap is not None and snap.state is DaemonState.OK:
            if current == "tree":
                row = self.query_one(ContainerTree).selected_row
                if row is not None:
                    group = row.group
                    if row.kind == "container" and row.container is not None:
                        container = snap.container(row.container.id) or row.container
            elif current == "images":
                image = self.query_one(ImagesTable).selected_image
        return Subject(target=target, snapshot=snap, container=container, group=group, image=image, focus=focus,
                       demo=self.lapp.is_demo, pending=frozenset(store.pending))

    @on(ActionBar.Requested)
    def _button_action(self, event: ActionBar.Requested) -> None:
        event.stop()
        self.lapp.trigger(BY_ID[event.action_id], self.current_subject())

    def action_act(self, key: str) -> None:
        subject = self.current_subject()
        spec = resolve(key, subject)
        if spec is not None:
            self.lapp.trigger(spec, subject)

    def action_tab(self, tab: str) -> None:
        self.query_one("#detail", DetailPane).set_tab(tab)

    def jump_to_container(self, cid: str) -> bool:
        """Palette jump: show the Containers section with ``cid`` under the cursor.

        (Not ``select_container``: that name is Textual's text-selection container property.)
        """
        if self.section is not Section.CONTAINERS:
            self.section = Section.CONTAINERS
            self.sync()
        store = self.lapp.store
        snap = store.snapshot(store.selected)
        containers = snap.containers if snap is not None else ()
        if self.query_text and not any(c.id == cid for g in build_groups(containers, self.query_text)
                                       for c in g.containers):
            self._close_filter(keep=False)
        tree = self.query_one(ContainerTree)
        found = tree.select_key(f"c:{cid}")
        if found:
            tree.focus()
        return found

    @on(Tree.NodeHighlighted)
    @on(DataTable.RowHighlighted)
    def _selection_moved(self) -> None:
        row = self.query_one(ContainerTree).selected_row
        on_tree = self.query_one("#switch", ContentSwitcher).current == "tree"
        if row is not None and self._synced_key is not None and on_tree:
            self.lapp.cursors[self._synced_key] = row.key
        self._update_detail()

    def on_descendant_focus(self) -> None:
        self._mark_focused_column()
        self._update_detail()

    def on_descendant_blur(self) -> None:
        self.call_after_refresh(self._mark_focused_column)

    def _mark_focused_column(self) -> None:
        col = self._column_of(self.focused)
        for cid in COLUMNS:
            self.query_one(f"#{cid}").set_class(cid == col, "-focused")

    # ------------------------------------------------------------------ sidebar events

    @on(TargetList.TargetHighlighted)
    def _target_highlighted(self, event: TargetList.TargetHighlighted) -> None:
        self.lapp.poller.select(event.key)

    @on(SectionSelector.Changed)
    def _section_highlighted(self, event: SectionSelector.Changed) -> None:
        self.section = event.section
        self.sync()

    @on(TargetList.OptionSelected)
    @on(SectionSelector.Opened)
    def _sidebar_enter(self) -> None:
        self._focus_column("middle")

    @on(Tree.NodeSelected)
    def _node_selected(self, event: Tree.NodeSelected[Row]) -> None:
        node = event.node
        if node.allow_expand:
            node.toggle()
        elif node.data is not None and node.data.kind == "container":
            self.open_logs()

    def open_logs(self) -> None:
        """enter on a container: Logs tab, focused (overlay on narrow terminals)."""
        if not self.query_one("#detail").region.width:
            self.action_toggle_detail()
            overlay = self.lapp.detail_overlay()
            if overlay is not None:
                overlay.set_tab("tab-logs")
            return
        pane = self.query_one("#detail", DetailPane)
        pane.set_tab("tab-logs")
        self.call_after_refresh(lambda: pane.log_view.log_widget.focus())

    # ------------------------------------------------------------------ columns / focus

    def _column_of(self, widget: Widget | None) -> str | None:
        node: DOMNode | None = widget
        while node is not None:
            column = next((col for col in COLUMNS if node.id == col), None)
            if column is not None:
                return column
            node = node.parent
        return None

    def _visible_columns(self) -> list[str]:
        cols = []
        for cid in COLUMNS:
            w = self.query_one(f"#{cid}")
            if w.display and w.styles.display != "none" and w.region.width > 0:
                cols.append(cid)
        return cols or ["middle"]

    def _focus_column(self, col: str) -> bool:
        if col == "sidebar":
            targets = self.query_one(TargetList)
            sections = self.query_one(SectionSelector)
            (sections if self.focused is sections else targets).focus()
            return True
        if col == "middle":
            widget = self._middle_widget()
            if widget is None:
                return False
            widget.focus()
            return True
        if col == "detail":
            pane = self.query_one("#detail", DetailPane)
            chain = [w for w in self.focus_chain if pane in w.ancestors_with_self]
            if not chain:
                return False
            if pane.subject.kind == "container":
                tabs = {"tab-logs": pane.log_view.log_widget}
                preferred = tabs.get(pane.active_tab)
                if preferred is not None and preferred in chain:
                    preferred.focus()
                    return True
                for w in chain:
                    if not isinstance(w, Button):
                        w.focus()
                        return True
            chain[0].focus()
            return True
        return False

    def _cycle(self, step: int) -> None:
        if isinstance(self.focused, Input):
            self._close_filter(keep=True)
        cols = self._visible_columns()
        current = self._column_of(self.focused)
        i = cols.index(current) if current in cols else -1
        for n in range(1, len(cols) + 1):
            if self._focus_column(cols[(i + step * n) % len(cols)]):
                return

    def action_next_column(self) -> None:
        self._cycle(+1)

    def action_prev_column(self) -> None:
        self._cycle(-1)

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        if event.character is not None and event.character in ACTION_KEYS and not isinstance(focused, Input):
            subject = self.current_subject()
            spec = resolve(event.character, subject)
            avail = spec.available(subject) if spec else None
            if spec is not None and isinstance(avail, Disabled):
                event.stop()
                event.prevent_default()
                self.lapp.refuse(spec, subject, avail.reason)
            return
        if event.key in ("left", "h") and isinstance(focused, ContainerTree):
            node = focused.cursor_node
            if node is None or (node.allow_expand and not node.is_expanded):
                event.stop()
                self._focus_column("sidebar")
        elif event.key in ("left", "h") and isinstance(focused, ImagesTable):
            event.stop()
            self._focus_column("sidebar")
        elif event.key in ("right", "l") and isinstance(focused, TargetList):
            event.stop()
            self._focus_column("middle")

    # ------------------------------------------------------------------ actions

    def action_refresh(self) -> None:
        self.lapp.poller.refresh()

    def _step_target(self, step: int) -> None:
        store = self.lapp.store
        keys = [t.key for t in store.targets]
        if not keys:
            return
        i = keys.index(store.selected) if store.selected in keys else -1
        self.lapp.poller.select(keys[(i + step) % len(keys)])

    def action_next_target(self) -> None:
        self._step_target(+1)

    def action_prev_target(self) -> None:
        self._step_target(-1)

    def action_sort(self) -> None:
        self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        self.sync()

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.value = self.query_text
        box.focus()

    def _close_filter(self, keep: bool) -> None:
        box = self.query_one("#filter", Input)
        if not keep:
            self.query_text = ""
            box.value = ""
        box.display = bool(self.query_text)
        self.sync()
        self._focus_middle_or_sidebar()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.query_text = event.value
        self.sync()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        self._close_filter(keep=True)

    def action_escape(self) -> None:
        if isinstance(self.focused, Input) or self.query_text:
            self._close_filter(keep=False)

    def action_toggle_detail(self) -> None:
        if self.query_one("#detail").region.width:
            self._focus_column("detail")
            return
        self.lapp.open_detail_overlay()

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.focused, Input) and action in ("sort", "refresh", "next_target", "prev_target",
                                                          "toggle_detail", "filter", "act", "tab"):
            return False
        if action == "act" and parameters:
            subject = self.current_subject()
            spec = resolve(str(parameters[0]), subject)
            if spec is None:
                return False
            return True if spec.available(subject) else None  # None: shown dimmed in the footer
        if action == "tab":
            return True if self.query_one("#detail", DetailPane).subject.kind == "container" else None
        return True
