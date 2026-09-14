from __future__ import annotations

import json
import subprocess
import sys

import pytest

from runtop.ps_cli import _parser, listing


async def test_ps_json_filters_and_target(capsys: pytest.CaptureFixture[str]) -> None:
    args = _parser().parse_args(["--demo", "--json", "--target", "lima:docker"])
    assert await listing(args) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == "runtop.snapshot/v1"
    assert len(document["targets"]) == 1
    assert all(c["state"] == "running" for c in document["targets"][0]["containers"])


async def test_ps_unknown_target(capsys: pytest.CaptureFixture[str]) -> None:
    assert await listing(_parser().parse_args(["--demo", "--target", "missing"])) == 2
    assert "unknown target" in capsys.readouterr().err


def test_ps_entrypoint() -> None:
    result = subprocess.run([sys.executable, "-m", "runtop", "ps", "--demo", "-a", "--json"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["targets"]
