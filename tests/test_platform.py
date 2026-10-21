"""Cross-platform publication and renderer discovery.

These run on every platform and assert the same product promises everywhere:
a published file never replaces an existing one, the published bytes match the
staged bytes, and the renderer lookup only returns something executable here.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
import platform_atomic as atomic
import platform_office as office


def test_publish_moves_file(tmp_path):
    source = tmp_path / "staged.png"
    source.write_bytes(b"payload")
    target = tmp_path / "published.png"
    atomic.publish_exclusive(source, target)
    assert target.read_bytes() == b"payload"
    assert not source.exists(), "the staged copy must not linger"


def test_publish_refuses_to_overwrite(tmp_path):
    existing = tmp_path / "published.png"
    existing.write_bytes(b"original")
    source = tmp_path / "staged.png"
    source.write_bytes(b"replacement")
    with pytest.raises(FileExistsError):
        atomic.publish_exclusive(source, target=existing)
    assert existing.read_bytes() == b"original", "existing content must be untouched"
    assert source.exists(), "a refused publish must not consume the staged file"


def test_publish_keeps_unicode_names(tmp_path):
    source = tmp_path / "讲义 第1页.png"
    source.write_bytes(b"x")
    target = tmp_path / "输出 讲义 第1页.png"
    atomic.publish_exclusive(source, target)
    assert target.exists()


def test_publish_reports_the_primitive_that_ran(tmp_path):
    # Asserting only that some candidate exists would be tautological, since the
    # candidate list is never empty. Record what actually executed instead.
    source = tmp_path / "staged.bin"
    source.write_bytes(b"x")
    atomic.publish_exclusive(source, tmp_path / "target.bin")
    ran = atomic.last_strategy()
    assert ran is not None
    assert ran in {name for name, _ in atomic._candidates()}


def test_renderer_lookup_returns_executable_or_none():
    # Search the real project root, not tmp_path: passing an empty directory made
    # this skip even on macOS where a bundled LibreOffice exists, which read as
    # "no renderer on this machine" and hid the actual coverage.
    found = office.find(ROOT / "engine")
    if found is None:
        pytest.skip("no Office renderer in this checkout and none on PATH")
    assert Path(found).exists()
    assert os.access(found, os.X_OK)


def test_renderer_lookup_ignores_foreign_binaries(tmp_path):
    # A file at the expected path that cannot run here must be rejected rather
    # than returned and then failing with "Exec format error".
    fake = tmp_path / "Helpers/LibreOffice.app/Contents/MacOS/soffice"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"\x7fELFnot-really" if sys.platform == "darwin" else b"\xcf\xfa\xed\xfe")
    fake.chmod(0o755)
    assert office.find(tmp_path) != str(fake)


def test_renderer_lookup_accepts_shebang_wrapper(tmp_path):
    wrapper = tmp_path / "Helpers/LibreOffice/program/soffice"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(b"#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    assert office.find(tmp_path) == str(wrapper)
