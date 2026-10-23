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


def test_a_chunk_worker_records_the_cancelled_file_as_cancelled(tmp_path, monkeypatch):
    """The multi-file worker writes one result per file; the cancelled one must say so."""
    job = _job(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    chunk = tmp_path / "chunk.json"
    chunk.write_text(json.dumps({"progress": str(tmp_path / "progress.json"),
                                 "entries": [
                                     {"job": job, "result": str(first)},
                                     {"job": job, "result": str(second)},
                                 ]}), encoding="utf-8")

    def cancelled(job):
        raise workbench.RunCancelled(CANCEL)

    monkeypatch.setattr(workbench, "one", cancelled)
    monkeypatch.setattr(sys, "argv", ["workbench", "--chunk", str(chunk)])
    workbench.main()

    result = json.loads(first.read_text(encoding="utf-8"))
    assert result["state"] == "cancelled", result
    assert CANCEL in result["error"]


def test_finish_withdraws_the_product_of_a_cancelled_file(tmp_path, monkeypatch):
    """finish() must trigger the withdrawal of what a cancelled file published.

    The deletion itself lives in _withdraw_product (covered by the engine tests);
    this test pins the wiring in finish(): that the helper is called, and that a
    failure in it is recorded instead of swallowed.
    """
    published = tmp_path / "out" / "讲义_0001.png"
    published.parent.mkdir(parents=True)
    published.write_bytes(b"published")

    removed = []

    def withdraw(result, out, req):
        for item in result.get("outputs", []):
            Path(item["path"]).unlink(missing_ok=True)
            removed.append(item["path"])

    monkeypatch.setattr(workbench, "event", lambda *a, **k: None)
    monkeypatch.setattr(workbench, "_committed_record",
                        lambda *a, **k: {"outputs": [{"path": str(published)}]})
    monkeypatch.setattr(workbench, "_withdraw_product", withdraw)

    record = tmp_path / "record"
    record.mkdir()
    stand_in = _SupervisorStandIn(tmp_path / "out", cancelled=True)
    entry = {"index": 0, "job": {"sources": [str(tmp_path / "a.pdf")],
                                 "output": str(tmp_path / "out")}}
    result = {"state": "cancelled", "error": CANCEL, "sources": [], "outputs": []}

    workbench.Supervisor.finish(stand_in, entry, result, record)

    assert removed == [str(published)]
    assert not published.exists(), "the published product survived the cancel"
    assert not result.get("withdraw_failed")


def test_a_failed_withdraw_is_recorded_not_swallowed(tmp_path, monkeypatch):
    """Silently keeping the product would break "a cancelled run leaves nothing"."""
    monkeypatch.setattr(workbench, "event", lambda *a, **k: None)

    def broken(*args, **kwargs):
        raise PermissionError("输出目录只读")

    monkeypatch.setattr(workbench, "_committed_record", broken)
    record = tmp_path / "record"
    record.mkdir()
    stand_in = _SupervisorStandIn(tmp_path / "out", cancelled=True)
    entry = {"index": 0, "job": {"sources": [str(tmp_path / "a.pdf")],
                                 "output": str(tmp_path / "out")}}
    result = {"state": "cancelled", "error": CANCEL, "sources": [], "outputs": []}

    workbench.Supervisor.finish(stand_in, entry, result, record)

    assert result.get("withdraw_failed") is True
    assert "撤回已发布成品失败" in result["error"]


def test_the_real_supervisor_and_withdraw_helpers_line_up():
    """The stand-in must not hide a missing attribute or a changed signature.

    finish() calls _committed_record(..., newer_than=self.started_wall) and then
    _withdraw_product(...). A typo in either name would be swallowed by the
    surrounding except, every cancel would be marked withdraw_failed, and the
    published product would silently survive - with the mocked tests still green.
    """
    import inspect

    supervisor = workbench.Supervisor({"output": "/tmp/chamo-cancel-gate"})
    assert hasattr(supervisor, "started_wall"), "finish() reads self.started_wall"
    assert isinstance(supervisor.started_wall, float)
    parameters = inspect.signature(workbench._committed_record).parameters
    assert "newer_than" in parameters, parameters
    assert callable(workbench._withdraw_product)


def test_a_wrapped_cancellation_is_still_a_cancellation():
    """pypdfium2 re-raises a callback exception as "argument 1: <type>: ...".

    The browser UI on a 300-page PDF showed that shape on the item line: state
    failed, text "argument 1: RunCancelled: 任务已取消…". The item line must say
    已取消, and the raw wrapper must not leak into the user's report.
    """
    try:
        try:
            raise workbench.RunCancelled(CANCEL)
        except Exception as inner:
            raise TypeError("argument 1: " + str(inner)) from inner
    except Exception as outer:
        found = workbench.cancellation_cause(outer)
        assert isinstance(found, workbench.RunCancelled), found
        assert CANCEL in str(found)


def test_an_ordinary_failure_is_not_mistaken_for_a_cancellation():
    """The opposite error is just as bad: a real failure must not read 已取消."""
    assert workbench.cancellation_cause(ValueError("boom")) is None
    job = {"sources": ["a.pdf"], "output": "/tmp/chamo-cancel-gate"}
    result = workbench.job_error_result(ValueError("boom"), job)
    assert result["state"] == "failed", result
    assert result["error"] == "boom", result


def test_the_worker_result_for_a_wrapped_cancel_is_the_cancelled_state():
    """Same chain the shard path caught: the result file must not say failed."""
    job = {"sources": ["a.pdf"], "output": "/tmp/chamo-cancel-gate"}
    try:
        try:
            raise workbench.RunCancelled(CANCEL)
        except Exception as inner:
            raise TypeError("argument 1: " + str(inner)) from inner
    except Exception as outer:
        result = workbench.job_error_result(outer, job)
    assert result["state"] == "cancelled", result
    assert result["error"] == CANCEL, result
    assert result["outputs"] == [], result
