"""The local UI has to do the real work, not look like it does.

It is a second front end for the same engine, so the risk is a page that reports
progress while nothing converts. These tests drive the HTTP API the browser uses and
then check the disk: files produced, manifest for a batch, no manifest for a single
file, a cancelled batch that stops and leaves nothing behind, and a token that keeps
the localhost port from being driven by any other page.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "engine"))

import ui  # noqa: E402

PAGES = 6   # tests/fixtures/示例讲义.pdf


@pytest.fixture
def server(tmp_path):
    instance, token = ui.make_server(0, work=tmp_path / "ui-work")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % instance.server_address[1], token, instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=5)


def call(base, token, path, body=None, params=None, token_override=None):
    query = dict(params or {})
    query["token"] = token if token_override is None else token_override
    url = base + path + "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(payload)
        except ValueError:
            return error.code, {"raw": payload}


def wait_for(base, token, predicate, seconds=180):
    deadline = time.monotonic() + seconds
    state = {}
    while time.monotonic() < deadline:
        _, state = call(base, token, "/api/status")
        if predicate(state):
            return state
        time.sleep(0.4)
    raise AssertionError("timed out waiting for %r, last state %r" % (predicate, state))


def js_newline_offenders(page: str):
    """(unclosed quote, [(line, snippet)]) for newlines inside JS string literals.

    The page is a Python string, so a JavaScript newline escape that is not doubled
    becomes a real newline in the served script. A newline inside a JS string
    literal is a SyntaxError that kills the entire script - no dropdown, no buttons -
    which is exactly what the status line did before it was caught. There is no
    browser in CI, so this scanner is the browser-less way to notice.
    """
    script = page[page.index("<script>") + len("<script>"):page.index("</script>")]
    quote, line, offenders, index = None, 1, [], 0
    while index < len(script):
        char = script[index]
        if char == "\n":
            line += 1
            if quote is not None:
                offenders.append((line, script[max(0, index - 80):index].strip()[-80:]))
        elif quote is not None:
            if char == "\\":
                index += 2  # a JS escape: the next character cannot close the string
                continue
            if char == quote:
                quote = None
        elif char in ('"', "'", "\x60"):
            quote = char
        index += 1
    return quote, offenders


def last_request(instance) -> dict:
    """The request the UI itself wrote before starting the engine.

    Reading that file (instead of rebuilding the payload in the test) is the point:
    it proves what the server actually sent, not what the test believes it sends.
    """
    files = sorted(instance.ui_work.glob("ui-request-*.json"),
                   key=lambda path: (path.stat().st_mtime_ns, path.name))
    assert files, "界面没有写 request 文件（请求可能在校验阶段就被拒绝）"
    return json.loads(files[-1].read_text(encoding="utf-8"))


def files_under(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def published_under(root: Path) -> set:
    """Products in the user's folder; the dot-staging folder is not a product.

    The engine renders into "<output>/.Chamo暂存" and publishes atomically, so a
    staging file is not evidence that something was published. Waiting on files_under
    made the quit test cancel before the first product and then assert on an empty
    folder.
    """
    return {name for name in files_under(root) if not name.startswith(".Chamo暂存/")}


def copy_fixture(target: Path, name="讲义.pdf") -> Path:
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "示例讲义.pdf", target / name)
    return target / name


def test_the_page_needs_the_token(server):
    base, token, _ = server
    status, _ = call(base, token, "/", token_override="wrong")
    assert status == 403
    status, body = None, None
    with urllib.request.urlopen(base + "/?" + urllib.parse.urlencode({"token": token}), timeout=30) as response:
        status, body = response.status, response.read().decode("utf-8")
    assert status == 200 and "Chamo" in body and "/api/start" in body


def test_every_api_call_needs_the_token(server, tmp_path):
    base, token, _ = server
    for path in ("/api/status", "/api/browse", "/api/open"):
        status, _ = call(base, token, path, token_override="wrong")
        assert status == 403, path


def test_browse_lists_directories_before_files(server, tmp_path):
    base, token, _ = server
    root = tmp_path / "资料"
    (root / "子目录").mkdir(parents=True)
    (root / "a.txt").write_text("x", encoding="utf-8")
    status, listing = call(base, token, "/api/browse", params={"path": str(root)})
    assert status == 200, listing
    names = [entry["name"] for entry in listing["entries"]]
    assert names == ["子目录", "a.txt"], names
    assert listing["entries"][0]["dir"] is True and listing["entries"][1]["dir"] is False
    assert listing["path"] == str(root) and listing["parent"] == str(tmp_path)
    status, missing = call(base, token, "/api/browse", params={"path": str(tmp_path / "nope")})
    assert status == 400 and "不存在" in missing["error"]


def test_browse_skips_hidden_entries(server, tmp_path):
    base, token, _ = server
    root = tmp_path / "隐藏"
    root.mkdir()
    (root / ".DS_Store").write_text("x", encoding="utf-8")
    (root / "看得见.txt").write_text("x", encoding="utf-8")
    _, listing = call(base, token, "/api/browse", params={"path": str(root)})
    assert [entry["name"] for entry in listing["entries"]] == ["看得见.txt"]


def test_start_rejects_bad_input(server, tmp_path):
    base, token, _ = server
    out = str(tmp_path / "out")
    cases = [
        ({"inputs": [], "output": out, "operation": "pdf_images"}, "先加入"),
        ({"inputs": [str(tmp_path)], "output": out, "operation": "nope"}, "未知转换类型"),
        ({"inputs": [str(tmp_path)], "output": "", "operation": "pdf_images"}, "输出目录"),
        ({"inputs": [str(tmp_path)], "output": out, "operation": "pdf_images", "dpi": "abc"}, "DPI"),
        ({"inputs": [str(tmp_path)], "output": out, "operation": "pdf_images", "dpi": 1000}, "36-600"),
        ({"inputs": [str(tmp_path)], "output": out, "operation": "pdf_images", "format": "exe"}, "图片格式"),
        ({"inputs": "不是列表", "output": out, "operation": "pdf_images"}, "先加入"),
    ]
    for body, expected in cases:
        status, payload = call(base, token, "/api/start", body)
        assert status == 400, (body, status, payload)
        assert expected in payload["error"], (body, payload)


def test_a_single_file_converts_and_writes_nothing_extra(server, tmp_path):
    base, token, _ = server
    source = copy_fixture(tmp_path / "输入")
    out = tmp_path / "out"
    status, started = call(base, token, "/api/start", {"inputs": [str(source)], "output": str(out),
                                                       "operation": "pdf_images", "dpi": 72,
                                                       "range": "1-1"})
    assert status == 200 and started.get("started"), started
    state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    assert state["state"] == "complete", state
    assert files_under(out) == {"讲义/讲义_001.png"}
    assert any("完成" in line for line in state["lines"]), state["lines"]
    assert state["total"] == 1 and state["done"] == 1, state


def test_a_batch_writes_one_manifest(server, tmp_path):
    base, token, _ = server
    first = copy_fixture(tmp_path / "批量", "甲.pdf")
    second = copy_fixture(tmp_path / "批量", "乙.pdf")
    out = tmp_path / "out"
    call(base, token, "/api/start", {"inputs": [str(first), str(second)], "output": str(out),
                                     "operation": "pdf_images", "dpi": 72})
    state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    assert state["state"] == "complete", state
    expected = {"%s/%s_%03d.png" % (name, name, index)
                for name in ("甲", "乙") for index in range(1, PAGES + 1)}
    assert files_under(out) == expected | {"清单.json"}
    manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))
    assert manifest["成功"] == 2 and manifest["任务数"] == 2
    assert sorted(Path(s).name for entry in manifest["明细"] for s in entry["源文件"]) == ["乙.pdf", "甲.pdf"]


def test_cancel_stops_the_run_and_writes_no_manifest(server, tmp_path):
    base, token, _ = server
    sources = [str(copy_fixture(tmp_path / "取消", "取消%d.pdf" % index)) for index in range(8)]
    out = tmp_path / "out"
    status, started = call(base, token, "/api/start", {"inputs": sources, "output": str(out),
                                                       "operation": "pdf_images", "dpi": 300})
    assert status == 200 and started.get("started"), started
    # Cancel immediately: the engine reads this channel while scanning, so the run
    # must stop even if the files would have been quick. Waiting first would make the
    # test depend on machine speed.
    _, cancelled = call(base, token, "/api/cancel", {})
    assert cancelled.get("cancelled") is True, cancelled
    state = wait_for(base, token, lambda s: s.get("state") in ("cancelled", "complete", "failed"), seconds=120)
    assert state["state"] == "cancelled", state
    assert state["cancelled"] is True
    assert not (out / "清单.json").exists()
    assert files_under(out) <= {"%s/%s_%03d.png" % (name, name, i)
                                for name in ("取消%d" % n for n in range(8))
                                for i in range(1, PAGES + 1)}


def test_open_reports_a_missing_result(server, tmp_path, monkeypatch):
    base, token, _ = server
    status, payload = call(base, token, "/api/open", {"path": str(tmp_path / "nothing")})
    assert status == 400 and "不存在" in payload["error"]
    opened = []
    monkeypatch.setattr(ui, "open_path", lambda path: opened.append(str(path)))
    target = tmp_path / "结果"
    target.mkdir()
    status, payload = call(base, token, "/api/open", {"path": str(target)})
    assert status == 200 and payload["opened"] == str(target) and opened == [str(target)]
    # With no path in the body the button opens the run's output folder.
    source = copy_fixture(tmp_path / "输入")
    call(base, token, "/api/start", {"inputs": [str(source)], "output": str(target),
                                     "operation": "pdf_images", "dpi": 36, "range": "1-1"})
    wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    status, payload = call(base, token, "/api/open", {})
    assert status == 200 and payload["opened"] == str(target)


def test_open_reports_a_missing_platform_opener(server, tmp_path, monkeypatch):
    """A minimal Linux install has no xdg-open. That is a message, not a 500."""
    base, token, _ = server
    target = tmp_path / "有结果"
    target.mkdir()

    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "xdg-open")

    # Each platform opens results with its own mechanism, so remove that one:
    # Windows uses os.startfile (a missing file association raises OSError), the
    # others launch open/xdg-open through subprocess. Patching Popen on Windows
    # removed nothing, and the test really opened the folder.
    if os.name == "nt":
        monkeypatch.setattr(ui.os, "startfile", missing, raising=False)
    else:
        monkeypatch.setattr(ui.subprocess, "Popen", missing)
    status, payload = call(base, token, "/api/open", {"path": str(target)})
    assert status == 400, payload
    assert "打开" in payload["error"] and str(target) in payload["error"]


def test_the_page_declares_the_operation_list_before_using_it(server):
    """The dropdown is filled by script that runs top to bottom.

    It was built from a const declared further down, which is a ReferenceError and
    left the whole controls section dead while the page still looked fine. A browser
    is not available in CI, so the ordering itself is the assertion.
    """
    base, token, _ = server
    with urllib.request.urlopen(base + "/?" + urllib.parse.urlencode({"token": token}), timeout=30) as response:
        body = response.read().decode("utf-8")
    assert "__OPERATIONS__" not in body
    script = body[body.index("<script>"):]
    declaration = script.index("const OPERATIONS")
    assert declaration < script.index("OPERATIONS.map"), script[:declaration]
    assert declaration < script.index("document.getElementById"), script[:declaration]
    assert '"pdf_merge"' in body and "RAG 预处理" in body


def test_the_page_script_has_no_newline_inside_a_string_literal(server):
    """A newline inside a JS string literal is a SyntaxError that kills the script.

    The page is a Python string, so a JavaScript newline escape that is not doubled
    becomes a real newline in the served script. The status line did exactly that:
    every HTTP test passed while a real browser parsed nothing at all. The second
    assertion feeds the scanner a known-broken page, so a scanner that stopped
    detecting the bug could not silently turn this test green.
    """
    base, token, _ = server
    with urllib.request.urlopen(base + "/?" + urllib.parse.urlencode({"token": token}),
                                timeout=30) as response:
        page = response.read().decode("utf-8")
    _, detected = js_newline_offenders("<script>var a = \"line1\nline2\";</script>")
    assert detected, "扫描器失效：构造出的坏页面没有被识别"
    quote, offenders = js_newline_offenders(page)
    assert quote is None, "页面脚本里的字符串没有闭合（引号 %r 未结束）" % quote
    assert not offenders, (
        "JS 字符串字面量里出现了真实换行（Python 模板中的换行转义要写两次）：%r" % offenders)


def test_a_second_start_is_rejected_while_one_runs(server, tmp_path):
    """Without this the first conversion keeps running while the page shows the second."""
    base, token, _ = server
    sources = [str(copy_fixture(tmp_path / "忙", "忙%d.pdf" % index)) for index in range(8)]
    out = tmp_path / "out"
    status, _ = call(base, token, "/api/start", {"inputs": sources, "output": str(out),
                                                 "operation": "pdf_images", "dpi": 300})
    assert status == 200
    try:
        status, payload = call(base, token, "/api/start", {"inputs": sources, "output": str(out),
                                                           "operation": "pdf_images", "dpi": 300})
        assert status == 400 and "已有任务" in payload["error"], payload
    finally:
        call(base, token, "/api/cancel", {})
        wait_for(base, token, lambda s: s.get("state") in ("cancelled", "complete", "failed"), seconds=120)


def test_ui_dispatch_does_not_swallow_a_file_named_ui():
    """"ui" is a normal folder name; converting it must not start the UI instead."""
    import chamo

    assert chamo.is_ui_command(["ui"]) is True
    assert chamo.is_ui_command(["ui", "--no-browser", "--port", "8000"]) is True
    assert chamo.is_ui_command(["ui", "--port=8000"]) is True
    assert chamo.is_ui_command(["ui", "-o", "输出", "-t", "pdf-images"]) is False
    assert chamo.is_ui_command(["ui", "--output=输出", "--operation=pdf-images"]) is False
    # argparse also accepts the attached spellings, so a real conversion must too.
    assert chamo.is_ui_command(["ui", "-o输出", "-tpdf-images"]) is False
    assert chamo.is_ui_command(["ui", "-timage-convert", "-o输出"]) is False
    assert chamo.is_ui_command([]) is False
    assert chamo.is_ui_command(["讲义.pdf", "-o", "输出", "-t", "pdf-images"]) is False


def test_prune_work_keeps_only_the_recent_request_files(tmp_path):
    work = tmp_path / "ui-work"
    work.mkdir()
    for index in range(60):
        path = work / ("ui-request-%d.json" % index)
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (1000 + index, 1000 + index))
    ui.prune_work(work, keep=50)
    remaining = sorted(work.glob("ui-request-*.json"))
    assert len(remaining) == 50
    assert remaining[0].name == "ui-request-10.json" and remaining[-1].name == "ui-request-59.json"
    ui.prune_work(work, keep=0)
    assert list(work.glob("ui-request-*.json")) == []


def test_quit_stops_the_server(tmp_path):
    instance, token = ui.make_server(0, work=tmp_path / "ui-work")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % instance.server_address[1]
    status, payload = call(base, token, "/api/quit", {})
    assert status == 200 and payload["bye"] is True
    thread.join(timeout=10)
    assert not thread.is_alive(), "the server kept running after quit"
    instance.server_close()


def test_packaged_acceptance_resolves_relative_paths(tmp_path, monkeypatch):
    """CI caught a relative --package: the child ran in the scratch folder.

    The acceptance script starts the launcher with cwd set to its scratch directory,
    so a relative path to the package resolved inside that directory instead of the
    checkout. The packaged Windows job is where this surfaced; this is the cheap
    version of the same regression.
    """
    import argparse

    sys.path.insert(0, str(ROOT / "scripts"))
    import verify_ui

    package = tmp_path / "chamo"
    (package / "python").mkdir(parents=True)
    (package / "engine").mkdir()
    (package / "python" / ("python.exe" if os.name == "nt" else "python3")).write_text("")
    (package / "engine" / "chamo.py").write_text("")
    monkeypatch.chdir(tmp_path)

    relative = argparse.Namespace(package="chamo", chamo=None)
    verify_ui.resolve_args(relative)
    assert Path(relative.package).is_absolute() and Path(relative.package) == package.resolve()
    command = verify_ui.command_for(relative)
    assert all(os.path.isabs(part) for part in command[:1] + command[-1:]), command
    assert Path(command[-1]).is_file()

    launcher = tmp_path / "chamo-cli"
    launcher.write_text("#!/bin/sh\n") 
    resolved = argparse.Namespace(package=None, chamo=str(launcher))
    verify_ui.resolve_args(resolved)
    assert Path(resolved.chamo) == launcher.resolve()


def test_every_operation_the_page_offers_converts(server, tmp_path):
    """Every operation the page offers has a real conversion in this file.

    This table covers the ones with the same one-in/one-product shape; pdf_images is
    covered by the single/batch/cancel tests and the two Office operations have their
    own tests because they need a renderer. The page used to send a partial config:
    image_convert read cfg['format'] from a request the page never put it in, so
    every image conversion ended "失败 'format'" while the page looked healthy. One
    case per operation is the cheap way to stop that class of bug coming back.
    """
    base, token, _ = server
    group = tmp_path / "一组"
    group.mkdir()
    png = group / "透明图片.png"
    shutil.copyfile(FIXTURES / "透明图片.png", png)
    pdf = copy_fixture(group, "讲义.pdf")
    pdf2 = copy_fixture(group, "讲义2.pdf")
    rag_dir = tmp_path / "组-RAG"
    rag_dir.mkdir()
    shutil.copyfile(FIXTURES / "透明图片.png", rag_dir / "透明图片.png")

    cases = [
        ("image_convert", [str(png)], {"format": "jpeg"}, {"透明图片.jpg"}),
        ("image_compress", [str(png)], {}, {"透明图片.png"}),
        # pdf_compress must convert through the same request path.
        # The body deliberately carries no dpi/quality: the lossless mode must not
        # send them (their CLI semantics say they only exist under --lossy).
        ("pdf_compress", [str(pdf)], {}, {"讲义_压缩.pdf"}),
        ("images_pdf", [str(png)], {}, {"透明图片_合并.pdf"}),
        ("pdf_split", [str(pdf)], {"pdf_pages": 3},
         {"讲义.pdf_part000001.pdf", "讲义.pdf_part000002.pdf"}),
        ("pdf_merge", [str(pdf), str(pdf2)], {}, {"讲义_合并.pdf"}),
        # A byte-identical copy keeps the source's own name: the strict export is
        # "the source itself", and renaming it to 透明图片.png_part000001.png changed
        # what the user gets (and made the benchmark's name-based check report a
        # correct export as missing).
        ("rag", [str(rag_dir)], {}, {"组-RAG/透明图片.png"}),
    ]
    for operation, inputs, extra, expected in cases:
        out = tmp_path / ("out-" + operation)
        body = dict({"inputs": inputs, "output": str(out), "operation": operation}, **extra)
        status, started = call(base, token, "/api/start", body)
        assert status == 200 and started.get("started"), (operation, started)
        state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
        assert state["state"] == "complete", (operation, state)
        assert state["error"] == "", (operation, state)
        assert files_under(out) == expected, (operation, files_under(out))


def test_office_conversion_through_the_ui(server, tmp_path):
    """Office work goes through the same request path; skip when no renderer."""
    sys.path.insert(0, str(ROOT / "engine"))
    import platform_office

    renderer = platform_office.find(ROOT / "engine")
    if not renderer:
        pytest.skip("no Office renderer on this machine")
    base, token, _ = server
    source = tmp_path / "课件" / "示例课件.pptx"
    source.parent.mkdir()
    shutil.copyfile(FIXTURES / "示例课件.pptx", source)
    out = tmp_path / "out-office"
    status, started = call(base, token, "/api/start", {"inputs": [str(source)], "output": str(out),
                                                       "operation": "office_pdf"})
    assert status == 200 and started.get("started"), started
    state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    assert state["state"] == "complete", state
    produced = files_under(out)
    assert len(produced) == 1 and next(iter(produced)).endswith(".pdf"), produced


def test_office_images_through_the_ui(server, tmp_path):
    """The second Office operation: a deck becomes one image per slide."""
    sys.path.insert(0, str(ROOT / "engine"))
    import platform_office

    if not platform_office.find(ROOT / "engine"):
        pytest.skip("no Office renderer on this machine")
    base, token, _ = server
    source = tmp_path / "课件图片" / "示例课件.pptx"
    source.parent.mkdir()
    shutil.copyfile(FIXTURES / "示例课件.pptx", source)
    out = tmp_path / "out-office-images"
    status, started = call(base, token, "/api/start", {"inputs": [str(source)], "output": str(out),
                                                       "operation": "office_images", "format": "png"})
    assert status == 200 and started.get("started"), started
    state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    assert state["state"] == "complete", state
    produced = files_under(out)
    # The 2-slide fixture renders as 示例课件/示例课件_001.png + _002.png, no manifest.
    assert produced == {"示例课件/示例课件_001.png", "示例课件/示例课件_002.png"}, produced
    assert all((out / name).stat().st_size > 0 for name in produced)


def test_post_endpoints_need_the_token(server):
    base, token, _ = server
    for path, body in (("/api/start", {}), ("/api/cancel", {}),
                       ("/api/open", {}), ("/api/quit", {})):
        status, _ = call(base, token, path, body, token_override="wrong")
        assert status == 403, path


def test_a_non_ascii_token_is_rejected_not_dropped(server):
    """compare_digest raises TypeError on non-ASCII; an anonymous caller controls it."""
    base, token, _ = server
    status, _ = call(base, token, "/api/status", token_override="令牌")
    assert status == 403


def test_quit_cancels_the_running_conversion(tmp_path):
    """Closing the window must stop the work, not orphan it into the output folder."""
    instance, token = ui.make_server(0, work=tmp_path / "ui-work")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % instance.server_address[1]
    sources = [str(copy_fixture(tmp_path / "退出", "退出%d.pdf" % index)) for index in range(8)]
    out = tmp_path / "out"
    status, started = call(base, token, "/api/start", {"inputs": sources, "output": str(out),
                                                       "operation": "pdf_images", "dpi": 300})
    assert status == 200 and started.get("started"), started
    # Quit only once the batch is demonstrably under way. A fixed sleep could let a
    # fast machine finish first, and then "the batch was cancelled" is not provable.
    deadline = time.monotonic() + 60
    partial = set()
    while time.monotonic() < deadline and not partial:
        partial = published_under(out)
        time.sleep(0.05)
    assert partial, "the batch wrote nothing before the quit, so cancellation is unproven"
    status, payload = call(base, token, "/api/quit", {})
    assert status == 200 and payload["bye"] is True, payload
    # "stopped" alone is also true when nothing was running; "cancelled" is the
    # evidence that the quit actually reached the engine's control channel.
    assert payload["cancelled"] is True and payload["stopped"] is True, payload
    thread.join(timeout=30)
    assert not thread.is_alive(), "the port stayed open after quit"
    instance.server_close()
    before = files_under(out)
    time.sleep(3)
    assert files_under(out) == before, "the conversion kept writing after quit"
    published = published_under(out)
    # 0 is a valid outcome: cancelling an item rolls back the pages it had already
    # published, so a cancel that lands during the first file leaves no product at
    # all (this flaked once on a slower CI runner). Completion is still caught by
    # the upper bound plus the cancelled flag asserted above; the other cancel test
    # makes the same allowance.
    assert len(published) < 8 * PAGES, (
        "the batch finished instead of being cancelled: %r" % sorted(published))
    assert not (out / "清单.json").exists()


def test_the_page_offers_pdf_compression(server):
    """The operation table is the page's only source of operations.

    pdf_compress is one of the operations. A page that cannot select it, or a server-side list
    that omits it, is the "page looks healthy while nothing converts" bug this
    file exists to catch.
    """
    base, token, _ = server
    offered = {name for name, _ in ui.OPERATIONS}
    assert "pdf_compress" in offered, "操作表缺少 pdf_compress：%r" % sorted(offered)
    labels = dict(ui.OPERATIONS)
    assert "压缩" in labels["pdf_compress"], (
        "操作显示名应说明是 PDF 压缩：%r" % labels["pdf_compress"])
    with urllib.request.urlopen(base + "/?" + urllib.parse.urlencode({"token": token}),
                                timeout=30) as response:
        body = response.read().decode("utf-8")
    assert '"pdf_compress"' in body, "页面脚本没有下发 pdf_compress 操作"
    # Lossy is an explicit user decision, so the page must offer that choice and
    # send the lossy field; a lossless-only page would misrepresent the operation.
    assert "lossy" in body, "页面没有「无损/有损」选择（payload 需要 lossy 字段）"


def test_pdf_compress_lossy_through_the_ui(server, tmp_path):
    """A real lossy conversion through /api/start: same pages, smaller product.

    The fixture is image-only, so the rebuild can genuinely be smaller; the
    assertions read the published PDF, not the status text.
    """
    import io
    import random

    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    base, token, _ = server
    source = tmp_path / "扫描件" / "扫描.pdf"
    source.parent.mkdir(parents=True)
    pixel_w, pixel_h, source_dpi = 600, 800, 150
    page_w, page_h = pixel_w * 72.0 / source_dpi, pixel_h * 72.0 / source_dpi
    rng = random.Random(3)
    sheet = canvas.Canvas(str(source), pagesize=(page_w, page_h))
    image = Image.frombytes("RGB", (pixel_w, pixel_h), rng.randbytes(pixel_w * pixel_h * 3))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    sheet.drawImage(ImageReader(buffer), 0, 0, width=page_w, height=page_h)
    sheet.showPage()
    sheet.save()
    original_size = source.stat().st_size

    out = tmp_path / "out-pdf-compress-lossy"
    status, started = call(base, token, "/api/start",
                           {"inputs": [str(source)], "output": str(out),
                            "operation": "pdf_compress", "lossy": True,
                            "dpi": 100, "quality": 60})
    assert status == 200 and started.get("started"), started
    state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    assert state["state"] == "complete", state
    assert state["error"] == "", state
    assert files_under(out) == {"扫描_压缩.pdf"}, files_under(out)

    pikepdf = pytest.importorskip("pikepdf")
    product = out / "扫描_压缩.pdf"
    with pikepdf.open(product) as pdf:
        assert len(pdf.pages) == 1, "lossy product must keep the page count"
    assert product.stat().st_size < original_size, (
        "lossy rebuild must be smaller than the original to be published: "
        "%d >= %d bytes" % (product.stat().st_size, original_size))
    assert not (out / "清单.json").exists(), "a single file gets no manifest"


# MARK: - image_compress: the page used to hard-code lossy=False, so JPEG could
# never run through the browser UI. These tests drive the HTTP API, then read the
# request file the UI wrote to disk so the payload assertions cannot drift from
# what is actually sent.


def _png_source(tmp_path: Path) -> Path:
    source = tmp_path / "输入" / "透明图片.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "透明图片.png", source)
    return source


def test_image_compress_lossless_payload_and_pixels(server, tmp_path):
    """The default stays lossless: PNG-only payload, pixel-identical product."""
    from PIL import Image

    base, token, instance = server
    source = _png_source(tmp_path)
    out = tmp_path / "out-image-lossless"
    status, started = call(base, token, "/api/start",
                           {"inputs": [str(source)], "output": str(out),
                            "operation": "image_compress"})
    assert status == 200 and started.get("started"), started
    cfg = last_request(instance)["config"]
    assert cfg["operation"] == "image_compress"
    assert cfg["lossy"] is False, cfg
    assert "quality" not in cfg, "无损请求不应携带 quality：%r" % cfg
    assert "max_edge" not in cfg, "无损请求不应携带 max_edge：%r" % cfg

    state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    assert state["state"] == "complete" and state["error"] == "", state
    assert files_under(out) == {"透明图片.png"}, files_under(out)
    with Image.open(source) as before, Image.open(out / "透明图片.png") as after:
        before.load()
        after.load()
        assert before.size == after.size, (before.size, after.size)
        assert before.convert("RGBA").tobytes() == after.convert("RGBA").tobytes(),             "无损档必须保持像素完全一致"


def test_image_compress_lossy_payload_and_product(server, tmp_path):
    """A real JPEG conversion through the page: smaller file, real max_edge."""
    from PIL import Image

    base, token, instance = server
    source = _png_source(tmp_path)
    out = tmp_path / "out-image-lossy"
    status, started = call(base, token, "/api/start",
                           {"inputs": [str(source)], "output": str(out),
                            "operation": "image_compress", "lossy": True,
                            "quality": 70, "max_edge": 200})
    assert status == 200 and started.get("started"), started
    cfg = last_request(instance)["config"]
    assert cfg["lossy"] is True and cfg["format"] == "jpeg", cfg
    assert cfg["quality"] == 70 and cfg["max_edge"] == 200, cfg

    state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    assert state["state"] == "complete" and state["error"] == "", state
    assert files_under(out) == {"透明图片.jpg"}, files_under(out)
    product = out / "透明图片.jpg"
    assert product.stat().st_size < source.stat().st_size, (
        "有损 JPEG 应比源 PNG 小：%d >= %d" % (product.stat().st_size, source.stat().st_size))
    with Image.open(product) as produced:
        produced.load()
        assert produced.format == "JPEG", produced.format
        assert max(produced.size) == 200, produced.size


def test_image_compress_lossy_defaults_match_the_cli(server, tmp_path):
    """quality defaults to 90 and max_edge to 0, exactly like the CLI."""
    base, token, instance = server
    source = _png_source(tmp_path)
    out = tmp_path / "out-image-defaults"
    status, started = call(base, token, "/api/start",
                           {"inputs": [str(source)], "output": str(out),
                            "operation": "image_compress", "lossy": True})
    assert status == 200 and started.get("started"), started
    cfg = last_request(instance)["config"]
    assert (cfg["quality"], cfg["max_edge"], cfg["format"]) == (90, 0, "jpeg"), cfg
    state = wait_for(base, token, lambda s: s.get("state") in ("complete", "failed", "cancelled"))
    assert state["state"] == "complete", state
    assert files_under(out) == {"透明图片.jpg"}, files_under(out)


def test_image_compress_rejects_invalid_or_ignored_parameters(server, tmp_path):
    """Bad values fail before the engine starts; lossy-only fields are not dropped."""
    base, token, instance = server
    source = _png_source(tmp_path)
    out = tmp_path / "out-image-invalid"
    cases = [
        ({"lossy": True, "quality": 0}, "质量"),
        ({"lossy": True, "quality": 101}, "质量"),
        ({"lossy": True, "quality": "abc"}, "质量"),
        ({"lossy": True, "max_edge": -1}, "最长边"),
        ({"lossy": True, "max_edge": 16001}, "最长边"),
        ({"lossy": True, "max_edge": "abc"}, "最长边"),
        # Lossless must not silently ignore settings that only exist in lossy mode.
        ({"lossy": False, "quality": 50}, "无损"),
        ({"lossy": False, "max_edge": 100}, "无损"),
    ]
    for extra, expected in cases:
        status, payload = call(base, token, "/api/start",
                               dict({"inputs": [str(source)], "output": str(out),
                                     "operation": "image_compress"}, **extra))
        assert status == 400, (extra, status, payload)
        assert expected in payload["error"], (extra, payload)
    assert not list(instance.ui_work.glob("ui-request-*.json")),         "被拒绝的请求不应写出 request 文件或启动转换"
    assert not out.exists(), "被拒绝的请求不应创建输出目录"


def test_image_compress_lossless_refuses_non_png_without_switching_to_lossy(server, tmp_path):
    """A JPEG source in the lossless mode gets a readable refusal, not a silent re-encode."""
    from PIL import Image

    base, token, instance = server
    source = tmp_path / "输入" / "照片.jpg"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (64, 48), (200, 40, 40)).save(source, "JPEG", quality=90)
    out = tmp_path / "out-image-refused"
    status, payload = call(base, token, "/api/start",
                           {"inputs": [str(source)], "output": str(out),
                            "operation": "image_compress"})
    assert status == 400, payload
    assert "PNG" in payload["error"] and "有损" in payload["error"], payload
    assert not list(instance.ui_work.glob("ui-request-*.json")), "拒绝时不应启动引擎"
    assert not out.exists(), "拒绝时不得产出任何文件（不得偷偷有损）"


def test_the_page_offers_image_compression_modes(server):
    """Both modes and their controls must reach the browser, not only the API."""
    base, token, _ = server
    labels = dict(ui.OPERATIONS)
    assert "压缩" in labels["image_compress"], labels["image_compress"]
    with urllib.request.urlopen(base + "/?" + urllib.parse.urlencode({"token": token}),
                                timeout=30) as response:
        body = response.read().decode("utf-8")
    assert '"image_compress"' in body, "页面脚本没有下发 image_compress 操作"
    for control in ("imageCompressMode", "imageQuality", "imageMaxEdge"):
        assert control in body, "页面缺少控件 %s" % control
    assert "无损（PNG，像素不变）" in body and "有损（JPEG" in body
    assert "不会自动改成有损" in body, "页面需要说明无损档不会偷偷转为有损"
    assert "body.quality" in body and "body.max_edge" in body, "页面脚本没有提交质量/最长边"
