"""Pure view-model helpers: grouping, sorting, filtering and which pane to show."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from runtop.data.models import Container, DaemonState, Image, Target, TargetKind, TargetSnapshot
from runtop.state.store import Section

SORTS = ("name", "cpu", "mem")
STANDALONE = "standalone"


@dataclass(frozen=True, slots=True)
class Group:
    project: str  # "" = standalone
    containers: tuple[Container, ...]

    @property
    def key(self) -> str:
        return f"group:{self.project}"

    @property
    def label(self) -> str:
        return self.project or STANDALONE

    @property
    def running(self) -> int:
        return sum(1 for c in self.containers if c.running)

    @property
    def total(self) -> int:
        return len(self.containers)

    @property
    def cpu(self) -> float | None:
        vals = [c.stats.cpu_percent for c in self.containers if c.stats and c.stats.cpu_percent is not None]
        return round(sum(vals), 4) if vals else None

    @property
    def mem(self) -> int | None:
        vals = [c.stats.mem_bytes for c in self.containers if c.stats]
        return sum(vals) if vals else None


def matches(c: Container, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    if q == "attention":
        return c.health == "unhealthy" or c.state in ("restarting", "dead") or bool(c.exit_code)
    return any(q in f.lower() for f in (c.name, c.image, c.project, c.service, c.state,
                                         c.health or "", c.status, *c.ports))


def _cpu(c: Container) -> float:
    return c.stats.cpu_percent if c.stats and c.stats.cpu_percent is not None else -1.0


def _mem(c: Container) -> int:
    return c.stats.mem_bytes if c.stats else -1


def build_groups(containers: tuple[Container, ...] | list[Container], query: str = "",
                 sort: str = "name") -> list[Group]:
    """Compose projects (by name) then ``standalone`` last; members sorted by ``sort``."""
    by_project: dict[str, list[Container]] = {}
    for c in containers:
        if matches(c, query):
            by_project.setdefault(c.project, []).append(c)
    member_key = {
        "cpu": lambda c: (-_cpu(c), not c.running, c.name),
        "mem": lambda c: (-_mem(c), not c.running, c.name),
    }.get(sort, lambda c: (c.name,))
    groups = [Group(p, tuple(sorted(cs, key=member_key))) for p, cs in by_project.items()]

    def group_key(g: Group) -> tuple[bool, float, str] | tuple[bool, str]:
        standalone_last = g.project == ""
        if sort == "cpu":
            return (standalone_last, -(g.cpu if g.cpu is not None else -1), g.project)
        if sort == "mem":
            return (standalone_last, -(g.mem if g.mem is not None else -1), g.project)
        return (standalone_last, g.project)

    return sorted(groups, key=group_key)


def display_name(c: Container, group: Group | None = None) -> str:
    """Inside a compose group, drop the ``<project>-`` prefix (``shop-api-1`` → ``api-1``)."""
    if c.project and c.name.startswith(c.project + "-"):
        rest = c.name[len(c.project) + 1 :]
        if rest and not rest.isdigit():
            return rest
    return c.name


def filter_images(images: tuple[Image, ...] | list[Image], query: str = "") -> list[Image]:
    q = query.strip().lower()
    return [i for i in images if not q or q in i.ref.lower() or q in i.id]


# ---------------------------------------------------------------- panes


class PaneKind(StrEnum):
    LOADING = "loading"
    CONTENT = "content"
    VM_STOPPED = "vm_stopped"
    NO_DOCKER_SOCKET = "no_docker_socket"
    UNREACHABLE = "unreachable"
    NO_TARGETS = "no_targets"
    EMPTY = "empty"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class PaneState:
    kind: PaneKind
    title: str = ""
    body: str = ""
    hint: str = ""  # a command or key the user can act on
    tone: str = "muted"  # muted | warning | error | primary

    @property
    def is_content(self) -> bool:
        return self.kind is PaneKind.CONTENT


def state_to_pane(*, targets_loaded: bool, discover_error: str | None, target: Target | None,
                  snapshot: TargetSnapshot | None, section: Section, query: str = "") -> PaneState:
    if not targets_loaded:
        return PaneState(PaneKind.LOADING, "Looking for machines", "Asking limactl and docker contexts…")
    if target is None:
        body = discover_error or "runtop shows Docker running inside Lima VMs and remote docker contexts."
        return PaneState(PaneKind.NO_TARGETS, "No machines found", body,
                         "limactl start --name=docker template:docker", "warning" if discover_error else "muted")
    if snapshot is None or snapshot.state is DaemonState.LOADING:
        verb = "Connecting to" if target.kind is TargetKind.CONTEXT else "Loading"
        return PaneState(PaneKind.LOADING, f"{verb} {target.name}",
                         "over ssh — this can take a few seconds" if target.is_remote else "")
    state = snapshot.state
    if state is DaemonState.VM_STOPPED:
        return PaneState(PaneKind.VM_STOPPED, f"{target.name} is stopped",
                         "Start the machine to see its containers and images.",
                         (f"colima start --profile {target.name}" if target.kind is TargetKind.COLIMA
                          else f"limactl start {target.name}"), "muted")
    if state is DaemonState.NO_DOCKER_SOCKET:
        return PaneState(PaneKind.NO_DOCKER_SOCKET, f"No Docker on {target.name}",
                         f"No Docker socket at {target.socket_path or target.endpoint}. "
                         "Check the runtime and socket configuration.", "runtop --doctor", "warning")
    if state is DaemonState.UNREACHABLE:
        return PaneState(PaneKind.UNREACHABLE, f"Can't reach {target.name}",
                         snapshot.error or "The docker daemon did not answer.", "r  retry", "error")
    if state is DaemonState.NO_TARGETS:  # pragma: no cover - not a per-target state
        return PaneState(PaneKind.NO_TARGETS, "No machines found")
    if section is Section.IMAGES:
        if snapshot.images_error and not snapshot.images:
            return PaneState(PaneKind.UNREACHABLE, "Images unavailable", snapshot.images_error, "r  retry", "warning")
        if not snapshot.images_loaded and not snapshot.images_error:
            return PaneState(PaneKind.LOADING, f"Loading images from {target.name}",
                             "containers are ready; image listing is still on its way")
        if not snapshot.images:
            return PaneState(PaneKind.EMPTY, "No images", f"{target.name} has no images yet.",
                             "docker pull alpine")
        if not filter_images(snapshot.images, query):
            return PaneState(PaneKind.NO_MATCH, f"No images match “{query}”", "", "esc  clear filter")
        return PaneState(PaneKind.CONTENT)
    if not snapshot.containers:
        return PaneState(PaneKind.EMPTY, "No containers", f"Nothing is running on {target.name} yet.",
                         "docker run -d --name hello nginx:alpine")
    if not any(matches(c, query) for c in snapshot.containers):
        return PaneState(PaneKind.NO_MATCH, f"No containers match “{query}”", "", "esc  clear filter")
    return PaneState(PaneKind.CONTENT)
