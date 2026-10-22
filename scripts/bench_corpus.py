#!/usr/bin/env python3
"""Deterministic synthetic corpora for Chamo's benchmarks.

Why a generator instead of committed samples
--------------------------------------------
A 120-page scan PDF is over 10 MB; keeping one per suite would put megabytes of
binary in the repository and still leave the next feature without a comparison.
A generator also makes the stronger promise a benchmark needs: the input bytes
are identical on every run and every machine. Every file's SHA-256 goes into
manifest.json, and --check rebuilds nothing but compares what is on disk, so a
change that silently alters the corpus cannot pass for a performance change.

Two suites, and the difference between them matters
---------------------------------------------------
"representative" is what a change is tuned against. "holdout" uses different
page counts, line spacing, scan resolution and image geometry and is never used
for tuning: a change that only helps the representative samples shows up here as
a regression. "smoke" is a few seconds of work for CI, not a benchmark.

Determinism
-----------
Every writer is pinned: reportlab runs with invariant=1, OOXML packages are
re-zipped with fixed entry timestamps and fixed core properties, and no path,
host name or wall-clock value reaches a file. Do not add a writer that embeds
the current time without normalising the package with _normalize_zip.

Byte identity is designed for, not assumed: entry times, document properties, the
"version made by" field and compression are all removed from the answer (see
_normalize_zip), so a Windows runner and a Linux runner produce the same archive
bytes for the same content. Two things are still outside this module's control
and are recorded here rather than hidden: JPEG bytes come from the pinned
Pillow/libjpeg build, and the PNG encoder's output may move with Pillow. The
manifest makes such a change visible rather than silent - a check fails and names
the file - but a dependency bump is expected to require regenerating the manifest,
and it must not be mistaken for a change in what Chamo does.

Usage
-----
    python scripts/bench_corpus.py --dest build/bench-corpus/representative
    python scripts/bench_corpus.py --dest build/bench-corpus/holdout --suite holdout
    python scripts/bench_corpus.py --dest build/bench-corpus/representative --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1

# Fixed timestamp for every zip entry: the OOXML writers (python-pptx, openpyxl)
# stamp entries with the current time, which would make the corpus differ on
# every build and turn "the corpus is the same" into a claim nobody can check.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

SUITES = {
    "smoke": {
        "text_pages": 6,
        "text_lines": 12,
        "scan_pages": 4,
        "scan_dpi": 96,
        "mixed_pages": 4,
        "images": 3,
        "image_size": (320, 240),
        "image_dpi": 120,
        "slides": 2,
        "rows": 40,
        "paragraphs": 6,
    },
    "representative": {
        "text_pages": 120,
        "text_lines": 55,
        "scan_pages": 24,
        "scan_dpi": 150,
        "mixed_pages": 20,
        "images": 12,
        "image_size": (1600, 1200),
        "image_dpi": 200,
        "slides": 6,
        "rows": 400,
        "paragraphs": 60,
    },
    "holdout": {
        # Deliberately different numbers: fewer, denser text pages; more, larger
        # scans; square images instead of 4:3. Tuning against "representative"
        # must not accidentally fit only those shapes.
        "text_pages": 37,
        "text_lines": 62,
        "scan_pages": 33,
        "scan_dpi": 200,
        "mixed_pages": 13,
        "images": 7,
        "image_size": (1024, 1024),
        "image_dpi": 300,
        "slides": 9,
        "rows": 250,
        "paragraphs": 24,
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Normalisation applied to every OOXML package this module writes
# --------------------------------------------------------------------------
# ISO-8601 timestamps inside docProps/*.xml. openpyxl and python-pptx refresh
# these at save time even when the caller pinned the properties, which makes two
# builds of the same workbook differ. Only the property parts are rewritten; a
# timestamp that belongs to the document's own content is left alone.
_PACKAGE_TIMESTAMP = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
_PACKAGE_TIMESTAMP_REPLACEMENT = b"2020-01-01T00:00:00Z"


def _normalize_zip(path: Path) -> None:
    """Rewrite an OOXML package into a reproducible form.

    Writers can produce the same document and different bytes in four ways, and all
    four are removed here: the entry time (part of the archive), the document
    properties (the save time), the "version made by" field (CPython writes the
    host platform into it, so Windows and POSIX disagree), and DEFLATE output (its
    bytes depend on the zlib build, which the dependency lock does not pin).

    Entries are re-written STORED, not deflated: a corpus is generated at runtime,
    so its size does not matter, and no compression means no zlib version in the
    answer. Entry order is preserved and the original is replaced atomically
    (write beside it, then rename).
    """
    with zipfile.ZipFile(path) as source:
        entries = [(info.filename, source.read(info.filename)) for info in source.infolist()]
    temporary = path.with_name(path.name + ".normalizing")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as target:
        for name, data in entries:
            if name.startswith("docProps/") and name.endswith(".xml"):
                data = _PACKAGE_TIMESTAMP.sub(_PACKAGE_TIMESTAMP_REPLACEMENT, data)
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            # CPython defaults this to the host platform (0 on Windows, 3
            # elsewhere), and it is written into the archive. Pinning it is what
            # lets the same corpus have the same bytes on every runner.
            info.create_system = 3
            info.external_attr = 0o644 << 16
            target.writestr(info, data)
    temporary.replace(path)


# --------------------------------------------------------------------------
# PDF fixtures (reportlab; invariant=1 removes the creation date)
# --------------------------------------------------------------------------
def _canvas(path: Path):
    """A canvas with no creation date (invariant=1) and no Flate compression.

    pageCompression=0 costs bytes and buys a deterministic file: DEFLATE output
    depends on the zlib build, which the dependency lock does not pin, and a
    corpus whose bytes change with the host's zlib cannot be compared across
    machines. The generators run at build time, so the size is not a concern.
    """
    from reportlab.pdfgen import canvas as reportlab_canvas

    return reportlab_canvas.Canvas(str(path), invariant=1, pageCompression=0)


def text_pdf(path: Path, pages: int, lines: int = 55) -> Path:
    """A born-digital text PDF: text objects only, no images at all."""
    canvas_obj = _canvas(path)
    for index in range(pages):
        canvas_obj.setFont("Helvetica", 10)
        for row in range(lines):
            canvas_obj.drawString(
                60, 800 - row * 12.5,
                "Page %d line %d: the quick brown fox jumps over the lazy dog 0123456789"
                % (index + 1, row))
        canvas_obj.showPage()
    canvas_obj.save()
    return path


def _scan_page_image(size, variant: int):
    """One deterministic raster page: geometry and a flat, noise-free background.

    No font is used on purpose. PIL's default font differs between Pillow builds
    and the corpus has to be byte-identical across the three CI platforms.
    """
    from PIL import Image, ImageDraw

    width, height = size
    image = Image.new("RGB", (width, height), (250, 250, 248))
    draw = ImageDraw.Draw(image)
    for band in range(24):
        top = int(height * band / 24)
        shade = 30 + (band * 7 + variant * 13) % 200
        draw.line([(int(width * 0.06), top), (int(width * 0.94), top)], fill=(shade,) * 3,
                  width=2)
    draw.rectangle([int(width * 0.1), int(height * 0.15),
                    int(width * 0.9), int(height * 0.55)], outline=(20, 20, 20), width=3)
    for step in range(6):
        y = int(height * (0.65 + step * 0.04))
        draw.line([(int(width * 0.12), y), (int(width * 0.88 - step * width * 0.05), y)],
                  fill=(60, 60, 90), width=3)
    image.putpixel((0, 0), ((variant * 37) % 256, 0, 0))   # per-page identity
    return image


def scan_pdf(path: Path, pages: int, source_dpi: int = 150) -> Path:
    """An image-dominant PDF: exactly one raster image per page and no text.

    Several pages share an image XObject, so a 33-page fixture builds in about a
    second instead of minutes; page identity is not needed because the renderer
    names outputs by absolute page number.
    """
    import io as _io

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader

    width, height = A4
    pixel_w = int(width / 72.0 * source_dpi)
    pixel_h = int(height / 72.0 * source_dpi)
    canvas_obj = _canvas(path)
    readers = []
    for variant in range(min(8, pages)):
        image = _scan_page_image((pixel_w, pixel_h), variant)
        buffer = _io.BytesIO()
        image.save(buffer, "JPEG", quality=80)
        buffer.seek(0)
        image.close()
        readers.append(ImageReader(buffer))
    for index in range(pages):
        canvas_obj.drawImage(readers[index % len(readers)], 0, 0, width=width, height=height)
        canvas_obj.showPage()
    canvas_obj.save()
    return path


def mixed_pdf(path: Path, pages: int) -> Path:
    """Alternating text and raster pages.

    The content classifier must not call this either pure text or pure image, and
    rendering it must not change a page.
    """
    import io as _io

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader

    width, height = A4
    image = _scan_page_image((int(width), int(height)), 3)
    buffer = _io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    buffer.seek(0)
    image.close()
    reader = ImageReader(buffer)
    canvas_obj = _canvas(path)
    for index in range(pages):
        if index % 2 == 0:
            canvas_obj.setFont("Helvetica", 11)
            for row in range(40):
                canvas_obj.drawString(60, 780 - row * 16,
                                      "Mixed page %d row %d" % (index + 1, row))
        else:
            canvas_obj.drawImage(reader, 0, 0, width=width, height=height)
        canvas_obj.showPage()
    canvas_obj.save()
    return path


# --------------------------------------------------------------------------
# Office fixtures
# --------------------------------------------------------------------------
def _fixed_properties(package) -> None:
    """Pin the document properties that would otherwise carry the current time.

    python-pptx and openpyxl expose this differently (core_properties vs
    properties, author vs creator), and the openpyxl one is what a .xlsx package
    would otherwise stamp with "now" on every build.
    """
    import datetime

    moment = datetime.datetime(2020, 1, 1, 0, 0, 0)
    core = getattr(package, "core_properties", None)
    if core is not None:
        core.created = moment
        core.modified = moment
        core.last_modified_by = "Chamo synthetic corpus"
        core.author = "Chamo synthetic corpus"
        core.revision = 1
        return
    properties = package.properties
    properties.created = moment
    properties.modified = moment
    properties.creator = "Chamo synthetic corpus"
    properties.lastModifiedBy = "Chamo synthetic corpus"
    properties.revision = "1"


def pptx_deck(path: Path, slides: int) -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    for index in range(slides):
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "Synthetic slide %d" % (index + 1)
        box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(7), Inches(1))
        box.text = "Body text for slide %d: 2 + 3 = 5" % (index + 1)
        table = slide.shapes.add_table(3, 3, Inches(1), Inches(3.4), Inches(7), Inches(1.4)).table
        for row in range(3):
            for column in range(3):
                table.cell(row, column).text = "r%dc%d" % (row + 1, column + 1)
        slide.notes_slide.notes_text_frame.text = "Notes stay in the original deck."
    _fixed_properties(presentation)
    presentation.save(path)
    _normalize_zip(path)
    return path


def xlsx_sheet(path: Path, rows: int) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Numbers"
    sheet.append(["row", "value", "label"])
    for row in range(2, rows + 2):
        sheet.append([row - 1, (row * 17) % 1000, "label-%d" % (row - 1)])
    second = workbook.create_sheet("Summary")
    second["A1"] = "total"
    second["B1"] = "=SUM(Numbers!B2:B%d)" % (rows + 1)
    _fixed_properties(workbook)
    workbook.save(path)
    _normalize_zip(path)
    return path


_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>')

_DOCX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>')

_DOCX_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body>%s<w:sectPr/></w:body></w:document>')


def docx_document(path: Path, paragraphs: int) -> Path:
    """A minimal but valid .docx.

    Written here because the repository does not - and should not - add
    python-docx as a runtime dependency just to produce a fixture.
    """
    body = []
    for index in range(paragraphs):
        # No style references: this package has no word/styles.xml, and a style id
        # that resolves to nothing is a LibreOffice warning we do not need.
        body.append('<w:p><w:r><w:t>Paragraph %d: the quick brown fox 0123456789</w:t></w:r></w:p>'
                    % (index + 1))
    document = _DOCX_DOCUMENT % "".join(body)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, data in (("[Content_Types].xml", _DOCX_CONTENT_TYPES),
                           ("_rels/.rels", _DOCX_RELS),
                           ("word/document.xml", document)):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3      # see _normalize_zip
            info.external_attr = 0o644 << 16
            archive.writestr(info, data.encode("utf-8"))
    return path


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
def image_set(directory: Path, count: int, size=(1600, 1200), dpi: int = 200):
    """A mixed image batch: opaque JPEG, RGBA PNG with metadata, grayscale PNG.

    The RGBA and metadata cases exist so a "lossless" claim is checked against
    something that can actually be lost (alpha, ancillary chunks), not only
    against opaque RGB.
    """
    from PIL import Image, ImageDraw, PngImagePlugin

    directory.mkdir(parents=True, exist_ok=True)
    written = []
    width, height = size
    for index in range(count):
        image = Image.new("RGB", (width, height), (18 + index * 7, 90, 140))
        draw = ImageDraw.Draw(image)
        draw.ellipse([width // 8, height // 8, width * 7 // 8, height * 7 // 8],
                     fill=((index * 53) % 256, 200 - (index * 11) % 160, 60))
        for step in range(12):
            draw.line([(0, step * height // 12), (width, step * height // 12 + height // 24)],
                      fill=(240, 240, 240), width=3)
        jpeg = directory / ("photo-%02d.jpg" % (index + 1))
        image.save(jpeg, "JPEG", quality=88, dpi=(dpi, dpi))
        written.append(jpeg)
        if index < 2:
            rgba = Image.new("RGBA", (width // 2, height // 2), (40, 100, 200, 150))
            ImageDraw.Draw(rgba).rectangle([10, 10, width // 2 - 10, height // 4],
                                           fill=(255, 0, 0, 255))
            info = PngImagePlugin.PngInfo()
            info.add_text("Description", "Synthetic RGBA sample %d" % (index + 1))
            png = directory / ("alpha-%02d.png" % (index + 1))
            rgba.save(png, pnginfo=info, compress_level=1, dpi=(dpi, dpi))
            written.append(png)
        gray = Image.new("L", (width // 2, height // 2), 128 + index)
        draw_gray = ImageDraw.Draw(gray)
        draw_gray.rectangle([0, 0, width // 4, height // 4], fill=20 + index)
        gray_path = directory / ("gray-%02d.png" % (index + 1))
        gray.save(gray_path, compress_level=1, dpi=(dpi, dpi))
        written.append(gray_path)
    return written


# --------------------------------------------------------------------------
# Build / check
# --------------------------------------------------------------------------
def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def build(dest, suite: str = "representative") -> dict:
    """Write the corpus into dest and return its manifest."""
    if suite not in SUITES:
        raise SystemExit("unknown suite %r (choose from %s)" % (suite, ", ".join(sorted(SUITES))))
    spec = SUITES[suite]
    dest = Path(dest)
    if dest.exists() and not dest.is_dir():
        raise SystemExit("corpus destination is a file, not a directory: %s" % dest)
    if dest.exists() and dest.is_dir():
        # A build interrupted between "write beside it" and "rename" leaves a
        # .normalizing file behind. It is not part of the corpus, so clean it up
        # instead of refusing to rebuild in a directory that looks used.
        for stale in dest.rglob("*.normalizing"):
            stale.unlink(missing_ok=True)
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit("refusing to write into a non-empty directory: %s" % dest)
    dest.mkdir(parents=True, exist_ok=True)
    files = [
        text_pdf(dest / "text.pdf", spec["text_pages"], lines=spec["text_lines"]),
        scan_pdf(dest / "scan.pdf", spec["scan_pages"], source_dpi=spec["scan_dpi"]),
        mixed_pdf(dest / "mixed.pdf", spec["mixed_pages"]),
        pptx_deck(dest / "deck.pptx", spec["slides"]),
        xlsx_sheet(dest / "sheet.xlsx", spec["rows"]),
        docx_document(dest / "memo.docx", spec["paragraphs"]),
    ]
    files += image_set(dest / "images", spec["images"], size=tuple(spec["image_size"]),
                       dpi=spec["image_dpi"])
    entries = [{"path": _relative(path, dest), "bytes": path.stat().st_size,
                "sha256": sha256_file(path)} for path in sorted(files)]
    manifest = {"schema": MANIFEST_SCHEMA, "suite": suite,
                "spec": {key: (list(value) if isinstance(value, tuple) else value)
                         for key, value in spec.items()},
                "files": entries,
                "total_bytes": sum(entry["bytes"] for entry in entries)}
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def check(dest) -> list:
    """Compare a built corpus against its manifest. An empty list means OK."""
    dest = Path(dest)
    manifest_path = dest / MANIFEST_NAME
    if not manifest_path.is_file():
        return ["no manifest at %s" % manifest_path]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return ["manifest at %s cannot be read: %s" % (manifest_path, error)]
    if not isinstance(manifest, dict) or "files" not in manifest:
        return ["manifest at %s has no file list" % manifest_path]
    issues = []
    listed = {entry["path"] for entry in manifest["files"]}
    for entry in manifest["files"]:
        path = dest / entry["path"]
        if not path.is_file():
            issues.append("%s: missing" % entry["path"])
        elif sha256_file(path) != entry["sha256"]:
            issues.append("%s: content changed (sha256 differs from the manifest)" % entry["path"])
    for path in sorted(dest.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            relative = _relative(path, dest)
            if relative not in listed:
                issues.append("%s: not listed in the manifest" % relative)
    return issues


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate the deterministic benchmark corpora")
    parser.add_argument("--dest", required=True, help="empty directory to write the corpus into")
    parser.add_argument("--suite", default=None, choices=sorted(SUITES),
                        help="which corpus to build (default representative); ignored with --check")
    parser.add_argument("--check", action="store_true",
                        help="verify an existing corpus against its manifest instead of building")
    args = parser.parse_args(argv)
    if args.check and args.suite:
        parser.error("--check verifies an existing corpus; --suite only applies when building")
    if args.check:
        issues = check(args.dest)
        for issue in issues:
            print("FAIL %s" % issue)
        print("corpus %s: %s" % (args.dest, "OK" if not issues else "%d issue(s)" % len(issues)))
        return 0 if not issues else 1
    manifest = build(args.dest, args.suite or "representative")
    print("corpus %s written: %d files, %.1f MB"
          % (args.dest, len(manifest["files"]), manifest["total_bytes"] / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
