"""Action registry: the single source of truth for what can be done, by which key, and why not.

The same :class:`ActionSpec` drives key handling and footer gating (``check_action``), the
detail pane's buttons (disabled state + tooltip), and the command palette entries. Actions
capture their :class:`Subject` when triggered, so async work always hits the target and
container the user was looking at — never whatever is selected when the work finishes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from runtop.data.models import Container, DaemonState, Image, Target, TargetKind, TargetSnapshot
from runtop.state.viewmodel import Group

READ_ONLY_REASON = "read-only context — view only"


@dataclass(frozen=True, slots=True)
class Ok:
    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Disabled:
    reason: str

    def __bool__(self) -> bool:
        return False


OK = Ok()
Availability = Ok | Disabled


@dataclass(frozen=True, slots=True)
class Subject:
    """What an action applies to, captured at trigger time."""

    target: Target | None = None
    snapshot: TargetSnapshot | None = None
    container: Container | None = None
    group: Group | None = None
    image: Image | None = None
    focus: str = "middle"  # sidebar | middle | detail
    demo: bool = False
    pending: frozenset[str] = field(default_factory=frozenset)

    @property
    def key(self) -> str | None:
        return self.target.key if self.target else None

    def container_pending(self) -> bool:
        return bool(self.target and self.container and f"{self.target.key}/{self.container.id}" in self.pending)

    def target_pending(self) -> bool:
        return bool(self.target and self.target.key in self.pending)


@dataclass(frozen=True, slots=True)
class Plan:
    """What running an action does: a backend call plus UI words."""

    method: str  # backend method name, or "exec"
    pending: str  # verb shown while in flight ("stopping")
    done: str  # toast on success ("Stopped")


@dataclass(frozen=True, slots=True)
class ActionSpec:
    id: str
    title: str
    key: str
    scope: str  # container | images | machine
    glyph: str
    destructive: bool
    available: Callable[[Subject], Availability]
    plan: Plan
    confirm: Callable[[Subject], tuple[str, str, str]] | None = None  # (title, body, confirm label)

    def label(self, s: Subject) -> str:
        if self.scope == "container" and s.container:
            return f"{self.title} {s.container.name}"
        if self.scope == "machine" and s.target:
            return f"{self.title} {s.target.name}"
        if self.scope == "images" and s.target:
            return f"{self.title} on {s.target.name}"
        return self.title


# ---------------------------------------------------------------- availability rules


def _writable_target(s: Subject) -> Availability:
    if s.target is None:
        return Disabled("no machine selected")
    if s.target.read_only or s.target.kind is TargetKind.CONTEXT:
        return Disabled(READ_ONLY_REASON)
    return OK


def _container(s: Subject) -> Availability:
    base = _writable_target(s)
    if not base:
        return base
    if s.container is None:
        return Disabled("select a container")
    if s.snapshot is None or s.snapshot.state is not DaemonState.OK:
        return Disabled("docker is not available")
    if s.snapshot.stale:
        return Disabled("data is stale — reconnect before changing containers")
    if s.target_pending():
        return Disabled("an action is running on this machine")
    if s.container_pending():
        return Disabled("an action is already running on this container")
    return OK


def _start(s: Subject) -> Availability:
    base = _container(s)
    if not base:
        return base
    assert s.container is not None
    return Disabled("already running") if s.container.running else OK


def _stop(s: Subject) -> Availability:
    base = _container(s)
    if not base:
        return base
    assert s.container is not None
    return OK if s.container.state in ("running", "restarting", "paused") else Disabled("not running")


def _exec(s: Subject) -> Availability:
    base = _container(s)
    if not base:
        return base
    assert s.container is not None
    if not s.container.running:
        return Disabled("container is not running")
    if s.demo:
        return Disabled("exec needs a real daemon (demo mode)")
    return OK


def _prune(s: Subject) -> Availability:
    base = _writable_target(s)
    if not base:
        return base
    if s.snapshot is None or s.snapshot.state is not DaemonState.OK or not s.snapshot.images_loaded or s.snapshot.stale:
        return Disabled("docker is not available")
    if s.target_pending():
        return Disabled("an action is already running")
    if not any(i.dangling for i in s.snapshot.images):
        return Disabled("no dangling images")
    return OK


def _machine(s: Subject, want_running: bool) -> Availability:
    base = _writable_target(s)
    if not base:
        return base
    assert s.target is not None
    if s.target.kind not in (TargetKind.LIMA, TargetKind.COLIMA) or s.target.vm is None:
        return Disabled("not a Lima VM")
    if s.target_pending():
        return Disabled(f"{s.target.name} is busy")
    if s.target.vm.running == want_running:
        return Disabled("already running" if want_running else "already stopped")
    return OK


def _confirm_remove(s: Subject) -> tuple[str, str, str]:
    name = s.container.name if s.container else "container"
    where = s.target.name if s.target else ""
    running = " It is running and will be killed." if s.container and s.container.running else ""
    return (f"Remove {name}?", f"The container is force-removed from {where}.{running} This can't be undone.",
            "Remove")


def _confirm_prune(s: Subject) -> tuple[str, str, str]:
    images = [i for i in (s.snapshot.images if s.snapshot else ()) if i.dangling]
    where = s.target.name if s.target else ""
    n = len(images)
    return ("Prune dangling images?", f"Ask Docker on {where} to remove unused, untagged images "
            f"({n} untagged listed). Shared layers and images in use are retained. "
            "Actual reclaimed space is reported afterwards.", "Prune")


def _confirm_vm_stop(s: Subject) -> tuple[str, str, str]:
    name = s.target.name if s.target else "machine"
    return (f"Stop {name}?", "Every container on this machine stops with it.", "Stop machine")


def _project(s: Subject) -> Availability:
    base = _writable_target(s)
    if not base:
        return base
    if s.group is None or not s.group.project or s.container:
        return Disabled("select a Compose project")
    if s.snapshot is None or s.snapshot.state is not DaemonState.OK or s.snapshot.stale:
        return Disabled("fresh container data required")
    if s.target_pending() or any(k.startswith(f"{s.key}/") for k in s.pending):
        return Disabled("an action is already running on this machine")
    return OK


def _confirm_project(s: Subject, verb: str) -> tuple[str, str, str]:
    assert s.group is not None
    names = ", ".join(c.name for c in s.group.containers)
    return (f"{verb} {s.group.label} on {s.target.name if s.target else ''}?",
            f"Applies to these {s.group.total} listed containers: {names}. "
            "Only existing containers are changed; Compose files are not executed.", "Confirm")


REGISTRY: tuple[ActionSpec, ...] = (
    ActionSpec("project.start", "Start project", "s", "project", "▶", True, _project,
               Plan("start", "starting", "Started"), confirm=lambda s: _confirm_project(s, "Start")),
    ActionSpec("project.stop", "Stop project", "x", "project", "■", True, _project,
               Plan("stop", "stopping", "Stopped"), confirm=lambda s: _confirm_project(s, "Stop")),
    ActionSpec("project.restart", "Restart project", "R", "project", "↻", True, _project,
               Plan("restart", "restarting", "Restarted"), confirm=lambda s: _confirm_project(s, "Restart")),
    ActionSpec("container.start", "Start", "s", "container", "▶", False, _start,
               Plan("start", "starting", "Started")),
    ActionSpec("container.stop", "Stop", "x", "container", "■", False, _stop,
               Plan("stop", "stopping", "Stopped")),
    ActionSpec("container.restart", "Restart", "R", "container", "↻", False, _container,
               Plan("restart", "restarting", "Restarted")),
    ActionSpec("container.exec", "Shell", "e", "container", "›_", False, _exec,
               Plan("exec", "", "Shell closed")),
    ActionSpec("container.remove", "Remove", "X", "container", "✕", True, _container,
               Plan("remove", "removing", "Removed"), confirm=_confirm_remove),
    ActionSpec("images.prune", "Prune dangling images", "p", "images", "✕", True, _prune,
               Plan("prune_dangling_images", "pruning", "Pruned"), confirm=_confirm_prune),
    ActionSpec("machine.start", "Start machine", "s", "machine", "▶", False, lambda s: _machine(s, True),
               Plan("vm_start", "starting", "Started")),
    ActionSpec("machine.stop", "Stop machine", "x", "machine", "■", True, lambda s: _machine(s, False),
               Plan("vm_stop", "stopping", "Stopped"), confirm=_confirm_vm_stop),
)

BY_ID = {spec.id: spec for spec in REGISTRY}
ACTION_KEYS = tuple(dict.fromkeys(spec.key for spec in REGISTRY))


def resolve(key: str, s: Subject) -> ActionSpec | None:
    """Which action a key means right now: machine actions from the sidebar, container actions elsewhere."""
    candidates = [spec for spec in REGISTRY if spec.key == key]
    if not candidates:
        return None
    on_vm = s.target is not None and s.target.kind in (TargetKind.LIMA, TargetKind.COLIMA)
    preferred = "machine" if s.focus == "sidebar" and on_vm else "container"
    if s.group and s.group.project and s.container is None and s.focus != "sidebar":
        preferred = "project"
    for spec in candidates:
        if spec.scope == preferred:
            return spec
    return next((spec for spec in candidates if spec.scope == "container"), candidates[0])


def availability(spec: ActionSpec, s: Subject) -> Availability:
    return spec.available(s)


RunAction = Callable[[ActionSpec, Subject], Awaitable[None]]
