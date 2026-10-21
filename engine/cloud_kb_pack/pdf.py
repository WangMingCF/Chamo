import math
from pathlib import Path

import pypdfium2 as pdfium

from . import images
from .output import Sink


def strict_metadata(source, document):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject
    reader = PdfReader(source)
    root = reader.trailer["/Root"]
    safe = {"/Type", "/Pages", "/Metadata", "/PageLayout", "/PageMode", "/ViewerPreferences", "/Lang"}
    extra = set(root) - safe
    if extra:
        raise ValueError("PDF 含书签、表单、附件、结构标签或其他文档级关系，不能确认无损拆分，保留原件；结构：" + ", ".join(sorted(extra)))
    for index in range(len(document)):
        page = document[index]
        try:
            if pdfium.raw.FPDFPage_GetAnnotCount(page):
                raise ValueError("PDF 含批注/链接/交互关系，不能确认无损拆分，保留原件")
        finally:
            page.close()

    def preserve(candidate):
        target = candidate.with_name("metadata.pdf")
        with candidate.open("rb") as handle:
            part = PdfReader(handle)
            writer = PdfWriter(clone_from=part)
            original_info = reader.trailer.get("/Info")
            writer._info = original_info.get_object().clone(writer) if original_info else None
            if "/ID" in reader.trailer:
                writer._ID = reader.trailer.raw_get("/ID").clone(writer)
            writer.pdf_header = reader.pdf_header
            for key in safe - {"/Type", "/Pages"}:
                if key in root:
                    writer._root_object[NameObject(key)] = root.raw_get(key).clone(writer)
            writer.write(target)
            writer.close()
        target.replace(candidate)
    def verify(candidate, start, end):
        from .pdfproof import page_fingerprint
        with candidate.open("rb") as handle:
            output = PdfReader(handle)
            if len(output.pages) != end-start:
                raise ValueError("PDF 拆分页数不完整")
            for index in range(end-start):
                if page_fingerprint(reader.pages[start+index]) != page_fingerprint(output.pages[index]):
                    raise ValueError(f"PDF 第 {start+index+1} 页正文/图像/字体/资源完整性验证失败，保留原件")
    preserve.verify = verify
    return preserve


def pack(source: Path, work: Path, sink: Sink, options: dict):
    with pdfium.PdfDocument(source) as document:
        total = len(document)
        if not total:
            raise ValueError("PDF 没有页面")
        if options["pdf_mode"] == "images":
            sink.notice("PDF 每页栅格化为一张 JPEG；文字图层、链接、书签不保留，云端需要 OCR")
            for index in range(total):
                page = document[index]
                try:
                    w, h = page.get_size()
                    scale = min(options["dpi"] / 72, sink.limits.image_max_edge / max(w, h),
                                math.sqrt(options["render_pixels"] / (w * h)))
                    bitmap = page.render(scale=scale)
                    try:
                        images.encode(bitmap.to_pil(), work, sink, {"page_start": index + 1, "page_end": index + 1, "operation": "render"})
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            return
        if total <= sink.limits.pdf_page_cap and source.stat().st_size <= sink.limits.pdf_byte_cap:
            sink.emit(source, "document", {"page_start": 1, "page_end": total, "operation": "copy"}, verified={"pages": total})
            return
        sink.notice("PDF 拆分保留页面内容；文档级书签、签名及跨页链接不保证保留")
        preserve = None
        if options.get("policy", "strict") == "strict":
            try:
                preserve = strict_metadata(source, document)
            except ValueError:
                from .pdfstructure import pack as structured_pack
                # A separate adapter preserves and verifies navigation/catalog data;
                # do not silently retry the same file with the lossy derive policy.
                sink.notices.remove("PDF 拆分保留页面内容；文档级书签、签名及跨页链接不保证保留")
                structured_pack(source, work, sink, options)
                return
        if preserve:
            sink.notices.remove("PDF 拆分保留页面内容；文档级书签、签名及跨页链接不保证保留")
            sink.notice("PDF 仅在未发现批注/交互/文档级结构关系时拆分，保留文档元数据；复杂文件保留原件等待处理")

        def emit_range(start: int, end: int):
            candidate = work / "part.pdf"
            # PDFium imports only a page range; keep encoded data on disk instead of bytes blobs.
            with pdfium.PdfDocument.new() as output:
                output.import_pages(document, pages=list(range(start, end)))
                output.save(candidate)
            if preserve:
                preserve(candidate)
            if candidate.stat().st_size <= sink.limits.pdf_byte_cap:
                if preserve:
                    preserve.verify(candidate, start, end)
                sink.emit(candidate, "document", {"page_start": start + 1, "page_end": end, "operation": "split",
                          "page_content_and_resources": "verified" if preserve else "unverified_derivation"})
            elif end - start > 1:
                middle = (start + end) // 2
                emit_range(start, middle)
                emit_range(middle, end)
            else:
                raise ValueError(f"PDF 第 {start + 1} 页单页仍超过 PDF 字节上限，保留原件等待处理")

        # A small upper bound limits one import's resource use even with permissive cloud limits.
        window = min(sink.limits.pdf_page_cap, options["page_window"])
        for start in range(0, total, window):
            emit_range(start, min(total, start + window))
