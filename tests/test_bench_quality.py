"""The quality checks have to go RED for the shortcuts that would fake a number.

"Faster" and "smaller" are only interesting if the output is still correct, so the
checks in scripts/bench_quality.py are the part of the benchmark that says no: a
missing page, a page rendered at a lower DPI, a page belonging to another
document, a dropped text layer, changed pixels in lossless work, a dropped PNG
text chunk, or a picture that changed under compression.

These drive the checks directly, on documents built in the test, so a regression
in a check is caught without an engine run.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


quality = _load("chamo_bench_quality", "scripts/bench_quality.py")


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
    assert any("missing 1 page" in issue
               for issue in quality.check_pdf_images(source, missing, 72))

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
    assert any("does not contain" in issue
               for issue in quality.check_pdf_images(source, extra, 72))


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
