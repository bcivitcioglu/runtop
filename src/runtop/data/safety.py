"""Read-only safety rails shared by every backend."""

from __future__ import annotations

from runtop.data.models import Target, TargetKind
from runtop.data.remote import ReadOnlyViolation

EXEC_SHELL = "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"


def ensure_writable(target: Target) -> None:
    """Second safety layer (after the action registry): never mutate a read-only target."""
    if target.read_only or target.kind is TargetKind.CONTEXT:
        raise ReadOnlyViolation(f"{target.name} is a read-only context")


__all__ = ["EXEC_SHELL", "ReadOnlyViolation", "ensure_writable"]
