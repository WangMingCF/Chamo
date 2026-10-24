"""Batch Office conversion (opt-in): what it may and may not trust.

One soffice call converts several files, but its exit code is always 0 and its error
lines carry no file name, so every input is checked on its own and anything that did
not come out as a readable PDF is left to the single-file path. soffice is replaced by
a fake that writes PDFs for the files it is given, so these tests need no LibreOffice.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench  # noqa: E402


def _pdf(path: Path):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with open(path, "wb") as handle:
        writer.write(handle)


@pytest.fixture
def fake_soffice(monkeypatch, tmp_path):
    exe = tmp_path / "soffice"
    exe.write_bytes(b"")
    monkeypatch.setattr(workbench.platform_office, "runnable", lambda path: True)
    calls = []

    class Done:
        def __init__(self, argv): self.argv = argv
        def communicate(self, timeout=None): return b"", b""

    def fake_spawn(argv, **kwargs):
        calls.append([Path(a).name for a in argv if not a.startswith("-") and Path(a).suffix])
        outdir = Path(argv[argv.index("--outdir") + 1])
        for name in argv[argv.index("pdf") + 1:]:
            if name.startswith("--outdir") or name == str(outdir):
                continue
            source = Path(name)
            if source.suffix and not source.name.startswith("lost"):
                _pdf(outdir / (source.stem + ".pdf"))
        return Done(argv)

    monkeypatch.setattr(workbench, "spawn_group", fake_spawn)
    yield {"soffice": str(exe)}, calls
    workbench.office_batch_clear()


def _zip(path: Path):
    import zipfile
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    return path


def test_each_input_is_checked_and_a_missing_result_is_left_to_the_single_path(fake_soffice, tmp_path):
    cfg, calls = fake_soffice
    good = [_zip(tmp_path / f"d{i}.pptx") for i in range(5)]
    lost = _zip(tmp_path / "lost.docx")          # soffice "succeeds" but writes nothing for it
    workbench.office_batch_prepare([*map(str, good), str(lost)], cfg, 3)
    assert set(workbench.BATCH_PDFS) == {str(p) for p in good}
    assert str(lost) not in workbench.BATCH_PDFS
    assert len(calls) == 2                       # 6 files in batches of 3


def test_name_clashes_and_files_that_are_not_what_their_suffix_says_are_never_batched(fake_soffice, tmp_path):
    cfg, _ = fake_soffice
    a = _zip(tmp_path / "a.docx")
    other = tmp_path / "sub"
    other.mkdir()
    clash = _zip(other / "a.pptx")               # would also write a.pdf into the same folder
    garbage = tmp_path / "text.pptx"
    garbage.write_bytes(b"not a zip at all")
    workbench.office_batch_prepare([str(a), str(clash), str(garbage)], cfg, 8)
    assert set(workbench.BATCH_PDFS) == {str(a)}


def test_a_batch_that_times_out_falls_back_and_does_not_fail_the_run(monkeypatch, fake_soffice, tmp_path):
    cfg, calls = fake_soffice
    real = workbench.spawn_group
    state = {"n": 0}
    killed = []

    class Hangs:
        def communicate(self, timeout=None):
            if timeout == 30 or killed:
                return b"", b""
            raise subprocess.TimeoutExpired("soffice", 1)

    def flaky(argv, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return Hangs()
        return real(argv, **kwargs)

    monkeypatch.setattr(workbench, "spawn_group", flaky)
    monkeypatch.setattr(workbench, "kill_tree", lambda proc: killed.append(proc))
    files = [_zip(tmp_path / f"d{i}.pptx") for i in range(4)]
    workbench.office_batch_prepare([*map(str, files)], cfg, 2)
    assert set(workbench.BATCH_PDFS) == {str(files[2]), str(files[3])}
    assert len(killed) == 1, "a timed-out batch must have its whole process tree killed"


def test_cancel_stops_batches_that_have_not_started(fake_soffice, tmp_path):
    cfg, calls = fake_soffice
    files = [_zip(tmp_path / f"d{i}.pptx") for i in range(12)]
    # 6 batches, at most 3 soffice processes at once: once 3 have started, cancel.
    workbench.office_batch_prepare([*map(str, files)], cfg, 2, cancelled=lambda: len(calls) >= 3)
    assert 3 <= len(calls) < 6, "batches that had not started must not start after a cancel"
    calls.clear()
    workbench.office_batch_prepare([*map(str, files)], cfg, 2, cancelled=lambda: True)
    assert calls == [], "a cancel before the start must spawn nothing"


def test_batches_run_concurrently_but_never_more_than_three(fake_soffice, tmp_path, monkeypatch):
    import threading
    import time
    cfg, _ = fake_soffice
    real = workbench.spawn_group
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow(argv, **kwargs):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.15)
        try:
            return real(argv, **kwargs)
        finally:
            with lock:
                live["now"] -= 1

    monkeypatch.setattr(workbench, "spawn_group", slow)
    files = [_zip(tmp_path / f"d{i}.pptx") for i in range(16)]
    workbench.office_batch_prepare([*map(str, files)], cfg, 2)   # 8 batches
    assert 2 <= live["peak"] <= 3, live
    assert len(workbench.BATCH_PDFS) == 16


def test_office_pdf_uses_the_batched_pdf_without_starting_soffice(monkeypatch, fake_soffice, tmp_path):
    cfg, _ = fake_soffice
    source = _zip(tmp_path / "deck.pptx")
    workbench.office_batch_prepare([str(source)], cfg, 2)
    monkeypatch.setattr(workbench.subprocess, "run", lambda *a, **k: pytest.fail("soffice was started again"))
    monkeypatch.setattr(workbench, "spawn_group", lambda *a, **k: pytest.fail("soffice was started again"))
    work = tmp_path / "work"
    work.mkdir()
    pdf = workbench.office_pdf(source, work, cfg)
    assert pdf.is_file() and pdf.name == "deck.pdf"


def test_clear_removes_the_scratch_folder_and_forgets_the_results(fake_soffice, tmp_path):
    cfg, _ = fake_soffice
    workbench.office_batch_prepare([str(_zip(tmp_path / "d.pptx"))], cfg, 2)
    scratch = workbench.BATCH_DIR
    assert scratch is not None and scratch.is_dir()
    workbench.office_batch_clear()
    assert not scratch.exists() and not workbench.BATCH_PDFS


def test_a_timed_out_batch_leaves_no_grandchild_behind(monkeypatch, tmp_path):
    """The real process tree: a launcher that starts a long-running child, then hangs.

    This is the Linux/Windows shape of soffice (a launcher in front of the renderer);
    killing only the direct child would leave the grandchild running.
    """
    import time

    marker = tmp_path / "grandchild.pid"
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(workbench.platform_office, "runnable", lambda path: True)
    real = workbench.spawn_group

    def through_launcher(argv, **kwargs):
        return real([sys.executable, str(launcher), str(marker)], **kwargs)

    monkeypatch.setattr(workbench, "spawn_group", through_launcher)
    source = _zip(tmp_path / "d.pptx")
    started = time.time()
    workbench.office_batch_prepare([str(source)], {"soffice": str(launcher), "timeout": 2}, 2)
    assert time.time() - started < 30
    workbench.office_batch_clear()
    pid = int(marker.read_text())
    deadline = time.time() + 10
    alive = True
    while time.time() < deadline:
        try:
            if sys.platform == "win32":
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                alive = bool(handle)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
            else:
                import os
                os.kill(pid, 0)
        except OSError:
            alive = False
        if not alive:
            break
        time.sleep(0.2)
    assert not alive, "the launcher's child survived the batch timeout"
