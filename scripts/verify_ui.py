#!/usr/bin/env python3
"""Acceptance for the local UI, driven through the launcher users actually run.

tests/test_ui.py imports engine/ui.py and calls the same HTTP API in-process, which
is the right place to test the handlers. It cannot prove that the artifact ships the
front end, that "chamo ui" starts from an installed .deb or the portable Windows ZIP,
that the bundled runtime has every import, or that the URL it prints is reachable.
Those are the failures that only the package can expose, so this script launches the
real command, then checks the disk and the page.

It also asserts the boundary the page promises: bound to loopback only, every /api
call token-guarded, no external URL in the page, and the output folder holds the
conversion products and nothing else.

Usage:
    python scripts/verify_ui.py                        # this checkout, current Python
    python scripts/verify_ui.py --chamo /usr/bin/chamo     # installed Debian package
    python scripts/verify_ui.py --package dist/extracted/chamo   # Windows portable ZIP

Exit code 0 means every check passed; the last line is "UI ACCEPTANCE: PASS/FAIL".
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# The acceptance runs under the package interpreter, which always ships Pillow
# (the engine needs it to convert images); decoding the produced JPEG back is
# part of the image-compression promise, so reading it is not optional.
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "示例讲义.pdf"
IMAGE_FIXTURE = ROOT / "tests" / "fixtures" / "透明图片.png"
PAGES = 6   # page count of FIXTURE; cancel checks compare against it, never a guess
URL_PATTERN = re.compile(r"http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_\-]+)")

FAILURES: list[str] = []
PASSED = 0


def check(name: str, actual, expected) -> None:
    global PASSED
    if actual == expected:
        PASSED += 1
        print("PASS %s (%r)" % (name, actual), flush=True)
    else:
        FAILURES.append(name)
        print("FAIL %s: got %r want %r" % (name, actual, expected), flush=True)


def note(name: str, detail: str) -> None:
    print("NOTE %s: %s" % (name, detail), flush=True)


def files_under(root: Path) -> set:
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def published_under(root: Path) -> set:
    """Products already moved into the user's folder; staging does not count.

    The engine renders into "<output>/.Chamo暂存" and publishes atomically, so a
    staging file appearing proves work started but not that anything was published.
    Waiting on files_under() here made the quit test quit before the first product
    and then see an empty folder: the incompleteness check failed on a real cancel.
    """
    staging = ".Chamo暂存/"
    return {name for name in files_under(root) if not name.startswith(staging)}


def resolve_args(args):
    """Resolve every path the launcher will see before it starts.

    CI caught this the expensive way: a relative --package worked on paper, but the
    child runs in the scratch directory, so Windows looked for
    "dist/extracted/chamo/engine/chamo.py" inside the scratch folder and the run died
    before printing a URL. Resolution is a separate step so a test can assert it.
    """
    for attribute in ("package", "chamo"):
        value = getattr(args, attribute, None)
        if value:
            setattr(args, attribute, str(Path(value).expanduser().resolve()))
    return args


def command_for(args) -> list:
    """The installed command first: that is the thing whose absence users would hit."""
    if args.package:
        package = Path(args.package)
        exe = package / "python" / ("python.exe" if os.name == "nt" else "python3")
        if not exe.is_file():
            exe = package / "python" / "python"
        return [str(exe), "-I", "-B", str(package / "engine" / "chamo.py")]
    if args.chamo:
        base = [str(Path(args.chamo))]
        if os.name == "nt" and base[0].lower().endswith((".cmd", ".bat")):
            base = [os.environ.get("COMSPEC", "cmd.exe"), "/c"] + base
        return base
    return [sys.executable, "-I", "-B", str(ROOT / "engine" / "chamo.py")]


def opener_shim(work: Path, marker: Path):
    """A fake platform opener on PATH, so the open button is observed, not launched.

    On a headless machine the real opener either does not exist (minimal Linux) or
    would pop a window on a developer's screen during a test run.
    """
    if os.name == "nt":
        return None
    shim = work / "shim"
    shim.mkdir(parents=True, exist_ok=True)
    name = "open" if sys.platform == "darwin" else "xdg-open"
    script = shim / name
    script.write_text("#!/bin/sh\nprintf '%%s' \"$1\" > \"%s\"\n" % marker, encoding="utf-8")
    script.chmod(0o755)
    return shim


def isolated_env(home: Path, state: Path, ui_work: Path, shim) -> dict:
    """A fresh user profile per run: the state folder layout is part of the promise."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CFFIXED_USER_HOME"] = str(home)          # macOS: Foundation ignores HOME
    env["CHAMO_STATE_DIR"] = str(state)
    env["CHAMO_UI_WORK"] = str(ui_work)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if shim is not None:
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    return env


class Client:
    """The browser's side, with proxies off: a proxy must not intercept 127.0.0.1."""

    def __init__(self, base: str, token: str):
        self.base, self.token = base, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _url(self, path: str, params=None, token=None) -> str:
        query = dict(params or {})
        query["token"] = self.token if token is None else token
        return self.base + path + "?" + urllib.parse.urlencode(query)

    def call(self, path: str, body=None, params=None, token=None, timeout: int = 120):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self._url(path, params, token), data=data,
            method="GET" if body is None else "POST",
            headers={"content-type": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"raw": raw}

    def page(self, token=None):
        try:
            with self.opener.open(self._url("/", None, token), timeout=60) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def wait(self, predicate, seconds: float = 240, step: float = 0.4):
        deadline = time.monotonic() + seconds
        state = {}
        while time.monotonic() < deadline:
            _, state = self.call("/api/status")
            if predicate(state):
                return state
            time.sleep(step)
        raise AssertionError("timed out; last status %r" % (state,))


def start_server(cmd, env, work: Path):
    """Launch "chamo ui" and wait for the URL it prints."""
    process = subprocess.Popen(
        list(cmd) + ["ui", "--no-browser", "--port", "0"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env, cwd=str(work))
    lines: list[str] = []

    def pump():
        for line in process.stdout:
            lines.append(line.rstrip("\n"))

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    deadline = time.monotonic() + 120
    url = None
    while time.monotonic() < deadline:
        for line in lines:
            match = URL_PATTERN.search(line)
            if match:
                url = "http://127.0.0.1:%s" % match.group(1)
                token = match.group(2)
                return process, url, token, lines
        if process.poll() is not None:
            break
        time.sleep(0.2)
    process.kill()
    raise AssertionError("chamo ui never printed a URL; output:\n%s" % "\n".join(lines[-20:]))


def loopback_only(port: int) -> tuple:
    """Connecting through a non-loopback local address must fail.

    A server bound to 0.0.0.0 would answer here, which on a shared network would
    expose the page and its token to other machines. When the host has no address
    other than loopback there is nothing to probe.
    """
    candidates = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            address = info[4][0]
            if ":" in address or address.startswith("127."):
                continue
            candidates.add(address)
    except OSError:
        pass
    if not candidates:
        return None, "no non-loopback address on this host"
    for address in sorted(candidates):
        connection = http.client.HTTPConnection(address, port, timeout=3)
        try:
            connection.connect()
        except OSError:
            continue
        finally:
            connection.close()
        return False, "reachable via %s" % address
    return True, "tried %s" % ", ".join(sorted(candidates))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Acceptance for the local browser UI")
    parser.add_argument("--chamo", help="installed launcher (e.g. /usr/bin/chamo)")
    parser.add_argument("--package", help="portable package directory (Windows ZIP)")
    parser.add_argument("--work", help="scratch directory (default: a temp dir)")
    parser.add_argument("--fixture", default=str(FIXTURE))
    args = parser.parse_args(argv)

    args = resolve_args(args)
    fixture = Path(args.fixture).expanduser().resolve()
    if not fixture.is_file():
        print("FAIL fixture missing: %s" % fixture)
        return 1
    if not IMAGE_FIXTURE.is_file():
        print("FAIL image fixture missing: %s" % IMAGE_FIXTURE)
        return 1
    work = (Path(tempfile.mkdtemp(prefix="chamo-ui-")) if not args.work
            else Path(args.work).expanduser().resolve())
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    home, state, ui_work = work / "home", work / "state", work / "ui-work"
    marker = work / "opened.txt"
    shim = opener_shim(work, marker)
    env = isolated_env(home, state, ui_work, shim)
    cmd = command_for(args)

    process = None
    try:
        process, base, token, lines = start_server(cmd, env, work)
        port = int(base.rsplit(":", 1)[1])
        check("chamo ui prints a loopback URL with a token", bool(URL_PATTERN.search("\n".join(lines))), True)
        check("the URL is 127.0.0.1", base.startswith("http://127.0.0.1:"), True)
        check("the token is long enough to be unguessable", len(token) >= 20, True)
        check("process still running after start", process.poll(), None)

        client = Client(base, token)
        status, page = client.page()
        check("page served", status, 200)
        check("page is the workbench", "Chamo" in page and "/api/start" in page, True)
        check("page lists the operations", '"pdf_merge"' in page and "RAG 预处理" in page, True)
        check("no unsubstituted placeholder in the page", "__OPERATIONS__" in page, False)
        check("page loads nothing from the network",
              any(url in page for url in ("http://", "https://", "//cdn")), False)

        status, _ = client.page(token="wrong")
        check("page rejects a wrong token", status, 403)
        for path in ("/api/status", "/api/browse", "/api/open"):
            status, _ = client.call(path, token="wrong")
            check("%s rejects a wrong token" % path, status, 403)
        # The POST side too: a page in another tab must not be able to start or
        # cancel work, and the code path is separate from the GET handlers.
        for path, body in (("/api/start", {}), ("/api/cancel", {}),
                           ("/api/open", {}), ("/api/quit", {})):
            status, _ = client.call(path, body, token="wrong")
            check("%s rejects a wrong token" % path, status, 403)
        status, _ = client.call("/api/status", token="令牌")
        check("a non-ascii token is rejected, not dropped", status, 403)

        listing_dir = work / "输入"
        listing_dir.mkdir()
        shutil.copyfile(fixture, listing_dir / "讲义.pdf")
        status, listing = client.call("/api/browse", params={"path": str(listing_dir)})
        check("browse works", status, 200)
        check("browse lists the folder", [e["name"] for e in listing["entries"]], ["讲义.pdf"])
        status, _ = client.call("/api/browse", params={"path": str(work / "missing")})
        check("browse reports a missing folder", status, 400)

        status, payload = client.call("/api/start", {"inputs": [], "output": str(work / "out"),
                                                     "operation": "pdf_images"})
        check("start rejects an empty list", status, 400)
        check("start says what is missing", "先加入" in payload.get("error", ""), True)

        # 1. Single file: products only, no manifest, no engine bookkeeping.
        source = work / "单个" / "验收.pdf"
        source.parent.mkdir()
        shutil.copyfile(fixture, source)
        single_out = work / "out-single"
        status, started = client.call("/api/start", {"inputs": [str(source)], "output": str(single_out),
                                                     "operation": "pdf_images", "dpi": 72, "range": "1-1"})
        check("single conversion accepted", status, 200)
        state_now = client.wait(lambda s: s.get("state") in ("complete", "failed", "cancelled"))
        check("single conversion finished", state_now.get("state"), "complete")
        check("single output folder is clean", files_under(single_out), {"验收/验收_001.png"})
        check("single run wrote no manifest", (single_out / "清单.json").exists(), False)
        check("state lives outside the output folder", state.is_dir(), True)
        check("engine records are in the state root",
              any((state / "outputs").glob("*/records/run-*")), True)
        check("the request file stays out of the output folder",
              any((ui_work).glob("ui-request-*.json")), True)
        check("progress reached the page", any("完成" in line for line in state_now["lines"]), True)

        # 2. Batch: products plus exactly one manifest.
        batch = work / "批量"
        batch.mkdir()
        for name in ("甲.pdf", "乙.pdf"):
            shutil.copyfile(fixture, batch / name)
        batch_out = work / "out-batch"
        client.call("/api/start", {"inputs": [str(batch / "甲.pdf"), str(batch / "乙.pdf")],
                                   "output": str(batch_out), "operation": "pdf_images",
                                   "dpi": 72, "range": "1-1"})
        state_now = client.wait(lambda s: s.get("state") in ("complete", "failed", "cancelled"))
        check("batch finished", state_now.get("state"), "complete")
        check("batch output folder is products plus one manifest",
              files_under(batch_out), {"甲/甲_001.png", "乙/乙_001.png", "清单.json"})
        manifest = json.loads((batch_out / "清单.json").read_text(encoding="utf-8"))
        check("manifest counts add up",
              manifest["成功"] + manifest["异常"], manifest["任务数"])
        check("manifest lists both sources",
              sorted(Path(s).name for entry in manifest["明细"] for s in entry["源文件"]),
              ["乙.pdf", "甲.pdf"])

        # 2b. Image compression: the page used to hard-code lossy=False, so JPEG
        # could never run from the browser UI. Drive the opt-in lossy mode through
        # the same /api/start the page uses, then read the product back from disk.
        image_dir = work / "图片压缩"
        image_dir.mkdir()
        image_source = image_dir / "透明图片.png"
        shutil.copyfile(IMAGE_FIXTURE, image_source)
        compress_out = work / "out-image-compress"
        status, started = client.call("/api/start",
                                      {"inputs": [str(image_source)],
                                       "output": str(compress_out),
                                       "operation": "image_compress",
                                       "lossy": True, "quality": 70, "max_edge": 200})
        check("image compression accepted", status, 200)
        state_now = client.wait(lambda s: s.get("state") in ("complete", "failed", "cancelled"))
        check("image compression finished", state_now.get("state"), "complete")
        check("image compression output folder is clean",
              files_under(compress_out), {"透明图片.jpg"})
        product = compress_out / "透明图片.jpg"
        check("image compression produced a JPEG", product.is_file(), True)
        if product.is_file():
            with Image.open(product) as produced:
                produced.load()
                check("image compression product opens as JPEG", produced.format, "JPEG")
                check("image compression honours max_edge", max(produced.size), 200)
            check("image compression product is smaller than the source PNG",
                  product.stat().st_size < image_source.stat().st_size, True)

        # 3. Open: the shim records what the platform opener was asked to show.
        if shim is not None:
            status, opened = client.call("/api/open", {"path": str(batch_out)})
            check("open accepts an existing result", status, 200)
            # The opener is a separate process: the request returns before it runs.
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            check("open handed the result to the platform opener",
                  marker.read_text(encoding="utf-8") if marker.exists() else "<never called>",
                  str(batch_out))
        else:
            note("open", "skipped the real launch on Windows; the error path is checked below")
        status, payload = client.call("/api/open", {"path": str(work / "nothing-here")})
        check("open reports a missing result", status, 400)
        check("open explains it", "不存在" in payload.get("error", ""), True)

        # 4. Cancel: a cancelled batch stops and leaves no manifest behind.
        cancel_dir = work / "取消"
        cancel_dir.mkdir()
        cancelled_sources = []
        for index in range(8):
            target = cancel_dir / ("取消%d.pdf" % index)
            shutil.copyfile(fixture, target)
            cancelled_sources.append(str(target))
        cancel_out = work / "out-cancel"
        status, started = client.call("/api/start", {"inputs": cancelled_sources,
                                                     "output": str(cancel_out),
                                                     "operation": "pdf_images", "dpi": 300})
        check("cancel test run started", status, 200)
        status, response = client.call("/api/cancel", {})
        check("cancel was delivered", response.get("cancelled"), True)
        state_now = client.wait(lambda s: s.get("state") in ("cancelled", "complete", "failed"))
        check("cancelled run reports cancellation", state_now.get("state"), "cancelled")
        check("a cancelled batch writes no manifest", (cancel_out / "清单.json").exists(), False)

        # 5. The boundary, then quit *while work is running*: closing the window must
        # stop the conversion, not orphan it into the user's output folder.
        allowed, detail = loopback_only(port)
        if allowed is None:
            note("loopback-only", detail)
        else:
            check("the server is not reachable from a network address", allowed, True)
            if not allowed:
                note("loopback-only", detail)
        quit_dir = work / "退出"
        quit_dir.mkdir()
        quit_sources = []
        for index in range(8):
            target = quit_dir / ("退出%d.pdf" % index)
            shutil.copyfile(fixture, target)
            quit_sources.append(str(target))
        quit_out = work / "out-quit"
        status, started = client.call("/api/start", {"inputs": quit_sources, "output": str(quit_out),
                                                     "operation": "pdf_images", "dpi": 300})
        check("quit test run started", status, 200)
        # Cancel as soon as there is proof the work is really under way. A fixed
        # sleep let a fast machine finish the batch before the quit, and the old
        # threshold was 8 * 36 files on an 8 x 6-page batch (max 48), so "the run
        # is incomplete" could never fail. Review caught it with a broken quit:
        # only the manifest check went red. Wait for the first product instead.
        started_at = time.monotonic()
        produced = set()
        while time.monotonic() - started_at < 60 and not produced:
            produced = published_under(quit_out)
            time.sleep(0.05)
        check("the quit test produced output before quit", len(produced) > 0, True)
        status, bye = client.call("/api/quit", {})
        check("quit acknowledged", (status, bye.get("bye")), (200, True))
        check("quit reported the run as cancelled", bye.get("cancelled"), True)
        check("quit stopped the running conversion", bye.get("stopped"), True)
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            code = "still running"
        check("chamo ui exits after quit", code, 0)
        settled = files_under(quit_out)
        time.sleep(3)
        check("nothing is written after quit", len(files_under(quit_out)), len(settled))
        published = published_under(quit_out)
        check("the cancelled run is incomplete", 0 < len(published) < 8 * PAGES, True)
        check("the cancelled run wrote no manifest", (quit_out / "清单.json").exists(), False)
        try:
            client.call("/api/status", timeout=5)
            still_up = True
        except (urllib.error.URLError, ConnectionError, OSError):
            still_up = False
        check("the port is closed after quit", still_up, False)
        stray = {p.name for p in work.iterdir() if p.is_file()} - {"opened.txt"}
        check("nothing was written loose in the working folder", stray, set())
        expected_dirs = {"state", "ui-work", "输入", "单个", "批量", "取消", "退出", "图片压缩",
                         "out-single", "out-batch", "out-cancel", "out-quit", "out-image-compress"}
        if shim is not None:
            expected_dirs.add("shim")
        check("the work folder holds only the folders this run created",
              {p.name for p in work.iterdir() if p.is_dir()}, expected_dirs)
    except AssertionError as error:
        FAILURES.append("exception")
        print("FAIL %s" % error, flush=True)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()

    total = PASSED + len(FAILURES)
    if not FAILURES:
        print("UI ACCEPTANCE: PASS (%d checks)" % total, flush=True)
        return 0
    print("UI ACCEPTANCE: FAIL (%d/%d failed: %s)" % (len(FAILURES), total, ", ".join(FAILURES)), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
