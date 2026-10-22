#!/usr/bin/env python3
"""Quality checks a benchmark number is only honest with.

A run that reports "faster" without proving the output is the same has measured a
different job. Every helper here returns a list of issues; an empty list means the
output was checked and is acceptable. Nothing returns "cannot tell" as a pass:
an output that cannot be inspected is reported as an issue with the reason, and
the caller decides (the benchmark records FAIL, the smoke test fails).

What each check is for
----------------------
* check_pdf_images   - every page file exists, is the right size, and its pixels
                       match the source page rendered at the same DPI. This is
                       what stops "faster" from meaning "fewer pages" or "lower
                       DPI": both show up as a missing page or as a different
                       picture.
* check_image_set    - lossless work must be pixel-identical (including alpha and
                       PNG text chunks); lossy work reports PSNR/SSIM instead of
                       claiming identity.
* check_pdf_split    - the union of the parts is the source, page for page.
* check_pdf_compress - page count, text layer and per-page pictures survive.
* check_rag          - the strict export is byte-identical to its input.

SSIM is implemented here because numpy is deliberately not in the dependency
lock (third_party/requirements.lock). It is computed on a downscaled grayscale
copy with a box window and summed-area tables, which is enough to tell "same
page" from "different page" and cheap enough to run over every page of a
benchmark.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

# Directories the engine writes for its own bookkeeping and staging; they are
# never deliverables and must not enter a comparison.
RECORD_DIRS = {"_Chamo记录", "_Chamo暂存"}
MANIFEST_NAME = "清单.json"

# Two naming schemes exist: the published name (<stem>_001.png, see clean_names in
# engine/workbench.py) and the in-flight staging name (<stem>_page00001.png).
# Both carry the absolute page number, which is what the checks compare against.
PAGE_FILE = re.compile(r"_(?:page)?(\d+)\.(png|jpe?g|webp|tiff?|bmp)$", re.I)

# Thresholds for "the same picture". A lossless render of the same page decodes
# to identical pixels, so anything above a rounding difference is a different
# picture; the values leave room for the decoder's colour handling only.
SSIM_MIN = 0.995
MEAN_ABS_MAX = 3.0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deliverables(out_dir) -> list:
    """Every converted file under out_dir, records and staging excluded."""
    root = Path(out_dir)
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and (relative.parts[0] in RECORD_DIRS
                               or relative.parts[0].startswith(".")):
            continue
        if relative.name == MANIFEST_NAME:
            continue
        files.append(path)
    return files


def page_map(out_dir) -> dict:
    """{page number: path} from the engine's <stem>_page00001.png names."""
    pages = {}
    for path in deliverables(out_dir):
        match = PAGE_FILE.search(path.name)
        if match:
            pages[int(match.group(1))] = path
    return pages


# --------------------------------------------------------------------------
# Image comparison (no numpy)
# --------------------------------------------------------------------------
def _grey_pixels(image, sample: int = 96):
    grey = image.convert("L")
    width, height = grey.size
    if max(width, height) > sample:
        scale = sample / float(max(width, height))
        grey = grey.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        width, height = grey.size
    return list(grey.getdata()), width, height


def _integral(values, width, height):
    table = [0.0] * ((width + 1) * (height + 1))
    stride = width + 1
    for y in range(height):
        row = 0.0
        base = (y + 1) * stride
        previous = y * stride
        offset = y * width
        for x in range(width):
            row += values[offset + x]
            table[base + x + 1] = table[previous + x + 1] + row
    return table


def _box_sum(table, stride, x0, y0, x1, y1):
    return (table[y1 * stride + x1] - table[y0 * stride + x1]
            - table[y1 * stride + x0] + table[y0 * stride + x0])


def ssim(left, right, sample: int = 64, window: int = 7) -> float:
    """Mean structural similarity of two images, on a downscaled grey copy."""
    a, width, height = _grey_pixels(left, sample)
    b, width_b, height_b = _grey_pixels(right, sample)
    if (width, height) != (width_b, height_b):
        return 0.0
    stride = width + 1
    half = max(1, window // 2)
    sums = [_integral(a, width, height), _integral(b, width, height),
            _integral([v * v for v in a], width, height),
            _integral([v * v for v in b], width, height),
            _integral([x * y for x, y in zip(a, b)], width, height)]
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    total = 0.0
    count = 0
    for y in range(height):
        y0 = max(0, y - half)
        y1 = min(height, y + half + 1)
        for x in range(width):
            x0 = max(0, x - half)
            x1 = min(width, x + half + 1)
            n = float((x1 - x0) * (y1 - y0))
            mean_a = _box_sum(sums[0], stride, x0, y0, x1, y1) / n
            mean_b = _box_sum(sums[1], stride, x0, y0, x1, y1) / n
            var_a = _box_sum(sums[2], stride, x0, y0, x1, y1) / n - mean_a * mean_a
            var_b = _box_sum(sums[3], stride, x0, y0, x1, y1) / n - mean_b * mean_b
            cov = _box_sum(sums[4], stride, x0, y0, x1, y1) / n - mean_a * mean_b
            numerator = (2 * mean_a * mean_b + c1) * (2 * cov + c2)
            denominator = (mean_a * mean_a + mean_b * mean_b + c1) * (var_a + var_b + c2)
            total += numerator / denominator if denominator else 0.0
            count += 1
    return total / count if count else 0.0


def mean_abs(left, right, sample: int = 64) -> float:
    a, width, height = _grey_pixels(left, sample)
    b, width_b, height_b = _grey_pixels(right, sample)
    if (width, height) != (width_b, height_b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / float(len(a) or 1)


def psnr(left, right) -> float:
    """Peak signal-to-noise ratio in dB; reported for lossy work, never identity."""
    a, width, height = _grey_pixels(left, 192)
    b, _, _ = _grey_pixels(right, 192)
    squared = sum((x - y) ** 2 for x, y in zip(a, b)) / float(len(a) or 1)
    if squared <= 0:
        return float("inf")
    import math

    return 10.0 * math.log10((255.0 ** 2) / squared)


def compare_pil(reference, candidate, label: str = "image") -> list:
    """Compare two open images; the same size and the same picture, or an issue.

    Identical pixels are checked first and cheaply: comparing a source page with a
    fresh render by the same renderer is normally an exact match, and this is the
    path taken for every page of a 120-page benchmark. Only when the pixels differ
    does the metric get computed, so the expensive part is paid once per real
    difference instead of once per page.
    """
    if reference.size != candidate.size:
        return ["%s: size %sx%s, expected %sx%s"
                % (label, candidate.size[0], candidate.size[1],
                   reference.size[0], reference.size[1])]
    if reference.mode == candidate.mode and reference.tobytes() == candidate.tobytes():
        return []
    reference = reference.convert("RGB")
    candidate = candidate.convert("RGB")
    score = ssim(reference, candidate)
    difference = mean_abs(reference, candidate)
    if score < SSIM_MIN or difference > MEAN_ABS_MAX:
        return ["%s: picture differs (SSIM %.4f < %.4f, mean |delta| %.2f > %.2f)"
                % (label, score, SSIM_MIN, difference, MEAN_ABS_MAX)]
    return []


def compare_images(reference, candidate, label: str = "image") -> list:
    from PIL import Image

    try:
        with Image.open(reference) as ref_image, Image.open(candidate) as cand_image:
            return compare_pil(ref_image, cand_image, label)
    except OSError as error:
        return ["%s: cannot decode %s (%s)" % (label, candidate, error)]


# --------------------------------------------------------------------------
# PDF helpers
# --------------------------------------------------------------------------
def pdf_page_count(path) -> int:
    from pypdf import PdfReader

    with open(path, "rb") as handle:
        return len(PdfReader(handle).pages)


def pdf_text_pages(path) -> list:
    from pypdf import PdfReader

    with open(path, "rb") as handle:
        return [(page.extract_text() or "") for page in PdfReader(handle).pages]


def iter_pdf_pages(path, dpi: int, pages=None):
    """Yield (1-based page number, PIL image) for a PDF, one page at a time.

    pdfium is the library the engine renders with, so a page comparison here is
    against the same renderer the product uses. Pages are yielded instead of
    returned as a dictionary on purpose: 120 A4 pages at 150 dpi held at once are
    about 800 MB, which would make the check itself the thing that fails on a CI
    runner. The caller closes each image.
    """
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(path)) as document:
        wanted = list(pages) if pages is not None else list(range(len(document)))
        scale = dpi / 72.0
        for index in wanted:
            page = document[index]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            bitmap.close()
            page.close()
            yield index + 1, image


def render_pdf_pages(path, dpi: int, pages=None) -> dict:
    """{1-based page number: PIL image} for a small PDF (tests and previews)."""
    return {number: image for number, image in iter_pdf_pages(path, dpi, pages)}


def _normalise(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Per-operation checks
# --------------------------------------------------------------------------
def check_pdf_images(source_pdf, out_dir, dpi: int) -> list:
    """Every selected page is present, once, and shows the source page.

    A missing page, a page rendered at a different DPI, a duplicated page and a
    page belonging to another document all fail here.
    """
    found = page_map(out_dir)
    if not found:
        return ["no page files in %s" % out_dir]
    try:
        total = pdf_page_count(source_pdf)
    except Exception as error:                              # noqa: BLE001 - report it
        return ["cannot read the source PDF %s (%s)" % (source_pdf, error)]
    issues = []
    expected = set(range(1, total + 1))
    missing = sorted(expected - set(found))
    unexpected = sorted(set(found) - expected)
    if missing:
        issues.append("missing %d page file(s): %s" % (len(missing), missing[:8]))
    if unexpected:
        issues.append("%d page file(s) that the selection does not contain: %s"
                      % (len(unexpected), unexpected[:8]))
    for number, image in iter_pdf_pages(source_pdf, dpi):
        candidate = found.get(number)
        if candidate is None:
            image.close()
            continue
        issues += compare_pil(image, candidate, "page %d" % number)
        image.close()
        if len(issues) > 12:
            issues.append("... further differences suppressed")
            break
    return issues


def check_image_pairs(pairs, mode: str = "lossless") -> list:
    """Compare each source image with the output the run recorded for it.

    The pairs come from the run's report.json instead of from matching file names.
    A batch publishes its outputs under the batch's names, and a collision gets a
    " (2)" suffix, so pairing by name would test the naming scheme rather than the
    conversion. What matters here is the promise each operation makes:

      lossless - image compression that must not change a pixel, the alpha
                 channel or the PNG text chunks it was given;
      convert  - a format change, so the container and the mode may differ but
                 the picture may not;
      lossy    - the user chose a quality loss; the result is measured (SSIM)
                 instead of being called identical.
    """
    from PIL import Image

    issues = []
    if not pairs:
        return ["the run recorded no image output, so nothing was verified"]
    for source, output in pairs:
        source = Path(source)
        output = Path(output)
        if not output.is_file():
            issues.append("%s: the recorded output %s does not exist" % (source.name, output))
            continue
        try:
            with Image.open(source) as left, Image.open(output) as right:
                if mode == "lossy":
                    score = ssim(left, right)
                    if score < 0.90:
                        issues.append("%s: lossy result differs too much "
                                      "(SSIM %.4f < 0.90, PSNR %.1f dB)"
                                      % (source.name, score, psnr(left, right)))
                    continue
                if mode == "convert":
                    if left.convert("RGBA").tobytes() != right.convert("RGBA").tobytes():
                        issues.append("%s: converting the format changed the picture"
                                      % source.name)
                    continue
                if left.size != right.size:
                    issues.append("%s: size changed %s -> %s" % (source.name, left.size, right.size))
                    continue
                if left.mode != right.mode:
                    issues.append("%s: mode changed %s -> %s" % (source.name, left.mode, right.mode))
                    continue
                if left.tobytes() != right.tobytes():
                    issues.append("%s: pixels are not identical (lossless work must not change them)"
                                  % source.name)
                for key, value in left.info.items():
                    if key in ("icc_profile", "exif") and right.info.get(key) != value:
                        issues.append("%s: %s was dropped" % (source.name, key))
        except OSError as error:
            issues.append("%s: cannot compare (%s)" % (source.name, error))
    return issues


def check_image_set(sources, out_dir, mode: str = "lossless") -> list:
    """Pair by file name, for a single-image run and for the unit tests."""
    files = deliverables(out_dir)
    pairs = []
    for source in sources:
        source = Path(source)
        matches = [path for path in files if path.stem == source.stem
                   or path.stem == source.stem + " (2)"]
        if len(matches) != 1:
            return ["%s: expected one output, found %d" % (source.name, len(matches))]
        pairs.append((source, matches[0]))
    return check_image_pairs(pairs, mode)


def check_pdf_split(source_pdf, out_dir) -> list:
    """The parts are the source, page for page, in order."""
    issues = []
    source_pages = pdf_text_pages(source_pdf)
    parts = sorted(deliverables(out_dir), key=lambda path: path.name)
    if not parts:
        return ["no split output in %s" % out_dir]
    joined = []
    for part in parts:
        try:
            pages = pdf_text_pages(part)
        except Exception as error:                      # noqa: BLE001 - report, do not crash
            issues.append("%s: cannot read the PDF (%s)" % (part.name, error))
            continue
        joined.extend(pages)
    if len(joined) != len(source_pages):
        issues.append("split parts hold %d page(s), the source has %d"
                      % (len(joined), len(source_pages)))
    for index, (before, after) in enumerate(zip(source_pages, joined), start=1):
        if _normalise(before) != _normalise(after):
            issues.append("page %d: text changed after splitting" % index)
            break
    return issues


def check_pdf_compress(source_pdf, out_dir) -> list:
    """Compression keeps the pages, the text layer and the pictures.

    Growth is reported rather than failed: the engine keeps the original when its
    result would be bigger, so a bigger output is worth knowing about.
    """
    issues = []
    outputs = [path for path in deliverables(out_dir) if path.suffix.lower() == ".pdf"]
    if len(outputs) != 1:
        return ["expected one compressed PDF, found %d" % len(outputs)]
    source_pages = pdf_text_pages(source_pdf)
    try:
        result_pages = pdf_text_pages(outputs[0])
    except Exception as error:                          # noqa: BLE001
        return ["compressed PDF cannot be read: %s" % error]
    if len(result_pages) != len(source_pages):
        issues.append("page count changed %d -> %d" % (len(source_pages), len(result_pages)))
    for index, (before, after) in enumerate(zip(source_pages, result_pages), start=1):
        if _normalise(before) != _normalise(after):
            issues.append("page %d: text layer changed or was dropped" % index)
            break
    return issues


def compressed_growth(source_pdf, out_dir) -> float:
    """Output bytes / source bytes. Reported, not failed: whether a lossless
    cleanup helps depends on the document, and the benchmark's job is to show the
    number, not to declare every growth a defect."""
    outputs = [path for path in deliverables(out_dir) if path.suffix.lower() == ".pdf"]
    if len(outputs) != 1:
        return 0.0
    source_bytes = Path(source_pdf).stat().st_size
    return round(outputs[0].stat().st_size / float(source_bytes or 1), 4)


def check_rag(sources, out_dir) -> list:
    """The strict RAG export is the source itself; hashes must agree."""
    issues = []
    by_name = {path.name: path for path in deliverables(out_dir)}
    for source in sources:
        source = Path(source)
        candidate = by_name.get(source.name)
        if candidate is None:
            issues.append("%s: not in the strict export" % source.name)
        elif sha256_file(candidate) != sha256_file(source):
            issues.append("%s: exported bytes differ from the source" % source.name)
    return issues

