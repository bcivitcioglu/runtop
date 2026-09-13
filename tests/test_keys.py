from __future__ import annotations

from runtop.keys import Step, parse_keys


def test_parse_keys_tokens() -> None:
    assert parse_keys("down up, enter wait:1.5 tab esc space ctrl+p X / wait:x") == [
        Step("down"), Step("up"), Step("enter"), Step(wait=1.5), Step("tab"), Step("escape"), Step("space"),
        Step("ctrl+p"), Step("X"), Step("/"), Step(wait=1.0)]
