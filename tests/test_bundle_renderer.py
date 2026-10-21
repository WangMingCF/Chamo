"""The app must find a usable Office renderer, preferring the one it ships.

These run on every platform. On macOS the bundled LibreOffice must be found; on
Linux, where the checkout has a macOS binary that cannot execute, a system
LibreOffice is the correct answer. What must never happen is "no renderer" while
a usable one exists.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
import platform_office as office


def bundled() -> Path:
    return ROOT / "third_party/LibreOffice.app/Contents/MacOS/soffice"


def test_finds_a_runnable_renderer_in_the_project():
    found = office.find(ROOT / "engine")
    if found is None:
        pytest.skip("no renderer for this platform in this checkout")
    assert Path(found).exists()
    assert office.runnable(found), f"returned a path that cannot run here: {found}"


def test_prefers_the_bundled_renderer_when_it_can_run():
    if not office.runnable(bundled()):
        pytest.skip("bundled renderer is not executable on this platform")
    found = office.find(ROOT / "engine")
    assert found is not None
    assert "third_party/LibreOffice.app" in found, f"picked {found}"


def test_built_app_can_find_its_own_renderer():
    app = ROOT / "build/Chamo.app"
    engine_dir = app / "Contents/Resources/engine"
    shipped = app / "Contents/Helpers/LibreOffice.app/Contents/MacOS/soffice"
    if not engine_dir.is_dir():
        pytest.skip("app bundle not built here")
    if not office.runnable(shipped):
        pytest.skip("shipped renderer is not executable on this platform")
    found = office.find(engine_dir)
    assert found is not None, "shipped app could not locate its own LibreOffice"
    assert "Helpers/LibreOffice.app" in found, f"picked a renderer outside the bundle: {found}"


def test_prefers_the_shipped_renderer_over_path(tmp_path, monkeypatch):
    """An explicit bundle must win over whatever is on PATH."""
    fake_bundle = tmp_path / "Helpers/LibreOffice.app/Contents/MacOS/soffice"
    fake_bundle.parent.mkdir(parents=True)
    fake_bundle.write_bytes(b"#!/bin/sh\nexit 0\n")
    fake_bundle.chmod(0o755)
    monkeypatch.setattr(office.shutil, "which", lambda name: "/usr/bin/soffice")
    assert office.find(tmp_path) == str(fake_bundle)
