"""After a conversion the output folder must contain the converted files only.

The engine used to write a "_Chamo记录" folder with a request log, per-item
progress, journals and the run report next to the user's PNGs, plus an empty
"_Chamo暂存". That bookkeeping is still needed (resume, history, the shard
decision) but it is internal, so it lives in a per-user state folder now. A batch
conversion is the one case that gets an extra file in the output folder: one
清单.json, because a list of what each source produced is part of the
deliverable when converting a folder of files.

These tests pin the user-visible rule, not the implementation: they inspect the
output folder itself, so moving bookkeeping back into it fails them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine" / "workbench.py"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "engine"))

import platform_state as state_paths

MANIFEST = "清单.json"
LEGACY = {"_Chamo记录", "_Chamo暂存"}


def engine_config(operation: str = "pdf_images", **overrides) -> dict:
    base = {"operation": operation, "naming": "clean", "dpi": 72, "format": "png",
            "quality": 90, "range": "1-2", "pdf_pages": 100,
            "pdf_bytes": 100_000_000, "workers": 2, "max_edge": 0,
            "batch_files": 50, "lossy": False}
    base.update(overrides)
    return base


def engine_run(work: Path, inputs, output: Path, config: dict, state: Path,
               extra: dict = None) -> dict:
    """Run the real engine in a subprocess, the way the GUI and the CLI run it."""
    work.mkdir(parents=True, exist_ok=True)
    index = len(list(work.glob("request-*.json")))
    request_path = work / ("request-%02d.json" % index)
    request = {"inputs": [str(p) for p in inputs], "output": str(output), "config": config}
    request.update(extra or {})
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    environment = dict(os.environ)
    environment["CHAMO_STATE_DIR"] = str(state)
    environment["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(ENGINE), "--request", str(request_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=environment, timeout=300)
    events = [json.loads(line) for line in done.stdout.splitlines()
              if line.strip().startswith("{")]
    finished = next((e for e in reversed(events) if e.get("type") == "finished"), None)
    assert finished is not None, (done.stdout[-3000:], done.stderr[-3000:])
    return {"rc": done.returncode, "events": events, "finished": finished,
            "report": report_dir(events), "stderr": done.stderr}


def report_dir(events) -> Path:
    scan = next((e for e in events if e.get("type") == "scan"), None)
    assert scan and scan.get("report"), "scan event must point at the run record"
    return Path(scan["report"])


def pdf_fixture(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copyfile(FIXTURES / "示例讲义.pdf", target)
    return target


def state_slot(state: Path, output: Path) -> Path:
    """The engine's slot for an output folder, resolved against this test's state root.

    slot() without a request falls back to the real per-user state directory, which
    would make these tests read and touch the developer's own records.
    """
    return state_paths.slot(output, {"state_root": str(state)})


def files_under(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_single_conversion_leaves_only_the_outputs(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    source = pdf_fixture(tmp_path, "讲义.pdf")
    result = engine_run(tmp_path, [source], out, engine_config(), state)
    assert files_under(out) == {"讲义/讲义_001.png", "讲义/讲义_002.png"}
    entries = sorted(p.name for p in out.iterdir())
    assert entries == ["讲义"], entries
    # A single conversion gets no manifest: the outputs are the whole answer.
    assert "manifest" not in result["finished"]


def test_single_split_conversion_has_no_manifest(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    source = pdf_fixture(tmp_path, "拆分.pdf")
    # Strict split always keeps every page, so the request must not carry a range.
    result = engine_run(tmp_path, [source], out,
                        engine_config("pdf_split", range=""), state)
    names = files_under(out)
    assert names and all(name.endswith(".pdf") for name in names)
    assert MANIFEST not in names
    assert result["finished"]["outputs"] == len(names)


def test_batch_writes_exactly_one_manifest(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    first = pdf_fixture(tmp_path, "甲.pdf")
    second = pdf_fixture(tmp_path, "乙.pdf")
    result = engine_run(tmp_path, [first, second], out, engine_config(), state)
    produced = files_under(out) - {MANIFEST}
    assert produced == {"甲/甲_001.png", "甲/甲_002.png", "乙/乙_001.png", "乙/乙_002.png"}
    assert (out / MANIFEST).is_file()
    assert files_under(out) == produced | {MANIFEST}
    manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["任务数"] == 2
    assert manifest["成功"] == 2 and manifest["异常"] == 0
    listed = sorted(item for entry in manifest["明细"] for item in entry["成品"])
    assert listed == sorted(produced)
    # The manifest is a portable record: separators must be "/" on every platform
    # (CI's Windows runner caught os.sep here writing backslashes).
    assert all("\\" not in item for item in listed)
    for relative in listed:
        assert (out / relative).is_file()
    assert result["finished"]["manifest"] == str(out / MANIFEST)


def test_batch_manifest_records_failures(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    good = pdf_fixture(tmp_path, "好的.pdf")
    broken = tmp_path / "坏的.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not a readable pdf")
    result = engine_run(tmp_path, [good, broken], out, engine_config(), state)
    manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["任务数"] == 2
    assert manifest["成功"] == 1 and manifest["异常"] == 1
    failed = [entry for entry in manifest["明细"] if entry["状态"] == "failed"]
    assert len(failed) == 1 and failed[0].get("错误")
    assert failed[0]["成品"] == []
    assert result["finished"]["failed"] == 1


def test_bookkeeping_lives_in_the_state_folder(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    source = pdf_fixture(tmp_path, "记录.pdf")
    result = engine_run(tmp_path, [source], out, engine_config(), state)
    record = result["report"]
    assert state in record.parents, record
    assert out not in record.parents, record
    assert (record / "report.json").is_file()
    assert (record / "request.json").is_file()
    assert (record / "处理报告.txt").is_file()
    report = json.loads((record / "report.json").read_text(encoding="utf-8"))
    assert report["success"] == 1 and report["state"] == "complete"
    assert report["report"] == str(record)


def test_resume_uses_the_state_folder_not_the_output_folder(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    source = pdf_fixture(tmp_path, "续跑.pdf")
    first = engine_run(tmp_path, [source], out, engine_config(), state)
    before = files_under(out)
    second = engine_run(tmp_path, [source], out, engine_config(), state)
    report = json.loads((second["report"] / "report.json").read_text(encoding="utf-8"))
    assert report["results"][0]["state"] == "resumed"
    assert second["finished"]["success"] == 1
    assert files_under(out) == before
    assert first["finished"]["outputs"] == len(before)


def test_clearing_the_state_folder_never_overwrites(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    source = pdf_fixture(tmp_path, "原样.pdf")
    engine_run(tmp_path, [source], out, engine_config(), state)
    original = {name: (out / name).read_bytes() for name in files_under(out)}
    shutil.rmtree(state)
    engine_run(tmp_path, [source], out, engine_config(), state)
    after = files_under(out)
    for name, content in original.items():
        assert (out / name).read_bytes() == content, name
    assert any(name.startswith("原样 (2)/") for name in after), sorted(after)


def test_staging_folder_is_gone_after_a_successful_run(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    source = pdf_fixture(tmp_path, "暂存.pdf")
    engine_run(tmp_path, [source], out, engine_config(), state)
    leftovers = [p.name for p in out.iterdir()]
    assert leftovers == ["暂存"], leftovers
    assert not (out / ".Chamo暂存").exists()


def test_prune_keeps_the_newest_runs(tmp_path):
    out, state = tmp_path / "out", tmp_path / "state"
    request = {"state_root": str(state)}
    records = state_paths.records(out, request)
    records.mkdir(parents=True)
    for index in range(state_paths.RUN_KEEP + 5):
        run = records / ("run-%02d" % index)
        run.mkdir()
        os.utime(run, (1_600_000_000 + index, 1_600_000_000 + index))
    removed = state_paths.prune(out, request)
    left = sorted(p.name for p in records.iterdir())
    assert len(left) == state_paths.RUN_KEEP
    assert "run-00" not in left
    assert "run-%02d" % (state_paths.RUN_KEEP + 4) in left
    assert removed["runs"] == 5


# --- Defects found by independent review ----------------------------------

def test_state_root_request_field_is_used(tmp_path):
    """The documented request field must work, not crash the run that uses it.

    The supervisor hands each worker the extracted value, which is a string, while
    the GUI sends it inside the request dict; root() only understood the dict, so
    any caller that set the field got "'str' object has no attribute 'get'" and an
    empty output folder. Every other test used CHAMO_STATE_DIR and missed it.
    """
    out = tmp_path / "out"
    environment_state = tmp_path / "env-state"
    requested_state = tmp_path / "request-state"
    source = pdf_fixture(tmp_path, "字段.pdf")
    result = engine_run(tmp_path, [source], out, engine_config(), environment_state,
                        extra={"state_root": str(requested_state)})
    assert result["finished"]["success"] == 1, result["finished"]
    assert result["report"].is_relative_to(requested_state), result["report"]
    assert files_under(out) == {"字段/字段_001.png", "字段/字段_002.png"}
    # The request wins over the environment; the env folder is never even probed.
    assert not environment_state.exists()


def test_prune_keeps_a_slot_that_is_still_in_use(tmp_path):
    """A slot's own mtime says when it was created, not when it was last used.

    Aging only the slot directory (as it ages in real use: new runs are written
    into records/ and receipts/) used to make the next run into another folder
    delete this folder's records - losing resume and history for a folder used
    minutes earlier.
    """
    state = tmp_path / "state"
    source = pdf_fixture(tmp_path, "常用.pdf")
    engine_run(tmp_path, [source], tmp_path / "used", engine_config(), state)
    slot = state_slot(state, tmp_path / "used")
    assert slot.is_dir()
    old = time.time() - 31 * 24 * 3600
    os.utime(slot, (old, old))
    engine_run(tmp_path, [source], tmp_path / "other", engine_config(), state)
    assert slot.is_dir(), "records of a folder used minutes ago must survive"
    assert list((slot / "records").iterdir())


def test_prune_still_forgets_a_slot_untouched_for_a_month(tmp_path):
    """The positive control: genuinely idle slots must still be reclaimed."""
    state = tmp_path / "state"
    source = pdf_fixture(tmp_path, "闲置.pdf")
    engine_run(tmp_path, [source], tmp_path / "old", engine_config(), state)
    slot = state_slot(state, tmp_path / "old")
    old = time.time() - 31 * 24 * 3600
    for item in sorted(slot.rglob("*"), key=lambda p: len(p.parts), reverse=True) + [slot]:
        os.utime(item, (old, old))
    engine_run(tmp_path, [source], tmp_path / "live", engine_config(), state)
    assert not slot.exists()


def test_manifest_does_not_overwrite_the_users_own_file(tmp_path):
    """A name collision is the user's file; it is never replaced."""
    out, state = tmp_path / "out", tmp_path / "state"
    out.mkdir()
    user_file = out / MANIFEST
    user_file.write_text('{"我自己的": "清单"}', encoding="utf-8")
    first = pdf_fixture(tmp_path, "甲.pdf")
    second = pdf_fixture(tmp_path, "乙.pdf")
    result = engine_run(tmp_path, [first, second], out, engine_config(), state)
    assert user_file.read_text(encoding="utf-8") == '{"我自己的": "清单"}'
    written = Path(result["finished"]["manifest"])
    assert written.name == "清单 (2).json", written
    assert json.loads(written.read_text(encoding="utf-8"))["任务数"] == 2
    # A later batch keeps updating that same numbered manifest instead of climbing
    # to 清单 (3).json, (4).json ... while the user's own file stays untouched.
    again = engine_run(tmp_path, [pdf_fixture(tmp_path, "丙.pdf"), pdf_fixture(tmp_path, "丁.pdf")],
                       out, engine_config(), state)
    assert Path(again["finished"]["manifest"]).name == "清单 (2).json"
    assert user_file.read_text(encoding="utf-8") == '{"我自己的": "清单"}'
    # The user's own 清单.json stays; ours stays the single numbered one.
    assert sorted(p.name for p in out.iterdir() if p.suffix == ".json") == ["清单 (2).json", "清单.json"]


def test_manifest_is_a_portable_record(tmp_path):
    """Separators and a timezone-qualified timestamp, on every platform."""
    out, state = tmp_path / "out", tmp_path / "state"
    first = pdf_fixture(tmp_path, "甲.pdf")
    second = pdf_fixture(tmp_path, "乙.pdf")
    engine_run(tmp_path, [first, second], out, engine_config(), state)
    manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    assert re.search(r"[+-]\d{2}:\d{2}$", manifest["生成时间"]), manifest["生成时间"]
    listed = [item for entry in manifest["明细"] for item in entry["成品"]]
    assert listed and all("\\" not in item for item in listed)
    assert manifest["成功"] + manifest["异常"] == manifest["任务数"]


def test_second_batch_rotates_our_own_manifest_without_losing_outputs(tmp_path):
    """One manifest per folder: ours is the current record, the older batch's files stay."""
    out, state = tmp_path / "out", tmp_path / "state"
    first = pdf_fixture(tmp_path, "甲.pdf")
    engine_run(tmp_path, [first, pdf_fixture(tmp_path, "乙.pdf")], out, engine_config(), state)
    second = pdf_fixture(tmp_path, "丙.pdf")
    result = engine_run(tmp_path, [second, pdf_fixture(tmp_path, "丁.pdf")], out, engine_config(), state)
    assert Path(result["finished"]["manifest"]).name == MANIFEST
    assert files_under(out) == {"甲/甲_001.png", "甲/甲_002.png", "乙/乙_001.png", "乙/乙_002.png",
                                "丙/丙_001.png", "丙/丙_002.png", "丁/丁_001.png", "丁/丁_002.png",
                                MANIFEST}
    manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    # Completion order varies with the worker pool, and code-point order is not
    # pinyin order for Chinese names, so compare the set.
    sources = {Path(s).name for entry in manifest["明细"] for s in entry["源文件"]}
    assert sources == {"丙.pdf", "丁.pdf"}, sources


def test_folder_input_leaves_no_empty_mirror_folders(tmp_path):
    """Folders with nothing this operation can convert used to be mirrored empty."""
    inputs = tmp_path / "资料夹"
    (inputs / "空目录").mkdir(parents=True)
    (inputs / "只有文本").mkdir()
    (inputs / "只有文本" / "说明.txt").write_text("不是 PDF", encoding="utf-8")
    for name in ("甲.pdf", "乙.pdf"):
        shutil.copyfile(FIXTURES / "示例讲义.pdf", inputs / name)
    out, state = tmp_path / "out", tmp_path / "state"
    engine_run(tmp_path, [inputs], out, engine_config(), state)
    assert files_under(out) == {"资料夹/甲/甲_001.png", "资料夹/甲/甲_002.png",
                                "资料夹/乙/乙_001.png", "资料夹/乙/乙_002.png", MANIFEST}
    folders = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_dir()}
    assert folders == {"资料夹", "资料夹/甲", "资料夹/乙"}, folders


def test_deleted_outputs_are_converted_again(tmp_path):
    """Deleting the converted files is not "the output was changed".

    Records live in the state folder now, so clearing the output folder no longer
    clears the record; refusing with "已完成输出被改变" left the user with no way
    forward short of choosing a new folder.
    """
    out, state = tmp_path / "out", tmp_path / "state"
    source = pdf_fixture(tmp_path, "重做.pdf")
    engine_run(tmp_path, [source], out, engine_config(), state)
    shutil.rmtree(out / "重做")
    again = engine_run(tmp_path, [source], out, engine_config(), state)
    assert again["finished"]["success"] == 1 and again["finished"]["failed"] == 0, again["finished"]
    assert files_under(out) == {"重做/重做_001.png", "重做/重做_002.png"}


def test_changed_outputs_still_stop_the_run(tmp_path):
    """The safety property behind that guard must survive: never overwrite changes."""
    out, state = tmp_path / "out", tmp_path / "state"
    source = pdf_fixture(tmp_path, "被改.pdf")
    engine_run(tmp_path, [source], out, engine_config(), state)
    page = out / "被改" / "被改_001.png"
    page.write_bytes(page.read_bytes() + b"changed")
    again = engine_run(tmp_path, [source], out, engine_config(), state)
    assert again["finished"]["failed"] == 1
    assert page.read_bytes().endswith(b"changed")


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    """Publishing is write-temp-then-rename; a failure must not leave the temp behind.

    The manifest is written into the user's output folder, so a rename that fails
    (a Windows lock is the realistic case) or a process killed in between would
    otherwise leave a ".tmp-<uuid>" file that is neither a product nor a manifest.
    """
    sys.path.insert(0, str(ROOT / "engine"))
    import workbench

    def explode(*args, **kwargs):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(workbench, "replace_with_retry", explode)
    target = tmp_path / MANIFEST
    with pytest.raises(OSError):
        workbench.atomic_text(target, "{}")
    with pytest.raises(OSError):
        workbench.atomic_json(target, {"a": 1})
    assert [p.name for p in tmp_path.iterdir()] == [], "a failed publish left a temp file"


def test_cancelled_batch_writes_no_manifest(tmp_path):
    """A manifest of a cancelled run would not add up, so none is written."""
    import queue as queue_module
    import threading

    state = tmp_path / "state"
    out = tmp_path / "out"
    sources = [pdf_fixture(tmp_path, "取消%d.pdf" % index) for index in range(6)]
    request = {"inputs": [str(p) for p in sources], "output": str(out),
               "config": engine_config(dpi=300, workers=1)}
    request_path = tmp_path / "cancel-request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    environment = dict(os.environ)
    environment["CHAMO_STATE_DIR"] = str(state)
    environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(ENGINE), "--request", str(request_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=environment)
    events = queue_module.Queue()

    def read():
        for line in process.stdout:
            if line.strip().startswith("{"):
                events.put(json.loads(line))
        events.put(None)

    threading.Thread(target=read, daemon=True).start()
    while True:
        event = events.get(timeout=120)
        assert event is not None, "the engine exited before it started working"
        if event.get("type") == "item" and event.get("state") == "running":
            break
    process.stdin.write('{"command":"cancel"}\n')
    process.stdin.flush()
    assert process.wait(timeout=120) == 0
    finished = None
    while True:
        event = events.get(timeout=30)
        if event is None:
            break
        if event.get("type") == "finished":
            finished = event
    assert finished is not None and finished["state"] == "cancelled", finished
    assert not (out / MANIFEST).exists(), "a cancelled batch must not write a manifest"
