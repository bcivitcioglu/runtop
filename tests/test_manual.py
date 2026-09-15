from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_offline_manual_and_unknown_topic() -> None:
    result = subprocess.run([sys.executable, "-m", "runtop", "docs", "--json"],
                            capture_output=True, text=True, check=True)
    manual = json.loads(result.stdout)
    assert manual["schema"] == "runtop.docs/v1" and manual["edition"] == "full"
    assert {t["id"] for t in manual["topics"]} >= {"agents", "install", "cli", "preferences"}
    assert all(t["content"] for t in manual["topics"])
    assert "docs/internal/" not in result.stdout
    listing = subprocess.run([sys.executable, "-m", "runtop", "docs", "--list", "--json"],
                             capture_output=True, text=True, check=True)
    assert all("content" not in t for t in json.loads(listing.stdout)["topics"])
    bad = subprocess.run([sys.executable, "-m", "runtop", "docs", "missing-topic"], capture_output=True)
    assert bad.returncode == 2 and not bad.stdout


def test_manual_matches_public_sources() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts/build_docs.py"), "--check"], cwd=ROOT, check=True)


def test_full_preferences_roundtrip_and_invalid_values(tmp_path: Path) -> None:
    from runtop.config import Config, load, save

    path = tmp_path / "config.json"
    assert save(Config(section="images", sort="mem", last_target="ctx:sample"), path)
    loaded = load(path)
    assert (loaded.section, loaded.sort, loaded.last_target) == ("images", "mem", "ctx:sample")
    path.write_text('{"section":"bad","sort":"bad"}')
    assert (load(path).section, load(path).sort) == ("containers", "name")
    assert not list(tmp_path.glob(".config-*"))


async def test_full_restores_view_and_sort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtop.config import Config, load
    from runtop.state.store import Section

    from .helpers import main_screen, make_app, wait_for

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = make_app(config=Config(section="images", sort="mem"))
    async with app.run_test(size=(160, 45)) as pilot:
        assert await wait_for(pilot, lambda: app.focused is not None)
        screen = main_screen(app)
        assert screen.section is Section.IMAGES and screen.sort == "mem"
        screen.section, screen.sort = Section.CONTAINERS, "cpu"
        await pilot.press("q")
    assert (load().section, load().sort) == ("containers", "cpu")
