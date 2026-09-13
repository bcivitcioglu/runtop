"""``--keys`` scripts for recordings: space-separated tokens driven through Textual's Pilot.

Tokens: key names (``down up left right enter tab esc space ctrl+p shift+tab``), single
characters (``j`` ``X`` ``/``) and ``wait:N`` (seconds, float). Commas also separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import AutopilotCallbackType
    from textual.pilot import Pilot

STEP_DELAY = 0.45
TYPE_DELAY = 0.12

_ALIASES = {"return": "enter", "escape": "esc", " ": "space", "pgdn": "pagedown", "pgup": "pageup"}


@dataclass(frozen=True, slots=True)
class Step:
    key: str | None = None
    wait: float = 0.0


def parse_keys(script: str) -> list[Step]:
    steps: list[Step] = []
    for tok in script.replace(",", " ").split():
        if tok.startswith("wait:"):
            try:
                secs = float(tok.removeprefix("wait:"))
            except ValueError:
                secs = 1.0
            steps.append(Step(wait=max(0.0, secs)))
        else:
            key = _ALIASES.get(tok.lower(), tok) if len(tok) > 1 else tok
            if key == "esc":
                key = "escape"
            steps.append(Step(key=key))
    return steps


def autopilot(steps: list[Step], delay: float = STEP_DELAY) -> AutopilotCallbackType:
    import asyncio

    async def run(pilot: Pilot[object]) -> None:
        app = pilot.app

        async def sleep(secs: float) -> bool:
            """Sleep in slices; False once the app is exiting (so --quit-after is never delayed)."""
            end = asyncio.get_running_loop().time() + secs
            while not getattr(app, "_exit", False):
                left = end - asyncio.get_running_loop().time()
                if left <= 0:
                    return True
                await asyncio.sleep(min(0.05, left))
            return False

        if not await sleep(0.8):  # let the first frame and first fetch land
            return
        for i, step in enumerate(steps):
            if step.key is None:
                if not await sleep(step.wait):
                    return
            else:
                if getattr(app, "_exit", False):
                    return
                await pilot.press(step.key)
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                typing = len(step.key) == 1 or step.key == "space"
                if typing and nxt is not None and nxt.key is not None and (len(nxt.key) == 1 or nxt.key == "space"):
                    pause = TYPE_DELAY  # consecutive characters read as typing
                else:
                    pause = delay
                if not await sleep(pause):
                    return

    return run
