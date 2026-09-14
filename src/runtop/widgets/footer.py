"""Footer that rebuilds only when its bindings change and releases the keys it replaces."""

from __future__ import annotations

from typing import Any

from textual.screen import Screen
from textual.widgets import Footer
from typing_extensions import override


class RuntopFooter(Footer):
    """Textual's footer, safe to refresh after every poll.

    Each footer key data-binds ``compact`` to the footer, which registers a watcher on the footer.
    Textual prunes those watchers only when ``compact`` changes, so every rebuild kept all earlier
    keys alive and memory grew with each binding refresh. This footer rebuilds only when the shown
    bindings differ, and after a rebuild it drops watchers whose keys are no longer attached.
    """

    _shown: tuple[tuple[object, ...], ...] | None = None

    @override
    def bindings_changed(self, screen: Screen[Any]) -> None:
        shown = tuple(
            (binding.key, binding.action, binding.description, binding.show, binding.key_display, enabled, tooltip)
            for _node, binding, enabled, tooltip in screen.active_bindings.values()
        )
        if shown == self._shown:
            return
        super().bindings_changed(screen)
        if screen.app.app_focus and self.is_attached and screen is self.screen:
            self._shown = shown  # a rebuild is scheduled for exactly these bindings

    @override
    async def recompose(self) -> None:
        await super().recompose()
        watchers: object = getattr(self, "__watchers", None)
        if isinstance(watchers, dict):
            for entries in watchers.values():
                if isinstance(entries, list):
                    entries[:] = [entry for entry in entries if _attached(entry)]


def _attached(entry: object) -> bool:
    """Keep a ``(node, callback)`` watcher entry unless its node has left the DOM."""
    if not isinstance(entry, tuple) or not entry:
        return True
    node = entry[0]
    return bool(getattr(node, "is_attached", True))
