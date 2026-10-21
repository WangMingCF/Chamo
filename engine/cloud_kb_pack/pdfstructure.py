"""Split navigation-bearing PDFs without dropping their document-level information.

All bookmarks are retained in each part. Targets outside that part become standard
GoToR links to a sibling PDF. Page labels are sliced, while layer/form/private
catalog data is cloned through the same object translation as the page resources.
Interactive annotations, tagged structure, signatures and unknown catalogs still
fail closed; their semantics require their own verified adaptation.
"""
from decimal import Decimal

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DictionaryObject, IndirectObject, NameObject,
                          NumberObject, FloatObject, NullObject, BooleanObject,
                          ByteStringObject, StreamObject, TextStringObject)

from .pdfproof import page_fingerprint, stream_proof


def canonical(value, stack=None):
    """Compare decoded payloads, retaining keys that matter outside page trees."""
    stack = set() if stack is None else stack
    if isinstance(value, IndirectObject):
        value = value.get_object()
    if isinstance(value, (DictionaryObject, ArrayObject)):
        if id(value) in stack:
            return ["cycle"]
        stack.add(id(value))
        try:
            if isinstance(value, ArrayObject):
                return [canonical(v, stack) for v in value]
            ignored = set()
            if isinstance(value, StreamObject):
                ignored, proof_key, proof_value = stream_proof(value)
            result = {str(k): canonical(value[k], stack) for k in sorted(value) if k not in ignored}
            if isinstance(value, StreamObject):
                result[proof_key] = proof_value
            return result
        finally:
            stack.remove(id(value))
    if isinstance(value, ByteStringObject):
        return ["bytes", value.hex()]
    if isinstance(value, BooleanObject):
        return ["bool", value.value]
    if isinstance(value, NullObject) or value is None:
        return None
    if isinstance(value, (NumberObject, FloatObject)):
        return ["number", str(Decimal(str(value)).normalize() or Decimal(0))]
    return str(value)


def has_page(value, visited=None):
    visited = set() if visited is None else visited
    if isinstance(value, IndirectObject):
        value = value.get_object()
    if id(value) in visited:
        return False
    visited.add(id(value))
    if isinstance(value, DictionaryObject):
        return value.get("/Type") in {"/Page", "/Pages"} or any(has_page(v, visited) for v in value.values())
    if isinstance(value, ArrayObject):
        return any(has_page(v, visited) for v in value)
    return False


def number_tree(value):
    result = []
    value = value.get_object()
    nums = value.get("/Nums", [])
    result.extend((int(nums[i]), nums[i+1].get_object()) for i in range(0, len(nums), 2))
    for child in value.get("/Kids", []):
        result.extend(number_tree(child))
    return sorted(result, key=lambda pair: pair[0])


def outline_nodes(root, visited=None):
    nodes = []
    visited = set() if visited is None else visited
    current = root.get("/First")
    while current:
        obj = current.get_object()
        if id(obj) in visited:
            raise ValueError("PDF 书签链存在循环，保留原件")
        visited.add(id(obj))
        fields = {k: v for k, v in obj.items() if k not in {"/Parent", "/First", "/Last", "/Next", "/Prev"}}
        if "/A" in fields and "/Dest" in fields:
            raise ValueError("PDF 书签同时包含两个导航目标，保留原件")
        nodes.append((fields, outline_nodes(obj, visited)))
        current = obj.get("/Next")
    return nodes


def has_signature(value, visited=None):
    visited = set() if visited is None else visited
    if isinstance(value, IndirectObject):
        value = value.get_object()
    if id(value) in visited:
        return False
    visited.add(id(value))
    if isinstance(value, DictionaryObject):
        return value.get("/FT") == "/Sig" or value.get("/Type") == "/Sig" or "/ByteRange" in value or any(has_signature(v, visited) for v in value.values())
    if isinstance(value, ArrayObject):
        return any(has_signature(v, visited) for v in value)
    return False


class StructuredPdf:
    allowed = {"/Type", "/Pages", "/Metadata", "/PageLayout", "/PageMode", "/ViewerPreferences",
               "/Lang", "/Outlines", "/OpenAction", "/PageLabels", "/OCProperties", "/PieceInfo",
               "/AcroForm", "/Names"}

    def __init__(self, source, filename):
        self.reader = PdfReader(source)
        self.filename = filename
        self.root = self.reader.trailer["/Root"]
        extra = set(self.root) - self.allowed
        if extra:
            raise ValueError("PDF 尚未适配的文档结构：" + ", ".join(sorted(extra)))
        self.page_ids = {p.indirect_reference.idnum: i for i, p in enumerate(self.reader.pages)}
        if any(p.get("/Annots") or p.get("/B") for p in self.reader.pages):
            raise ValueError("PDF 含交互批注、文章链或结构标签，保留原件")
        if self.reader.named_destinations:
            raise ValueError("PDF 命名目标尚需跨分片适配，保留原件")
        special = {"/Type", "/Pages", "/Outlines", "/OpenAction", "/PageLabels"}
        self.copied_keys = set(self.root) - special
        if any(has_signature(self.root[k]) for k in self.copied_keys):
            raise ValueError("PDF 包含数字签名，拆分会使签名失效，保留原件")
        if any(has_page(self.root[k]) for k in self.copied_keys):
            raise ValueError("PDF 图层/表单/私有结构引用页树，保留原件")
        self.outlines = outline_nodes(self.root["/Outlines"]) if "/Outlines" in self.root else []
        # Implicit page labels also carry global page numbers: part 2 must not
        # silently renumber original page 101 as label 1.
        self.labels = number_tree(self.root["/PageLabels"]) if "/PageLabels" in self.root else [
            (0, DictionaryObject({NameObject("/S"): NameObject("/D")}))]
        self.page_proofs = [page_fingerprint(p) for p in self.reader.pages]

    def destination(self, value, reader=None, ranges=None, start=0):
        reader = reader or self.reader
        value = value.get_object()
        if isinstance(value, DictionaryObject):
            value = value["/D"]
        if not isinstance(value, ArrayObject) or len(value) < 2:
            raise ValueError("PDF 目标不是可验证的显式页面目标")
        page = value[0]
        if reader is self.reader:
            if isinstance(page, IndirectObject):
                index = self.page_ids[page.idnum]
            else:
                index = int(page)
        else:
            index = reader.get_page_number(page.get_object()) + start
        if not 0 <= index < len(self.reader.pages):
            raise ValueError("PDF 导航目标页不存在")
        return index, list(value[1:])

    def nav(self, value, writer, start, end, ranges):
        """Convert a destination to a local or remote action with the same view."""
        index, view = self.destination(value)
        action = DictionaryObject()
        if start <= index < end:
            action[NameObject("/S")] = NameObject("/GoTo")
            target = writer.pages[index-start].indirect_reference
        else:
            part = next(i for i, (a, b) in enumerate(ranges) if a <= index < b)
            action[NameObject("/S")] = NameObject("/GoToR")
            action[NameObject("/F")] = TextStringObject(self.filename(part+1))
            target = NumberObject(index-ranges[part][0])
        action[NameObject("/D")] = ArrayObject([target, *(v.clone(writer) for v in view)])
        return action

    def action(self, value, writer, start, end, ranges):
        value = value.get_object()
        if not isinstance(value, DictionaryObject):
            result = self.nav(value, writer, start, end, ranges)
            return result["/D"] if result["/S"] == "/GoTo" else result
        if value.get("/S") != "/GoTo":
            if has_page(value):
                raise ValueError("PDF 自定义导航动作引用页树")
            return value.clone(writer)
        if "/Next" in value:
            raise ValueError("PDF 串联动作尚未适配")
        result = self.nav(value["/D"], writer, start, end, ranges)
        for key in set(value)-{"/S", "/D"}:
            result[NameObject(key)] = value.raw_get(key).clone(writer)
        return result

    def write(self, target, start, end, ranges):
        writer = PdfWriter()
        try:
            for page in self.reader.pages[start:end]:
                cloned = writer.add_page(page)
                # pypdf removes this key by default. With no StructTreeRoot in
                # the source catalog it is an orphan ID, which we retain verbatim.
                if "/StructParents" in page:
                    cloned[NameObject("/StructParents")] = page.raw_get("/StructParents").clone(writer)
            if "/Info" in self.reader.trailer:
                writer._info = self.reader.trailer.raw_get("/Info").clone(writer)
            else:
                writer._info = None
            if "/ID" in self.reader.trailer:
                writer._ID = self.reader.trailer.raw_get("/ID").clone(writer)
            writer.pdf_header = self.reader.pdf_header
            for key in self.copied_keys:
                writer._root_object[NameObject(key)] = self.root.raw_get(key).clone(writer)
            if self.labels:
                nums = ArrayObject()
                active = [(i, label) for i, label in self.labels if i <= start]
                if not active:
                    raise ValueError("PDF 页标签缺少首组")
                index, label = active[-1]
                initial = label.clone(writer)
                if "/S" in initial:
                    initial[NameObject("/St")] = NumberObject(int(label.get("/St", 1))+start-index)
                nums.extend([NumberObject(0), initial])
                for index, label in self.labels:
                    if start < index < end:
                        nums.extend([NumberObject(index-start), label.clone(writer)])
                writer._root_object[NameObject("/PageLabels")] = DictionaryObject({NameObject("/Nums"): nums})
            if "/Outlines" in self.root:
                original = self.root["/Outlines"]
                tree = DictionaryObject({NameObject(k): v.clone(writer) for k, v in original.items() if k not in {"/First", "/Last"}})
                tree_ref = writer._add_object(tree)
                writer._root_object[NameObject("/Outlines")] = tree_ref
                def insert(nodes, parent_ref):
                    refs = []
                    for fields, children in nodes:
                        node = DictionaryObject()
                        for key, value in fields.items():
                            if key == "/Dest":
                                node[NameObject("/A")] = self.nav(value, writer, start, end, ranges)
                            elif key == "/A":
                                node[NameObject(key)] = self.action(value, writer, start, end, ranges)
                            else:
                                if has_page(value):
                                    raise ValueError("PDF 书签额外属性引用页树")
                                node[NameObject(key)] = value.clone(writer)
                        node[NameObject("/Parent")] = parent_ref
                        ref = writer._add_object(node)
                        if refs:
                            refs[-1].get_object()[NameObject("/Next")] = ref
                            node[NameObject("/Prev")] = refs[-1]
                        refs.append(ref)
                        insert(children, ref)
                    if refs:
                        parent_ref.get_object()[NameObject("/First")] = refs[0]
                        parent_ref.get_object()[NameObject("/Last")] = refs[-1]
                insert(self.outlines, tree_ref)
            if "/OpenAction" in self.root:
                writer._root_object[NameObject("/OpenAction")] = self.action(self.root["/OpenAction"], writer, start, end, ranges)
            writer.write(target)
        finally:
            writer.close()

    def normalized_action(self, value, reader, start, ranges):
        value = value.get_object()
        if isinstance(value, ArrayObject):
            index, view = self.destination(value, reader=reader, start=start)
            return {"page": index, "view": canonical(ArrayObject(view))}
        kind = value.get("/S")
        if kind in {"/GoTo", "/GoToR"}:
            if kind == "/GoTo":
                index, view = self.destination(value["/D"], reader=reader, start=start)
            else:
                names = [self.filename(i+1) for i in range(len(ranges))]
                index = ranges[names.index(str(value["/F"]))][0] + int(value["/D"][0])
                view = list(value["/D"][1:])
            return {"page": index, "view": canonical(ArrayObject(view)),
                    "extra": canonical(DictionaryObject({NameObject(k): v for k, v in value.items() if k not in {"/S", "/D", "/F"}}))}
        return canonical(value)

    def outline_model(self, root, reader, start, ranges):
        def model(nodes):
            result = []
            for fields, children in nodes:
                data = canonical(DictionaryObject({NameObject(k): v for k, v in fields.items() if k not in {"/A", "/Dest"}}))
                if "/Dest" in fields:
                    action = DictionaryObject({NameObject("/S"): NameObject("/GoTo"), NameObject("/D"): fields["/Dest"]})
                    data["navigation"] = self.normalized_action(action, reader, start, ranges)
                elif "/A" in fields:
                    data["navigation"] = self.normalized_action(fields["/A"], reader, start, ranges)
                result.append([data, model(children)])
            return result
        return model(outline_nodes(root))

    def verify(self, target, start, end, ranges):
        output = PdfReader(target)
        if len(output.pages) != end-start:
            raise ValueError("PDF 页面数量不完整")
        for i, page in enumerate(output.pages):
            if page_fingerprint(page) != self.page_proofs[start+i]:
                raise ValueError(f"PDF 原始第 {start+i+1} 页内容/图像/字体/资源不一致")
        root = output.trailer["/Root"]
        for key in self.copied_keys:
            if key not in root or canonical(root[key]) != canonical(self.root[key]):
                raise ValueError(f"PDF {key} 元数据/图层/表单/私有数据不一致")
        for key in ("/Info", "/ID"):
            if canonical(output.trailer.get(key)) != canonical(self.reader.trailer.get(key)):
                raise ValueError(f"PDF {key} 信息不一致")
        if output.page_labels != self.reader.page_labels[start:end]:
            raise ValueError("PDF 原始页标签未完整保留")
        if "/Outlines" in self.root:
            headings = lambda value: canonical(DictionaryObject({NameObject(k): v for k, v in value.items() if k not in {"/First", "/Last"}}))
            if headings(root["/Outlines"]) != headings(self.root["/Outlines"]):
                raise ValueError("PDF 书签根属性不一致")
            if self.outline_model(root["/Outlines"], output, start, ranges) != self.outline_model(self.root["/Outlines"], self.reader, 0, ranges):
                raise ValueError("PDF 书签层级、标题、样式或目标不一致")
        if "/OpenAction" in self.root:
            a = self.normalized_action(self.root["/OpenAction"], self.reader, 0, ranges)
            b = self.normalized_action(root["/OpenAction"], output, start, ranges)
            # Array destinations and /GoTo actions have identical navigation semantics.
            if isinstance(a, dict): a.setdefault("extra", {})
            if isinstance(b, dict): b.setdefault("extra", {})
            if a != b:
                raise ValueError("PDF 初始导航目标不一致")


def pack(source, work, sink, options):
    def filename(index):
        stem = source.name if sink.tree_relative is not None else source.stem
        return f"{stem}_part{index:06d}.pdf"
    document = StructuredPdf(source, filename)
    window = min(sink.limits.pdf_page_cap, options["page_window"])
    total = len(document.reader.pages)
    ranges = [(i, min(i+window, total)) for i in range(0, total, window)]
    # Determine the byte-driven ranges before committing any remote navigation.
    while True:
        for i, (start, end) in enumerate(ranges):
            target = work / f"structured-{i}.pdf"
            document.write(target, start, end, ranges)
            if target.stat().st_size > sink.limits.pdf_byte_cap:
                if end-start == 1:
                    raise ValueError(f"PDF 第 {start+1} 页与必需文档结构仍超过大小限制，保留原件")
                middle = (start+end)//2
                ranges[i:i+1] = [(start, middle), (middle, end)]
                break
        else:
            break
    with pdfium.PdfDocument(source) as original:
        for i, (start, end) in enumerate(ranges):
            target = work / f"structured-{i}.pdf"
            document.verify(target, start, end, ranges)
            with pdfium.PdfDocument(target) as result:
                for local in {0, end-start-1}:
                    # Boundary renders cover appearance plus optional-content state.
                    before = original[start+local]; after = result[local]
                    try:
                        a = before.render(scale=0.5); b = after.render(scale=0.5)
                        try:
                            if a.to_pil().tobytes() != b.to_pil().tobytes():
                                raise ValueError(f"PDF 第 {start+local+1} 页渲染不一致")
                        finally:
                            a.close(); b.close()
                    finally:
                        before.close(); after.close()
            sink.emit(target, "document", {"page_start": start+1, "page_end": end, "operation": "split",
                      "page_content_and_resources": "verified", "document_structure": "verified",
                      "navigation": "local_or_sibling_GoToR", "boundary_render": "pixel_identical",
                      "source_encrypted_empty_password": document.reader.is_encrypted})
    sink.notice("保留全部书签、页标签、图层、元数据及已验证表单/私有数据；跨分片导航使用同目录 GoToR 链接。保留原件，未栅格化或降低图片质量")
