"""Typed access to the running :class:`~runtop.app.RuntopApp`.

Textual types ``node.app`` as ``App[object]``; screens, widgets and palette providers go through
:func:`runtop_app` to reach the store, poller and backend with real types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App

    from runtop.app import RuntopApp


def runtop_app(app: App[object]) -> RuntopApp:
    from runtop.app import RuntopApp  # deferred: app.py imports every screen and widget

    if not isinstance(app, RuntopApp):
        raise TypeError(f"expected a RuntopApp, got {type(app).__name__}")
    return app
