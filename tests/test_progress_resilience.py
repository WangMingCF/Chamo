"""Progress reporting must never be able to fail a conversion.

Windows CI failed a 1200-page export with:

    PermissionError: [WinError 5] Access is denied: child-0.progress.tmp ->
    child-0.progress

The parent polls a shard's progress file while the shard replaces it. POSIX lets
that rename through; Windows does not. The engine treated the failed write as a
failed shard and published nothing, so a cosmetic progress update killed the job.

These tests patch os.replace narrowly (only for the file under test) so the fixture
setup itself still works, and they assert the retry happened -- removing the retry
turns them red rather than leaving them green.
"""
import json
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))
import workbench  # noqa: E402


def _patch_replace(monkeypatch, target_name, failures, seen):
    real = os.replace

    def replace(source, target):
        if Path(target).name == target_name:
            seen.append(target)
            if len(seen) <= failures:
                raise PermissionError(13, "Access is denied")
        return real(source, target)

    monkeypatch.setattr(workbench.os, "replace", replace)


def test_progress_never_raises_when_the_target_stays_locked(tmp_path, monkeypatch):
    seen = []
    _patch_replace(monkeypatch, "progress", 99, seen)
    monkeypatch.setattr(workbench, "PROGRESS", str(tmp_path / "progress"))
    workbench.report_progress(5, 10, force=True)
    assert len(seen) >= 2, "the write was not retried at all: %d attempt(s)" % len(seen)
    assert not (tmp_path / "progress.tmp").exists(), "temporary file left behind"


def test_progress_recovers_once_the_lock_clears(tmp_path, monkeypatch):
    seen = []
    _patch_replace(monkeypatch, "progress", 2, seen)
    target = tmp_path / "progress"
    monkeypatch.setattr(workbench, "PROGRESS", str(target))
    workbench.report_progress(7, 10, force=True)
    assert len(seen) == 3, "expected two failures then success, saw %d" % len(seen)
    assert json.loads(target.read_text(encoding="utf-8")) == {"done": 7, "total": 10}


def test_result_write_retries_before_failing(tmp_path, monkeypatch):
    seen = []
    _patch_replace(monkeypatch, "record.json", 1, seen)
    target = tmp_path / "record.json"
    workbench.atomic_json(target, {"ok": True}, durable=False)
    assert len(seen) == 2, "the retry did not happen: %d attempt(s)" % len(seen)
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_result_write_still_fails_when_the_target_never_unlocks(tmp_path, monkeypatch):
    """The retry must not turn a genuine write failure into a silent success."""
    seen = []
    _patch_replace(monkeypatch, "record.json", 99, seen)
    target = tmp_path / "record.json"
    try:
        workbench.atomic_json(target, {"ok": True}, durable=False)
    except OSError:
        pass
    else:
        raise AssertionError("a permanently unwritable record was reported as written")
    assert not target.exists()
