"""Right detail pane: header card, action buttons and Info · Logs · Stats for the selection.

Views: container (tabs), compose group (aggregate charts + per-service rows), image, machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Unpack

from rich.console import Group as RichGroup
from rich.console import RenderableType
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, ContentSwitcher, Static, TabbedContent, TabPane
from typing_extensions import override

from runtop.appref import runtop_app
from runtop.data.backend import InspectBackend, LogsBackend
from runtop.data.engine import LogLine
from runtop.data.format import human_bytes, human_bytes_long, percent, tilde
from runtop.data.models import Container, DaemonState, Image, TargetKind, TargetSnapshot
from runtop.data.wire import ContainerInspect, EndpointSettings, InspectHostConfig, InspectNetworkSettings
from runtop.state.actions import REGISTRY, ActionSpec, Disabled, Subject
from runtop.state.viewmodel import Group, display_name
from runtop.widgets.charts import BlockChart, bar_text
from runtop.widgets.container_tree import dot_for, sum_histories
from runtop.widgets.kwargs import WidgetKwargs
from runtop.widgets.log_view import LogView
from runtop.widgets.styles import fg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from runtop.app import RuntopApp


@dataclass(frozen=True, slots=True)
class DetailSubject:
    kind: str  # "container" | "group" | "image" | "target" | "none"
    snapshot: TargetSnapshot | None = None
    container: Container | None = None
    group: Group | None = None
    image: Image | None = None


STATE_REASONS = {"already running", "not running", "already stopped"}
TONE = {"ctree--ok": "ok", "ctree--warn": "warn", "ctree--bad": "bad", "ctree--idle": "idle"}


def uptime_text(c: Container) -> str:
    status = c.status
    if c.running and status.startswith("Up "):
        return "up " + status[3:].split(" (", 1)[0].lower()
    if status.startswith("Exited") and ") " in status:
        return "exited " + status.split(") ", 1)[1].lower()
    return status.lower()


def mask_env(env: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in env:
        k, _, v = item.partition("=")
        out.append((k, "•" * min(8, max(3, len(v))) if v else ""))
    return out


def two_col(card: Card, title: str, value: str, extra: str = "") -> Table:
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(justify="right")
    right = Text(value, style=card.s("title"))
    if extra:
        right.append(extra, card.s("muted"))
    grid.add_row(Text(title, style=card.s("key")), right)
    return grid


# ---------------------------------------------------------------- building blocks


class Card(Static):
    """Rich-rendered block whose colours come from theme component classes."""

    COMPONENT_CLASSES = {
        "card--title", "card--sub", "card--key", "card--ok", "card--warn", "card--bad", "card--idle",
        "card--muted", "card--bar", "card--track", "card--badge-ok", "card--badge-warn", "card--badge-bad",
        "card--badge-idle", "card--mono",
    }
    DEFAULT_CSS = """
    Card {
        width: 1fr; height: auto;
        & > .card--title { text-style: bold; color: $foreground; }
        & > .card--sub { color: $foreground-muted; }
        & > .card--key { color: $foreground-muted; }
        & > .card--ok { color: $success; }
        & > .card--warn { color: $warning; }
        & > .card--bad { color: $error; }
        & > .card--idle { color: $foreground-muted; }
        & > .card--muted { color: $foreground-muted; }
        & > .card--bar { color: $primary; }
        & > .card--track { color: $foreground 12%; }
        & > .card--badge-ok { color: $success; background: $success 16%; text-style: bold; }
        & > .card--badge-warn { color: $warning; background: $warning 16%; text-style: bold; }
        & > .card--badge-bad { color: $error; background: $error 16%; text-style: bold; }
        & > .card--badge-idle { color: $foreground-muted; background: $foreground 8%; text-style: bold; }
        & > .card--mono { color: $foreground; }
    }
    """

    def s(self, name: str, *, bg: bool = False) -> Style:
        return fg(self, f"card--{name}", keep_bg=bg)

    def badge(self, text: str, tone: str) -> Text:
        return Text(f" {text} ", style=self.s(f"badge-{tone}", bg=True))

    def header(self, title: str, badge: Text | None, subtitle: Text | str) -> Table:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(Text(title, style=self.s("title")), badge or "")
        sub = subtitle if isinstance(subtitle, Text) else Text(subtitle, style=self.s("sub"))
        sub.no_wrap, sub.overflow = True, "ellipsis"
        grid.add_row(sub, "")
        return grid

    def facts(self, rows: list[tuple[str, RenderableType]], key_width: int = 9) -> Table:
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(no_wrap=True, width=key_width)
        grid.add_column(ratio=1, overflow="fold")
        for key, value in rows:
            grid.add_row(Text(key.upper(), style=self.s("key")), Text(value) if isinstance(value, str) else value)
        return grid

    def meter(self, used: float, total: float, width: int = 22) -> Text:
        frac = used / total if total else 0.0
        text = bar_text(frac, width, self.s("bar"), self.s("track"))
        text.append(f"  {frac * 100:.0f}%", self.s("muted"))
        return text


class ActionButton(Button):
    """A registry action's button (styled by ``ActionBar > Button``)."""

    def __init__(self, label: str, action_id: str, *, destructive: bool, id: str) -> None:
        super().__init__(label, id=id, compact=True, disabled=True, classes="-destructive" if destructive else "")
        self.action_id = action_id


class ActionBar(Horizontal):
    """Buttons for one registry scope. Disabled state and tooltip come from the registry."""

    class Requested(Message):
        def __init__(self, action_id: str) -> None:
            super().__init__()
            self.action_id = action_id

    DEFAULT_CSS = """
    ActionBar {
        height: 1; width: 1fr; margin: 1 0 0 0;
        & > Button {
            min-width: 0; height: 1; border: none; margin: 0 1 0 0; padding: 0;
            background: $foreground 8%; color: $foreground; text-style: none;
            &:hover { background: $primary 35%; }
            &:focus { background: $primary 45%; text-style: bold; }
            &.-destructive { color: $error; }
            &.-destructive:hover { background: $error 25%; }
            &:disabled { color: $foreground 35%; background: $foreground 4%; }
        }
    }
    """

    def __init__(self, scope: str, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self.specs: list[ActionSpec] = [spec for spec in REGISTRY if spec.scope == scope]

    @staticmethod
    def button_id(spec: ActionSpec) -> str:
        return "btn-" + spec.id.replace(".", "-")

    @override
    def compose(self) -> ComposeResult:
        for spec in self.specs:
            label = spec.title.split(" ")[0] if spec.scope != "images" else "Prune dangling"
            yield ActionButton(f"{spec.glyph} {label}", spec.id, destructive=spec.destructive, id=self.button_id(spec))

    def update_subject(self, subject: Subject) -> None:
        for spec in self.specs:
            try:
                button = self.query_one(f"#{self.button_id(spec)}", Button)
            except Exception:
                continue
            avail = spec.available(subject)
            reason = avail.reason if isinstance(avail, Disabled) else ""
            # Start/Stop swap places instead of showing a permanently disabled twin.
            button.display = reason not in STATE_REASONS
            button.disabled = not avail
            button.tooltip = f"{spec.label(subject)}  [{spec.key}]" if avail else f"{spec.title}: {reason}"

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if isinstance(event.button, ActionButton) and event.button.action_id:
            self.post_message(self.Requested(event.button.action_id))


# ---------------------------------------------------------------- container


class StatsView(VerticalScroll):
    DEFAULT_CSS = """
    StatsView {
        height: 1fr; scrollbar-size-vertical: 1;
        & > BlockChart { height: 5; margin: 0 0 1 0; }
        & > #st-mem-bar { margin: 0 0 1 0; }
    }
    """

    @override
    def compose(self) -> ComposeResult:
        yield Card(id="st-cpu-head")
        yield BlockChart(id="st-cpu", floor=5.0, fmt=lambda v: f"{v:g}%")
        yield Card(id="st-mem-head")
        yield BlockChart(id="st-mem", floor=16 << 20, fmt=human_bytes, zero_based=False)
        yield Card(id="st-mem-bar")
        yield Card(id="st-note")

    def update_stats(self, c: Container, cpu: tuple[float, ...], mem: tuple[int, ...], *, remote: bool,
                     interval: float) -> None:
        cpu_head, mem_head = self.query_one("#st-cpu-head", Card), self.query_one("#st-mem-head", Card)
        note, bar = self.query_one("#st-note", Card), self.query_one("#st-mem-bar", Card)
        charts = [self.query_one("#st-cpu", BlockChart), self.query_one("#st-mem", BlockChart)]
        live = c.running and not remote
        for w in (*charts, mem_head, bar):
            w.display = live
        if not live:
            reason = ("Stats aren't polled on remote contexts: each sample would be an ssh round trip."
                      if remote else f"{c.name} is not running, so there is nothing to measure.")
            cpu_head.update(Text(reason, style=cpu_head.s("muted")))
            note.update("")
            return
        st = c.stats
        cpu_head.update(two_col(cpu_head, "CPU", percent(st.cpu_percent) if st else "–"))
        charts[0].values = tuple(cpu)
        limit = st.mem_limit_bytes if st else 0
        mem_head.update(two_col(mem_head, "MEMORY", human_bytes_long(st.mem_bytes) if st else "–",
                                f" / {human_bytes_long(limit)}" if limit else ""))
        charts[1].values = tuple(float(m) for m in mem)
        bar.update(bar.meter(st.mem_bytes, limit, 24) if st and limit else "")
        span = max(1, len(cpu)) * interval
        window = f"{span / 60:.0f} min" if span >= 90 else f"{span:.0f} s"
        note.update(Text(f"last {window} · sampled every {interval:g} s", style=note.s("muted")))


class ContainerView(Vertical):
    DEFAULT_CSS = """
    ContainerView {
        height: 1fr;
        & > #cv-head { padding: 0 2; }
        & > ActionBar { padding: 0 2; }
        & > TabbedContent { height: 1fr; margin-top: 1; }
        TabbedContent > ContentTabs { margin: 0 2; }
        TabbedContent TabPane { height: 1fr; padding: 1 2 0 2; }
        #cv-info { height: 1fr; scrollbar-size-vertical: 1; }
    }
    """

    @override
    def compose(self) -> ComposeResult:
        yield Card(id="cv-head")
        yield ActionBar("container", id="cv-actions")
        with TabbedContent(id="cv-tabs", initial="tab-info"):
            with TabPane("Info", id="tab-info"), VerticalScroll(id="cv-info"):
                yield Card(id="cv-facts")
            with TabPane("Logs", id="tab-logs"):
                yield LogView(id="cv-logs")
            with TabPane("Stats", id="tab-stats"):
                yield StatsView(id="cv-stats")


class DetailPane(Vertical):
    DEFAULT_CSS = """
    DetailPane {
        width: 52;
        height: 1fr;
        background: $background;
        & > #detail-title { height: 1; padding: 0 2; color: $foreground-muted; text-style: bold; margin: 1 0 1 0; }
        & > ContentSwitcher { height: 1fr; }
        .dv-scroll { height: 1fr; padding: 0 2; scrollbar-size-vertical: 1; }
        .dv-scroll > BlockChart { height: 4; margin-bottom: 1; }
        .dv-scroll > Card { margin-bottom: 1; }
        .dv-scroll > .dv-label { margin-bottom: 0; }
        .dv-scroll > ActionBar { margin: 0 0 1 0; }
    }
    """

    TITLES = {"container": "CONTAINER", "group": "COMPOSE PROJECT", "image": "IMAGE", "target": "MACHINE",
              "none": "DETAILS"}

    def __init__(self, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(**kwargs)
        self.subject = DetailSubject("none")
        self.action_subject = Subject()
        self._inspect_key: str | None = None
        self._inspect: ContainerInspect | None = None
        self._inspect_error: str | None = None

    @property
    def lapp(self) -> RuntopApp:
        return runtop_app(self.app)

    @override
    def compose(self) -> ComposeResult:
        yield Static("DETAILS", id="detail-title")
        with ContentSwitcher(initial="dv-none", id="detail-switch"):
            yield ContainerView(id="dv-container")
            with VerticalScroll(id="dv-group", classes="dv-scroll"):
                yield Card(id="gv-head")
                yield ActionBar("project", id="gv-actions")
                yield Card(id="gv-cpu-label", classes="dv-label")
                yield BlockChart(id="gv-cpu", floor=5.0, fmt=lambda v: f"{v:g}%")
                yield Card(id="gv-mem-label", classes="dv-label")
                yield BlockChart(id="gv-mem", floor=16 << 20, fmt=human_bytes, zero_based=False)
                yield Card(id="gv-services")
            with VerticalScroll(id="dv-image", classes="dv-scroll"):
                yield Card(id="iv-head")
                yield Card(id="iv-facts")
            with VerticalScroll(id="dv-machine", classes="dv-scroll"):
                yield Card(id="mv-head")
                yield ActionBar("machine", id="mv-actions")
                yield Card(id="mv-facts")
            with VerticalScroll(id="dv-none", classes="dv-scroll"):
                yield Card(id="nv-text")

    def on_mount(self) -> None:
        self.watch(self.app, "theme", lambda _: self.show(self.subject, self.action_subject), init=False)

    # -- public

    @property
    def active_tab(self) -> str:
        return self.query_one("#cv-tabs", TabbedContent).active

    def set_tab(self, tab: str) -> None:
        self.query_one("#cv-tabs", TabbedContent).active = tab

    @property
    def log_view(self) -> LogView:
        return self.query_one(LogView)

    def show(self, subject: DetailSubject, action_subject: Subject | None = None) -> None:
        self.subject = subject
        if action_subject is not None:
            self.action_subject = action_subject
        title = self.TITLES.get(subject.kind, "DETAILS")
        if subject.kind == "group" and subject.group is not None and not subject.group.project:
            title = "STANDALONE"
        if subject.kind == "target" and subject.snapshot is not None and subject.snapshot.target.is_remote:
            title = "REMOTE CONTEXT"
        self.query_one("#detail-title", Static).update(title)
        switch = self.query_one("#detail-switch", ContentSwitcher)
        views = {"container": "dv-container", "group": "dv-group", "image": "dv-image", "target": "dv-machine"}
        current = views.get(subject.kind, "dv-none") if self._has_payload(subject) else "dv-none"
        if switch.current != current:
            switch.current = current
        if current != "dv-container":
            self.log_view.stop()
        {
            "dv-container": self._show_container, "dv-group": self._show_group, "dv-image": self._show_image,
            "dv-machine": self._show_machine, "dv-none": self._show_none,
        }[current]()

    @staticmethod
    def _has_payload(s: DetailSubject) -> bool:
        payload = {"container": s.container, "group": s.group, "image": s.image, "target": s.snapshot}
        return bool(payload.get(s.kind)) and (s.kind == "target" or s.snapshot is not None)

    # -- container

    def _show_container(self) -> None:
        sub = self.subject
        c, snap = sub.container, sub.snapshot
        assert c is not None and snap is not None
        head = self.query_one("#cv-head", Card)
        glyph, cls = dot_for(c)
        pending = self.lapp.store.pending_for(snap.key, c.id)
        badge = head.badge(f"{pending}…" if pending else f"{glyph} {c.state}",
                           "warn" if pending else TONE.get(cls, "idle"))
        sub_text = Text(c.image, style=head.s("sub"))
        sub_text.append(f" · {uptime_text(c)}", head.s("muted"))
        if c.health:
            sub_text.append(" · ", head.s("muted"))
            sub_text.append(c.health, head.s("ok" if c.health == "healthy" else "warn"))
        head.update(head.header(c.name, badge, sub_text))
        self.query_one("#cv-actions", ActionBar).update_subject(self.action_subject)
        self._show_info(c, snap)
        tab = self.active_tab
        logs = self.log_view
        if tab == "tab-logs":
            target = snap.target
            key = f"{target.key}/{c.id}/{'live' if c.running else 'done'}"
            backend = self.lapp.backend
            if isinstance(backend, LogsBackend):
                reader: LogsBackend = backend
                follow = c.running
                cid = c.id

                def source() -> AsyncIterator[LogLine]:
                    return reader.logs(target, cid, tail=200, follow=follow)

                logs.show(key, source)
            else:
                logs.show(key, None, placeholder="this backend has no logs")
        else:
            logs.stop()
        if tab == "tab-stats":
            store = self.lapp.store
            remote = snap.target.kind is TargetKind.CONTEXT
            interval = self.lapp.poller.remote_interval if remote else self.lapp.poller.local_interval
            self.query_one(StatsView).update_stats(c, store.cpu_history(snap.key, c.id),
                                                   store.mem_history(snap.key, c.id), remote=remote,
                                                   interval=interval)

    def _show_info(self, c: Container, snap: TargetSnapshot) -> None:
        facts = self.query_one("#cv-facts", Card)
        key = f"{snap.key}/{c.id}/{c.state}"
        if key != self._inspect_key:
            self._inspect_key, self._inspect, self._inspect_error = key, None, None
            self._load_inspect(key, snap, c)
        rows: list[tuple[str, RenderableType]] = [("status", c.status)]
        if c.project:
            rows.append(("project", Text.assemble(c.project, ("  ·  service ", facts.s("muted")), c.service)))
        rows.append(("ports", Text(", ".join(p.replace("->", " → ") for p in c.ports))
                     if c.ports else Text("none", style=facts.s("muted"))))
        rows.append(("id", c.id))
        rows.append(("machine", snap.target.name))
        blocks: list[RenderableType] = [facts.facts(rows)]
        data = self._inspect
        if data is None:
            blocks += [Text(""), Text(self._inspect_error or "loading details…", style=facts.s("muted"))]
        else:
            blocks += self._inspect_blocks(facts, data)
        facts.update(RichGroup(*blocks))

    def _inspect_blocks(self, facts: Card, data: ContainerInspect) -> list[RenderableType]:
        cfg = data.get("Config") or {}
        extra: list[tuple[str, RenderableType]] = []
        state = data.get("State") or {}
        if "RestartCount" in data:
            extra.append(("restarts", str(data["RestartCount"])))
        if state.get("OOMKilled"):
            extra.append(("failure", Text("Killed: memory limit exceeded", style=facts.s("bad"))))
        if state.get("Error"):
            extra.append(("error", Text(state["Error"])))
        health = state.get("Health") or {}
        if health:
            extra.append(("health", f"{health.get('Status', 'unknown')} · "
                          f"{health.get('FailingStreak', 0)} consecutive failures"))
            checks = health.get("Log") or []
            if checks:
                check = checks[-1]
                extra.append(("last check", Text(f"exit {check.get('ExitCode', 0)} · "
                                                  + check.get("Output", "").strip()[:2000])))
        created = str(data.get("Created", ""))[:19].replace("T", " ")
        if created:
            extra.append(("created", created))
        cmd = [str(x) for x in (cfg.get("Cmd") or [])]
        entry = data.get("Path")
        parts = ([str(entry)] if entry and entry not in cmd else []) + cmd
        if parts:
            extra.append(("command", Text(" ".join(parts), style=facts.s("mono"))))
        host: InspectHostConfig = data.get("HostConfig") or {}
        restart = (host.get("RestartPolicy") or {}).get("Name")
        if restart:
            extra.append(("restart", str(restart)))
        settings: InspectNetworkSettings = data.get("NetworkSettings") or {}
        nets: dict[str, EndpointSettings | None] = settings.get("Networks") or {}
        if nets:
            t = Text()
            for i, (name, n) in enumerate(nets.items()):
                t.append(("\n" if i else "") + name)
                if n and n.get("IPAddress"):
                    t.append(f"  {n['IPAddress']}", facts.s("muted"))
            extra.append(("network", t))
        mounts = data.get("Mounts") or []
        if mounts:
            t = Text()
            for i, m in enumerate(mounts):
                t.append(("\n" if i else "") + str(m.get("Name") or tilde(str(m.get("Source", "")))))
                t.append(f" → {m.get('Destination', '')}", facts.s("muted"))
            extra.append(("mounts", t))
        env = mask_env([str(e) for e in (cfg.get("Env") or [])])
        if env:
            t = Text()
            for i, (k, v) in enumerate(env):
                t.append(("\n" if i else "") + k)
                t.append(f"={v}", facts.s("muted"))
            extra.append(("env", t))
        out: list[RenderableType] = [Text(""), facts.facts(extra)]
        if env:
            out.append(Text("env values are masked", style=facts.s("muted")))
        return out

    @work(group="inspect", exclusive=True, exit_on_error=False)
    async def _load_inspect(self, key: str, snap: TargetSnapshot, c: Container) -> None:
        backend = self.lapp.backend
        if not isinstance(backend, InspectBackend):
            self._inspect = {}
            return
        try:
            data = await backend.inspect(snap.target, c.id)
        except Exception as e:
            if key == self._inspect_key:
                self._inspect_error = f"details unavailable: {str(e).strip() or type(e).__name__}"
                self._refresh_info()
            return
        if key == self._inspect_key:
            self._inspect = data
            self._refresh_info()

    def _refresh_info(self) -> None:
        current = self.subject
        if current.kind == "container" and current.container and current.snapshot:
            self._show_info(current.container, current.snapshot)

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        event.stop()
        if self.subject.kind == "container" and self.subject.container and self.subject.snapshot:
            self._show_container()

    # -- group

    def _show_group(self) -> None:
        g, snap = self.subject.group, self.subject.snapshot
        assert g is not None and snap is not None
        head = self.query_one("#gv-head", Card)
        tone = "ok" if g.running == g.total else ("idle" if g.running == 0 else "warn")
        n = f"{g.total} container{'s' if g.total != 1 else ''}"
        sub = n + ("" if g.project else " without a compose project") + f"  ·  {snap.target.name}"
        head.update(head.header(g.label, head.badge(f"{g.running}/{g.total} up", tone), sub))
        self.query_one("#gv-actions", ActionBar).update_subject(self.action_subject)
        store = self.lapp.store
        remote = snap.target.kind is TargetKind.CONTEXT
        labels = [self.query_one("#gv-cpu-label", Card), self.query_one("#gv-mem-label", Card)]
        charts = [self.query_one("#gv-cpu", BlockChart), self.query_one("#gv-mem", BlockChart)]
        show_charts = not remote and g.running > 0
        for w in (*labels, *charts):
            w.display = show_charts
        if show_charts:
            labels[0].update(two_col(labels[0], "CPU", percent(g.cpu)))
            labels[1].update(two_col(labels[1], "MEMORY", human_bytes_long(g.mem)))
            charts[0].values = sum_histories([store.cpu_history(snap.key, c.id) for c in g.containers])
            charts[1].values = sum_histories([tuple(float(m) for m in store.mem_history(snap.key, c.id))
                                              for c in g.containers])
        services = self.query_one("#gv-services", Card)
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=1, no_wrap=True)
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column(justify="right", no_wrap=True, width=6)
        table.add_column(justify="right", no_wrap=True, width=6)
        key = services.s("key")
        table.add_row("", Text("SERVICE", style=key), Text("" if remote else "CPU", style=key),
                      Text("" if remote else "MEM", style=key))
        for c in g.containers:
            glyph, cls = dot_for(c)
            name = Text(c.service or display_name(c, g))
            if not c.running:
                name.stylize(services.s("muted"))
                name.append(f"  {c.state}", services.s("bad" if cls == "ctree--bad" else "muted"))
            st = c.stats if not remote else None
            table.add_row(Text(glyph, style=services.s(TONE.get(cls, "idle"))), name,
                          Text(percent(st.cpu_percent) if st else ""), Text(human_bytes(st.mem_bytes) if st else ""))
        services.update(table)

    # -- image

    def _show_image(self) -> None:
        img, snap = self.subject.image, self.subject.snapshot
        assert img is not None and snap is not None
        head = self.query_one("#iv-head", Card)
        badge = (head.badge("dangling", "warn") if img.dangling
                 else head.badge("usage unknown", "idle") if img.containers is None
                 else head.badge("in use", "ok") if img.containers else head.badge("unused", "idle"))
        head.update(head.header(img.ref, badge, f"{img.id}  ·  {human_bytes_long(img.size_bytes)}"))
        facts = self.query_one("#iv-facts", Card)
        users = [c for c in snap.containers if (c.image_id == img.id if c.image_id else c.image == img.ref)]
        used = Text()
        for i, c in enumerate(users):
            glyph, cls = dot_for(c)
            used.append("\n" if i else "")
            used.append(glyph + " ", facts.s(TONE.get(cls, "idle")))
            used.append(c.name)
        if not users:
            count = "–" if img.containers is None else f"{img.containers} container{'s' if img.containers != 1 else ''}"
            used = Text(count, style=facts.s("muted"))
        total = sum(i.size_bytes for i in snap.images)
        rows: list[tuple[str, RenderableType]] = [
            ("size", human_bytes_long(img.size_bytes)),
            ("of total", facts.meter(img.size_bytes, total, 18) if total else "–"),
            ("used by", used),
        ]
        dangling = [i for i in snap.images if i.dangling]
        if dangling:
            size = human_bytes_long(sum(i.size_bytes for i in dangling))
            rows.append(("dangling", Text(f"{len(dangling)} image{'s' if len(dangling) != 1 else ''} · {size} listed",
                                          style=facts.s("warn"))))
            rows.append(("note", Text("layers may be shared", style=facts.s("muted"))))
        facts.update(facts.facts(rows))

    # -- machine

    def _show_machine(self) -> None:
        snap = self.subject.snapshot
        assert snap is not None
        t = snap.target
        head = self.query_one("#mv-head", Card)
        facts = self.query_one("#mv-facts", Card)
        actions = self.query_one("#mv-actions", ActionBar)
        pending = self.lapp.store.pending_for(t.key)
        rows: list[tuple[str, RenderableType]] = []
        if t.kind is TargetKind.CONTEXT:
            badge = head.badge("read-only", "bad")
            sub = "docker context over " + t.endpoint.split("://", 1)[0]
            rows.append(("endpoint", Text(t.endpoint, overflow="ellipsis", no_wrap=True)))
            actions.display = False
        else:
            vm = t.vm
            running = vm.running if vm else True
            badge = (head.badge(f"{pending}…", "warn") if pending
                     else head.badge("● running", "ok") if running else head.badge("○ stopped", "idle"))
            sub = (f"{'Colima' if t.kind is TargetKind.COLIMA else 'Lima'} VM · {vm.arch}"
                   if vm else "local Docker socket")
            actions.display = t.kind in (TargetKind.LIMA, TargetKind.COLIMA)
            if vm is not None:
                rows += [("cpus", str(vm.cpus)), ("memory", human_bytes_long(vm.memory_bytes))]
                if vm.disk_used_bytes is not None:
                    rows.append(("host use", human_bytes_long(vm.disk_used_bytes)))
                rows.append(("disk cap", human_bytes_long(vm.disk_bytes)))
            rows.append(("socket", Text(tilde(t.endpoint.removeprefix("unix://")), overflow="ellipsis",
                                        no_wrap=True)))
        head.update(head.header(t.name, badge, sub))
        actions.update_subject(self.action_subject)
        if snap.state is DaemonState.OK:
            running_n = sum(1 for c in snap.containers if c.running)
            rows.append(("docker", Text.assemble((f"{running_n}", facts.s("ok")),
                                                 f" running · {len(snap.containers)} containers")))
            if snap.images_loaded:
                rows.append(("images", f"{len(snap.images)} · {human_bytes(sum(i.size_bytes for i in snap.images))}"))
            else:
                rows.append(("images", Text("loading…", style=facts.s("muted"))))
        elif snap.state is DaemonState.UNREACHABLE:
            rows.append(("docker", Text(snap.error or "unreachable", style=facts.s("bad"))))
        elif snap.state is DaemonState.NO_DOCKER_SOCKET:
            rows.append(("docker", Text("no socket", style=facts.s("warn"))))
        elif snap.state is DaemonState.LOADING:
            rows.append(("docker", Text("connecting…", style=facts.s("muted"))))
        facts.update(facts.facts(rows))

    def _show_none(self) -> None:
        card = self.query_one("#nv-text", Card)
        card.update(Text("Nothing selected", style=card.s("muted")))
