"""Images section: a DataTable with computed column widths and right-aligned numbers."""

from __future__ import annotations

from typing import Unpack

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable
from typing_extensions import override

from runtop.data.format import human_bytes, truncate
from runtop.data.models import Image
from runtop.widgets.kwargs import WidgetKwargs
from runtop.widgets.styles import fg


def split_ref(ref: str) -> tuple[str, str]:
    """``ghcr.io/a/b:1.2`` → (``ghcr.io/a/b``, ``1.2``); digests keep ``@…`` as the tag."""
    if ref == "<none>:<none>":
        return "<none>", "<none>"
    if "@" in ref:
        repo, _, digest = ref.partition("@")
        return repo, "@" + digest
    head, sep, tag = ref.rpartition(":")
    if not sep or "/" in tag:
        return ref, ""
    return head, tag


class ImagesTable(DataTable[Text]):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_top", "Top", show=False),
        Binding("G", "scroll_bottom", "Bottom", show=False),
    ]

    COMPONENT_CLASSES = {"images--muted", "images--warn"}

    DEFAULT_CSS = """
    ImagesTable {
        background: transparent;
        padding: 0 1 0 0;
        scrollbar-size-vertical: 1;
        overflow-x: hidden;
        & > .datatable--header { background: transparent; color: $foreground-muted; text-style: none; }
        & > .datatable--cursor { background: $foreground 9%; color: $foreground; text-style: none; }
        &:focus > .datatable--cursor { background: $primary 32%; color: $foreground; text-style: none; }
        &:focus { background-tint: transparent; }
        & > .datatable--hover { background: $foreground 5%; }
        & > .datatable--header-hover { background: transparent; }
        & > .images--muted { color: $foreground-muted; }
        & > .images--warn { color: $warning; }
    }
    """

    def __init__(self, **kwargs: Unpack[WidgetKwargs]) -> None:
        super().__init__(cursor_type="row", zebra_stripes=False, show_cursor=True, cell_padding=1, **kwargs)
        self._images: list[Image] = []
        self._width = -1

    @override
    def on_mount(self) -> None:
        self.watch(self.app, "theme", lambda _: self._rebuild(force=True), init=False)

    def on_resize(self) -> None:
        self._rebuild()

    @property
    def selected_image(self) -> Image | None:
        if not self._images or self.cursor_row < 0 or self.cursor_row >= len(self._images):
            return None
        return self._images[self.cursor_row]

    def set_images(self, images: list[Image]) -> None:
        if [i.to_dict() for i in images] == [i.to_dict() for i in self._images] and self.columns:
            return
        self._images = list(images)
        self._rebuild(force=True)

    def _rebuild(self, force: bool = False) -> None:
        width = self.scrollable_content_region.width
        if width <= 0 or (width == self._width and not force):
            return
        self._width = width
        selected = self.selected_image.id if self.selected_image else None
        row = self.cursor_row
        muted = fg(self, "images--muted")
        warn = fg(self, "images--warn")
        pad = 2  # cell_padding on both sides
        id_w, size_w, used_w, tag_w = 12, 7, 6, 18
        repo_w = width - (id_w + size_w + used_w + tag_w) - pad * 5
        if repo_w < 18:
            tag_w = max(8, tag_w - (18 - repo_w))
            repo_w = width - (id_w + size_w + used_w + tag_w) - pad * 5
        if repo_w < 14:
            id_w = 0
            repo_w = width - (size_w + used_w + tag_w) - pad * 4
        self.clear(columns=True)
        self.add_column(Text("REPOSITORY"), width=max(6, repo_w), key="repo")
        self.add_column(Text("TAG"), width=tag_w, key="tag")
        if id_w:
            self.add_column(Text("IMAGE ID"), width=id_w, key="id")
        self.add_column(Text("SIZE", justify="right"), width=size_w, key="size")
        self.add_column(Text("IN USE", justify="right"), width=used_w, key="used")
        for img in self._images:
            repo, tag = split_ref(img.ref)
            if img.dangling:
                repo_cell = Text(truncate("‹dangling›", repo_w), style=warn)
                tag_cell = Text("", style=muted)
            else:
                repo_cell = Text(truncate(repo, max(6, repo_w)))
                tag_cell = Text(truncate(tag, tag_w), style=muted)
            cells = [repo_cell, tag_cell]
            if id_w:
                cells.append(Text(img.id, style=muted))
            cells.append(Text(human_bytes(img.size_bytes), justify="right"))
            used = "–" if not img.containers else str(img.containers)
            cells.append(Text(used, justify="right", style=muted if not img.containers else Style()))
            self.add_row(*cells, key=img.id)
        if self._images:
            idx = next((i for i, im in enumerate(self._images) if im.id == selected), min(row, len(self._images) - 1))
            self.move_cursor(row=max(0, idx), animate=False)
