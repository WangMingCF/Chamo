"""Content-aware page classification for PDF rendering.

The renderer shares one PDF across several worker processes only when the pages
are full-page rasters (scans). Splitting a born-digital page whose cost is text
and vector drawing is a net loss, because those pages render faster in a single
process than the processes are worth (measured in a page-count sweep).

So the decision has to be made from the content, not from the extension or the
file name. This module answers one question with a bounded sample:

    is this PDF a scanned document whose pages are each one raster covering the
    whole page, with no text objects and no large vector drawing?

It never renders a page and never parses the whole document, so the cost stays
negligible next to a real export. Any failure answers "unknown", which callers
must treat as "do not shard": a classifier that raises must never change the
behaviour of a conversion it could not understand.

Two false positives were measured before the coverage rule existed, and both made
sharding slower than not sharding:

  * a born-digital page with a small logo image and vector lines has an image
    object, but it covers 0.3% of the page -- "has an image" is not "is a scan";
  * a scan of small pages (200x300 pt) renders cheaply enough per page that six
    processes cost more than they save. Coverage alone cannot see this, so the
    report also carries the page area and the caller applies a pixel budget.
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["classify"]

# pdfium page-object type ids (FPDF_PAGEOBJ_*). Kept as literals so importing
# this module does not drag in pdfium until classify() actually runs.
_PAGEOBJ_TEXT = 1
_PAGEOBJ_PATH = 2
_PAGEOBJ_IMAGE = 3

# A scan is a raster that fills the page. Measured: a real scan page reports one
# image object at 100% coverage; the born-digital counter-example reported 0.32%.
MIN_IMAGE_COVERAGE = 0.7
# Vector drawing this large means the page still has real drawing work to do, so
# it renders like a born-digital page even when a big background image exists.
MAX_VECTOR_COVERAGE = 0.3


def _sample_indices(total: int, limit: int) -> list[int]:
    """Evenly spaced page indices, always including the first and the last page.

    Even spacing is what catches a document that is scans at the front and
    text at the back (or the reverse); the first and last page alone would miss
    a mixed middle.
    """
    if total <= 0:
        return []
    count = min(max(1, limit), total)
    if count == 1:
        return [0]
    step = (total - 1) / (count - 1)
    return sorted({round(i * step) for i in range(count)})


def _coverage(obj, page_area: float) -> float:
    """Fraction of the page this object's bounding box covers, 0.0 if unknown."""
    if page_area <= 0:
        return 0.0
    try:
        left, bottom, right, top = obj.get_bounds()
    except Exception:  # noqa: BLE001 - an unreadable object is not evidence of a scan
        return 0.0
    area = max(0.0, float(right) - float(left)) * max(0.0, float(top) - float(bottom))
    return min(1.0, area / page_area)


def _has_text_layer(page) -> bool:
    """Text inside a form XObject is not a top-level page object.

    A scanned page answers "" here without rasterising anything.
    """
    textpage = page.get_textpage()
    try:
        return bool(textpage.get_text_range().strip())
    finally:
        textpage.close()


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _classify_document(doc, sample_limit: int):
    """Inspect up to sample_limit pages of an open pdfium document."""
    total = len(doc)
    if total <= 0:
        return {"kind": "text", "pages": 0, "sampled": 0, "image_pages": 0,
                "area_pt2": None, "reason": "PDF 没有页面"}
    indices = _sample_indices(total, sample_limit)
    image_pages = 0
    checked = 0
    areas = []
    for index in indices:
        page = doc[index]
        checked += 1
        try:
            width, height = page.get_size()
            area = float(width) * float(height)
            if area > 0:
                areas.append(area)
            kinds = set()
            image_cover = 0.0
            path_cover = 0.0
            for obj in page.get_objects():
                kind = obj.type
                kinds.add(kind)
                if kind == _PAGEOBJ_IMAGE:
                    image_cover = max(image_cover, _coverage(obj, area))
                elif kind == _PAGEOBJ_PATH:
                    path_cover = max(path_cover, _coverage(obj, area))
            if _PAGEOBJ_TEXT in kinds or _has_text_layer(page):
                return {"kind": "text", "pages": total, "sampled": checked,
                        "image_pages": image_pages, "area_pt2": _median(areas),
                        "reason": f"第 {index + 1} 页含文字对象，按文字型文档处理"}
            if image_cover < MIN_IMAGE_COVERAGE:
                return {"kind": "text", "pages": total, "sampled": checked,
                        "image_pages": image_pages, "area_pt2": _median(areas),
                        "reason": (f"第 {index + 1} 页没有覆盖整页的图像"
                                   f"（最大图像覆盖 {image_cover:.1%}），按文字型处理")}
            if path_cover >= MAX_VECTOR_COVERAGE:
                return {"kind": "text", "pages": total, "sampled": checked,
                        "image_pages": image_pages, "area_pt2": _median(areas),
                        "reason": (f"第 {index + 1} 页含大幅矢量图形"
                                   f"（覆盖 {path_cover:.1%}），按文字型处理")}
            image_pages += 1
        finally:
            page.close()
    kind = "image" if image_pages == checked else "text"
    reason = (f"抽样 {checked}/{total} 页均为覆盖整页的扫描图像且无文字对象"
              if kind == "image" else
              f"仅 {image_pages}/{checked} 个抽样页是整页扫描图像")
    return {"kind": kind, "pages": total, "sampled": checked,
            "image_pages": image_pages, "area_pt2": _median(areas), "reason": reason}


def classify(path: Path, sample_limit: int = 12) -> dict:
    """Classify a PDF as image-only ("image"), text/other ("text") or "unknown".

    Returns {"kind", "pages", "sampled", "image_pages", "area_pt2", "reason"}.
    "unknown" is the answer for every exception, and callers must fall back to the
    existing single-process path instead of sharding. area_pt2 is the median page
    area in PDF points; it lets the caller judge per-page cost without reopening
    the file, because page count alone does not predict whether sharding pays.
    """
    result = {"kind": "unknown", "pages": None, "sampled": 0, "image_pages": 0,
              "area_pt2": None, "reason": ""}
    try:
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(str(path)) as doc:
            result.update(_classify_document(doc, max(1, int(sample_limit))))
        return result
    except Exception as error:  # noqa: BLE001 - see module docstring
        result.update(kind="unknown", pages=None, sampled=0, image_pages=0,
                      area_pt2=None,
                      reason=f"内容判定失败，按现有单一进程行为处理：{type(error).__name__}: {error}")
        return result
