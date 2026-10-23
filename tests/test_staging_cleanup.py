"""Cleanup of the staging folder must be safe to run twice.

`finish()` removes this run's staging folders *before* it emits the finished
event, and the surrounding finally removes them again. If either call were not
idempotent the retry would raise after the run had already finished and turn a
completed job into an error - the exact failure #52 exists to prevent. The
reviewer of that PR asked for this guarantee; here it is as a test instead of as
prose in a comment.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import platform_state  # noqa: E402
import workbench  # noqa: E402

FINGERPRINT = "abcd1234"


def _layout(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    root = platform_state.staging(out)
    staged = root / (FINGERPRINT + "-deadbeef")
    staged.mkdir(parents=True)
    (staged / "page0001.png").write_bytes(b"x")
    (record / ("staging-" + FINGERPRINT + ".json")).write_text(
        json.dumps({"path": str(staged), "fingerprint": FINGERPRINT}), encoding="utf-8")
    return out, record, staged, root


def test_cleanup_is_safe_to_run_twice(tmp_path):
    out, record, staged, root = _layout(tmp_path)

    workbench._cleanup_run_staging(out, record)
    workbench._cleanup_run_staging(out, record)      # the retry in the finally block

    assert not staged.exists(), "the staging folder survived the first call"
    assert not (record / ("staging-" + FINGERPRINT + ".json")).exists(), \
        "the register file survived"


def test_dropping_the_staging_root_is_safe_to_run_twice(tmp_path):
    out, record, staged, root = _layout(tmp_path)

    workbench._cleanup_run_staging(out, record)
    platform_state.drop_staging(out)
    platform_state.drop_staging(out)                 # rmdir on a missing path

    assert not root.exists(), "an empty staging root was left behind"


def test_cleanup_leaves_a_folder_it_did_not_register(tmp_path):
    """A concurrent run's staging folder must survive this run's cleanup."""
    out, record, staged, root = _layout(tmp_path)
    other = root / "ffff0000-keepme"
    other.mkdir()

    workbench._cleanup_run_staging(out, record)

    assert other.is_dir(), "cleanup removed a staging folder this run never registered"
