from html.parser import HTMLParser
from pathlib import Path

from .output import Sink


class VisibleHTML(HTMLParser):
    """Extract uploadable text incrementally without DOM loading or network requests."""
    def __init__(self, handle):
        super().__init__(convert_charrefs=True)
        self.handle = handle
        self.skip = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip.append(tag)
        if not self.skip and tag == "br":
            self.handle.write("\n")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}:
            self.handle.write("\n")
        elif tag in {"td", "th"}:
            self.handle.write("\t")

    def handle_data(self, data):
        if not self.skip:
            self.handle.write(data)


def pack(source: Path, work: Path, sink: Sink, options: dict):
    encoding = options["encoding"]
    suffix = source.suffix.lower()
    if suffix in {".html", ".htm"}:
        extracted = work / "visible.txt"
        with extracted.open("w", encoding="utf-8") as target, source.open(encoding=encoding) as handle:
            parser = VisibleHTML(target)
            while block := handle.read(65536):
                parser.feed(block)
                # HTMLParser buffers incomplete tags; avoid unbounded malformed-tag accumulation.
                if len(parser.rawdata) > 1_000_000:
                    raise ValueError("HTML 存在超过 1MB 的未结束标签，请修复 HTML")
            parser.close()
        source = extracted
        encoding = "utf-8"
        suffix = ".txt"
        sink.notice("HTML 提取静态可见文字为 TXT；表格用制表符分隔，不执行脚本，图片/样式/链接目标不保留")
    if suffix in {".md", ".markdown"}:
        suffix = ".md"
        sink.notice("Markdown 按 UTF-8 字节和换行切分；跨文件代码围栏/表格可能需要云端结合顺序读取")
    else:
        suffix = ".txt"
    candidate = work / f"text{suffix}"
    cap = sink.limits.text_bytes
    offset = 0
    start = 0
    size = 0
    output = candidate.open("wb")

    def flush():
        nonlocal output, start, size
        output.close()
        if size:
            sink.emit(candidate, "text", {"char_start": start, "char_end": offset,
                                          "coordinate": "extracted_text" if source.name == "visible.txt" else "decoded_text"})
        start = offset
        size = 0
        output = candidate.open("wb")

    try:
        with source.open(encoding=encoding, newline="") as handle:
            while block := handle.read(65536):
                data = block.encode("utf-8")
                while data:
                    available = cap - size
                    prefix = data[:available].decode("utf-8", errors="ignore")
                    if not prefix:
                        if not size:
                            raise ValueError("text_bytes 小于单个 UTF-8 字符长度")
                        flush()
                        continue
                    # Prefer a nearby newline only when this block fills the file.
                    if len(data) > available:
                        newline = prefix.rfind("\n")
                        if newline > len(prefix) * 0.75:
                            prefix = prefix[:newline + 1]
                    encoded = prefix.encode("utf-8")
                    output.write(encoded)
                    size += len(encoded)
                    offset += len(prefix)
                    data = data[len(encoded):]
                    if data or size == cap:
                        flush()
        if size:
            flush()
        if not offset:
            raise ValueError("文本没有可输出内容")
    finally:
        output.close()
