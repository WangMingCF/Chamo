"""Independent acceptance gates for the PDF compression operation.

These tests describe the promises made to the user, not the current
implementation: every assertion reads a real file on disk, opens it with an
independent tool (pikepdf / pypdf / pypdfium2) and names the promise that broke
in its failure message. A test that cannot fail would be worse than no test, so
each one has a reachable failure: this file must be red while pdf_compress is
not implemented, and the red run recorded with the task is the negative control.

Contract under test:
  * op id 'pdf_compress' / CLI alias 'pdf-compress'; lossless by default;
    '--lossy' is explicit; '--dpi'/'--quality' only exist under '--lossy' and
    are refused before any conversion otherwise.
  * lossless: pikepdf rewrite, same page count, text still extractable, product
    never larger than the original (a rewrite that does not shrink publishes a
    byte-identical copy of the original).
  * lossy: explicit only, same page count, per-page physical size within 1 pt,
    product re-renderable by PDFium, fidelity flag 'lossy_user_selected', and
    no product + a readable item failure when the rebuild is not smaller.
  * output naming '<stem>_压缩.pdf'; a single-file run leaves exactly that one
    file (no manifest); a batch leaves exactly one '清单.json' with correct
    counts; an existing same-named file is never overwritten ("名称 (2)").
  * cancel leaves no half product and no manifest; encrypted PDFs are rejected
    with a readable error; a repeated identical run is idempotent (resumed).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
CHAMO_CLI = ENGINE / "chamo.py"

MANIFEST = "清单.json"
STAGING = ".Chamo暂存"

pikepdf = pytest.importorskip("pikepdf")
pdfium = pytest.importorskip("pypdfium2")
from PIL import Image  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic fixtures. All are generated in the test's tmp_path: no binary test
# fixture is committed and every expectation below is reproducible from code.
# ---------------------------------------------------------------------------
def make_text_pdf(path: Path, pages: int = 3) -> Path:
    """A text PDF whose text must survive the lossless rewrite."""
    from reportlab.pdfgen import canvas

    sheet = canvas.Canvas(str(path))
    for index in range(pages):
        sheet.setFont("Helvetica", 18)
        sheet.drawString(72, 720, "Page %d: alpha beta gamma delta" % (index + 1))
        sheet.drawString(72, 690, "The quick brown fox jumps over the lazy dog %d" % (index + 1))
        sheet.showPage()
    sheet.save()
    return path


def make_fixpoint_pdf(path: Path, pages: int = 3) -> Path:
    """A PDF already at pikepdf's save fixpoint, so a rewrite cannot shrink it.

    The lossless contract says a rewrite that is not smaller must publish a
    byte-identical copy of the original. This fixture reproduces that branch:
    re-saving it with the packaged pikepdf (any of the usual settings) returns
    exactly the same bytes, so the engine has no smaller rewrite to publish.
    """
    pdf = pikepdf.Pdf.new()
    font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                              BaseFont=pikepdf.Name.Helvetica)
    for index in range(pages):
        page = pdf.add_blank_page(page_size=(595, 842))
        page.obj.Contents = pdf.make_stream(
            ("BT /F1 24 Tf 72 700 Td (Page %d alpha beta gamma) Tj ET" % (index + 1)).encode())
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    first = io.BytesIO()
    pdf.save(first)
    second = io.BytesIO()
    pikepdf.open(io.BytesIO(first.getvalue())).save(
        second, compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate)
    path.write_bytes(second.getvalue())
    return path


def make_scan_pdf(path: Path, pages: int = 2, pixels=(850, 1100),
                  source_dpi: int = 150, seed: int = 7) -> Path:
    """An image-only "scanned" PDF whose Flate image data is far bigger than a JPEG render.

    Uniform RGB noise is the worst case for lossless compression: ~3 bytes per
    pixel stored, while the same page rendered and JPEG-encoded is a fraction of
    that. This makes the lossy branch's "rebuild must get smaller" condition
    genuine instead of accidental.
    """
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    pixel_w, pixel_h = pixels
    page_w, page_h = pixel_w * 72.0 / source_dpi, pixel_h * 72.0 / source_dpi
    rng = random.Random(seed)
    sheet = canvas.Canvas(str(path), pagesize=(page_w, page_h))
    for _ in range(pages):
        raw = rng.randbytes(pixel_w * pixel_h * 3)
        image = Image.frombytes("RGB", (pixel_w, pixel_h), raw)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        buffer.seek(0)
        sheet.drawImage(ImageReader(buffer), 0, 0, width=page_w, height=page_h)
        sheet.showPage()
    sheet.save()
    return path


def make_dense_text_pdf(path: Path, pages: int = 40) -> Path:
    """Many pages of fine text: slow enough at --dpi 300 that cancellation can land."""
    from reportlab.pdfgen import canvas

    sheet = canvas.Canvas(str(path))
    for page in range(pages):
        sheet.setFont("Helvetica", 9)
        for line in range(64):
            sheet.drawString(36, 800 - line * 12,
                             "row %02d page %02d: cancellation residue acceptance fixture"
                             % (line, page + 1))
        sheet.showPage()
    sheet.save()
    return path


def make_encrypted_pdf(path: Path, source: Path) -> Path:
    """An encrypted PDF (user password, so even "decrypt with empty" fails)."""
    writer = PdfWriter(clone_from=str(source))
    writer.encrypt(user_password="secret", owner_password="owner", algorithm="AES-256")
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def make_signed_pdf(path: Path, with_signature: bool) -> Path:
    """A scan with an AcroForm signature field; with_signature adds /V (a signature).

    A field with /FT /Sig and no /V is an empty placeholder, which fillable
    forms commonly ship with; only a field with a value is a real signature.
    """
    source = make_scan_pdf(path.with_name("签名底稿.pdf"), pages=2)
    with pikepdf.open(str(source)) as pdf:
        field = pikepdf.Dictionary(FT=pikepdf.Name("/Sig"), T="Signature1")
        if with_signature:
            field["/V"] = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name("/Sig")))
        pdf.Root.AcroForm = pdf.make_indirect(
            pikepdf.Dictionary(Fields=pikepdf.Array([pdf.make_indirect(field)])))
        pdf.save(str(path))
    source.unlink()
    return path


def make_box_variant_pdf(path: Path, variant: str) -> Path:
    """A scan whose page 1 has /Rotate=90 or a CropBox smaller than the MediaBox."""
    source = make_scan_pdf(path.with_name("版式底稿.pdf"), pages=2)
    with pikepdf.open(str(source)) as pdf:
        page = pdf.pages[0]
        box = [float(value) for value in page.obj.MediaBox]
        if variant == "rotate":
            page.obj.Rotate = 90
        else:
            page.obj.CropBox = [box[0] + 20, box[1] + 20, box[2] - 20, box[3] - 20]
        pdf.save(str(path))
    source.unlink()
    return path


def make_user_unit_pdf(path: Path, unit: int = 2) -> Path:
    """A scan whose points print unit times larger (/UserUnit, large-format drawings)."""
    source = make_scan_pdf(path.with_name("大图纸底稿.pdf"), pages=2)
    with pikepdf.open(str(source)) as pdf:
        for page in pdf.pages:
            page.obj.UserUnit = unit
        pdf.save(str(path))
    source.unlink()
    return path


def make_single_text_pdf(target: Path, name: str = "讲义.pdf") -> Path:
    target.mkdir(parents=True, exist_ok=True)
    return make_text_pdf(target / name)


# ---------------------------------------------------------------------------
# Running the supported CLI and reading its real artifacts.
# ---------------------------------------------------------------------------
def run_cli(*args, timeout: int = 300) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-I", "-B", str(CHAMO_CLI), *[str(a) for a in args]],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, stdin=subprocess.DEVNULL, env=environment)


def events_of(result: subprocess.CompletedProcess) -> list:
    return [json.loads(line) for line in result.stdout.splitlines()
            if line.strip().startswith("{")]


def finished_of(events: list) -> dict:
    return next((event for event in reversed(events) if event.get("type") == "finished"), {})


def report_of(events: list) -> dict:
    scan = next((event for event in events if event.get("type") == "scan"), {})
    record = scan.get("report")
    assert record, "scan 事件没有给出运行记录目录，无法核对报告中的字节数/保真标记"
    path = Path(record) / "report.json"
    assert path.is_file(), "报告缺失：%s" % path
    return json.loads(path.read_text(encoding="utf-8"))


def results_of(report: dict) -> list:
    results = report.get("results")
    assert isinstance(results, list) and results, "报告里没有 results 明细"
    return results


def item_events(events: list, state: str = None) -> list:
    return [event for event in events if event.get("type") == "item"
            and (state is None or event.get("state") == state)]


def files_under(root: Path) -> set:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def products_under(root: Path) -> dict:
    """The published products only: the dot-staging folder is not a deliverable."""
    return {name: root / name for name in files_under(root) if not name.startswith(STAGING + "/")}


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def page_count(path: Path) -> int:
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def page_texts(path: Path) -> list:
    return [page.extract_text() for page in PdfReader(str(path)).pages]


def page_sizes(path: Path) -> list:
    return [(float(page.mediabox.width), float(page.mediabox.height))
            for page in PdfReader(str(path)).pages]


def normalized_texts(path: Path) -> list:
    return [re.sub(r"\s+", " ", text).strip() for text in page_texts(path)]


def numbers_in(value) -> str:
    """All digits of a JSON record, with thousands separators removed.

    The engine's report schema is not ours to pin; the promise is only that the
    original and result byte counts are recorded. Accepting "2498" and "2,498"
    keeps that promise checkable without freezing a field name.
    """
    return re.sub(r"[,_ ]", "", json.dumps(value, ensure_ascii=False))


def assert_valid_pdf(path: Path, expected_pages: int, promise: str) -> None:
    assert path.is_file(), promise + "：成品不存在：%s" % path
    with pikepdf.open(path) as pdf:
        actual = len(pdf.pages)
    assert actual == expected_pages, (
        promise + "：成品页数 %d，原件页数 %d（文件 %s）" % (actual, expected_pages, path))


def pdfium_render(path: Path) -> list:
    """Render every page of the product with PDFium; returns grayscale spreads."""
    document = pdfium.PdfDocument(str(path))
    try:
        spreads = []
        for index in range(len(document)):
            image = document[index].render(scale=1.0).to_pil().convert("L")
            low, high = image.getextrema()
            spreads.append((image.size, int(high) - int(low)))
        return spreads
    finally:
        document.close()


# ---------------------------------------------------------------------------
# Contract gates
# ---------------------------------------------------------------------------
def test_cli_help_lists_the_pdf_compress_alias():
    completed = run_cli("--help")
    assert completed.returncode == 0, "chamo --help 必须成功：%r" % completed.stderr[-800:]
    assert "pdf-compress" in completed.stdout, (
        "CLI 别名承诺破裂：chamo --help 没有列出 pdf-compress；"
        "帮助里现有操作：%r" % sorted(re.findall(r"[a-z]+-[a-z]+", completed.stdout)))


def test_lossless_is_the_default_and_leaves_only_the_product(tmp_path):
    """Default (no --lossy) must clean the PDF losslessly, not render it."""
    source = make_single_text_pdf(tmp_path / "输入")
    original_size = source.stat().st_size
    original_texts = normalized_texts(source)
    assert original_texts and all(original_texts), "fixture 自身没有可抽取文本"

    completed = run_cli(source, "-o", tmp_path / "输出", "-t", "pdf-compress", "--json")
    events = events_of(completed)
    finished = finished_of(events)
    assert completed.returncode == 0, (
        "无损档承诺破裂：默认档（无 --lossy）应成功，实际退出码 %r；stdout=%r stderr=%r"
        % (completed.returncode, completed.stdout[-1200:], completed.stderr[-1200:]))
    assert finished.get("state") == "complete" and finished.get("success") == 1 \
        and finished.get("failed") == 0, "无损档承诺破裂：完成事件不是 1 成功 0 异常：%r" % finished

    published = products_under(tmp_path / "输出")
    product = tmp_path / "输出" / "讲义_压缩.pdf"
    assert set(published) == {"讲义_压缩.pdf"}, (
        "输出命名/单文件承诺破裂：输出目录应只有 <stem>_压缩.pdf，实际 %r" % sorted(published))
    assert not (tmp_path / "输出" / MANIFEST).exists(), (
        "单文件承诺破裂：单文件无损转换不应写清单.json")

    assert_valid_pdf(product, len(original_texts), "无损档页数承诺")
    product_texts = normalized_texts(product)
    assert len(product_texts) == len(original_texts)
    assert product_texts == original_texts, (
        "无损档文本承诺破裂：重写后文本应可抽取且与原件一致；原件 %r，成品 %r"
        % (original_texts, product_texts))
    assert product.stat().st_size <= original_size, (
        "无损档体积承诺破裂：重写不变小时必须发布与原件相同的副本，成品 %d 字节 > 原件 %d 字节"
        % (product.stat().st_size, original_size))

    report = report_of(events)
    results = results_of(report)
    record = results[0]
    assert record.get("state") == "complete", "报告结果状态应为 complete：%r" % record.get("state")
    recorded = numbers_in(record)
    for size, label in ((original_size, "原始字节数"), (product.stat().st_size, "结果字节数")):
        assert str(size) in recorded, (
            "字节数记录承诺破裂：报告 results[0] 没有记录%s %d；记录内容=%r"
            % (label, size, json.dumps(record, ensure_ascii=False)[:1500]))
    assert record.get("fidelity") != "lossy_user_selected", (
        "默认档保真承诺破裂：没有 --lossy 的运行被标记为 lossy_user_selected")


def test_lossless_no_gain_publishes_the_original_bytes(tmp_path):
    """When the rewrite cannot shrink, the published copy must be byte-identical."""
    source = make_fixpoint_pdf(tmp_path / "已最优.pdf")
    original = source.read_bytes()
    # Fixture precondition: a packaged-pikepdf rewrite really cannot shrink it.
    rewrite = io.BytesIO()
    pikepdf.open(io.BytesIO(original)).save(
        rewrite, compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate)
    assert len(rewrite.getvalue()) >= len(original), (
        "fixture 设计失败：该 PDF 还能被 pikepdf 重写变小（%d → %d），"
        "无法用来验收 no-gain 分支" % (len(original), len(rewrite.getvalue())))

    completed = run_cli(source, "-o", tmp_path / "输出", "-t", "pdf-compress", "--json")
    assert completed.returncode == 0, (
        "无损 no-gain 承诺破裂：重写不变小时仍应成功发布副本，实际退出码 %r；stderr=%r"
        % (completed.returncode, completed.stderr[-1200:]))
    product = tmp_path / "输出" / "已最优_压缩.pdf"
    assert_valid_pdf(product, 3, "无损 no-gain 页数承诺")
    published = product.read_bytes()
    assert published == original, (
        "无损 no-gain 承诺破裂：重写不变小时必须发布与原件逐字节相同的副本；"
        "原件 %d 字节，成品 %d 字节（文件 %s）"
        % (len(original), len(published), product))


def test_lossy_scan_keeps_geometry_and_rerenders(tmp_path):
    """Explicit --lossy: same pages, same physical page size (±1 pt), PDFium-renderable."""
    source = make_scan_pdf(tmp_path / "扫描.pdf")
    original_size = source.stat().st_size
    original_sizes = page_sizes(source)
    original_pages = len(original_sizes)

    completed = run_cli(source, "-o", tmp_path / "输出", "-t", "pdf-compress",
                        "--lossy", "--dpi", "100", "--quality", "60", "--json")
    events = events_of(completed)
    assert completed.returncode == 0, (
        "有损档承诺破裂：扫描件显式 --lossy 应成功，实际退出码 %r；stdout=%r stderr=%r"
        % (completed.returncode, completed.stdout[-1200:], completed.stderr[-1200:]))

    published = products_under(tmp_path / "输出")
    product = tmp_path / "输出" / "扫描_压缩.pdf"
    assert set(published) == {"扫描_压缩.pdf"}, (
        "有损档输出承诺破裂：输出目录应只有 扫描_压缩.pdf，实际 %r" % sorted(published))
    assert_valid_pdf(product, original_pages, "有损档页数承诺")

    sizes = page_sizes(product)
    for index, (before, after) in enumerate(zip(original_sizes, sizes)):
        for axis, original_value, product_value in zip(("宽", "高"), before, after):
            assert abs(product_value - original_value) <= 1.0, (
                "有损档尺寸承诺破裂：第 %d 页%s度原件 %.2fpt，成品 %.2fpt，误差 %.2fpt > 1pt"
                % (index + 1, axis, original_value, product_value,
                   abs(product_value - original_value)))

    renders = pdfium_render(product)
    assert len(renders) == original_pages, "有损档渲染承诺破裂：PDFium 打开的页数不符"
    for index, (size, spread) in enumerate(renders):
        assert size[0] > 0 and size[1] > 0, "有损档渲染承诺破裂：第 %d 页渲染为空" % (index + 1)
        assert spread > 10, (
            "有损档渲染承诺破裂：第 %d 页渲染后没有任何内容变化（灰度极差 %d）"
            % (index + 1, spread))

    assert product.stat().st_size < original_size, (
        "有损档收益承诺破裂：重建结果不小于原件时不得发布成品；原件 %d 字节，成品 %d 字节"
        % (original_size, product.stat().st_size))
    assert sha(product) != sha(source), "有损档承诺破裂：发布的是原件副本而不是重新构建的 PDF"

    report = report_of(events)
    record = results_of(report)[0]
    assert record.get("fidelity") == "lossy_user_selected", (
        "有损档保真标记承诺破裂：报告 results[0].fidelity=%r，期望 lossy_user_selected"
        % record.get("fidelity"))
    outputs = record.get("outputs") or []
    assert outputs and all(row.get("fidelity") == "lossy_user_selected" for row in outputs), (
        "有损档保真标记承诺破裂：成品行的 fidelity 应为 lossy_user_selected：%r" % outputs)


def test_lossy_no_gain_is_refused_with_a_readable_reason(tmp_path):
    """If the rebuild would not be smaller, the item fails and nothing is published."""
    source = make_fixpoint_pdf(tmp_path / "太小.pdf")
    original_size = source.stat().st_size
    original = source.read_bytes()

    completed = run_cli(source, "-o", tmp_path / "输出", "-t", "pdf-compress",
                        "--lossy", "--json")
    events = events_of(completed)
    assert completed.returncode == 1, (
        "有损 no-gain 承诺破裂：重建不小于原件时应作为该项失败（退出码 1），实际 %r；"
        "stdout=%r stderr=%r"
        % (completed.returncode, completed.stdout[-1200:], completed.stderr[-1200:]))
    published = products_under(tmp_path / "输出")
    assert published == {}, "有损 no-gain 承诺破裂：不小于原件时不得写成品，实际 %r" % sorted(published)
    assert source.read_bytes() == original, "有损 no-gain 承诺破裂：原件被改动"

    failures = item_events(events, "failed")
    assert failures, "有损 no-gain 承诺破裂：没有给出失败项事件：%r" % events[-5:]
    detail = failures[0].get("detail") or ""
    assert detail, "有损 no-gain 承诺破裂：失败项没有可读原因"
    assert "字节" in detail and str(original_size) in numbers_in(detail), (
        "有损 no-gain 承诺破裂：可读原因应写出原始字节数；实际 %r（原件 %d 字节）"
        % (detail, original_size))
    report = report_of(events)
    record = results_of(report)[0]
    assert record.get("state") == "failed" and record.get("error"), (
        "有损 no-gain 承诺破裂：报告应记录该项失败与原因，实际 %r" % record)


def test_encrypted_pdf_is_rejected_readably(tmp_path):
    base = make_fixpoint_pdf(tmp_path / "基础.pdf")
    source = make_encrypted_pdf(tmp_path / "加密.pdf", base)
    assert PdfReader(str(source)).is_encrypted, "fixture 设计失败：生成的 PDF 未加密"
    original = source.read_bytes()

    completed = run_cli(source, "-o", tmp_path / "输出", "-t", "pdf-compress", "--json")
    events = events_of(completed)
    assert completed.returncode == 1, (
        "加密 PDF 承诺破裂：应以该项失败（退出码 1）拒绝，实际 %r；stdout=%r stderr=%r"
        % (completed.returncode, completed.stdout[-1200:], completed.stderr[-1200:]))
    assert products_under(tmp_path / "输出") == {}, "加密 PDF 承诺破裂：不得写成品"
    assert source.read_bytes() == original, "加密 PDF 承诺破裂：原件被改动"
    failures = item_events(events, "failed")
    assert failures, "加密 PDF 承诺破裂：没有可读的失败项事件"
    detail = failures[0].get("detail") or ""
    assert any(word in detail for word in ("加密", "密码")), (
        "加密 PDF 承诺破裂：错误信息必须说明是加密 PDF；实际 %r" % detail)


def test_an_existing_same_named_file_is_never_overwritten(tmp_path):
    source = make_single_text_pdf(tmp_path / "输入")
    out = tmp_path / "输出"
    out.mkdir(parents=True)
    occupied = out / "讲义_压缩.pdf"
    marker = b"%PDF-1.4 user-owned file that must survive\n"
    occupied.write_bytes(marker)

    completed = run_cli(source, "-o", out, "-t", "pdf-compress", "--json")
    assert completed.returncode == 0, (
        "同名不覆盖承诺破裂：转换本身应成功，实际退出码 %r；stderr=%r"
        % (completed.returncode, completed.stderr[-1200:]))
    assert occupied.read_bytes() == marker, (
        "同名不覆盖承诺破裂：已存在的 讲义_压缩.pdf 被覆盖")
    stepped = out / "讲义_压缩 (2).pdf"
    assert stepped.is_file(), (
        "同名不覆盖承诺破裂：新成品应命名为「讲义_压缩 (2).pdf」，实际目录 %r"
        % sorted(products_under(out)))
    assert_valid_pdf(stepped, 3, "同名不覆盖承诺")


def test_a_batch_writes_exactly_one_manifest_with_correct_counts(tmp_path):
    sources = make_single_text_pdf(tmp_path / "批量", "甲.pdf")
    second = make_text_pdf(tmp_path / "批量" / "乙.pdf", pages=2)
    out = tmp_path / "输出"

    completed = run_cli(sources, second, "-o", out, "-t", "pdf-compress", "--json")
    assert completed.returncode == 0, (
        "批量承诺破裂：两个 PDF 的无损批量应成功，实际退出码 %r；stderr=%r"
        % (completed.returncode, completed.stderr[-1200:]))

    manifest_names = [name for name in files_under(out) if Path(name).name == MANIFEST]
    assert manifest_names == [MANIFEST], (
        "批量清单承诺破裂：批量输出必须恰好一个 清单.json，实际 %r" % sorted(files_under(out)))
    assert set(products_under(out)) == {"甲_压缩.pdf", "乙_压缩.pdf", MANIFEST}, (
        "批量输出承诺破裂：应只有两个成品与一个清单，实际 %r" % sorted(files_under(out)))

    manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    assert manifest.get("任务数") == 2 and manifest.get("成功") == 2 \
        and manifest.get("异常") == 0 and manifest.get("成品数") == 2, (
        "批量清单计数破裂：任务数/成功/异常/成品数 = %r/%r/%r/%r"
        % (manifest.get("任务数"), manifest.get("成功"), manifest.get("异常"),
           manifest.get("成品数")))
    assert manifest.get("操作") == "pdf_compress", (
        "批量清单操作破裂：操作应为 pdf_compress，实际 %r" % manifest.get("操作"))
    details = manifest.get("明细") or []
    assert len(details) == 2, "批量清单明细破裂：应有 2 条明细，实际 %d" % len(details)
    assert all(entry.get("状态") == "complete" and entry.get("成品") for entry in details), (
        "批量清单明细破裂：每条明细都应有状态与成品：%r" % details)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_cancel_leaves_no_partial_product_or_manifest(tmp_path):
    """Ctrl-C mid-run: EXIT_CANCELLED, nothing published, no process still writing."""
    source = make_dense_text_pdf(tmp_path / "长文档.pdf")
    original = source.read_bytes()
    out = tmp_path / "输出"
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-I", "-B", str(CHAMO_CLI), str(source), "-o", str(out),
         "-t", "pdf-compress", "--lossy", "--dpi", "300", "--quality", "75", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", stdin=subprocess.DEVNULL, env=environment)

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and proc.poll() is None and not (out / STAGING).exists():
        time.sleep(0.05)
    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        pytest.fail(
            "取消承诺无法验证：转换在取消前就结束了（退出码 %r）。当前实现可能还不支持 "
            "pdf-compress；stdout=%r stderr=%r" % (proc.returncode, stdout[-800:], stderr[-800:]))
    assert (out / STAGING).exists(), (
        "取消承诺无法验证：引擎已启动但从未进入暂存目录，无法证明取消路径")
    time.sleep(0.5)
    assert proc.poll() is None, "取消承诺无法验证：取消前转换已经完成，取消路径未被证明"
    proc.send_signal(signal.SIGINT)
    try:
        _, stderr = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("取消承诺破裂：Ctrl-C 后 CLI 120 秒内没有退出")
    assert proc.returncode == 3, (
        "取消承诺破裂：Ctrl-C 应以 EXIT_CANCELLED(3) 退出，实际 %r；stderr=%r"
        % (proc.returncode, stderr[-1200:]))
    assert "Traceback" not in stderr, "取消承诺破裂：Ctrl-C 打印了 traceback：%r" % stderr[-1200:]

    settle = time.monotonic() + 20
    while time.monotonic() < settle and files_under(out):
        time.sleep(0.2)
    assert files_under(out) == set(), (
        "取消承诺破裂：取消后输出目录仍留下文件 %r" % sorted(files_under(out)))
    assert not (out / MANIFEST).exists(), "取消承诺破裂：取消后留下清单.json"
    assert source.read_bytes() == original, "取消承诺破裂：原件被改动"

    # No engine process may keep writing into that folder after the CLI returned.
    gone = time.monotonic() + 30
    leftovers = []
    while time.monotonic() < gone:
        listing = subprocess.run(["ps", "-axo", "command"], capture_output=True,
                                 text=True).stdout
        leftovers = [line for line in listing.splitlines()
                     if "workbench.py" in line and str(out) in line]
        if not leftovers:
            break
        time.sleep(0.3)
    assert not leftovers, "取消承诺破裂：CLI 退出后仍有引擎进程在写输出目录：%r" % leftovers


def test_a_repeated_identical_run_is_idempotent_and_resumes(tmp_path):
    source = make_single_text_pdf(tmp_path / "输入")
    out = tmp_path / "输出"
    first = run_cli(source, "-o", out, "-t", "pdf-compress", "--json")
    assert first.returncode == 0, "fixture 前置失败：第一次运行未成功：%r" % first.stderr[-800:]
    product = out / "讲义_压缩.pdf"
    first_bytes = product.read_bytes()

    second = run_cli(source, "-o", out, "-t", "pdf-compress", "--json")
    second_events = events_of(second)
    finished = finished_of(second_events)
    assert second.returncode == 0, (
        "幂等承诺破裂：相同请求第二次运行应成功，实际退出码 %r；stderr=%r"
        % (second.returncode, second.stderr[-1200:]))
    assert finished.get("success") == 1 and finished.get("failed") == 0, (
        "幂等承诺破裂：第二次运行完成事件应 1 成功 0 异常：%r" % finished)
    items = item_events(second_events)
    states = [event.get("state") for event in items]
    # Every job first emits state=running (existing engine behaviour for all
    # operations) and then its final state, so look for the final resumed event
    # instead of pinning the first one.
    assert "resumed" in states, (
        "续跑复用承诺破裂：第二次相同运行应复用既有记录（应出现 item.state=resumed），"
        "实际条目状态 %r" % states)
    assert "failed" not in states, "续跑复用承诺破裂：第二次运行出现失败条目：%r" % states
    assert set(products_under(out)) == {"讲义_压缩.pdf"}, (
        "幂等承诺破裂：第二次运行不得再发布一份副本，实际 %r" % sorted(files_under(out)))
    assert product.read_bytes() == first_bytes, "幂等承诺破裂：第二次运行改动了成品字节"


def test_dpi_and_quality_are_refused_without_lossy(tmp_path):
    """--dpi/--quality exist only under --lossy; otherwise: readable, pre-execution error."""
    source = make_single_text_pdf(tmp_path / "输入")
    # Precondition: the lossless operation itself must work here. Without this,
    # an unknown-operation usage error (invalid choice) would masquerade as the
    # parameter being refused and this gate would pass for the wrong reason.
    baseline = run_cli(source, "-o", tmp_path / "输出-基线", "-t", "pdf-compress", "--json")
    assert baseline.returncode == 0, (
        "fixture 前置失败：无损档本身应能成功，实际退出码 %r；stderr=%r"
        % (baseline.returncode, baseline.stderr[-1000:]))
    cases = ((("--dpi", "100"), "dpi"),
             (("--quality", "60"), "quality"))
    for index, (flags, parameter) in enumerate(cases):
        out = tmp_path / ("输出-%d" % index)
        completed = run_cli(source, "-o", out, "-t", "pdf-compress", *flags, "--json")
        text = completed.stdout + "\n" + completed.stderr
        assert completed.returncode == 2, (
            "参数前置校验承诺破裂：无损档带 %r 应在执行前以用法错误（退出码 2）拒绝，"
            "实际退出码 %r；输出=%r" % (list(flags), completed.returncode, text[-1200:]))
        assert "invalid choice" not in text, (
            "参数前置校验承诺破裂：拒绝 %r 的原因不是参数只用于有损档，而是操作本身"
            "不被识别（invalid choice）；输出=%r" % (list(flags), text[-1200:]))
        assert "有损" in text or "lossy" in text.lower(), (
            "参数前置校验承诺破裂：拒绝 %r 时没有说明它只用于有损档；输出=%r"
            % (list(flags), text[-1200:]))
        assert parameter in text.lower() or "质量" in text, (
            "参数前置校验承诺破裂：拒绝 %r 时没有指出是哪个参数；输出=%r"
            % (list(flags), text[-1200:]))
        assert products_under(out) == {}, (
            "参数前置校验承诺破裂：拒绝 %r 前已经执行并产出了文件 %r"
            % (list(flags), sorted(files_under(out))))


def test_lossy_preserves_user_unit_page_size(tmp_path):
    """Large-format pages: dropping /UserUnit would silently halve the printed size."""
    source = make_user_unit_pdf(tmp_path / "大图纸.pdf")
    completed = run_cli(source, "-o", tmp_path / "输出", "-t", "pdf-compress",
                        "--lossy", "--dpi", "100", "--quality", "60", "--json")
    assert completed.returncode == 0, (
        "大图纸承诺破裂：/UserUnit=2 的扫描件有损压缩应成功，实际退出码 %r；stderr=%r"
        % (completed.returncode, completed.stderr[-1200:]))
    product = tmp_path / "输出" / "大图纸_压缩.pdf"
    assert product.is_file(), "大图纸承诺破裂：没有生成 大图纸_压缩.pdf"
    with pikepdf.open(str(source)) as before, pikepdf.open(str(product)) as after:
        assert len(before.pages) == len(after.pages), "大图纸承诺破裂：页数不一致"
        for index, (source_page, product_page) in enumerate(zip(before.pages, after.pages), 1):
            source_unit = float(source_page.obj.get("/UserUnit", 1) or 1)
            product_unit = float(product_page.obj.get("/UserUnit", 1) or 1)
            source_box = [float(value) for value in source_page.obj.MediaBox]
            product_box = [float(value) for value in product_page.obj.MediaBox]
            assert abs(product_unit - source_unit) < 1e-6, (
                "大图纸承诺破裂：第 %d 页丢了 /UserUnit（原件 %r，成品 %r），物理尺寸会被悄悄减半"
                % (index, source_unit, product_unit))
            expected = ((source_box[2] - source_box[0]) * source_unit,
                        (source_box[3] - source_box[1]) * source_unit)
            actual = ((product_box[2] - product_box[0]) * product_unit,
                      (product_box[3] - product_box[1]) * product_unit)
            for axis, want, got in zip(("宽", "高"), expected, actual):
                assert abs(want - got) <= 1.0, (
                    "大图纸承诺破裂：第 %d 页%s物理尺寸原件 %.2fpt，成品 %.2fpt，误差 > 1pt"
                    % (index, axis, want, got))
    assert product.stat().st_size < source.stat().st_size, "大图纸承诺破裂：成品没有变小"


@pytest.mark.parametrize("variant,needle", [("rotate", "旋转"), ("cropbox", "裁切框")])
def test_lossy_refuses_layout_it_cannot_rebuild_while_lossless_keeps_the_boxes(
        tmp_path, variant, needle):
    """Advertised refusal: /Rotate or a smaller CropBox is refused by lossy only."""
    source = make_box_variant_pdf(tmp_path / "特殊版式.pdf", variant)
    original = source.read_bytes()

    lossy_out = tmp_path / "有损输出"
    lossy = run_cli(source, "-o", lossy_out, "-t", "pdf-compress",
                    "--lossy", "--dpi", "100", "--quality", "60", "--json")
    assert lossy.returncode == 1, (
        "版式拒绝承诺破裂：%s 的 PDF 有损档应失败，实际退出码 %r；stderr=%r"
        % (variant, lossy.returncode, lossy.stderr[-1200:]))
    assert products_under(lossy_out) == {}, "版式拒绝承诺破裂：拒绝时不得写成品"
    failures = item_events(events_of(lossy), "failed")
    assert failures, "版式拒绝承诺破裂：没有给出失败项事件"
    detail = failures[0].get("detail") or ""
    assert needle in detail, "版式拒绝承诺破裂：失败原因应说明「%s」，实际 %r" % (needle, detail)
    assert source.read_bytes() == original, "版式拒绝承诺破裂：原件被改动"

    lossless_out = tmp_path / "无损输出"
    lossless = run_cli(source, "-o", lossless_out, "-t", "pdf-compress", "--json")
    assert lossless.returncode == 0, (
        "无损保框承诺破裂：同一文件的无损档应成功，实际退出码 %r；stderr=%r"
        % (lossless.returncode, lossless.stderr[-1200:]))
    product = lossless_out / "特殊版式_压缩.pdf"
    assert product.is_file(), "无损保框承诺破裂：没有生成成品"
    with pikepdf.open(str(source)) as before, pikepdf.open(str(product)) as after:
        first_before, first_after = before.pages[0], after.pages[0]
        if variant == "rotate":
            assert int(first_after.obj.get("/Rotate", 0) or 0) == 90, (
                "无损保框承诺破裂：/Rotate 没有被保留")
        else:
            want = [float(value) for value in first_before.obj.CropBox]
            got = [float(value) for value in first_after.obj.CropBox]
            assert all(abs(a - b) <= 0.5 for a, b in zip(want, got)), (
                "无损保框承诺破裂：/CropBox 没有被保留（原件 %r，成品 %r）" % (want, got))


def test_signed_pdf_is_refused_in_both_modes(tmp_path):
    """A real signature must not be silently destroyed by either compression mode."""
    source = make_signed_pdf(tmp_path / "已签名.pdf", with_signature=True)
    original = source.read_bytes()
    for index, (flags, label) in enumerate((((), "无损"), (("--lossy",), "有损"))):
        out = tmp_path / ("输出-%d" % index)
        completed = run_cli(source, "-o", out, "-t", "pdf-compress", *flags, "--json")
        assert completed.returncode == 1, (
            "签名拒绝承诺破裂：%s档应拒绝已签名 PDF，实际退出码 %r；stderr=%r"
            % (label, completed.returncode, completed.stderr[-1200:]))
        assert products_under(out) == {}, "签名拒绝承诺破裂：%s档拒绝时不得写成品" % label
        failures = item_events(events_of(completed), "failed")
        assert failures, "签名拒绝承诺破裂：%s档没有给出失败项事件" % label
        detail = failures[0].get("detail") or ""
        assert "数字签名" in detail, (
            "签名拒绝承诺破裂：%s档失败原因应说明数字签名，实际 %r" % (label, detail))
    assert source.read_bytes() == original, "签名拒绝承诺破裂：原件被改动"


def test_an_unsigned_signature_field_is_not_treated_as_a_signature(tmp_path):
    """An empty /FT /Sig placeholder (no /V) certifies nothing and must not block."""
    source = make_signed_pdf(tmp_path / "待填表单.pdf", with_signature=False)
    completed = run_cli(source, "-o", tmp_path / "输出", "-t", "pdf-compress", "--json")
    assert completed.returncode == 0, (
        "空签域承诺破裂：只有空 /FT /Sig 域、没有 /V 的 PDF 不应被当成已签名拒绝，"
        "实际退出码 %r；stderr=%r" % (completed.returncode, completed.stderr[-1200:]))
    assert (tmp_path / "输出" / "待填表单_压缩.pdf").is_file(), "空签域承诺破裂：没有生成成品"
