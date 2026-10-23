"""The page-cache sweep deletes files and had no test at all.

sweep_page_cache runs at the start of every job and removes cached page sets by age
and by a size bound. Two properties keep that safe, and neither was pinned:

  * a disabled cache is never touched;
  * a set an export is still writing (fresh .active marker) is never removed.

An untested deletion path is the worst kind to leave uncovered: a wrong predicate
quietly throws away another run's work instead of raising.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import platform_state


def make_set(base: Path, source: str, config: str, pages: int = 2, size: int = 1000) -> Path:
    """One cached page set: <root>/pdf_images/<source>/<config>/<pages>."""
    folder = base / source / config
    folder.mkdir(parents=True)
    for index in range(pages):
        (folder / ("page%05d.jpeg" % index)).write_bytes(b"x" * size)
    return folder


def cache_base(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "pdf_images"


def age_folder(folder: Path, seconds: int) -> None:
    stamp = time.time() - seconds
    os.utime(folder, (stamp, stamp))
    for child in folder.iterdir():
        os.utime(child, (stamp, stamp))


def test_a_disabled_cache_is_never_touched(tmp_path, monkeypatch):
    base = cache_base(tmp_path)
    folder = make_set(base, "s1", "c1")
    age_folder(folder, 10_000)
    monkeypatch.setenv("CHAMO_PAGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CHAMO_PAGE_CACHE", "0")
    # Bounds that would definitely collect this set if the guard were missing. Without
    # them this test passes for the wrong reason: the default age bound alone keeps the
    # folder, so removing the guard would go unnoticed (verified by mutation).
    monkeypatch.setenv("CHAMO_PAGE_CACHE_MAX_AGE", "0")
    monkeypatch.setenv("CHAMO_PAGE_CACHE_MAX_BYTES", "1")

    result = platform_state.sweep_page_cache()

    assert folder.is_dir(), "a disabled cache must not lose files"
    assert result["dirs"] == 0 and result["bytes"] == 0


def test_an_active_set_survives_even_when_it_alone_exceeds_the_limit(tmp_path, monkeypatch):
    base = cache_base(tmp_path)
    active = make_set(base, "s1", "active-set", size=5000)
    (active / platform_state.PAGE_CACHE_MARKER).write_bytes(b"")
    monkeypatch.setenv("CHAMO_PAGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CHAMO_PAGE_CACHE", raising=False)
    monkeypatch.setenv("CHAMO_PAGE_CACHE_MAX_BYTES", "1")
    monkeypatch.setenv("CHAMO_PAGE_CACHE_MAX_AGE", "999999999")

    platform_state.sweep_page_cache()

    assert active.is_dir(), "a set another export is writing must never be evicted"


def test_an_old_set_is_removed_and_its_bytes_are_counted(tmp_path, monkeypatch):
    base = cache_base(tmp_path)
    old = make_set(base, "s1", "old-set", pages=2, size=1000)
    age_folder(old, 100_000)
    monkeypatch.setenv("CHAMO_PAGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CHAMO_PAGE_CACHE", raising=False)
    monkeypatch.setenv("CHAMO_PAGE_CACHE_MAX_BYTES", "100000000")
    monkeypatch.setenv("CHAMO_PAGE_CACHE_MAX_AGE", "60")

    result = platform_state.sweep_page_cache()

    assert not old.exists(), "a set past the age bound is collectable"
    assert result["dirs"] == 1
    assert result["bytes"] == 2000, result


def test_a_stale_marker_is_dropped_so_the_set_can_be_collected(tmp_path, monkeypatch):
    base = cache_base(tmp_path)
    stale = make_set(base, "s1", "stale-marker")
    marker = stale / platform_state.PAGE_CACHE_MARKER
    marker.write_bytes(b"")
    age_folder(stale, platform_state.PAGE_CACHE_ACTIVE_WINDOW + 600)
    monkeypatch.setenv("CHAMO_PAGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CHAMO_PAGE_CACHE", raising=False)
    monkeypatch.setenv("CHAMO_PAGE_CACHE_MAX_BYTES", "100000000")
    monkeypatch.setenv("CHAMO_PAGE_CACHE_MAX_AGE", "999999999")

    result = platform_state.sweep_page_cache()

    assert not marker.exists(), "a killed writer's marker must not protect the set forever"
    assert result["markers"] == 1