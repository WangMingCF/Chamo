import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor

from . import __version__, images, media, office, pdf, tables, text, native
from .config import Limits
from .output import Sink, digest, dump, verify_job, remove_temporary


FORMATS = {
    "document": [".pdf", ".doc", ".docx", ".ppt", ".pptx", ".odt", ".odp", ".rtf"],
    "table": [".xls", ".xlsx", ".csv"],
    "text": [".txt", ".md", ".markdown", ".html", ".htm", ".log"],
    "image": [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"],
    "audio": [".aac", ".amr", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"],
    "video": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".mpg", ".mpeg", ".webm"],
}


def override(raw):
    key, separator, value = raw.partition("=")
    if not separator:
        raise ValueError("--set 需要 key=value")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(KB|MB|GB|KiB|MiB|GiB)", value, re.I)
    if match:
        factor = {"kb": 1000, "mb": 1000**2, "gb": 1000**3,
                  "kib": 1024, "mib": 1024**2, "gib": 1024**3}[match[2].lower()]
        return key, int(float(match[1]) * factor)
    return key, json.loads(value)


def process(source: Path, root: Path, limits: Limits, options: dict, resume: bool,
            tree_relative: Path | None = None, upload_root: Path | None = None,
            work_root: Path | None = None):
    source = source if tree_relative is not None else source.resolve()
    if not source.is_file():
        raise ValueError("输入不是普通文件")
    suffix = source.suffix.lower()
    category = next((name for name, extensions in FORMATS.items() if suffix in extensions), None)
    if category is None:
        raise ValueError(f"不支持 {suffix or '无扩展名'}，运行 kb-pack formats 查看支持类型")
    before = source.stat()
    # Tree mode identifies immutable inputs using size/mtime for fast resume on 700GB volumes.
    # SHA-256 is still recorded; a copied original is hashed during its only copy pass.
    source_hash = None if tree_relative is not None else digest(source)
    identity = {"source": str(source), "sha256": source_hash, "size": before.st_size,
                "mtime_ns": before.st_mtime_ns, "limits": asdict(limits),
                "options": {k: v for k, v in options.items() if k != "fast_resume"}, "version": __version__}
    # Policy changes only invalidate outputs that actually changed. Native PDF copies
    # from the earlier policy remain byte-identical and can be safely reused.
    identity["format_policy"] = "native_information_preserving_v2"
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    job_name = f"{source.stem[:80]}_{suffix.lstrip('.')}_{fingerprint}"
    destination = root / job_name
    if destination.exists():
        if not resume:
            raise ValueError("同一输入/配置的输出已存在，使用 --resume 校验后跳过，或指定新输出目录")
        result = verify_job(destination, checksum=not options.get("fast_resume", False))
        return {**result, "status": "resumed", "source": str(source)}
    # Only a fully validated source appears in an upload directory. Failed partial files
    # stay in an isolated temporary transaction and never masquerade as successful output.
    with tempfile.TemporaryDirectory(prefix=".kb-pack-", dir=work_root or root) as staging:
        directory = Path(staging)
        work = directory / ".work"
        work.mkdir()
        sink = Sink(directory, source, limits, tree_relative)
        undo_publication = None
        try:
            if category == "document":
                if suffix != ".pdf":
                    native.pack(source, work, sink, options, category)
                else:
                    pdf.pack(source, work, sink, options)
            elif category == "table":
                if options.get("policy", "strict") == "strict":
                    native.pack(source, work, sink, options, category)
                else:
                    current = source
                    if suffix == ".xls":
                        current = office.convert(source, work / "converted", "xlsx", options["soffice"], options["timeout"])
                    tables.pack(current, work, sink, options)
            elif category == "image":
                if options.get("policy", "strict") == "strict":
                    native.pack(source, work, sink, options, category)
                else:
                    images.pack(source, work, sink, options)
            elif category == "text":
                if options.get("policy", "strict") == "strict" and source.stat().st_size <= limits.text_bytes:
                    if suffix == ".log":
                        import shutil
                        candidate = work / "log.txt"
                        shutil.copyfile(source, candidate)
                        sink.emit(candidate, "text", {"operation": "copy_native_rename_extension"})
                    else:
                        sink.emit(source, "text", {"operation": "copy_native"})
                elif options.get("policy", "strict") == "strict" and suffix in {".html", ".htm"}:
                    raise ValueError("HTML 超限，不能默认删除图片、链接或脚本内容，保留原件")
                elif options.get("policy", "strict") == "strict" and suffix in {".md", ".markdown"}:
                    from .markdown import pack
                    pack(source, work, sink, options)
                else:
                    text.pack(source, work, sink, options)
            else:
                metadata = media.probe(source)
                if not metadata["video"] and metadata["audio"]:
                    category = "audio"
                if options.get("policy", "strict") == "strict" and source.stat().st_size > getattr(limits, f"{category}_bytes"):
                    raise ValueError("音视频超限，无法确认章节、全部音轨、字幕和时序无损拆分，保留原件")
                media.pack(source, work, sink, options, category)
            if not sink.count:
                raise ValueError("没有生成可上传内容")
            # Detect source changes during a long conversion instead of issuing false provenance.
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise ValueError("输入文件在处理期间发生变化，请重新运行")
            actual_hash = sink.copied_source_hash or digest(source)
            if source_hash is not None and actual_hash != source_hash:
                raise ValueError("输入文件在处理期间发生变化，请重新运行")
            identity["sha256"] = actual_hash
            info = {**identity, "status": "complete", "parts": sink.count, "notices": sink.notices}
            sink.close()
            if tree_relative is not None:
                from .tree import publish
                undo_publication = publish(directory, upload_root, info, replace_existing=options.get("replace_existing", False))
            dump(directory / "job.json", info)
            # Work products are never exposed as additional upload candidates.
            remove_temporary(work)
            try:
                directory.rename(destination)
            except OSError as error:
                import errno, shutil
                if error.errno != errno.EXDEV:
                    raise
                # Local SSD staging leaves just small provenance files to publish here.
                shutil.copytree(directory, destination)
        except BaseException:
            if undo_publication:
                undo_publication()
                if destination.exists():
                    remove_temporary(destination)
            raise
        finally:
            sink.close()
    return {"job": job_name, "status": "complete", "parts": info["parts"], "source": str(source), "notices": info["notices"]}


def parser():
    command = argparse.ArgumentParser(description="按云知识库限制拆分、转换和压缩本地文件")
    command.add_argument("--version", action="version", version=__version__)
    actions = command.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init-config", help="写入默认限制（PDF 100 页/100 MB），之后可自行修改")
    init.add_argument("path", type=Path)
    actions.add_parser("formats", help="查看支持类型与默认限制")
    actions.add_parser("doctor", help="检查可选 Office / 音视频依赖")
    verify = actions.add_parser("verify", help="复查输出哈希、大小、页数、行数和尺寸")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--workers", type=int, default=0, help="校验并发，0 自动，1..16 可指定")
    merge = actions.add_parser('merge-ocr', help='归并 OCR 输出树，备份原目录并同步活动清单和本地解析状态')
    merge.add_argument('directory', type=Path)
    merge.add_argument('--apply', action='store_true', help='实际执行；默认只盘点')
    merge.add_argument('--workers', type=int, default=4)
    pack = actions.add_parser("pack", help="生成独立可上传的文件及来源清单")
    pack.add_argument("inputs", nargs="+", type=Path)
    pack.add_argument("-o", "--out", type=Path, required=True)
    pack.add_argument("--config", type=Path)
    pack.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    pack.add_argument("--recursive", action="store_true", help="递归读取输入目录")
    pack.add_argument("--resume", action="store_true", help="完整校验并跳过已完成文件；失败文件重做")
    pack.add_argument("--preserve-tree", action="store_true", help="保留输入目录结构；失败原件存入 _待重处理")
    pack.add_argument("--exclude", action="append", type=Path, default=[], help="排除目录，可重复；输出目录自动排除")
    pack.add_argument("--include", action="append", type=Path, default=[], help="目录模式仅处理指定子目录，可重复；仍按输入根保持原路径")
    pack.add_argument("--fast-resume", action="store_true", help="恢复时只复查大小/结构，省略重复 SHA-256 扫描")
    pack.add_argument("--exclude-type", action="append", choices=sorted(FORMATS), default=[], help="排除类型，可重复；排除文件不会复制到失败目录")
    pack.add_argument("--workers", type=int, default=0, help="目录模式进程数，0 自动按 CPU 选择（最多 8）")
    pack.add_argument("--work-dir", type=Path, help="临时处理目录；默认用系统临时目录的本地 SSD，成品仍写入输出目录")
    pack.add_argument("--retry-report", type=Path, help="目录模式只重试指定 JSONL 报告中的失败源文件")
    pack.add_argument("--replace-existing", action="store_true", help="目录模式新结果验证后归档旧成品和清单；失败恢复旧成品")
    pack.add_argument("--pdf-mode", choices=["split", "images"], default="split")
    pack.add_argument("--policy", choices=["strict", "derive"], default="strict", help="strict 保持信息；derive 允许图片/表格等派生处理，Word/PPT 始终保留原格式")
    pack.add_argument("--dpi", type=int, default=144, help="PDF 转图片的目标 DPI")
    pack.add_argument("--render-pixels", type=int, default=16_000_000, help="单页渲染像素预算，必要时自动降低 DPI")
    pack.add_argument("--page-window", type=int, default=100, help="PDF 单次导入页数上限")
    pack.add_argument("--header-rows", type=int, default=1, help="每个表格分片重复的表头行数，0 为不重复")
    pack.add_argument("--encoding", default="utf-8-sig", help="文本/CSV/HTML 输入编码，例如 gb18030")
    pack.add_argument("--csv-field-bytes", type=int, default=10_000_000, help="CSV 单字段解析上限")
    pack.add_argument("--media-seconds", type=int, default=600, help="音视频初始分段时长上限，超限再二分")
    pack.add_argument("--media-mode", choices=["auto", "transcode"], default="auto", help="auto 先尝试无损分段，超限/不兼容时重编码")
    pack.add_argument("--soffice", help="LibreOffice 可执行文件路径，也可设置 KB_PACK_SOFFICE")
    pack.add_argument("--timeout", type=int, default=600, help="单次 Office/FFmpeg 转换超时秒数")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "init-config":
            Limits().save(args.path)
            print(str(args.path.resolve()))
            return 0
        if args.action == "formats":
            print(json.dumps({"formats": FORMATS, "limits": asdict(Limits())}, ensure_ascii=False, indent=2))
            return 0
        if args.action == "doctor":
            import shutil
            try:
                soffice = office.find_soffice()
            except ValueError:
                soffice = None
            print(json.dumps({"version": __version__, "soffice": soffice,
                              "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe")}, ensure_ascii=False, indent=2))
            return 0
        if args.action == 'merge-ocr':
            from .reorganize import run
            run(args.directory, args.apply, args.workers)
            return 0
        if args.action == "verify":
            directory = args.directory.resolve()
            jobs = [directory] if (directory / "job.json").is_file() else sorted(p.parent for p in directory.glob("*/job.json"))
            if (directory / "_处理记录/任务").is_dir():
                jobs = sorted(p.parent for p in (directory / "_处理记录/任务").glob("*/job.json"))
            if not jobs:
                raise ValueError("目录中没有已完成的处理任务")
            import os
            workers = args.workers or min(os.cpu_count() or 1, 8)
            if not 1 <= workers <= 16:
                raise ValueError("校验 workers 必须在 1..16 范围")
            if workers == 1:
                for job in jobs:
                    print(json.dumps(verify_job(job), ensure_ascii=False))
            else:
                # Bound the submitted queue so huge directories do not allocate one future per file.
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    iterator = iter(jobs)
                    pending = {}
                    for _ in range(workers * 2):
                        job = next(iterator, None)
                        if job is not None:
                            pending[pool.submit(verify_job, job)] = job
                    while pending:
                        from concurrent.futures import wait, FIRST_COMPLETED
                        ready, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in ready:
                            pending.pop(future)
                            print(json.dumps(future.result(), ensure_ascii=False), flush=True)
                            job = next(iterator, None)
                            if job is not None:
                                pending[pool.submit(verify_job, job)] = job
            return 0
        limits_raw = asdict(Limits.load(args.config))
        limits_raw.update(dict(override(value) for value in args.set))
        limits = Limits(**limits_raw)
        options = {key: getattr(args, key) for key in ("pdf_mode", "dpi", "render_pixels", "page_window",
                   "header_rows", "encoding", "csv_field_bytes", "media_seconds", "media_mode", "soffice", "timeout", "policy")}
        if options["pdf_mode"] == "images" and options["policy"] == "strict":
            raise ValueError("PDF 栅格化会改变信息表达；请显式使用 --policy derive --pdf-mode images，默认 strict 不转换为图片")
        for key in ("dpi", "render_pixels", "page_window", "csv_field_bytes", "media_seconds", "timeout"):
            if options[key] <= 0:
                raise ValueError(f"{key} 必须为正数")
        if options["header_rows"] < 0:
            raise ValueError("header_rows 不能为负数")
        root = args.out.resolve()
        if args.preserve_tree:
            from .tree import run
            if args.replace_existing:
                options["replace_existing"] = True
            if args.fast_resume:
                options["fast_resume"] = True
            return run(args, limits, options)
        if args.workers not in {0, 1}:
            raise ValueError("--workers 当前需配合 --preserve-tree 使用")
        if args.exclude:
            raise ValueError("--exclude 当前需配合 --preserve-tree 使用")
        if args.include:
            raise ValueError("--include 需配合 --preserve-tree 使用")
        if args.retry_report:
            raise ValueError("--retry-report 需配合 --preserve-tree 使用")
        if args.replace_existing:
            raise ValueError("--replace-existing 需配合 --preserve-tree 使用")
        # Snapshot inputs before making output files, excluding existing outputs from directory walks.
        sources = []
        seen = set()
        for path in args.inputs:
            candidates = sorted(path.rglob("*") if args.recursive else path.iterdir()) if path.is_dir() else [path]
            for candidate in candidates:
                resolved = candidate.resolve()
                if candidate.is_dir() or resolved in seen:
                    continue
                if any(candidate.suffix.lower() in FORMATS[kind] for kind in args.exclude_type):
                    continue
                if path.is_dir() and resolved.is_relative_to(root):
                    continue
                seen.add(resolved)
                sources.append(resolved)
        if not sources:
            raise ValueError("没有输入文件")
        root.mkdir(parents=True, exist_ok=True)
        results = []
        for index, source in enumerate(sources, 1):
            print(f"[{index}/{len(sources)}] {source.name}", file=sys.stderr)
            try:
                result = process(source, root, limits, options, args.resume)
            except Exception as error:
                result = {"source": str(source), "status": "failed", "error": str(error)}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
        # Reserve a fresh report name so a later run cannot overwrite an earlier failure report.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="run-", dir=root,
                                         encoding="utf-8", delete=False) as report:
            json.dump({"results": results}, report, ensure_ascii=False, indent=2)
            report.write("\n")
        return int(any(item["status"] == "failed" for item in results))
    except (ValueError, OSError, TypeError, json.JSONDecodeError) as error:
        print(f"kb-pack: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
