"""Directory-preserving batch conversion and failure-original retention."""
from collections import Counter
from dataclasses import asdict
import json
import os
import errno
from pathlib import Path
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import re
import uuid

from .config import Limits
from .output import copy_digest, digest, dump, remove_temporary, verify_job


def archive_previous(root: Path, info: dict):
    """Archive only proven outputs of this source, with a reversible move journal."""
    source = Path(info["source"])
    jobs = root / "_处理记录/任务"
    prefix = f"{source.stem[:80]}_{source.suffix.lstrip('.')}_"
    previous = []
    for job in jobs.iterdir():
        if not job.name.startswith(prefix) or not (job / "job.json").is_file():
            continue
        old_info = json.loads((job / "job.json").read_text(encoding="utf-8"))
        if old_info.get("source") == str(source) and old_info.get("upload_root") == str(root):
            verify_job(job)
            previous.append((job, old_info))
    if not previous:
        return None, [], []
    backup = root / "_处理记录/历史成品" / uuid.uuid4().hex
    files, known = [], set()
    for job, _ in previous:
        for line in (job / "parts.jsonl").read_text(encoding="utf-8").splitlines():
            relative = Path(json.loads(line)["file"])
            if not (root / relative).resolve().is_relative_to(root):
                raise ValueError("旧清单路径越界")
            if relative not in known:
                known.add(relative)
                files.append((root / relative, backup / "files" / relative))
    backup.mkdir(parents=True)
    dump(backup / "transaction.json", {"status": "archiving", "source": str(source),
         "files": [str(a.relative_to(root)) for a, _ in files], "jobs": [j.name for j, _ in previous]})
    moved_files, moved_jobs = [], []
    try:
        for old, target in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            old.rename(target)
            moved_files.append((old, target))
        for job, old_info in previous:
            target = backup / "任务" / job.name
            target.parent.mkdir(parents=True, exist_ok=True)
            job.rename(target)
            moved_jobs.append((job, target, old_info))
            dump(target / "job.json", {**old_info, "upload_root": str(backup / "files")})
        return backup, moved_files, moved_jobs
    except BaseException:
        restore_previous(moved_files, moved_jobs)
        raise


def restore_previous(files, jobs):
    for old, archived, info in reversed(jobs):
        dump(archived / "job.json", info)
        archived.rename(old)
    for old, archived in reversed(files):
        archived.rename(old)


def publish(directory: Path, root: Path, info: dict, replace_existing=False):
    rewritten = directory / "tree-parts.jsonl"
    created = []
    backup, archived_files, archived_jobs = None, [], []
    def rollback():
        for path in created:
            path.unlink(missing_ok=True)
        restore_previous(archived_files, archived_jobs)
        if backup:
            dump(backup / "transaction.json", {"status": "rolled_back", "source": info["source"]})
    try:
        if replace_existing:
            # New parts have already passed all format/content checks. Do not
            # remove old results while conversion can still fail.
            records = [json.loads(line) for line in (directory / "parts.jsonl").read_text().splitlines()]
            parent = root / Path(records[0]["file"]).relative_to("files").parent
            # A previous export of this source is either the source's own name (a
            # byte-identical copy, which is what the strict export now keeps) or that
            # name plus a part number (a real split). Both count as "our previous
            # output", so a re-run replaces the set it produced instead of leaving a
            # stale copy beside the new one.
            #
            # A name that collided inside its own export can also carry one or two
            # further markers (see Sink.emit): "..._part000001.pdf_part000001.pdf"
            # when the colliding file was itself a part, or "..._part000001_dup2.pdf"
            # in the last-resort case. Those belong to the same previous export, so
            # they are recognised here too - otherwise a re-run would leave them
            # behind beside the new set.
            source_name = re.escape(Path(info["source"]).name)
            # Recognise, in order: the source's own name; that name plus a part
            # number (a real split); the same again when the colliding file was
            # itself a part, which nests a second extension and part marker; and the
            # last-resort _dupN form. Keeping this list in one pattern means a re-run
            # archives the whole previous set whatever shape it took.
            pattern = re.compile(
                source_name
                + r"(?:_part\d{6}\.[^.]+)*"           # a part, or a part of a part
                + r"(?:_part\d{6}_dup\d+\.[^.]+)?"   # the last-resort unique name
                + r"$")
            if parent.is_dir() and any(pattern.fullmatch(p.name) for p in parent.iterdir()):
                backup, archived_files, archived_jobs = archive_previous(root, info)
        with (directory / "parts.jsonl").open(encoding="utf-8") as old, rewritten.open("w", encoding="utf-8") as new:
            for line in old:
                record = json.loads(line)
                relative = Path(record["file"]).relative_to("files")
                if not (root / relative).resolve().is_relative_to(root):
                    raise ValueError("生成路径越界")
                source = directory / record["file"]
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if not target.is_file() or digest(target) != record["sha256"]:
                        raise ValueError(f"目标文件已存在且内容不同，不覆盖：{relative}")
                else:
                    # Staging is on the same volume; rename avoids a second huge-file copy.
                    try:
                        source.rename(target)
                    except OSError as error:
                        if error.errno != errno.EXDEV:
                            raise
                        if copy_digest(source, target) != record["sha256"]:
                            target.unlink(missing_ok=True)
                            raise ValueError("跨卷发布校验失败")
                    created.append(target)
                record["file"] = relative.as_posix()
                new.write(json.dumps(record, ensure_ascii=False) + "\n")
        rewritten.replace(directory / "parts.jsonl")
        info["upload_root"] = str(root)
        remove_temporary(directory / "files")
        if backup:
            info["previous_outputs_archive"] = str(backup)
            dump(backup / "transaction.json", {"status": "complete", "source": info["source"],
                 "files": [str(a.relative_to(root)) for a, _ in archived_files],
                 "jobs": [j.name for j, _, _ in archived_jobs]})
        # Keep rollback available until the caller commits the new job metadata.
        return rollback
    except BaseException:
        rollback()
        raise


def archive_failure(source: Path, relative: Path, root: Path, reason: str):
    target = root / "_待重处理" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    source_hash = digest(source)
    if target.exists():
        if digest(target) != source_hash:
            raise ValueError("待重处理原件路径已存在不同内容，不覆盖")
    else:
        with tempfile.TemporaryDirectory(prefix=".original-", dir=target.parent) as tmp:
            candidate = Path(tmp) / "original"
            copied_hash = copy_digest(source, candidate)
            if copied_hash != source_hash:
                raise ValueError("失败原件复制期间变化或复制校验不通过")
            candidate.rename(target)
    return {"status": "failed", "source": str(source), "relative": relative.as_posix(),
            "original": target.relative_to(root).as_posix(), "sha256": source_hash, "error": reason}


def convert_one(source, relative, root, jobs, limits, options, resume, work_root=None):
    from .cli import process
    try:
        result = process(source, jobs, limits, options, resume,
                         tree_relative=relative, upload_root=root, work_root=work_root)
    except Exception as error:
        try:
            result = archive_failure(source, relative, root, f"{type(error).__name__}: {error}")
        except Exception as archive_error:
            result = {"status": "failed", "relative": relative.as_posix(), "error": str(error),
                      "archive_error": str(archive_error)}
    return relative, result


def run(args, limits: Limits, options: dict):
    from .cli import FORMATS
    from .layout import load, remap
    source_root = args.inputs[0].resolve()
    root = args.out.resolve()
    if len(args.inputs) != 1 or not source_root.is_dir():
        raise ValueError("--preserve-tree 需要唯一的输入目录")
    if source_root == root or source_root.is_relative_to(root):
        raise ValueError("输出不能是输入目录或输入目录的祖先")
    if args.workers == 0:
        args.workers = max(1, min(os.cpu_count() or 1, 8))
    if not 1 <= args.workers <= 16:
        raise ValueError("workers 必须在 1..16 范围内")
    excludes = [root, *(path.resolve() for path in args.exclude)]
    includes = [path.resolve() for path in args.include]
    if any(not path.is_relative_to(source_root) for path in includes):
        raise ValueError("include 必须位于输入根目录内")
    metadata = root / "_处理记录"
    if (metadata / '.layout-migration.lock').exists():
        raise ValueError('目录布局正在归并，完成后重试')
    layout_rules = load(root)
    jobs = metadata / "任务"
    jobs.mkdir(parents=True, exist_ok=True)
    work_root = (args.work_dir or Path(tempfile.gettempdir()) / "cloud-kb-pack-work").resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    lock = metadata / ".running"
    try:
        handle = lock.open("x")
    except FileExistsError:
        try:
            previous = json.loads(lock.read_text())
            os.kill(previous["pid"], 0)
        except ProcessLookupError:
            lock.unlink()
            handle = lock.open("x")
        else:
            raise ValueError("目录处理已有运行进程；若异常终止且 PID 已复用，请核实后移除 _处理记录/.running")
    counters = Counter()
    with handle:
        json.dump({"pid": os.getpid()}, handle)
    report_path = metadata / f"run-{os.getpid()}.jsonl"
    upload_index = metadata / f"upload-batches-{os.getpid()}.jsonl"
    dump(metadata / "effective-config.json", {"limits": asdict(limits), "options": options,
                                               "source_root": str(source_root), "excluded": [str(p) for p in excludes],
                                               "excluded_types": args.exclude_type, "workers": args.workers,
                                               "work_dir": str(work_root), "included": [str(p) for p in includes],
                                               "output_layout": layout_rules})
    def inputs():
        if args.retry_report:
            seen = set()
            with args.retry_report.open(encoding="utf-8") as report:
                for line in report:
                    record = json.loads(line)
                    if record.get("status") != "failed":
                        continue
                    source = Path(record["source"]).resolve()
                    if not source.is_relative_to(source_root):
                        raise ValueError("重试报告中源文件超出输入目录")
                    if source in seen or any(source.is_relative_to(path) for path in excludes):
                        continue
                    if any(source.suffix.lower() in FORMATS[kind] for kind in args.exclude_type):
                        continue
                    seen.add(source)
                    yield source, remap(source.relative_to(source_root), layout_rules)
            return
        for parent, directories, names in os.walk(source_root, followlinks=False):
            directories[:] = sorted(d for d in directories if not d.startswith(".")
                and d.casefold() not in {"$recycle.bin", "system volume information", "recycler", "recycled"}
                and not (Path(parent) / d).is_symlink()
                and (not includes or any((Path(parent)/d).is_relative_to(p) or p.is_relative_to(Path(parent)/d) for p in includes))
                and not any((Path(parent) / d).is_relative_to(p) for p in excludes))
            # Mirror visited directories even when empty or all their files are excluded.
            # Otherwise a missing folder looks like a missed input in the prepared tree.
            target_directory = (root / remap(Path(parent).relative_to(source_root), layout_rules)).resolve()
            if not target_directory.is_relative_to(root):
                raise ValueError("镜像目录路径越界")
            target_directory.mkdir(parents=True, exist_ok=True)
            for name in sorted(names):
                source = Path(parent) / name
                if name.startswith(".") or source.is_symlink() or not source.is_file():
                    counters["skipped_system"] += 1
                    continue
                if any(source.is_relative_to(path) for path in excludes):
                    counters["excluded"] += 1
                    continue
                if includes and not any(source.is_relative_to(path) for path in includes):
                    counters["not_included"] += 1
                    continue
                if any(source.suffix.lower() in FORMATS[kind] for kind in args.exclude_type):
                    counters["excluded_type"] += 1
                    continue
                yield source, remap(source.relative_to(source_root), layout_rules)

    def results():
        if args.workers == 1:
            for source, relative in inputs():
                print(f"处理：{relative}", flush=True)
                yield convert_one(source, relative, root, jobs, limits, options, args.resume, work_root)
            return
        # PDFium is not thread-safe; independent processes keep per-file native state isolated.
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            iterator = iter(inputs())
            pending = set()
            finished = False
            while pending or not finished:
                while not finished and len(pending) < args.workers * 2:
                    try:
                        source, relative = next(iterator)
                    except StopIteration:
                        finished = True
                        break
                    pending.add(pool.submit(convert_one, source, relative, root, jobs, limits, options, args.resume, work_root))
                if pending:
                    completed, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                    for future in completed:
                        yield future.result()
    try:
        with report_path.open("a", encoding="utf-8") as report, upload_index.open("w", encoding="utf-8") as batches:
            upload_number = 0
            last_flush = 0.0
            for relative, result in results():
                counters[result["status"]] += 1
                report.write(json.dumps(result, ensure_ascii=False) + "\n")
                if result["status"] in {"complete", "resumed"}:
                    with (jobs / result["job"] / "parts.jsonl").open(encoding="utf-8") as manifest:
                        for line in manifest:
                            part = json.loads(line)
                            upload_number += 1
                            batches.write(json.dumps({"batch": (upload_number - 1) // limits.batch_files + 1,
                                                      "file": part["file"]}, ensure_ascii=False) + "\n")
                # Batch small progress writes on external FAT volumes; per-file provenance
                # is already committed, so a restart can still recover every completed job.
                now = time.monotonic()
                if now - last_flush >= 1.0:
                    report.flush()
                    batches.flush()
                    dump(metadata / "progress.json", {"status": "running", "counts": dict(counters),
                         "last": relative.as_posix(), "upload_files": upload_number, "report": str(report_path)})
                    last_flush = now
                print(json.dumps({"relative": relative.as_posix(), "status": result["status"],
                                  "parts": result.get("parts"), "error": result.get("error")}, ensure_ascii=False))
            dump(metadata / "progress.json", {"status": "complete", "counts": dict(counters),
                 "upload_files": upload_number, "report": str(report_path), "upload_batches": str(upload_index)})
    finally:
        lock.unlink(missing_ok=True)
    return int(bool(counters["failed"]))
