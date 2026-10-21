"""Split only self-contained DOCX body blocks, keeping every ancillary ZIP part."""
import hashlib
import copy
from pathlib import Path
import shutil
import sqlite3
import zipfile

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
UNSAFE = {"bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd",
          "commentReference", "footnoteReference", "endnoteReference", "fldChar",
          "fldSimple", "instrText", "moveFrom", "moveTo", "altChunk"}


def split(source: Path, work: Path, sink, options):
    with zipfile.ZipFile(source) as original, sqlite3.connect(work / "body.sqlite") as spool:
        if len(original.namelist()) != len(set(original.namelist())):
            raise ValueError("DOCX 有重复命名部件，不能确认无损拆分")
        if any(name.startswith("_xmlsignatures/") for name in original.namelist()):
            raise ValueError("签名 DOCX 不能在保持签名有效的前提下拆分，保留原件")
        spool.execute("CREATE TABLE blocks (id INTEGER PRIMARY KEY, payload BLOB NOT NULL, hash TEXT NOT NULL)")
        root_template = None
        body_attributes = {}
        section = b""
        count = 0
        with original.open("word/document.xml") as handle:
            for event, node in etree.iterparse(handle, events=("start", "end"), resolve_entities=False, no_network=True):
                if root_template is None:
                    root_template = etree.Element(node.tag, attrib=node.attrib, nsmap=node.nsmap)
                if event == "start" and node.tag == f"{{{W}}}body":
                    body_attributes = dict(node.attrib)
                if event != "end" or node.getparent() is None:
                    continue
                if node.getparent().tag == f"{{{W}}}body":
                    if node.tag == f"{{{W}}}sectPr":
                        section = etree.tostring(node, with_tail=False)
                    else:
                        for element in node.iter():
                            if not isinstance(element.tag, str):
                                raise ValueError("DOCX 含实体或非标准节点，不能确认安全拆分")
                            name = etree.QName(element).localname
                            if name in UNSAFE or (name == "sectPr") or element.get(f"{{{W}}}anchor"):
                                raise ValueError(f"DOCX 含 {name} 跨块/分页关系，不能确认无损拆分，保留原件")
                        count += 1
                        payload = etree.tostring(node, with_tail=False)
                        canonical = etree.tostring(node, method="c14n")
                        spool.execute("INSERT INTO blocks VALUES (?, ?, ?)", (count, payload, hashlib.sha256(canonical).hexdigest()))
                    node.clear()
                    while node.getprevious() is not None:
                        del node.getparent()[0]
                elif node.getparent().tag == root_template.tag and node.tag != f"{{{W}}}body":
                    # Preserve background and any other document-level siblings.
                    root_template.append(etree.fromstring(etree.tostring(node)))
        if not count:
            raise ValueError("DOCX 没有可拆分正文块")
        spool.commit()
        root_bytes = etree.tostring(root_template)
        root_name = (f"{root_template.prefix}:" if root_template.prefix else "") + etree.QName(root_template).localname
        closing = f"</{root_name}>".encode()
        # root_template may serialize as self-closing when there is no background.
        if root_bytes.endswith(b"/>"):
            prefix = root_bytes[:-2] + b">"
        else:
            prefix = root_bytes[:-len(closing)]
        body = etree.Element(f"{{{W}}}body", attrib=body_attributes, nsmap=root_template.nsmap)
        body_open = etree.tostring(body)[:-2] + b">"
        body_name = (f"{body.prefix}:" if body.prefix else "") + "body"
        body_close = f"</{body_name}>".encode()

        def emit_range(start, end):
            main = work / "document.xml"
            with main.open("wb") as handle:
                handle.write(b'<?xml version="1.0" encoding="UTF-8"?>' + prefix + body_open)
                for payload, in spool.execute("SELECT payload FROM blocks WHERE id BETWEEN ? AND ? ORDER BY id", (start, end)):
                    handle.write(payload)
                handle.write(section + body_close + closing)
            candidate = work / "part.docx"
            with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED) as result:
                result.comment = original.comment
                for entry in original.infolist():
                    if entry.filename == "word/document.xml":
                        result.write(main, entry.filename)
                    else:
                        # Do not drop unreferenced images, OLE objects, custom XML, comments,
                        # styles, headers, revisions, or relationships to make the file smaller.
                        # zipfile mutates offsets on write; never reuse the source ZipInfo object.
                        with original.open(entry) as src, result.open(copy.copy(entry), "w") as target:
                            shutil.copyfileobj(src, target, length=1024 * 1024)
            if candidate.stat().st_size > sink.limits.document_bytes:
                if start == end:
                    raise ValueError("DOCX 单个段落/整张表格加保留资源仍超限，不能无损拆分，保留原件")
                middle = (start + end) // 2
                emit_range(start, middle)
                emit_range(middle + 1, end)
                return
            with zipfile.ZipFile(candidate) as result:
                # Verify every auxiliary part byte-for-byte, and body blocks canonically.
                for entry in original.infolist():
                    if entry.filename != "word/document.xml":
                        def checksum(stream):
                            value = hashlib.sha256()
                            while block := stream.read(1024 * 1024):
                                value.update(block)
                            return value.hexdigest()
                        with original.open(entry) as a, result.open(entry.filename) as b:
                            if checksum(a) != checksum(b):
                                raise ValueError("DOCX 附属资源完整性验证失败")
                with result.open("word/document.xml") as handle:
                    tree = etree.parse(handle, etree.XMLParser(resolve_entities=False, no_network=True))
                parsed = tree.find(f"{{{W}}}body")
                blocks = [node for node in parsed if node.tag != f"{{{W}}}sectPr"]
                expected = [value for value, in spool.execute("SELECT hash FROM blocks WHERE id BETWEEN ? AND ? ORDER BY id", (start, end))]
                actual = [hashlib.sha256(etree.tostring(node, method="c14n")).hexdigest() for node in blocks]
                if actual != expected:
                    raise ValueError("DOCX 正文、表格、图像/公式引用或顺序验证失败")
            sink.emit(candidate, "document", {"operation": "split_docx", "block_start": start,
                      "block_end": end, "auxiliary_parts": "sha256_identical", "body_blocks": "canonical_xml_identical",
                      "page_count_method": "unknown"})
        emit_range(1, count)
        sink.notice("DOCX 仅拆独立段落/整张表格；正文块与附属资源已逐项校验。含跨块字段、批注、脚注、书签或分节关系的文件不拆分。分片重新分页，不保留原分页位置")
