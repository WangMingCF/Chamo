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


def test_losing_pages_under_compression_is_reported_not_crashed(tmp_path):
    """The check must report a truncated result, not raise on the shorter file."""
    from pypdf import PdfReader, PdfWriter

    source = _source_pdf(tmp_path / "source.pdf", 6)
    truncated = tmp_path / "truncated.pdf"
    # Cut it down to two pages: the sample range is computed from the source, so
    # the shorter result is exactly the case that used to raise IndexError.
    cut = PdfWriter()
    for index in range(2):
        cut.add_page(PdfReader(str(source)).pages[index])
    with open(truncated, "wb") as handle:
        cut.write(handle)

    out = tmp_path / "out"
    out.mkdir()
    (out / "source_压缩.pdf").write_bytes(truncated.read_bytes())
    issues = quality.check_pdf_compress(source, out)
    assert any("page count changed 6 -> 2" in issue for issue in issues), issues


def _split(source: Path, out_dir: Path, plan) -> None:
    """Write parts[ (page range, file name) ] with pypdf, in the order given."""
    from pypdf import PdfReader, PdfWriter

    out_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(source))
    for name, pages in plan:
        writer = PdfWriter()
        for index in pages:
            writer.add_page(reader.pages[index])
        with open(out_dir / name, "wb") as handle:
            writer.write(handle)


def test_a_correct_split_passes_and_a_broken_one_fails(tmp_path):
    source = _source_pdf(tmp_path / "source.pdf", 4)
    good = tmp_path / "good"
    _split(source, good, [("part_1.pdf", [0, 1]), ("part_2.pdf", [2, 3])])
    assert quality.check_pdf_split(source, good) == []

    # a part is missing: the union is no longer the source
    missing = tmp_path / "missing"
    _split(source, missing, [("part_1.pdf", [0, 1])])
    issues = quality.check_pdf_split(source, missing)
    assert any("split parts hold 2 page(s)" in issue for issue in issues), issues

    # the parts exist but are swapped
    swapped = tmp_path / "swapped"
    _split(source, swapped, [("part_1.pdf", [2, 3]), ("part_2.pdf", [0, 1])])
    issues = quality.check_pdf_split(source, swapped)
    assert any("text changed after splitting" in issue for issue in issues), issues


def test_more_than_nine_parts_are_ordered_by_number_not_by_string(tmp_path):
    """part_10 sorts before part_2 as a string, which would fail a correct split."""
    source = _source_pdf(tmp_path / "source.pdf", 12)
    out = tmp_path / "many"
    _split(source, out, [("part_%d.pdf" % (n + 1), [n]) for n in range(12)])
    assert quality.check_pdf_split(source, out) == []


def test_parts_are_ordered_by_their_own_number_when_the_stem_has_digits(tmp_path):
    """A source named report_2024.pdf puts 2024 in front of every part name.

    Sorting by the first number would tie on 2024 for all parts, fall back to
    string order and put part_10 before part_2 - failing a correct split.
    """
    source = _source_pdf(tmp_path / "report_2024.pdf", 12)
    out = tmp_path / "report_2024"
    _split(source, out, [("report_2024_%02d.pdf" % (n + 1), [n]) for n in range(12)])
    assert quality.check_pdf_split(source, out) == []


def test_the_strict_export_must_be_the_source_and_nothing_else(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.pdf").write_bytes(b"document a")
    (source / "b.pdf").write_bytes(b"document b")
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.pdf").write_bytes(b"document a")
    (out / "b.pdf").write_bytes(b"document b")
    assert quality.check_rag([source / "a.pdf", source / "b.pdf"], out) == []

    (out / "b.pdf").write_bytes(b"document B")          # changed bytes
    issues = quality.check_rag([source / "a.pdf", source / "b.pdf"], out)
    assert any("b.pdf" in issue and "differ" in issue for issue in issues), issues

    (out / "b.pdf").unlink()                            # missing file
    issues = quality.check_rag([source / "a.pdf", source / "b.pdf"], out)
    assert any("not in the strict export" in issue for issue in issues), issues

    (out / "stray.txt").write_bytes(b"not asked for")   # extra file
    issues = quality.check_rag([source / "a.pdf"], out)
    assert any("does not have" in issue for issue in issues), issues


def test_format_conversion_must_keep_the_picture_and_lossy_work_is_measured(tmp_path):
    from PIL import Image

    source = tmp_path / "photo.png"
    image = Image.new("RGB", (32, 24), (10, 120, 200))
    image.save(source)

    other = Image.new("RGB", (32, 24), (200, 10, 10))

    # The contract comes from the caller (the engine knows if the run was
    # lossless), never from the suffix: WebP exists in both forms.
    converted = tmp_path / "converted.jpg"
    image.save(converted, "JPEG", quality=95)
    assert quality.check_image_pairs([(source, converted)], mode="convert",
                                     lossy_target=True) == []

    # A conversion declared lossless must stay pixel-identical, JPEG or not.
    issues = quality.check_image_pairs([(source, converted)], mode="convert")
    assert any("changed the picture" in issue for issue in issues), issues

    same_png = tmp_path / "same.png"
    image.save(same_png)
    assert quality.check_image_pairs([(source, same_png)], mode="convert") == []
    changed_png = tmp_path / "changed.png"
    other.save(changed_png)
    issues = quality.check_image_pairs([(source, changed_png)], mode="convert")
    assert any("changed the picture" in issue for issue in issues), issues

    changed = tmp_path / "changed.jpg"
    other.save(changed, "JPEG", quality=95)
    issues = quality.check_image_pairs([(source, changed)], mode="convert",
                                       lossy_target=True)
    assert any("changed the picture" in issue for issue in issues), issues

    # lossy is measured, not required to be identical: a mild JPEG passes,
    # a different picture does not.
    mild = tmp_path / "mild.jpg"
    image.save(mild, "JPEG", quality=60)
    assert quality.check_image_pairs([(source, mild)], mode="lossy") == []
    issues = quality.check_image_pairs([(source, changed)], mode="lossy")
    assert any("differs too much" in issue for issue in issues), issues


def test_a_chroma_only_change_is_reported(tmp_path):
    """Luma weights hide a colour shift; the gate must compare channels.

    Blue is weighted 0.114 in luma, so an 8-level blue shift moves the grey
    difference by 0.91, under the 1.0 mean threshold: before the per-channel fix a
    picture whose colour had changed passed the "the picture did not change" gate.
    """
    from PIL import Image

    left = Image.new("RGB", (32, 32), (10, 20, 30))
    right = Image.new("RGB", (32, 32), (10, 20, 38))
    issues = quality.compare_pil(left, right, "page 1")
    assert any("picture differs" in issue for issue in issues), issues


def test_palette_images_are_compared_by_colour_not_by_index(tmp_path):
    """Same indices, different palettes: identical bytes, completely different picture."""
    from PIL import Image

    def paletted(colour):
        image = Image.new("P", (16, 16), 0)
        image.putpalette(list(colour) + [0] * 765)
        return image

    source = tmp_path / "source.png"
    other = tmp_path / "other.png"
    paletted((200, 10, 10)).save(source)
    paletted((10, 200, 10)).save(other)
    issues = quality.check_image_pairs([(source, other)])
    assert any("not identical" in issue for issue in issues), issues


def test_a_third_name_collision_is_still_found(tmp_path):
    """The engine numbers collisions " (2)", " (3)" ...; only " (2)" was matched."""
    from PIL import Image

    source = tmp_path / "photo.png"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(source)
    out = tmp_path / "out"
    out.mkdir()
    for name in ("photo.png", "photo (2).png", "photo (3).png"):
        Image.new("RGB", (8, 8), (1, 2, 3)).save(out / name)
    issues = quality.check_image_set([source], out)
    assert any("expected one output, found 3" in issue for issue in issues), issues


def test_the_same_name_twice_in_the_export_is_reported(tmp_path):
    """Two subfolders can hold a.pdf; the hash gate cannot know which one counts."""
    out = tmp_path / "out"
    (out / "a").mkdir(parents=True)
    (out / "b").mkdir()
    (out / "a" / "a.pdf").write_bytes(b"one")
    (out / "b" / "a.pdf").write_bytes(b"two")
    source = tmp_path / "a.pdf"
    source.write_bytes(b"one")
    issues = quality.check_rag([source], out)
    assert any("more than once" in issue for issue in issues), issues
