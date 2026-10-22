"""A cancelled file is cancelled, not failed.

The user pressed stop. The file that was in flight when the signal arrived keeps
its source, publishes nothing, and must be reported as "cancelled" so the window
and the browser show 已取消 and the failure count does not include it. Before this
test the worker wrote state "failed" with the cancellation text, so the app had
nothing to tell apart from a real error.

The gate is deliberately narrow: it patches one() to raise RunCancelled exactly
where the signal handler does, and reads the result file the worker leaves for the
supervisor. Whether a real Ctrl-C lands in that window is covered by the cancel
tests (test_cancel_encode_race.py, test_page_resume.py), which drive real signals.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench  # noqa: E402

CANCEL = "任务已取消，原件保留，可重新核验并恢复"


def _job(tmp_path: Path) -> dict:
    source = tmp_path / "a.pdf"
    source.write_bytes(b"%PDF-1.4\n% synthetic\n")
    return {"sources": [str(source)], "output": str(tmp_path / "out"),
            "config": {"operation": "pdf_images", "format": "png", "dpi": 72}}


def test_the_worker_records_a_cancelled_file_as_cancelled(tmp_path, monkeypatch):
    job = _job(tmp_path)
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    result_path = tmp_path / "result.json"

    def cancelled(job):
        raise workbench.RunCancelled(CANCEL)

    monkeypatch.setattr(workbench, "one", cancelled)
    monkeypatch.setattr(sys, "argv", ["workbench", "--one", str(job_path),
                                      "--result", str(result_path)])
    workbench.main()

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "cancelled", result
    assert CANCEL in result["error"]
    assert result["sources"] == job["sources"]
    assert result["outputs"] == []


def test_a_real_failure_is_still_a_failure(tmp_path, monkeypatch):
    """The narrow gate above must not turn every error into a cancellation."""
    job = _job(tmp_path)
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    result_path = tmp_path / "result.json"

    def broken(job):
        raise ValueError("引擎内部错误")

    monkeypatch.setattr(workbench, "one", broken)
    monkeypatch.setattr(sys, "argv", ["workbench", "--one", str(job_path),
                                      "--result", str(result_path)])
    workbench.main()

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "failed", result
    assert "引擎内部错误" in result["error"]


class _SupervisorStandIn:
    """Just enough of Supervisor for finish(): the downgrade path needs no worker."""

    def __init__(self, output: Path, cancelled: bool):
        import threading

        self.lock = threading.RLock()
        self.results = []
        self.done = 0
        self.total = 1
        self.cancelled = cancelled
        self.req = {"output": str(output)}
        self.started_wall = time.time()
        self.last_progress = 0.0


def test_a_worker_killed_for_another_reason_is_a_failure(tmp_path, monkeypatch):
    """Timeout, a full disk and a crash end in the same terminate handler.

    Only a run the user actually stopped may be reported as cancelled; otherwise
    the failure count would silently drop a real fault and the user would be told
    they pressed stop.
    """
    monkeypatch.setattr(workbench, "event", lambda *args, **kwargs: None)
    record = tmp_path / "record"
    record.mkdir()
    stand_in = _SupervisorStandIn(tmp_path / "out", cancelled=False)
    entry = {"index": 0, "job": {"sources": [str(tmp_path / "a.pdf")],
                                 "output": str(tmp_path / "out")}}
    result = {"state": "cancelled", "error": CANCEL, "sources": [], "outputs": []}

    workbench.Supervisor.finish(stand_in, entry, result, record)

    assert result["state"] == "failed", result
    assert "不是用户取消" in result["error"]


def test_a_user_cancel_stays_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, "event", lambda *args, **kwargs: None)
    monkeypatch.setattr(workbench, "_committed_record", lambda *a, **k: None)
    record = tmp_path / "record"
    record.mkdir()
    stand_in = _SupervisorStandIn(tmp_path / "out", cancelled=True)
    entry = {"index": 0, "job": {"sources": [str(tmp_path / "a.pdf")],
                                 "output": str(tmp_path / "out")}}
    result = {"state": "cancelled", "error": CANCEL, "sources": [], "outputs": []}

    workbench.Supervisor.finish(stand_in, entry, result, record)

    assert result["state"] == "cancelled", result
