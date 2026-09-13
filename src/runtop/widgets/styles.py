"""Theme-aware Rich styles for hand-built labels.

``Widget.get_component_rich_style(partial=True)`` drops alpha (``$foreground-muted`` is
``#rrggbb99`` in most themes → it would render as full foreground). The non-partial style
is blended against the widget background; we keep its colour/attributes and drop the
background so label spans never paint over cursor or hover highlights.
"""

from __future__ import annotations

from rich.style import Style
from textual.widget import Widget


def fg(widget: Widget, name: str, *, keep_bg: bool = False) -> Style:
    st = widget.get_component_rich_style(name)
    return Style(color=st.color, bgcolor=st.bgcolor if keep_bg else None, bold=st.bold, italic=st.italic,
                 underline=st.underline, dim=st.dim)
