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
* check_pdf_compress - page count, text layer and a bounded sample of rendered
                       pages survive.
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

# Thresholds for "the same picture".
#
# Identical pixels are the normal case (the same renderer rendering the same page),
# and that path is byte comparison. These numbers only matter when the pixels are
# not identical, and they are deliberately strict: a page rendered at half the DPI
# or a page belonging to another document must not slip through. A global average
# is not enough for that - a page that is 96% white with a few lines of text can
# differ everywhere the text is and still average under a grey level or two - so
# the worst 1/16x1/16 block is checked as well.
MEAN_ABS_MAX = 1.0        # full-resolution mean of |delta|, in grey levels
BLOCK_MEAN_MAX = 8.0      # worst block mean of |delta|, in grey levels
LOSSY_SSIM_MIN = 0.90     # the bar a user-selected quality loss has to clear
CONVERT_SSIM_MIN = 0.95   # a conversion into a lossy container must stay this close


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
        # Only the top-level manifest is bookkeeping; a same-named file in a
        # subdirectory is a deliverable and must stay visible to the checks.
        if relative.parts == (MANIFEST_NAME,):
            continue
        files.append(path)
    return files


def page_entries(out_dir) -> list:
    """[(page number, path)] from the engine's <stem>_00001.png names, in file order."""
    entries = []
    for path in deliverables(out_dir):
        match = PAGE_FILE.search(path.name)
        if match:
            entries.append((int(match.group(1)), path))
    return entries


# --------------------------------------------------------------------------
# Image comparison (no numpy)
# --------------------------------------------------------------------------
def _plane_pixels(image, sample: int = 96):
    """One downscaled 8-bit plane as a byte list, plus its size."""
    width, height = image.size
    if max(width, height) > sample:
        scale = sample / float(max(width, height))
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        width, height = image.size
    return list(image.tobytes()), width, height


def _planes(image):
    """The planes to compare: R, G, B for colour, L otherwise.

    Colour must be compared per channel. Two flat images, one blue and one red,
    have almost the same luma, so a grey SSIM scores them as nearly identical and
    a lossy pass that shifted every hue would go through.
    """
    if image.mode in ("RGB", "RGBA"):
        return list(image.convert("RGB").split())
    return [image.convert("L")]


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


def _ssim_single(a, b, width, height, window: int) -> float:
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


def ssim(left, right, sample: int = 64, window: int = 7) -> float:
    """Structural similarity of two images, averaged over their colour planes."""
    # A grey picture and its RGB twin are the same picture: a lossy pass may well
    # turn a grey PNG into a 3-channel JPEG. Compare in colour when either side has
    # colour, so the plane counts always match. Returning 0.0 on a plane-count
    # mismatch scored a near-lossless grey result as "SSIM 0.0000" while its PSNR
    # was 72-92 dB - a contradiction, and a false FAIL for every grey input.
    if (left.mode in ("RGB", "RGBA")) != (right.mode in ("RGB", "RGBA")):
        left, right = left.convert("RGB"), right.convert("RGB")
    left_planes, right_planes = _planes(left), _planes(right)
    if len(left_planes) != len(right_planes):
        return 0.0
    scores = []
    for left_plane, right_plane in zip(left_planes, right_planes):
        a, width, height = _plane_pixels(left_plane, sample)
        b, width_b, height_b = _plane_pixels(right_plane, sample)
        if (width, height) != (width_b, height_b):
            return 0.0
        scores.append(_ssim_single(a, b, width, height, window))
    return sum(scores) / len(scores) if scores else 0.0


def psnr(left, right) -> float:
    """Peak signal-to-noise ratio in dB on a downscaled grey copy.

    Reported for lossy work next to SSIM, never as proof of identity. The number is
    computed after downscaling, so it is a comparison aid, not a full-resolution
    PSNR; the message that prints it says so.
    """
    a, width, height = _plane_pixels(left.convert("L"), 192)
    b, width_b, height_b = _plane_pixels(right.convert("L"), 192)
    if (width, height) != (width_b, height_b):
        return 0.0
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
    left = reference.convert("RGB")
    right = candidate.convert("RGB")
    if left.tobytes() == right.tobytes():
        return []
    from PIL import Image, ImageChops, ImageStat

    # Per channel, not on a greyscale copy: luma weights red at 0.299 and blue at
    # 0.114, so a blue shift of 8.8 grey levels passes a 1.0 mean threshold even
    # though the blue channel moved by nearly 9 - the printed number would be about
    # nine times looser than the picture change it is supposed to catch. The worst
    # channel decides.
    channels = ImageChops.difference(left, right).split()
    average = max(ImageStat.Stat(channel).mean[0] for channel in channels)
    worst = max(max(channel.resize((16, 16), Image.BOX).tobytes()) for channel in channels)
    if average <= MEAN_ABS_MAX and worst <= BLOCK_MEAN_MAX:
        return []
    score = ssim(left, right)
    return ["%s: picture differs (worst channel mean |delta| %.2f (max %.2f), worst block %.0f (max %.0f), SSIM %.4f)"
            % (label, average, MEAN_ABS_MAX, worst, BLOCK_MEAN_MAX, score)]


def compare_with_file(reference, candidate, label: str = "image") -> list:
    """Compare an open image with a file on disk: the usual case, a source page
    rendered in memory against the page the engine wrote."""
    from PIL import Image

    try:
        with Image.open(candidate) as cand_image:
            return compare_pil(reference, cand_image, label)
    except OSError as error:
        return ["%s: cannot decode %s (%s)" % (label, candidate, error)]


def compare_images(reference, candidate, label: str = "image") -> list:
    from PIL import Image

    try:
        with Image.open(reference) as ref_image:
            return compare_with_file(ref_image, candidate, label)
    except OSError as error:
        return ["%s: cannot decode %s (%s)" % (label, reference, error)]


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
    """Every page of the source is present, once, and shows that page.

    The benchmark converts whole documents, so "selected" is the whole source;
    a run that exports a page range needs a different contract, not a quieter
    check. A missing page, a page rendered at a different DPI, a page that appears
    twice (two subfolders can both hold a _0001.png) and a page belonging to
    another document all fail here.
    """
    entries = page_entries(out_dir)
    if not entries:
        return ["no page files in %s" % out_dir]
    found = {}
    for number, path in entries:
        found.setdefault(number, path)
    numbers = [number for number, _ in entries]
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    issues = []
    if duplicates:
        issues.append("%d page number(s) appear more than once: %s"
                      % (len(duplicates), duplicates[:8]))
    try:
        total = pdf_page_count(source_pdf)
    except Exception as error:                              # noqa: BLE001 - report it
        return ["cannot read the source PDF %s (%s)" % (source_pdf, error)]
    expected = set(range(1, total + 1))
    missing = sorted(expected - set(found))
    unexpected = sorted(set(found) - expected)
    if missing:
        issues.append("missing %d page file(s): %s" % (len(missing), missing[:8]))
    if unexpected:
        issues.append("%d page file(s) that the selection does not contain: %s"
                      % (len(unexpected), unexpected[:8]))
    try:
        for number, image in iter_pdf_pages(source_pdf, dpi):
            try:
                candidate = found.get(number)
                if candidate is None:
                    continue
                issues += compare_with_file(image, candidate, "page %d" % number)
            finally:
                # iter_pdf_pages hands ownership to the caller; a page that could
                # not be compared must still release its bitmap.
                image.close()
            if len(issues) > 12:
                issues.append("... further differences suppressed")
                break
    except Exception as error:                      # noqa: BLE001 - report, never raise
        # A pdfium render or a Pillow decode can fail on a single damaged page;
        # the check must name it instead of escaping with a traceback (the same
        # contract check_pdf_compress follows).
        issues.append("page rendering failed: %s: %s" % (type(error).__name__, error))
    return issues


def check_image_pairs(pairs, mode: str = "lossless", lossy_target: bool = False) -> list:
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
                    # Size first: SSIM downscales both images to 64x64, so a result
                    # cut to half the resolution scores about 1.0 - "smaller by
                    # throwing pixels away" is exactly the shortcut this gate
                    # exists to catch, and every other check here tests size first.
                    if left.size != right.size:
                        issues.append("%s: size changed %s -> %s (a downscaled result "
                                      "is smaller and still looks similar)"
                                      % (source.name, left.size, right.size))
                        continue
                    score = ssim(left, right)
                    if score < LOSSY_SSIM_MIN:
                        issues.append("%s: lossy result differs too much "
                                      "(SSIM %.4f < %.2f, PSNR %.1f dB on the "
                                      "downscaled grey comparison)"
                                      % (source.name, score, LOSSY_SSIM_MIN, psnr(left, right)))
                    continue
                if mode == "convert":
                    # Whether a conversion may lose detail is the caller's decision
                    # - the engine knows whether it ran lossless - not the file
                    # suffix's: WebP exists in both forms, and PNG to lossless WebP
                    # must still be pixel-identical or this gate quietly weakens.
                    if lossy_target:
                        if left.size != right.size:
                            issues.append("%s: size changed %s -> %s (a downscaled "
                                          "result is smaller and still looks similar)"
                                          % (source.name, left.size, right.size))
                            continue
                        score = ssim(left, right)
                        if score < CONVERT_SSIM_MIN:
                            issues.append("%s: converting to %s changed the picture "
                                          "(SSIM %.4f < %.2f)"
                                          % (source.name, Path(output).suffix, score,
                                             CONVERT_SSIM_MIN))
                    elif left.convert("RGBA").tobytes() != right.convert("RGBA").tobytes():
                        issues.append("%s: converting the format changed the picture"
                                      % source.name)
                    continue
                if left.size != right.size:
                    issues.append("%s: size changed %s -> %s" % (source.name, left.size, right.size))
                    continue
                if left.mode != right.mode:
                    issues.append("%s: mode changed %s -> %s" % (source.name, left.mode, right.mode))
                    continue
                # A palette image's bytes are palette indices: two files with the
                # same indices and different palettes look nothing alike but compare
                # equal, so the pixel comparison is made on real colours.
                palette = left.mode in ("P", "PA") or right.mode in ("P", "PA")
                left_pixels = left.convert("RGBA").tobytes() if palette else left.tobytes()
                right_pixels = right.convert("RGBA").tobytes() if palette else right.tobytes()
                if left_pixels != right_pixels:
                    issues.append("%s: pixels are not identical (lossless work must not change them)"
                                  % source.name)
                # Metadata is part of "lossless": a PNG text chunk the source
                # carried (a caption, a DPI record, a colour profile) must survive,
                # because the corpus deliberately puts one there to be lost.
                for key, value in left.info.items():
                    if key in ("icc_profile", "exif"):
                        if right.info.get(key) != value:
                            issues.append("%s: %s was dropped" % (source.name, key))
                    elif isinstance(value, str) and right.info.get(key) != value:
                        issues.append("%s: text metadata %r was dropped or changed"
                                      % (source.name, key))
                for key, value in right.info.items():
                    if (isinstance(value, str) and key not in left.info
                            and key not in ("transparency", "gamma", "dpi")):
                        issues.append("%s: text metadata %r was added" % (source.name, key))
        except OSError as error:
            issues.append("%s: cannot compare (%s)" % (source.name, error))
        except Exception as error:                      # noqa: BLE001 - report, never raise
            # Pillow raises DecompressionBombError for huge images and pypdfium2
            # raises its own types; letting them escape would kill the whole
            # benchmark instead of naming the one file that could not be checked.
            issues.append("%s: cannot compare (%s: %s)"
                          % (source.name, type(error).__name__, error))
    return issues


def check_image_set(sources, out_dir, mode: str = "lossless", lossy_target: bool = False) -> list:
    """Pair by file name, for a single-image run and for the unit tests."""
    files = deliverables(out_dir)
    pairs = []
    issues = []
    for source in sources:
        source = Path(source)
        # A name collision gets " (2)", " (3)" ... from the engine, so match the
        # whole series; recognising only " (2)" reported a correct output as missing.
        collision = re.compile(re.escape(source.stem) + r"(?: \(\d+\))?")
        matches = [path for path in files if collision.fullmatch(path.stem)]
        if len(matches) != 1:
            # Collect and keep going: reporting only the first problem makes the
            # fix-rerun loop slower than it has to be.
            issues.append("%s: expected one output, found %d" % (source.name, len(matches)))
            continue
        pairs.append((source, matches[0]))
    return issues + check_image_pairs(pairs, mode, lossy_target)


def _part_order(path: Path):
    """Sort split parts by the LAST number in the name, numerically.

    Plain string order puts _10 before _2, which would make a correct ten-part
    split compare every page against the wrong part and report a failure that is
    not there. The last number, not the first: a source called report_2024.pdf
    carries its own year into every part name, and the part number is what
    follows it. A name without a number sorts after the numbered ones and keeps a
    stable order between runs.
    """
    numbers = re.findall(r"\d+", path.stem)
    return (0, int(numbers[-1]), path.name) if numbers else (1, 0, path.name)


def check_pdf_split(source_pdf, out_dir) -> list:
    """The parts are the source, page for page and in order - by text layer.

    The comparison is the extracted text of every page. It catches lost and
    duplicated pages for every input, and swapped parts for born-digital ones.

    Two limits, stated here because they are the difference between "checked" and
    "assumed":
      * it does not re-render the pages, so a split that kept the text and rewrote
        the pictures passes (compression has its own check, with a rendered sample);
      * a scanned document has no text layer, so every page compares equal and a
        swapped order goes through unnoticed - for those, only the page count is
        verified.
    """
    issues = []
    try:
        source_pages = pdf_text_pages(source_pdf)
    except Exception as error:                          # noqa: BLE001 - report it
        return ["source PDF cannot be read: %s" % error]
    # Only PDFs are parts. A stray report or preview in the output folder is not a
    # part, and treating it as one failed a correct split (the same filter
    # check_pdf_compress already applies).
    parts = [path for path in sorted(deliverables(out_dir), key=_part_order)
             if path.suffix.lower() == ".pdf"]
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


def check_pdf_compress(source_pdf, out_dir, sample: int = 8) -> list:
    """Compression keeps the pages, the text layer and the pictures.

    Pictures are checked by rendering a bounded sample of pages from both files and
    comparing them: reading every image object out of a PDF and matching it to its
    counterpart is unreliable (renumbering, object reuse), while "the page still
    looks the same" is exactly what the user gets. The sample is evenly spaced and
    capped at 8 pages so a 120-page benchmark stays affordable; the docstring says
    sample, not every page, because that is what the check does. The sample is
    rendered at 72 dpi: an embedded picture that was heavily downsampled can hide
    below that rendering, which is a limit of this check, not a claim of full
    fidelity (the smoke suite's compression case is the one that would notice).
    """
    issues = []
    outputs = [path for path in deliverables(out_dir) if path.suffix.lower() == ".pdf"]
    if len(outputs) != 1:
        return ["expected one compressed PDF, found %d" % len(outputs)]
    try:
        source_pages = pdf_text_pages(source_pdf)
    except Exception as error:                          # noqa: BLE001
        return ["source PDF cannot be read: %s" % error]
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
    # Only pages both files have: a result that lost pages is already an issue, and
    # indexing the shorter document would raise IndexError instead of reporting
    # the very failure this check exists for.
    limit = min(len(source_pages), len(result_pages))
    if limit:
        wanted = list(range(0, limit, max(1, limit // sample)))[:sample]
        try:
            for (number, left), (_, right) in zip(iter_pdf_pages(source_pdf, 72, wanted),
                                                  iter_pdf_pages(outputs[0], 72, wanted)):
                issues += compare_pil(left, right, "page %d rendered at 72 dpi" % number)
                left.close()
                right.close()
        except Exception as error:                      # noqa: BLE001 - report it
            issues.append("pages could not be compared: %s" % error)
    return issues


def compressed_growth(source_pdf, out_dir):
    """Output bytes / source bytes, or None when there is no single result.

    Reported, not failed: whether a lossless cleanup helps depends on the
    document, and the benchmark's job is to show the number, not to declare every
    growth a defect. None rather than 0.0, because a caller printing the value
    would read 0.0 as "the result is zero bytes", which is a wrong answer to
    "how big is it".
    """
    outputs = [path for path in deliverables(out_dir) if path.suffix.lower() == ".pdf"]
    if len(outputs) != 1:
        return None
    source_bytes = Path(source_pdf).stat().st_size
    return round(outputs[0].stat().st_size / float(source_bytes or 1), 4)


def check_rag(sources, out_dir, pairs=None) -> list:
    """The strict RAG export is the source itself; hashes must agree.

    "The source itself" cuts both ways: a file in the export that the source does
    not have is a change of what the user gets, so it is reported too.

    Pass the run's own (source, output) pairs when they are available. The strict
    export deliberately keeps the source's whole name, extension included, and
    appends the part number: deck.pptx becomes deck.pptx_part000001.pptx. Pairing
    by file name therefore called a byte-identical export "not in the strict
    export" plus "1 file(s) the source does not have", and the rag.strict
    benchmark case could never pass on any platform. The hash is what "the same
    file" means; the naming promise is checked by its own test.
    """
    issues = []
    files = deliverables(out_dir)
    if pairs is not None:
        recorded = []
        for source, output in pairs:
            # Both sides come from the run's report, so compare resolved paths: a
            # relative path in one report must not read as "a different file".
            source, output = Path(source), Path(output)
            if not output.is_file():
                issues.append("%s: the recorded output %s does not exist"
                              % (source.name, output))
                continue
            recorded.append(output)
            try:
                same = sha256_file(output) == sha256_file(source)
            except OSError as error:
                issues.append("%s: cannot hash (%s)" % (source.name, error))
                continue
            if not same:
                issues.append("%s: exported bytes differ from the source" % source.name)
        paired = {Path(source).resolve() for source, _ in pairs}
        for source in sources:
            if Path(source).resolve() not in paired:
                issues.append("%s: the run recorded no export for it" % Path(source).name)
        exported = {path.resolve() for path in recorded}
        extra = sorted(str(path.relative_to(Path(out_dir).resolve())) for path in files
                       if path.resolve() not in exported)
        if extra:
            issues.append("%d file(s) in the export that the run did not record: %s"
                          % (len(extra), extra[:8]))
        return issues
    by_name = {}
    for path in files:
        if path.name in by_name:
            # Two files with the same name (different subfolders) make "the export
            # equals the source" unverifiable: naming the clash beats comparing one
            # copy and hoping it is the right one.
            issues.append("%s: appears more than once in the export (%s, %s)"
                          % (path.name, by_name[path.name], path))
            continue
        by_name[path.name] = path
    expected = {Path(source).name for source in sources}
    for source in sources:
        source = Path(source)
        candidate = by_name.get(source.name)
        if candidate is None:
            issues.append("%s: not in the strict export" % source.name)
            continue
        try:
            same = sha256_file(candidate) == sha256_file(source)
        except OSError as error:
            issues.append("%s: cannot hash (%s)" % (source.name, error))
            continue
        if not same:
            issues.append("%s: exported bytes differ from the source" % source.name)
    extra = sorted(name for name in by_name if name not in expected)
    if extra:
        issues.append("%d file(s) in the export that the source does not have: %s"
                      % (len(extra), extra[:8]))
    return issues

