"""Command palette providers: machines, containers and registry actions (ctrl+p)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual.command import DiscoveryHit, Hit, Hits, Provider
from typing_extensions import override

from runtop.appref import runtop_app
from runtop.data.models import DaemonState, TargetKind
from runtop.state.actions import REGISTRY, ActionSpec, Disabled, Subject
from runtop.state.viewmodel import build_groups

if TYPE_CHECKING:
    from runtop.app import RuntopApp

Candidate = tuple[str, str, Callable[[], object], bool]
"""(text, help, callback, enabled)."""


class _Base(Provider):
    boost = 1.0

    @property
    def lapp(self) -> RuntopApp:
        return runtop_app(self.app)

    def candidates(self) -> Iterator[Candidate]:
        raise NotImplementedError

    @override
    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for text, help_text, callback, enabled in self.candidates():
            score = matcher.match(text)
            if score > 0:
                display = matcher.highlight(text)
                yield Hit(score * self.boost * (1.0 if enabled else 0.6), display, callback, text=text, help=help_text)

    @override
    async def discover(self) -> Hits:
        for text, help_text, callback, enabled in self.discovery():
            yield DiscoveryHit(Text(text, style=Style(dim=not enabled)), callback, text=text, help=help_text)

    def discovery(self) -> Iterator[Candidate]:
        return iter(())


class TargetsProvider(_Base):
    """``prod`` → "Switch to prod-eu"."""

    boost = 1.2

    @override
    def candidates(self) -> Iterator[Candidate]:
        store = self.lapp.store
        for t in store.targets:
            state = store.state(t.key)
            kind = ("remote · read-only" if t.kind is TargetKind.CONTEXT else
                    "Colima VM" if t.kind is TargetKind.COLIMA else "Lima VM" if t.vm else "local socket")
            status = ("stopped" if t.vm and not t.vm.running else
                      {DaemonState.OK: "online", DaemonState.UNREACHABLE: "unreachable",
                       DaemonState.NO_DOCKER_SOCKET: "no docker"}.get(state, "not loaded yet"))
            current = " · current" if t.key == store.selected else ""
            yield f"Switch to {t.name}", f"{kind} · {status}{current}", partial(self.lapp.go_to_target, t.key), True

    @override
    def discovery(self) -> Iterator[Candidate]:
        yield from self.candidates()


class ContainersProvider(_Base):
    """``web`` → "shop-web-1 · shop · running" jumps to it (across cached machines)."""

    boost = 1.15  # a bare name should jump, not run "Stop <name>"

    @override
    def candidates(self) -> Iterator[Candidate]:
        store = self.lapp.store
        keys = [store.selected] + [t.key for t in store.targets if t.key != store.selected]
        for key in keys:
            snap = store.snapshot(key)
            if key is None or snap is None or snap.state is not DaemonState.OK:
                continue
            for c in snap.containers:
                where = "" if key == store.selected else f" · on {snap.target.name}"
                text = f"{c.name} · {c.project or 'standalone'} · {c.state}"
                yield text, f"{c.image}{where}", partial(self.lapp.go_to_container, key, c.id), True


class ActionsProvider(_Base):
    """``stop web`` → "Stop shop-web-1"; unavailable actions stay listed with their reason."""

    def _subjects(self, spec: ActionSpec) -> Iterator[tuple[Subject, str]]:
        app = self.lapp
        store = app.store
        key = store.selected
        snap = store.snapshot(key)
        if spec.scope == "container":
            if snap is None or snap.state is not DaemonState.OK:
                return
            for c in snap.containers:
                yield app.subject_for(key, container=c), f"{spec.title} {c.name}"
        elif spec.scope == "project" and snap is not None:
            for group in build_groups(snap.containers):
                if group.project:
                    yield replace(app.subject_for(key), group=group), f"{spec.title} {group.project}"
        elif spec.scope == "images":
            if key is not None:
                yield app.subject_for(key, focus="middle"), spec.label(app.subject_for(key))
        elif spec.scope == "machine":
            for t in store.targets:
                if t.kind in (TargetKind.LIMA, TargetKind.COLIMA):
                    yield app.subject_for(t.key, focus="sidebar"), f"{spec.title.replace(' machine', '')} {t.name}"

    @override
    def candidates(self) -> Iterator[Candidate]:
        for spec in REGISTRY:
            for subject, text in self._subjects(spec):
                avail = spec.available(subject)
                where = subject.target.name if subject.target else ""
                if isinstance(avail, Disabled):
                    help_text = f"unavailable: {avail.reason}"
                else:
                    help_text = f"{where} · key {spec.key}"
                if spec.scope == "machine":
                    text = f"{text} (machine)"
                yield text, help_text, partial(self.lapp.trigger, spec, subject), bool(avail)
        app = self.lapp
        yield "Refresh now", "key r", app.action_refresh_now, True
        if app.main is not None:
            yield "Inspect Docker storage", "on demand · key D", app.main.action_storage, True
            yield "Log archives", "record · export · search · key L", app.main.action_archives, True
        yield "Next theme", f"current: {app.theme} · key t", app.action_cycle_theme, True
        yield "Keyboard help", "key ?", app.action_show_help, True

    @override
    def discovery(self) -> Iterator[Candidate]:
        app = self.lapp
        subject = app.current_subject()
        for spec in REGISTRY:
            if spec.scope == "container" and subject.container is not None:
                avail = spec.available(subject)
                help_text = f"unavailable: {avail.reason}" if isinstance(avail, Disabled) else f"key {spec.key}"
                yield spec.label(subject), help_text, partial(app.trigger, spec, subject), bool(avail)
        yield "Keyboard help", "key ?", app.action_show_help, True
