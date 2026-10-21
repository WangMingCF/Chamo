import hashlib
import json
from pathlib import Path
import shutil

from .config import Limits


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def dump(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remove_temporary(path: Path):
    def onerror(function, failed_path, exception):
        # macOS/exFAT can remove AppleDouble sidecars as the data file is removed.
        # Missing children are already cleaned; do not turn valid output into a failure.
        if not isinstance(exception[1], FileNotFoundError):
            raise exception[1]
    shutil.rmtree(path, onerror=onerror)


def copy_digest(source: Path, destination: Path) -> str:
    result = hashlib.sha256()
    with source.open("rb") as src, destination.open("xb") as dest:
        while block := src.read(4 * 1024 * 1024):
            dest.write(block)
            result.update(block)
    return result.hexdigest()


def validate(path: Path, category: str, limits: Limits) -> dict:
    """Measure final encoded bytes and structure, never just estimate an input ratio."""
    size = path.stat().st_size
    cap = getattr(limits, f"{category}_bytes")
    if category == "document" and path.suffix.lower() == ".pdf":
        cap = limits.pdf_byte_cap
    if not size or size > cap:
        raise ValueError(f"{path.name} 实际大小 {size} 不符合 1..{cap} 字节限制")
    facts = {"bytes": size}
    if category == "document" and path.suffix.lower() != ".pdf":
        from .native import inspect_office
        facts.update(inspect_office(path))
        pages = facts.get("pages_metadata")
        if pages is not None and pages > limits.document_pages:
            raise ValueError("Office 源页数元数据超限")
    elif category == "document":
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(path) as doc:
            facts["pages"] = len(doc)
        if not 0 < facts["pages"] <= limits.pdf_page_cap:
            raise ValueError("PDF 页数超过限制或为空")
    elif category == "image":
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            w, h = image.size
        facts.update(width=w, height=h)
        if min(w, h) < limits.image_min_edge or max(w, h) > limits.image_max_edge or max(w, h) / min(w, h) >= limits.image_max_ratio:
            raise ValueError("图片尺寸或长宽比超限")
    elif category == "table":
        from .native import inspect_table
        if path.suffix.lower() == ".csv":
            import csv
            with path.open(encoding="utf-8-sig", newline="") as handle:
                facts["rows"] = sum(1 for _ in csv.reader(handle))
        else:
            facts.update(inspect_table(path))
        if facts["rows"] > limits.table_rows:
            raise ValueError("表格行数超限")
    elif category == "text":
        # Streaming decode catches accidental UTF-8 cuts and malformed output.
        with path.open(encoding="utf-8") as handle:
            while handle.read(65536):
                pass
    elif category in {"audio", "video"}:
        from .media import probe
        info = probe(path)
        facts["duration_seconds"] = info["duration"]
        if category == "video" and not info["video"]:
            raise ValueError("输出文件缺少视频流")
        if category == "audio" and not info["audio"]:
            raise ValueError("输出文件缺少音频流")
    return facts


class Sink:
    def __init__(self, directory: Path, source: Path, limits: Limits, tree_relative: Path | None = None):
        self.directory = directory
        self.source = source
        self.limits = limits
        self.count = 0
        self.manifest = (directory / "parts.jsonl").open("w", encoding="utf-8")
        self.notices = []
        self.tree_relative = tree_relative
        self.copied_source_hash = None

    def notice(self, message: str):
        if message not in self.notices:
            self.notices.append(message)

    def emit(self, candidate: Path, category: str, locator: dict, verified: dict | None = None):
        # For byte-identical copies, reuse the just-completed source inspection. Derived
        # files still get a fresh parse; copying checksums and final verify protect transport.
        facts = {"bytes": candidate.stat().st_size, **verified} if verified is not None else validate(candidate, category, self.limits)
        cap = self.limits.pdf_byte_cap if category == "document" and candidate.suffix.lower() == ".pdf" else getattr(self.limits, f"{category}_bytes")
        if not 0 < facts["bytes"] <= cap:
            raise ValueError("已校验原件在复制前大小超限")
        if category == "document" and candidate.suffix.lower() == ".pdf" and facts.get("pages", 0) > self.limits.pdf_page_cap:
            raise ValueError("已校验 PDF 页数超限")
        self.count += 1
        batch = (self.count - 1) // self.limits.batch_files + 1
        relative = Path("upload") / f"batch_{batch:06d}" / f"{self.source.stem}_part{self.count:06d}{candidate.suffix.lower()}"
        if self.tree_relative is not None:
            # Include the original extension to distinguish e.g. a.pdf and a.docx.
            relative = Path("files") / self.tree_relative.parent / f"{self.source.name}_part{self.count:06d}{candidate.suffix.lower()}"
        destination = self.directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        checksum = copy_digest(candidate, destination)
        if candidate == self.source:
            self.copied_source_hash = checksum
        record = {"file": relative.as_posix(), "category": category,
                  "sha256": checksum, **facts, "source": locator, "batch": batch}
        self.manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.manifest.flush()

    def close(self):
        self.manifest.close()


def verify_job(directory: Path, checksum: bool = True) -> dict:
    info = json.loads((directory / "job.json").read_text(encoding="utf-8"))
    # Historical manifests retain the caps under which they were produced. New
    # defaults must not retroactively invalidate an archive's integrity check.
    legacy = info["limits"]
    limits = Limits(**{"pdf_bytes": legacy.get("pdf_bytes", legacy["document_bytes"]),
                      "pdf_pages": legacy.get("pdf_pages", legacy["document_pages"]), **legacy})
    if info.get("status") != "complete":
        raise ValueError(f"{directory.name}: 任务未完成")
    expected = 0
    base = Path(info.get("upload_root", directory))
    with (directory / "parts.jsonl").open(encoding="utf-8") as manifest:
        for line in manifest:
            record = json.loads(line)
            expected += 1
            path = base / record["file"]
            if not path.resolve().is_relative_to(base.resolve()):
                raise ValueError("清单路径越界")
            if checksum and digest(path) != record["sha256"]:
                raise ValueError(f"{path.name}: SHA-256 不匹配")
            facts = validate(path, record["category"], limits)
            for key, value in facts.items():
                if record.get(key) != value:
                    raise ValueError(f"{path.name}: {key} 与清单不一致")
    actual = expected if "upload_root" in info else sum(1 for path in (directory / "upload").rglob("*") if path.is_file())
    if expected != info["parts"] or actual != expected:
        raise ValueError("输出数量与清单不一致")
    if "upload_root" not in info:
        for batch in (directory / "upload").iterdir():
            if not batch.is_dir() or sum(1 for p in batch.iterdir() if p.is_file()) > limits.batch_files:
                raise ValueError("单个上传批次文件数超限")
    return {"job": directory.name, "parts": expected, "status": "verified"}
