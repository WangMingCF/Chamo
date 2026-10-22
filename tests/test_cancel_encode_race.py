"""A cancel must not leave a page encoder running after render() returns.

CI once left ".Chamo暂存/<job>/扫描讲义.pdf_page00041.jpeg" behind after a
cancelled pdf-images run (tests/test_page_resume.py::
test_cancelled_then_resumed_outputs_match_a_clean_run, macos-latest).

Cause: ThreadPoolExecutor starts its threads lazily inside submit(). submit()
queues the page first, then starts a thread and waits for it to boot. The
cancel handler raises in the main thread, and when that lands inside that wait
the new thread is already running but was never added to the pool's thread
set. shutdown(wait=True) does not join it; it takes the page that was just
queued and writes it into staging while _run_staging() removes the folder. The
register file is gone by then, so the supervisor cannot clean up either.

The window is a few microseconds, so the gate does not wait for a signal to
land there. It raises the cancel exactly in that spot instead: the first pool
thread is started for real, the patched start() waits until that thread has
taken the queued page, and then raises like the signal handler would. A slow
encode (CHAMO_TEST_SLOW_ENCODE) makes the stray write happen well after
render() returns when nothing waits for it.
"""
from __future__ import annotations

import concurrent.futures.thread as pool_thread
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench  # noqa: E402

CANCEL = "任务已取消，原件保留，可重新核验并恢复"


def _pdf(path: Path, pages: int = 3) -> Path:
    frames = [Image.new("RGB", (200, 260), (40 * n, 90, 160)) for n in range(pages)]
    frames[0].save(str(path), "PDF", save_all=True, append_images=frames[1:], resolution=72.0)
    return path


def _pool_threads():
    return [t for t in threading.enumerate()
            if getattr(t, "_target", None) is pool_thread._worker]


@pytest.mark.parametrize("cached", [False, True], ids=["no-cache", "page-cache"])
def test_cancel_during_lazy_thread_start_leaves_no_writer(tmp_path, monkeypatch, cached):
    work = tmp_path / "staging"
    work.mkdir()
    source = _pdf(tmp_path / "scan.pdf")
    cfg = {"operation": "pdf_images", "format": "jpeg", "dpi": 72, "quality": 80, "range": ""}
    cache = None
    if cached:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cache = workbench.page_cache_adopt(str(cache_dir))

    monkeypatch.setattr(workbench, "THREADS", 2)
    monkeypatch.setattr(workbench, "PROGRESS", None)
    monkeypatch.setenv("CHAMO_TEST_SLOW_ENCODE", "0.5")

    before = set(_pool_threads())
    original_start = threading.Thread.start
    fired = []

    def start(self):
        original_start(self)
        if fired or getattr(self, "_target", None) is not pool_thread._worker:
            return
        fired.append(self)
        # The thread is running. Wait until it has taken the page that submit()
        # queued, so the stray write is certain, then cancel like the handler.
        queue = self._args[1]
        deadline = time.monotonic() + 5
        while not queue.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        raise RuntimeError(CANCEL)

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="任务已取消"):
        workbench.render(source, work, cfg, "scan.pdf", cache=cache)
    monkeypatch.setattr(threading.Thread, "start", original_start)
    assert fired, "the gate never reached a lazy thread start; it tests nothing"

    # This is the moment _run_staging() removes the folder. Nothing may write
    # into it from here on.
    when_render_returned = sorted(p.name for p in work.iterdir())
    for stray in set(_pool_threads()) - before:
        stray.join(timeout=10)
    after_writers_ended = sorted(p.name for p in work.iterdir())
    assert after_writers_ended == when_render_returned, (
        "a page encoder kept writing into staging after render() returned on cancel: "
        "%r appeared later. _run_staging() would remove the folder while it writes, "
        "and the cancelled run leaves that page behind."
        % sorted(set(after_writers_ended) - set(when_render_returned)))
