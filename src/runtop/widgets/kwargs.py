"""Keyword arguments every runtop widget forwards to its Textual base class."""

from __future__ import annotations

from typing import TypedDict


class WidgetKwargs(TypedDict, total=False):
    name: str | None
    id: str | None
    classes: str | None
    disabled: bool
