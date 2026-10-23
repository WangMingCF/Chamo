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

    def fake_run(argv, **kwargs):
        calls.append([Path(a).name for a in argv if not a.startswith("-") and Path(a).suffix])
        outdir = Path(argv[argv.index("--outdir") + 1])
        for name in argv[argv.index("pdf") + 1:]:
            if name.startswith("--outdir") or name == str(outdir):
                continue
            source = Path(name)
            if source.suffix and not source.name.startswith("lost"):
                _pdf(outdir / (source.stem + ".pdf"))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(workbench.subprocess, "run", fake_run)
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
    real = workbench.subprocess.run
    state = {"n": 0}

    def flaky(argv, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise subprocess.TimeoutExpired(argv, 1)
        return real(argv, **kwargs)

    monkeypatch.setattr(workbench.subprocess, "run", flaky)
    files = [_zip(tmp_path / f"d{i}.pptx") for i in range(4)]
    workbench.office_batch_prepare([*map(str, files)], cfg, 2)
    assert set(workbench.BATCH_PDFS) == {str(files[2]), str(files[3])}


def test_cancel_stops_before_the_next_batch(fake_soffice, tmp_path):
    cfg, calls = fake_soffice
    files = [_zip(tmp_path / f"d{i}.pptx") for i in range(6)]
    workbench.office_batch_prepare([*map(str, files)], cfg, 2, cancelled=lambda: len(calls) >= 1)
    assert len(calls) == 1


def test_office_pdf_uses_the_batched_pdf_without_starting_soffice(monkeypatch, fake_soffice, tmp_path):
    cfg, _ = fake_soffice
    source = _zip(tmp_path / "deck.pptx")
    workbench.office_batch_prepare([str(source)], cfg, 2)
    monkeypatch.setattr(workbench.subprocess, "run", lambda *a, **k: pytest.fail("soffice was started again"))
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
