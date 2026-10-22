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
def test_the_same_suite_builds_the_same_bytes(tmp_path):
    """A benchmark comparison is only meaningful if the input did not change."""
    first = corpus.build(tmp_path / "a", "smoke")
    second = corpus.build(tmp_path / "b", "smoke")
    assert [entry["sha256"] for entry in first["files"]] == \
           [entry["sha256"] for entry in second["files"]], (
               "the smoke corpus is not reproducible: %s"
               % [entry["path"] for entry, other in zip(first["files"], second["files"])
                  if entry["sha256"] != other["sha256"]])
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
def _pages_at(directory: Path, images, dpi: int, stem: str = "doc") -> Path:
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
    reference = _pages_at(tmp_path / "good", quality.render_pdf_pages(source, 72), 72)
    assert quality.check_pdf_images(source, reference, 72) == []

    # 1. one page missing: "faster" by doing less work.
    missing = _pages_at(tmp_path / "missing", quality.render_pdf_pages(source, 72), 72)
    (missing / "doc_002.png").unlink()
    assert any("missing 1 page" in issue for issue in quality.check_pdf_images(source, missing, 72))

    # 2. a page rendered at a lower DPI: the classic way to fake a speed-up.
    low_dpi = _pages_at(tmp_path / "low-dpi", quality.render_pdf_pages(source, 36), 36)
    issues = quality.check_pdf_images(source, low_dpi, 72)
    assert any("size" in issue for issue in issues), issues

    # 3. the right number of pages, but one belongs to another document.
    swapped = _pages_at(tmp_path / "swapped", quality.render_pdf_pages(source, 72), 72)
    other = _source_pdf(tmp_path / "other.pdf", 1)
    quality.render_pdf_pages(other, 72)[1].save(swapped / "doc_003.png", "PNG")
    issues = quality.check_pdf_images(source, swapped, 72)
    assert any("page 3" in issue for issue in issues), issues

    # 4. an extra page file that the selection never asked for.
    extra = _pages_at(tmp_path / "extra", quality.render_pdf_pages(source, 72), 72)
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
    assert case["name"] == "pdf-images.text" and case["status"] == "PASS"
    assert case["median_seconds"] and case["median_seconds"] > 0
    assert case["quality"]["issues"] == []
    assert report["corpus"]["manifest_sha256"]


def test_the_office_fixtures_really_render(tmp_path):
    """The corpus's .pptx/.docx/.xlsx are real documents, not just bytes.

    Every office benchmark case would silently measure a failed conversion if a
    fixture were malformed, so the smoke suite converts all three and requires the
    rendered PDF to carry their words. This runs where LibreOffice is installed,
    which is every engine CI job.
    """
    report_path = tmp_path / "office.json"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench.py"), "--suite", "smoke",
         "--reps", "1", "--only", "office-pdf", "--json", str(report_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert {case["name"] for case in report["cases"]} == {
        "office-pdf.deck", "office-pdf.memo", "office-pdf.sheet"}
    for case in report["cases"]:
        assert case["status"] == "PASS", (case["name"], case["quality"]["issues"])
