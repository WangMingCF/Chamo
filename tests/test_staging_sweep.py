"""The staging sweep deletes folders left by crashed runs; it had no test either.

A crash, an OOM kill or a power loss can leave hundreds of megabytes of intermediate
pages in a staging folder. The sweep collects them, and its one safety property is
that a folder still being written to is never touched - that is what keeps a long
export running in another process alive. It also only ever removes folders, never
loose files.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench


def age(path: Path, seconds: int) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_a_staging_folder_a_running_job_still_writes_survives(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    fresh = root / "in-use"
    fresh.mkdir()
    (fresh / "page00001.jpeg").write_bytes(b"x" * 100)   # written recently
    old = root / "crashed"
    old.mkdir()
    (old / "page00001.jpeg").write_bytes(b"x" * 100)
    age(old, workbench.STAGING_MAX_AGE + 3600)
    age(old / "page00001.jpeg", workbench.STAGING_MAX_AGE + 3600)

    removed = workbench.sweep_staging(root)

    assert fresh.is_dir(), "a folder a running job keeps writing must never be collected"
    assert not old.exists(), "a crashed run's staging folder is collectable"
    assert removed == 1


def test_a_loose_file_at_the_staging_root_is_never_removed(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    leftover = root / "not-a-folder.bin"
    leftover.write_bytes(b"keep me")
    age(leftover, workbench.STAGING_MAX_AGE + 3600)

    removed = workbench.sweep_staging(root)

    assert leftover.is_file(), "the sweep only removes folders"
    assert removed == 0


def test_a_missing_staging_root_is_not_an_error(tmp_path):
    assert workbench.sweep_staging(tmp_path / "never-created") == 0
