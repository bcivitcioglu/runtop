from __future__ import annotations

from typing import TypedDict, Unpack

import pytest

from runtop.data.models import Container, DaemonState, Image, Stats, Target, TargetKind, TargetSnapshot, VMInfo
from runtop.state.store import Section, Store
from runtop.state.viewmodel import PaneKind, PaneState, build_groups, display_name, filter_images, state_to_pane
from runtop.widgets.columns import DOT, GAP, INDENT, layout


def c(name: str, project: str = "", state: str = "running", cpu: float | None = None, mem: int | None = None,
      image: str = "img", service: str = "") -> Container:
    stats = Stats(cpu, mem or 0, 1 << 30) if (cpu is not None or mem is not None) else None
    return Container(id=name[:12].ljust(12, "0"), name=name, image=image, state=state, status="Up 1 hour",
                     project=project, service=service, stats=stats)


def test_groups_projects_by_name_then_standalone_last() -> None:
    groups = build_groups([c("web"), c("zeta-a-1", "zeta"), c("api-db-1", "api"), c("api-web-1", "api")])
    assert [g.label for g in groups] == ["api", "zeta", "standalone"]
    assert [x.name for x in groups[0].containers] == ["api-db-1", "api-web-1"]


def test_group_aggregates() -> None:
    g = build_groups([c("a-1", "a", cpu=1.5, mem=10), c("a-2", "a", cpu=2.0, mem=5), c("a-3", "a", state="exited")])[0]
    assert (g.running, g.total, g.cpu, g.mem) == (2, 3, 3.5, 15)
    assert build_groups([c("x", "p", state="exited")])[0].cpu is None


def test_sort_by_cpu_and_mem() -> None:
    cs = [c("a-lo", "a", cpu=1), c("a-hi", "a", cpu=9), c("b-x", "b", cpu=50), c("a-off", "a", state="exited")]
    groups = build_groups(cs, sort="cpu")
    assert [g.project for g in groups] == ["b", "a"]
    assert [x.name for x in groups[1].containers] == ["a-hi", "a-lo", "a-off"]
    assert [x.name for x in build_groups([c("m1", mem=1), c("m2", mem=9)], sort="mem")[0].containers] == ["m2", "m1"]


def test_filter_matches_name_image_project_service() -> None:
    cs = [c("web", image="nginx"), c("api-1", "api", service="api"), c("db", image="postgres")]
    assert [x.name for g in build_groups(cs, "API") for x in g.containers] == ["api-1"]
    assert [x.name for g in build_groups(cs, "nginx") for x in g.containers] == ["web"]
    assert build_groups(cs, "zzz") == []
    imgs = [Image("abc123def456", "nginx:alpine", 1, 1, False), Image("fff", "<none>:<none>", 1, 0, True)]
    assert [i.ref for i in filter_images(imgs, "ngi")] == ["nginx:alpine"]


def test_display_name_strips_project_prefix() -> None:
    assert display_name(c("shop-api-1", "shop")) == "api-1"
    assert display_name(c("api-1", "api")) == "api-1"  # remainder is only digits
    assert display_name(c("web")) == "web"


LIMA = Target("lima:docker", TargetKind.LIMA, "docker", False, "unix:///x/sock/docker.sock",
              VMInfo("Running", "vz", "aarch64", 4, 1, 1))
CTX = Target("ctx:prod", TargetKind.CONTEXT, "prod", True, "ssh://prod")


class PaneOptions(TypedDict, total=False):
    snapshot: TargetSnapshot | None
    target: Target | None
    section: Section
    query: str
    loaded: bool
    error: str | None


def pane(**kw: Unpack[PaneOptions]) -> PaneState:
    return state_to_pane(targets_loaded=kw.get("loaded", True), discover_error=kw.get("error"),
                         target=kw.get("target", LIMA), snapshot=kw.get("snapshot"),
                         section=kw.get("section", Section.CONTAINERS), query=kw.get("query", ""))


@pytest.mark.parametrize(("kwargs", "kind", "text"), [
    ({"loaded": False}, PaneKind.LOADING, "Looking for machines"),
    ({"target": None}, PaneKind.NO_TARGETS, "No machines found"),
    ({"target": None, "error": "limactl not found"}, PaneKind.NO_TARGETS, "limactl not found"),
    ({}, PaneKind.LOADING, "Loading docker"),
    ({"target": CTX}, PaneKind.LOADING, "Connecting to prod"),
    ({"snapshot": TargetSnapshot(LIMA, DaemonState.VM_STOPPED)}, PaneKind.VM_STOPPED, "docker is stopped"),
    ({"snapshot": TargetSnapshot(LIMA, DaemonState.NO_DOCKER_SOCKET)}, PaneKind.NO_DOCKER_SOCKET, "No Docker"),
    ({"snapshot": TargetSnapshot(LIMA, DaemonState.UNREACHABLE, "boom")}, PaneKind.UNREACHABLE, "boom"),
    ({"snapshot": TargetSnapshot(LIMA, DaemonState.OK)}, PaneKind.EMPTY, "No containers"),
    ({"snapshot": TargetSnapshot(LIMA, DaemonState.OK), "section": Section.IMAGES}, PaneKind.EMPTY, "No images"),
    ({"snapshot": TargetSnapshot(LIMA, DaemonState.OK, containers=(c("web"),)), "query": "zz"},
     PaneKind.NO_MATCH, "No containers match"),
    ({"snapshot": TargetSnapshot(LIMA, DaemonState.OK, images=(Image("a", "x:1", 1, 0, False),)),
      "section": Section.IMAGES, "query": "zz"}, PaneKind.NO_MATCH, "No images match"),
    ({"snapshot": TargetSnapshot(LIMA, DaemonState.OK, containers=(c("web"),))}, PaneKind.CONTENT, ""),
])
def test_state_to_pane_matrix(kwargs: PaneOptions, kind: PaneKind, text: str) -> None:
    p = pane(**kwargs)
    assert p.kind is kind
    assert text in p.title + p.body


def test_loader_never_masquerades_as_empty() -> None:
    p = pane(snapshot=None)
    assert p.kind is PaneKind.LOADING and "No containers" not in p.title


@pytest.mark.parametrize("width", [20, 40, 60, 70, 80, 100, 140, 200])
@pytest.mark.parametrize("stats", [True, False])
def test_columns_fill_width_exactly(width: int, stats: bool) -> None:
    cols = layout(width, stats=stats)
    used = cols.name + sum(w + GAP for w in (cols.status, cols.cpu, cols.spark, cols.mem, cols.ports, cols.image) if w)
    assert used == width or cols.name == 4
    assert cols.cpu == (6 if stats else 0) and cols.mem == (6 if stats else 0)


def test_columns_drop_order() -> None:
    wide, mid, narrow, tiny = layout(120), layout(76), layout(46), layout(30)
    assert wide.image == 36 and wide.ports and wide.spark == 8
    assert mid.image and not mid.ports and mid.spark == 8  # 150-col terminal: IMAGE beats PORTS
    assert not narrow.image and narrow.cpu and narrow.mem
    assert not tiny.spark and not tiny.status and tiny.cpu
    assert INDENT + DOT == 4


def test_store_selection_default_and_vm_stopped_override() -> None:
    s = Store()
    stopped = Target("lima:ci", TargetKind.LIMA, "ci", False, "unix:///ci", VMInfo("Stopped", "vz", "a", 1, 1, 1))
    other = Target("lima:dev", TargetKind.LIMA, "dev", False, "unix:///dev", VMInfo("Running", "vz", "a", 1, 1, 1))
    s.set_targets([stopped, other, LIMA])
    assert s.selected == "lima:docker"  # running VM named docker wins
    s.apply(TargetSnapshot(LIMA, DaemonState.OK, containers=(c("web", cpu=1.0),)))
    assert s.state("lima:docker") is DaemonState.OK
    assert s.cpu_history("lima:docker", c("web").id) == (1.0,)
    stopped_now = Target("lima:docker", TargetKind.LIMA, "docker", False, LIMA.endpoint,
                         VMInfo("Stopped", "vz", "aarch64", 4, 1, 1))
    s.set_targets([stopped, other, stopped_now])
    assert s.state("lima:docker") is DaemonState.VM_STOPPED  # immediately, despite the cached OK snapshot
    assert s.selected == "lima:docker"


def test_store_prunes_history_of_removed_containers() -> None:
    s = Store()
    s.set_targets([LIMA])
    s.apply(TargetSnapshot(LIMA, DaemonState.OK, containers=(c("web", cpu=1.0), c("api", cpu=2.0))))
    s.apply(TargetSnapshot(LIMA, DaemonState.OK, containers=(c("web", cpu=1.5),)))
    assert s.cpu_history("lima:docker", c("api").id) == ()
    assert s.cpu_history("lima:docker", c("web").id) == (1.0, 1.5)
    s.set_targets([])
    assert s.snapshots == {} and s.selected is None
