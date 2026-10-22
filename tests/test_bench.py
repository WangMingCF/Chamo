"""The benchmark harness has to be able to say "no".

Two claims are tested here, because a benchmark nobody can distrust is worse than
no benchmark:
  * the corpus is reproducible - the same suite built twice is the same bytes, so
    two reports can only differ because the engine changed;
  * the quality checks go RED for the shortcuts that would fake a better number:
    a missing page, a page rendered at a lower DPI, a page that belongs to another
    document, a dropped text layer, and changed pixels in "lossless" work.

The last test drives the real script end to end on the smoke corpus. It does not
assert a time (shared runners are not a benchmark), only that the plumbing
produces a verdict and a report.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


corpus = _load("chamo_bench_corpus", "scripts/bench_corpus.py")
quality = _load("chamo_bench_quality", "scripts/bench_quality.py")
bench = _load("chamo_bench", "scripts/bench.py")


# --------------------------------------------------------------------------
# Corpus reproducibility
# --------------------------------------------------------------------------
def _member_differences(left: Path, right: Path) -> list:
    """Which parts inside two OOXML packages differ (empty for a non-zip file).

    A byte difference in a .xlsx or .pptx says "something changed" but not what,
    and the writer that changed it is what the next fix has to target. Naming the
    members turns a red test into a diagnosis.
    """
    import zipfile

    try:
        with zipfile.ZipFile(left) as first, zipfile.ZipFile(right) as second:
            names = sorted(set(first.namelist()) | set(second.namelist()))
            changed = []
            for name in names:
                if name not in first.namelist() or name not in second.namelist():
                    changed.append(name + " (present on one side only)")
                elif first.read(name) != second.read(name):
                    changed.append(name)
            return changed
    except zipfile.BadZipFile:
        return []


def test_the_same_suite_builds_the_same_bytes(tmp_path):
    """A benchmark comparison is only meaningful if the input did not change."""
    first = corpus.build(tmp_path / "a", "smoke")
    second = corpus.build(tmp_path / "b", "smoke")
    differing = [entry["path"] for entry, other in zip(first["files"], second["files"])
                 if entry["sha256"] != other["sha256"]]
    detail = {name: _member_differences(tmp_path / "a" / name, tmp_path / "b" / name)
              for name in differing}
    assert differing == [], "the smoke corpus is not reproducible: %s" % detail
    assert corpus.check(tmp_path / "a") == []


def test_a_changed_or_extra_file_is_reported(tmp_path):
    corpus.build(tmp_path / "c", "smoke")
    assert corpus.check(tmp_path / "c") == []
    (tmp_path / "c" / "text.pdf").write_bytes(b"not the same pdf")
    (tmp_path / "c" / "stray.bin").write_bytes(b"x")
    issues = corpus.check(tmp_path / "c")
    assert any("text.pdf" in issue and "changed" in issue for issue in issues), issues
    assert any("stray.bin" in issue for issue in issues), issues


def test_refuses_to_write_over_a_previous_corpus(tmp_path):
    corpus.build(tmp_path / "d", "smoke")
    try:
        corpus.build(tmp_path / "d", "smoke")
    except SystemExit as error:
        assert "non-empty" in str(error)
    else:
        raise AssertionError("building into a used directory must be refused")


# --------------------------------------------------------------------------
# Helpers for the quality checks
# --------------------------------------------------------------------------
def _pages_at(directory: Path, images, stem: str = "doc") -> Path:
    """Write a page set the way the engine names it, from rendered pages."""
    directory.mkdir(parents=True, exist_ok=True)
    for number, image in sorted(images.items()):
        image.save(directory / ("%s_%03d.png" % (stem, number)), "PNG")
    return directory


def _source_pdf(path: Path, pages: int = 3) -> Path:
    from reportlab.pdfgen import canvas

    canvas_obj = canvas.Canvas(str(path), invariant=1)
    for index in range(pages):
        canvas_obj.setFont("Helvetica", 14)
        canvas_obj.drawString(60, 760, "Benchmark page %d" % (index + 1))
        for row in range(12):
            canvas_obj.drawString(60, 700 - row * 20, "row %d value %d" % (row, row * index))
        canvas_obj.showPage()
    canvas_obj.save()
    return path


def test_identical_render_passes_and_every_shortcut_fails(tmp_path):
    source = _source_pdf(tmp_path / "source.pdf", 3)
    reference = _pages_at(tmp_path / "good", quality.render_pdf_pages(source, 72))
    assert quality.check_pdf_images(source, reference, 72) == []

    # 1. one page missing: "faster" by doing less work.
    missing = _pages_at(tmp_path / "missing", quality.render_pdf_pages(source, 72))
    (missing / "doc_002.png").unlink()
    assert any("missing 1 page" in issue for issue in quality.check_pdf_images(source, missing, 72))

    # 2. a page rendered at a lower DPI: the classic way to fake a speed-up.
    low_dpi = _pages_at(tmp_path / "low-dpi", quality.render_pdf_pages(source, 36))
    issues = quality.check_pdf_images(source, low_dpi, 72)
    assert any("size" in issue for issue in issues), issues

    # 3. the right number of pages, but one belongs to another document.
    swapped = _pages_at(tmp_path / "swapped", quality.render_pdf_pages(source, 72))
    other = _source_pdf(tmp_path / "other.pdf", 1)
    quality.render_pdf_pages(other, 72)[1].save(swapped / "doc_003.png", "PNG")
    issues = quality.check_pdf_images(source, swapped, 72)
    assert any("page 3" in issue for issue in issues), issues

    # 4. an extra page file that the selection never asked for.
    extra = _pages_at(tmp_path / "extra", quality.render_pdf_pages(source, 72))
    (extra / "doc_004.png").write_bytes((extra / "doc_001.png").read_bytes())
    assert any("does not contain" in issue for issue in quality.check_pdf_images(source, extra, 72))


def test_a_dropped_text_layer_is_reported(tmp_path):
    """The rasterisation shortcut: same pages, same pictures, no text left."""
    import img2pdf

    source = _source_pdf(tmp_path / "text.pdf", 2)
    images = quality.render_pdf_pages(source, 72)
    paths = []
    for number, image in sorted(images.items()):
        target = tmp_path / ("page-%d.png" % number)
        image.save(target, "PNG")
        paths.append(str(target))
    out = tmp_path / "out"
    out.mkdir()
    (out / "text_压缩.pdf").write_bytes(img2pdf.convert(paths))
    issues = quality.check_pdf_compress(source, out)
    assert any("text layer" in issue for issue in issues), issues


def test_lossless_image_work_must_not_change_pixels(tmp_path):
    from PIL import Image

    source = tmp_path / "src"
    source.mkdir()
    image = Image.new("RGBA", (40, 30), (10, 120, 200, 128))
    image.save(source / "photo.png")
    good = tmp_path / "good"
    good.mkdir()
    image.save(good / "photo.png")
    assert quality.check_image_set([source / "photo.png"], good) == []

    bad = tmp_path / "bad"
    bad.mkdir()
    changed = image.copy()
    changed.putpixel((0, 0), (255, 255, 255, 255))
    changed.save(bad / "photo.png")
    issues = quality.check_image_set([source / "photo.png"], bad)
    assert any("not identical" in issue for issue in issues), issues


def test_dropping_a_png_text_chunk_is_reported(tmp_path):
    """The corpus writes a caption into its RGBA sample; losing it is a regression."""
    from PIL import Image, PngImagePlugin

    image = Image.new("RGBA", (20, 10), (1, 2, 3, 128))
    info = PngImagePlugin.PngInfo()
    info.add_text("Description", "keep me")
    source = tmp_path / "source.png"
    image.save(source, pnginfo=info)
    kept = tmp_path / "kept.png"
    image.save(kept, pnginfo=info)
    assert quality.check_image_pairs([(source, kept)]) == []
    stripped = tmp_path / "stripped.png"
    image.save(stripped)
    issues = quality.check_image_pairs([(source, stripped)])
    assert any("text metadata" in issue for issue in issues), issues


def _page_pdf(path: Path, extra_drawing: bool = False) -> Path:
    from reportlab.pdfgen import canvas

    canvas_obj = canvas.Canvas(str(path), invariant=1)
    canvas_obj.setFont("Helvetica", 12)
    canvas_obj.drawString(60, 760, "The same text on every page")
    if extra_drawing:
        canvas_obj.rect(100, 400, 200, 100, fill=1)
    canvas_obj.showPage()
    canvas_obj.save()
    return path


def test_a_render_change_under_compression_is_reported(tmp_path):
    """Same text, different picture: the page check, not only the text check."""
    source = _page_pdf(tmp_path / "source.pdf")
    same = _page_pdf(tmp_path / "same.pdf")
    same_dir = tmp_path / "same"
    same_dir.mkdir()
    (same_dir / "source_压缩.pdf").write_bytes(same.read_bytes())
    assert quality.check_pdf_compress(source, same_dir) == []

    changed = _page_pdf(tmp_path / "changed.pdf", extra_drawing=True)
    changed_dir = tmp_path / "changed"
    changed_dir.mkdir()
    (changed_dir / "source_压缩.pdf").write_bytes(changed.read_bytes())
    issues = quality.check_pdf_compress(source, changed_dir)
    assert any("rendered at 72 dpi" in issue for issue in issues), issues


def test_holdout_cases_are_named_for_their_own_corpus():
    """A "text120" case that runs on a 37-page document is a wrong label."""
    holdout = corpus.SUITES["holdout"]
    representative = corpus.SUITES["representative"]
    names = {case["name"] for case in bench.CASES["holdout"]}
    assert "pdf-images.text%d" % holdout["text_pages"] in names
    assert "pdf-images.scan%d" % holdout["scan_pages"] in names
    assert "pdf-images.text%d" % representative["text_pages"] not in names


def test_a_timed_out_command_is_a_failure(monkeypatch):
    """run_command reports rc=None when it gives up; that must not read as a pass."""
    def fake_run_command(argv, timeout):
        return {"seconds": timeout, "timed_out": True, "rc": None, "stderr": "",
                "output_bytes": 0, "outputs": 0}

    monkeypatch.setattr(bench, "run_command", fake_run_command)
    record = bench.run_case({"name": "engine.probe", "kind": "command", "argv": ["x"]},
                            Path("/nonexistent"), Path("/nonexistent"), 1, 5, False)
    assert record["status"] == "FAIL"
    assert record["timing_adopted"] is False
    assert any("timed out" in issue for issue in record["quality"]["issues"]), record


def test_a_wrong_output_fails_the_case_and_its_timing_is_not_adopted(monkeypatch, tmp_path):
    """The chain "quality finds a problem -> case FAIL -> timing not adopted"."""
    def fake_run_engine(inputs, out_dir, operation, config, timeout):
        return {"seconds": 1.0, "timed_out": False, "rc": 0, "stderr": "",
                "output_bytes": 0, "outputs": 0, "report": {}}

    monkeypatch.setattr(bench, "run_engine", fake_run_engine)
    monkeypatch.setattr(bench, "quality_issues", lambda *args, **kwargs: ["page 1: differs"])
    case = {"name": "pdf-images.text", "operation": "pdf_images", "input": "text.pdf",
            "config": {}, "quality": {}}
    record = bench.run_case(case, tmp_path, tmp_path, 1, 5, False)
    assert record["status"] == "FAIL"
    assert record["timing_adopted"] is False
    assert record["quality"]["issues"]


def test_summarise_reports_median_and_spread_and_ignores_timeouts():
    runs = [{"seconds": 2.0}, {"seconds": 2.2}, {"seconds": 2.4}]
    summary = bench.summarise(runs)
    assert summary["median_seconds"] == 2.2
    assert summary["min_seconds"] == 2.0 and summary["max_seconds"] == 2.4
    assert summary["spread"] == 0.1818
    assert bench.summarise([{"seconds": 9.0, "timed_out": True}])["median_seconds"] is None


# --------------------------------------------------------------------------
# End to end on the smoke corpus
# --------------------------------------------------------------------------
def test_the_smoke_suite_runs_and_reports(tmp_path):
    report_path = tmp_path / "bench.json"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench.py"), "--suite", "smoke",
         "--reps", "1", "--only", "pdf-images.text", "--json", str(report_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["environment"]["CHAMO_PAGE_CACHE"] == "0"
    case = report["cases"][0]
    assert case["name"].startswith("pdf-images.text") and case["status"] == "PASS"
    assert case["timing_adopted"] is True
    assert case["median_seconds"] and case["median_seconds"] > 0
    assert case["quality"]["issues"] == []
    assert report["corpus"]["manifest_sha256"]


def test_the_office_fixtures_really_render(tmp_path):
    """The corpus's .pptx/.docx are real documents, not just bytes.

    Every office benchmark case would silently measure a failed conversion if a
    fixture were malformed, so the smoke suite converts them and requires the
    rendered PDF to carry their words. The .xlsx case is not here: the Linux CI
    image installs LibreOffice Writer and Impress but not Calc, and that is a CI
    environment gap to fix on its own, not a reason for this suite to fail on
    Linux while passing on Windows and macOS.
    """
    report_path = tmp_path / "office.json"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench.py"), "--suite", "smoke",
         "--reps", "1", "--only", "office-pdf", "--json", str(report_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert {case["name"] for case in report["cases"]} == {
        "office-pdf.deck", "office-pdf.memo"}
    for case in report["cases"]:
        assert case["status"] == "PASS", (case["name"], case["quality"]["issues"])
