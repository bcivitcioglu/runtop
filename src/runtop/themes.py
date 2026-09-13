"""runtop's own themes plus a curated cycle of Textual built-ins (``t``)."""

from __future__ import annotations

from textual.theme import Theme

RUNTOP_DARK = Theme(
    name="runtop",
    primary="#3B82F6",
    secondary="#64748B",
    accent="#60A5FA",
    warning="#F5B942",
    error="#F0616D",
    success="#3DD68C",
    foreground="#E6E8EC",
    background="#16181D",
    surface="#1C1F26",
    panel="#232730",
    boost="#2A2F3A",
    dark=True,
    variables={
        "foreground-muted": "#8B93A1",
        "footer-background": "#16181D",
        "footer-key-foreground": "#60A5FA",
        "footer-description-foreground": "#8B93A1",
        "block-cursor-background": "#3B82F6",
        "input-selection-background": "#3B82F6 40%",
    },
)

RUNTOP_LIGHT = Theme(
    name="runtop-light",
    primary="#2563EB",
    secondary="#64748B",
    accent="#2563EB",
    warning="#B7791F",
    error="#D93A49",
    success="#1F9D5C",
    foreground="#1E2430",
    background="#EEF0F4",
    surface="#F8F9FB",
    panel="#E4E7EC",
    boost="#DDE1E8",
    dark=False,
    variables={
        "foreground-muted": "#6B7383",
        "footer-background": "#EEF0F4",
        "footer-key-foreground": "#2563EB",
        "footer-description-foreground": "#6B7383",
    },
)

CUSTOM_THEMES = (RUNTOP_DARK, RUNTOP_LIGHT)

CYCLE = (
    "runtop",
    "runtop-light",
    "nord",
    "catppuccin-mocha",
    "tokyo-night",
    "gruvbox",
    "dracula",
    "rose-pine-dawn",
    "textual-dark",
)


def next_theme(current: str, available: set[str] | None = None) -> str:
    names = [n for n in CYCLE if available is None or n in available]
    if current not in names:
        return names[0]
    return names[(names.index(current) + 1) % len(names)]
