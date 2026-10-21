"""Split Markdown only at complete blocks; retain embedded images and chapter context."""
import hashlib
import re


def blocks(source, cap):
    heading_stack = []
    context = []
    buffer = bytearray()
    fence = None
    table_depth = 0
    math = False
    with source.open("rb") as handle:
        while line := handle.readline(cap + 1):
            if len(line) > cap:
                raise ValueError("Markdown 单行/内嵌图片超过 text_bytes，不能拆断图片或公式，保留原件")
            text = line.decode("utf-8").lstrip("\ufeff")
            if not buffer:
                context = list(heading_stack)
            buffer.extend(line)
            if len(buffer) > cap:
                raise ValueError("Markdown 单个完整段落、表格、代码或公式块超限，保留原件")
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", text.rstrip("\r\n"))
            if marker:
                if fence is None:
                    fence = (marker[1][0], len(marker[1]))
                elif marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                    fence = None
            if fence is None:
                table_depth += len(re.findall(r"<table(?:\s|>)", text, re.I))
                table_depth -= len(re.findall(r"</table\s*>", text, re.I))
                table_depth = max(0, table_depth)
                # Adjacent inline expressions '$a$$b$' are not a display-math fence.
                # Only a leading '$$' opens one; an existing display block closes at '$$'.
                if math and "$$" in text:
                    math = False
                elif not math and text.lstrip().startswith("$$") and text.count("$$") % 2:
                    math = True
                heading = re.match(r"^(#{1,6})\s+", text)
                if heading:
                    level = len(heading[1])
                    heading_stack = [h for h in heading_stack if h[0] < level]
                    heading_stack.append((level, line))
            if not text.strip() and fence is None and not table_depth and not math:
                yield bytes(buffer), context
                buffer.clear()
        if fence or table_depth or math:
            raise ValueError("Markdown 有未闭合的代码/表格/公式块，保留原件")
        if buffer:
            yield bytes(buffer), context


def pack(source, work, sink, options):
    cap = sink.limits.text_bytes
    candidate = work / "part.md"
    handle = None
    prefix = b""
    size = 0
    source_offset = 0
    start = 0
    original = hashlib.sha256()
    restored = hashlib.sha256()

    def finish():
        nonlocal handle
        if handle is None:
            return
        handle.close()
        handle = None
        # Reconstruct only original bytes; repeated chapter headers are identified explicitly.
        with candidate.open("rb") as check:
            check.seek(len(prefix))
            while data := check.read(65536):
                restored.update(data)
        sink.emit(candidate, "text", {"operation": "split_markdown_blocks",
                  "byte_start": start, "byte_end": source_offset, "context_prefix_bytes": len(prefix),
                  "embedded_images": "complete_original_markdown_tokens"})

    try:
        for block, context in blocks(source, cap):
            original.update(block)
            if handle is not None and size + len(block) > cap:
                finish()
            if handle is None:
                start = source_offset
                prefix = b"" if not start else b"".join(line for _, line in context) + (b"\n" if context else b"")
                # A first block's own new heading supersedes old headings at that depth.
                first = re.match(rb"^(#{1,6})\s+", block)
                if first and start:
                    prefix = b"".join(line for level, line in context if level < len(first[1]))
                    if prefix:
                        prefix += b"\n"
                if len(prefix) + len(block) > cap:
                    prefix = b""
                    sink.notice("个别章节标题上下文过长，未重复添加；原始正文完整保留")
                size = len(prefix)
                handle = candidate.open("wb")
                handle.write(prefix)
            handle.write(block)
            size += len(block)
            source_offset += len(block)
        finish()
        if original.digest() != restored.digest():
            raise ValueError("Markdown 分片拼接与原始字节不一致")
        sink.notice("Markdown 仅在完整块间拆分，内嵌 Base64 图片不拆断，重复章节上下文用前缀长度标记；剔除重复前缀后原始字节 SHA-256 已验证一致")
    finally:
        if handle is not None:
            handle.close()
