#!/usr/bin/env python3
"""A local cross-platform UI for the engine: one page served on 127.0.0.1.

macOS has the SwiftUI app. Windows and Linux ship the CLI only, and the product
goal is a working "pick files -> configure -> convert -> progress/cancel -> open
the result" flow on every platform, not a second converter. So this is a thin local
front end: it serves one page, and every action goes through the same versioned
request/event protocol the app and the CLI already use, which means there is exactly
one engine to trust and no logic to keep in sync.

Why a page instead of a native toolkit: the Windows package ships the embeddable
CPython, which has no tkinter, and a browser is the one UI runtime already present on
all three platforms. Nothing is installed, nothing leaves the machine (the server
binds 127.0.0.1 and every /api call needs the token printed in the URL), and no file
is uploaded anywhere.

Usage:
    python engine/ui.py [--port 0] [--no-browser]
    chamo ui [--port 0] [--no-browser]
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol  # noqa: E402

ENGINE = Path(__file__).resolve().parent / "workbench.py"

# The same names the CLI uses, so the two front ends cannot disagree about an
# operation's spelling. Values are the engine's own operation names.
OPERATIONS = [
    ("pdf_images", "PDF 转图片"),
    ("pdf_split", "严格拆分 PDF"),
    ("pdf_merge", "合并 PDF"),
    ("images_pdf", "图片合并为 PDF"),
    ("office_pdf", "Office 转 PDF"),
    ("office_images", "Office 转图片"),
    ("image_convert", "图片格式转换"),
    ("image_compress", "图片压缩"),
    ("pdf_compress", "PDF 压缩"),
    ("rag", "RAG 预处理"),
]

# PAGE is a normal Python string, so a JavaScript escape such as "\n" has to be
# written "\\n" here. Getting that wrong puts a real newline inside a JS string
# literal, which is a SyntaxError that kills the whole script: this page had dead
# buttons for a release while every HTTP-only test still passed.
PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>Chamo</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{color-scheme:light dark}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;margin:0;padding:24px;max-width:900px}
h1{font-size:20px;margin:0 0 4px}
p.lead{margin:0 0 20px;opacity:.7}
section{border:1px solid color-mix(in srgb,currentColor 18%,transparent);border-radius:10px;padding:14px;margin-bottom:14px}
label{display:block;font-size:12px;opacity:.7;margin-bottom:4px}
input,select,button{font:inherit;padding:6px 8px;border-radius:7px;border:1px solid color-mix(in srgb,currentColor 25%,transparent);background:transparent;color:inherit}
button{cursor:pointer}
button.primary{background:#0a84ff;border-color:#0a84ff;color:#fff}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
ul{list-style:none;margin:8px 0 0;padding:0;max-height:220px;overflow:auto}
li{display:flex;justify-content:space-between;gap:10px;padding:4px 6px;border-radius:6px}
li:hover{background:color-mix(in srgb,currentColor 8%,transparent)}
small{opacity:.65}
pre{white-space:pre-wrap;word-break:break-all;font-size:12px;margin:8px 0 0}
.bar{height:6px;border-radius:3px;background:color-mix(in srgb,currentColor 15%,transparent);overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;width:0;background:#0a84ff}
</style></head><body>
<h1>Chamo</h1>
<p class="lead">本机转换：文件不离开这台电脑。进度与取消走同一条引擎通道。</p>

<section>
  <label>当前目录</label>
  <div class="row"><input id="path" size="46" autocomplete="off"><button id="go">打开</button><button id="up">上级</button><button id="home">主目录</button></div>
  <ul id="entries"></ul>
  <small>点目录进入；点文件加入列表，或点“加入整个文件夹”。</small>
</section>

<section>
  <label>待转换（<span id="count">0</span>）</label>
  <ul id="inputs"></ul>
  <div class="row"><button id="clear">清空列表</button></div>
</section>

<section>
  <div class="row">
    <span><label>转换类型</label><select id="operation"></select></span>
    <span id="dpiBox"><label>导出 DPI</label><input id="dpi" type="number" value="150" min="36" max="600" size="5"></span>
    <span id="formatBox"><label>图片格式</label><select id="format"><option>png</option><option>jpeg</option><option>webp</option><option>tiff</option><option>bmp</option></select></span>
    <span id="rangeBox"><label>页范围</label><input id="range" size="8" placeholder="1-3,5"></span>
  </div>
  <div class="row" id="compressBox" style="display:none;margin-top:10px">
    <span><label>压缩方式</label><select id="compressMode">
      <option value="lossless">无损清理（页面内容不变）</option>
      <option value="lossy">有损重采样（文字层变为图片）</option>
    </select></span>
    <span class="lossyOnly" style="display:none"><label>有损 DPI</label><input id="compressDpi" type="number" value="150" min="72" max="600" size="5"></span>
    <span class="lossyOnly" style="display:none"><label>JPEG 质量</label><input id="compressQuality" type="number" value="75" min="1" max="100" size="5"></span>
  </div>
  <div class="row" id="imageCompressBox" style="display:none;margin-top:10px">
    <span><label>压缩方式</label><select id="imageCompressMode">
      <option value="lossless">无损（PNG，像素不变）</option>
      <option value="lossy">有损（JPEG，重新编码）</option>
    </select></span>
    <span class="imageLossyOnly" style="display:none"><label>JPEG 质量</label><input id="imageQuality" type="number" value="90" min="1" max="100" size="5"></span>
    <span class="imageLossyOnly" style="display:none"><label>最长边（0 不限制）</label><input id="imageMaxEdge" type="number" value="0" min="0" max="16000" size="6"></span>
  </div>
  <div class="row" id="imageCompressHint" style="display:none;margin-top:6px">
    <small>无损档只优化 PNG（像素完全不变）；JPEG/WebP 等请选「有损（JPEG）」，不会自动改成有损。</small>
  </div>
  <div class="row" style="margin-top:10px">
    <span style="flex:1"><label>输出目录</label><input id="output" size="46" autocomplete="off"></span>
    <button id="useCurrent">用当前目录作为输出</button>
  </div>
  <div class="row" style="margin-top:10px"><button id="start" class="primary">开始转换</button><button id="cancel">取消</button><button id="open">打开结果</button><button id="quit">退出</button></div>
  <div class="bar"><i id="bar"></i></div>
  <pre id="status"></pre>
</section>

<script>
const OPERATIONS = __OPERATIONS__;
const token = new URLSearchParams(location.search).get("token") || "";
let cwd = "", inputs = [];
async function api(path, body) {
  const res = await fetch(path + "?token=" + encodeURIComponent(token), {
    method: body ? "POST" : "GET",
    headers: {"content-type": "application/json"},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.status);
  return data;
}
function show(list) {
  const ul = document.getElementById("inputs"); ul.textContent = "";
  list.forEach((p) => { const li = document.createElement("li"); li.innerHTML = "<span></span><button>移除</button>";
    li.firstChild.textContent = p; li.lastChild.onclick = () => { inputs = inputs.filter((x) => x !== p); render(); }; ul.append(li); });
}
function render() { document.getElementById("count").textContent = inputs.length; show(inputs); }
async function browse(path) {
  const res = await fetch("/api/browse?token=" + encodeURIComponent(token) + "&path=" + encodeURIComponent(path));
  const listed = await res.json();
  if (listed.error) { document.getElementById("status").textContent = listed.error; return; }
  cwd = listed.path; document.getElementById("path").value = cwd;
  const ul = document.getElementById("entries"); ul.textContent = "";
  if (listed.parent) { const li = document.createElement("li"); li.textContent = "…上级目录"; li.onclick = () => browse(listed.parent); ul.append(li); }
  listed.entries.forEach((e) => {
    const li = document.createElement("li");
    const name = document.createElement("span"); name.textContent = (e.dir ? "📁 " : "📄 ") + e.name + (e.dir ? "" : "  " + Math.round(e.size / 1024) + " KB");
    const add = document.createElement("button"); add.textContent = e.dir ? "加入整个文件夹" : "加入";
    add.onclick = () => { inputs.push(e.path); render(); };
    li.append(name, add); ul.append(li);
  });
}
function status() {
  return api("/api/status", null).catch(() => ({}));
}
document.getElementById("operation").innerHTML = OPERATIONS.map(([v, t]) => '<option value="' + v + '">' + t + "</option>").join("");
function syncCompressOptions() {
  const operation = document.getElementById("operation").value;
  document.getElementById("compressBox").style.display = operation === "pdf_compress" ? "" : "none";
  const lossy = document.getElementById("compressMode").value === "lossy";
  document.querySelectorAll("#compressBox .lossyOnly").forEach((node) => {
    node.style.display = lossy ? "" : "none";
  });
  document.getElementById("imageCompressBox").style.display = operation === "image_compress" ? "" : "none";
  document.getElementById("imageCompressHint").style.display = operation === "image_compress" ? "" : "none";
  const imageLossy = document.getElementById("imageCompressMode").value === "lossy";
  document.querySelectorAll("#imageCompressBox .imageLossyOnly").forEach((node) => {
    node.style.display = imageLossy ? "" : "none";
  });
  // The image-compression mode decides the output format itself; leaving the
  // generic export controls visible would present settings that are never sent.
  const generic = operation !== "image_compress";
  ["dpiBox", "formatBox", "rangeBox"].forEach((id) => {
    document.getElementById(id).style.display = generic ? "" : "none";
  });
}
document.getElementById("operation").addEventListener("change", syncCompressOptions);
document.getElementById("compressMode").addEventListener("change", syncCompressOptions);
document.getElementById("imageCompressMode").addEventListener("change", syncCompressOptions);
syncCompressOptions();
document.getElementById("go").onclick = () => browse(document.getElementById("path").value);
document.getElementById("up").onclick = () => browse(cwd + "/..");
document.getElementById("home").onclick = () => browse("");
document.getElementById("useCurrent").onclick = () => { document.getElementById("output").value = cwd; };
document.getElementById("clear").onclick = () => { inputs = []; render(); };
document.getElementById("start").onclick = async () => {
  try {
    const operation = document.getElementById("operation").value;
    const body = { inputs, output: document.getElementById("output").value, operation };
    if (operation === "pdf_compress") {
      // Same semantics as the CLI: lossless by default, and DPI/quality are
      // decisions of the explicit lossy mode, so a lossless request never
      // submits them at all.
      body.lossy = document.getElementById("compressMode").value === "lossy";
      if (body.lossy) {
        body.dpi = Number(document.getElementById("compressDpi").value) || 150;
        body.quality = Number(document.getElementById("compressQuality").value) || 75;
      }
    } else if (operation === "image_compress") {
      // Same semantics as the CLI: lossless (the default) is a PNG-only,
      // pixel-preserving optimisation, so it submits neither quality nor max_edge;
      // the explicit lossy mode is JPEG and carries both.
      const imageLossy = document.getElementById("imageCompressMode").value === "lossy";
      body.lossy = imageLossy;
      if (imageLossy) {
        body.quality = Number(document.getElementById("imageQuality").value);
        body.max_edge = Number(document.getElementById("imageMaxEdge").value);
      }
    } else {
      body.dpi = Number(document.getElementById("dpi").value) || 150;
      body.format = document.getElementById("format").value;
      body.range = document.getElementById("range").value;
    }
    await api("/api/start", body);
  } catch (error) { document.getElementById("status").textContent = String(error); }
};
document.getElementById("cancel").onclick = () => api("/api/cancel", {}).catch(() => {});
document.getElementById("open").onclick = () => api("/api/open", {}).catch((error) => { document.getElementById("status").textContent = String(error); });
document.getElementById("quit").onclick = () => api("/api/quit", {}).catch(() => {});
setInterval(async () => {
  const state = await status();
  if (!state || !state.state) return;
  document.getElementById("status").textContent = state.lines.join("\\n");
  document.getElementById("bar").style.width = (state.total ? Math.round(100 * state.done / state.total) : (state.state === "running" ? 5 : 0)) + "%";
}, 700);
browse("");
</script></body></html>
"""


def prune_work(work: Path, keep: int = 50) -> None:
    """Keep the last few request files; they are diagnostics, not history.

    They live in the user's state folder, so without a cap a long-lived install
    accumulates one JSON file per click. Same reasoning as the engine's run cap.
    """
    try:
        files = sorted(work.glob("ui-request-*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in (files[:-keep] if keep > 0 else files):
        try:
            stale.unlink()
        except OSError:
            pass


def page() -> bytes:
    options = json.dumps(OPERATIONS, ensure_ascii=False)
    return PAGE.replace("__OPERATIONS__", options).encode("utf-8")


class Conversion:
    """One engine run, driven exactly like the app drives it: a request file plus JSONL."""

    def __init__(self, request: dict, request_path: Path):
        self.request = request
        self.request_path = request_path
        self.events: list[dict] = []
        self.state = "running"
        self.error = ""
        self.stderr: list[str] = []
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(ENGINE), "--request", str(request_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                self.events.append(event)
                if event.get("type") == "finished":
                    self.state = event.get("state", "complete")
                elif event.get("type") == "fatal":
                    self.state = "failed"
                    self.error = event.get("detail", "")
        self.process.wait()
        with self.lock:
            if self.state == "running":
                self.state = "failed"
                self.error = self.error or "转换进程意外结束"
        self._attach_stderr()

    def _read_stderr(self) -> None:
        """Drain stderr for two reasons.

        An unread pipe eventually blocks the writer, so a chatty engine would hang
        its own conversion; and without it the only thing a failure shows is "the
        process ended", with the traceback that explains it thrown away.
        """
        for line in self.process.stderr:
            line = line.rstrip()
            if not line:
                continue
            with self.lock:
                self.stderr.append(line)
                if len(self.stderr) > 200:
                    del self.stderr[:len(self.stderr) - 200]

    def _attach_stderr(self) -> None:
        with self.lock:
            tail = "\n".join(self.stderr[-5:]).strip()
            if tail and self.state == "failed" and not self.error.strip():
                self.error = "引擎输出：%s" % tail

    def control(self, command: str) -> None:
        """Same control channel the app's cancel button writes to."""
        if self.process.stdin and self.process.poll() is None:
            try:
                self.process.stdin.write(json.dumps({"command": command}) + "\n")
                self.process.stdin.flush()
            except OSError:
                pass

    def snapshot(self, limit: int = 12) -> dict:
        with self.lock:
            events = list(self.events)
        lines, done, total = [], 0, 0
        for event in events[-limit:]:
            kind = event.get("type")
            if kind == "scan":
                total = event.get("total", 0)
                lines.append("共 %d 个文件" % total)
            elif kind == "item":
                total = event.get("total", total)
                done = event.get("done", done)
                # Cancelling is not a failure: the file that was in flight is shown
                # as 已取消, never as 失败 (the engine reports it as cancelled).
                item_state = {"complete": "完成", "resumed": "复用", "failed": "失败",
                              "cancelled": "已取消"}.get(event.get("state"),
                                                        event.get("state", ""))
                lines.append("[%s/%s] %s %s %s" % (event.get("done"), event.get("total", "?"),
                                                  event.get("source", ""), item_state,
                                                  event.get("detail", "")))
            elif kind == "progress":
                lines.append("%s %s/%s" % (event.get("state", "进度"), event.get("done"), event.get("total")))
            elif kind == "control":
                lines.append("控制：%s %s" % (event.get("state", ""), event.get("detail", "")))
            elif kind == "finished":
                if event.get("state") == "cancelled":
                    # Not a completion either: the run was stopped on purpose and
                    # whatever had already finished is kept. The cancelled file is
                    # not an exception, so it is not counted as one.
                    # A run can be stopped after some files really failed; hiding
                    # that count here would make the summary disagree with the
                    # report file and with the filter bar.
                    lines.append("已取消：完成 %s / 异常 %s / 成品 %s（被取消的文件不计入异常）"
                                 % (event.get("success", 0), event.get("failed", 0) or 0,
                                    event.get("outputs", 0)))
                else:
                    lines.append("完成：成功 %s / 异常 %s / 成品 %s" % (event.get("success"), event.get("failed"), event.get("outputs")))
                lines.append("记录：%s" % event.get("report", ""))
            elif kind == "fatal":
                lines.append("失败：%s" % event.get("detail", ""))
        with self.lock:
            stderr_tail = "\n".join(self.stderr[-8:])
        if stderr_tail and self.state in ("failed", "cancelled"):
            lines.append("引擎输出：%s" % stderr_tail)
        return {"state": self.state, "error": self.error, "done": done, "total": total,
                "lines": lines, "output": self.request.get("output", ""),
                "stderr": stderr_tail, "cancelled": self.state == "cancelled"}


def list_directory(path: Path) -> dict:
    """One directory, for the picker. Directories first, hidden entries skipped."""
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.home()
    try:
        path = path.resolve(strict=True)
    except OSError:
        return {"error": "目录不存在：%s" % path}
    if not path.is_dir():
        return {"error": "不是目录：%s" % path}
    entries = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                continue
            entries.append({"name": child.name, "path": str(child), "dir": is_dir, "size": size})
            if len(entries) >= 2000:
                break
    except OSError as error:
        return {"error": "无法列出目录：%s" % error}
    return {"path": str(path), "parent": str(path.parent) if path.parent != path else "",
            "entries": entries}


def open_path(path: str) -> None:
    """Show a result with the platform's own opener - the same button the app has.

    A missing opener is a normal state on a minimal Linux install, not an internal
    error: the page has to say "install xdg-utils" instead of hanging on a 500.
    """
    target = Path(path).expanduser()
    if not target.exists():
        raise ValueError("结果路径不存在：%s" % target)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        elif os.name == "nt":
            os.startfile(str(target))  # noqa: S606 - the documented Windows way to open a path
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except FileNotFoundError as error:
        raise ValueError("系统里找不到打开文件的工具（%s），请手动打开：%s"
                         % (error.filename or "open", target)) from error
    except OSError as error:
        raise ValueError("无法打开结果：%s（%s）" % (target, error)) from error


def make_handler(token: str, work: Path, state: dict):
    """Build the request handler; state carries the one running conversion."""

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ChamoUI"

        def log_message(self, fmt, *args):  # keep the console readable
            pass

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, query: dict) -> bool:
            # compare_digest raises TypeError when compared with non-ASCII text, and
            # an unauthenticated caller controls that string. A rejected request must
            # come back as 403, not as a dropped connection and a traceback.
            try:
                return secrets.compare_digest(query.get("token", [""])[0], token)
            except TypeError:
                return False

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path in ("/", "/index.html"):
                if not self._authorized(query):
                    self.send_error(403, "token required")
                    return
                body = page()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._authorized(query):
                self._json({"error": "token"}, 403)
                return
            if parsed.path == "/api/browse":
                requested = query.get("path", [str(Path.home())])[0]
                listed = list_directory(Path(requested))
                self._json(listed, 400 if "error" in listed else 200)
                return
            if parsed.path == "/api/status":
                job = state.get("job")
                self._json(job.snapshot() if job else {"state": "", "lines": []})
                return
            self._json({"error": "未知路径"}, 404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if not self._authorized(query):
                self._json({"error": "token"}, 403)
                return
            length = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json({"error": "请求不是 JSON"}, 400)
                return
            if parsed.path == "/api/start":
                result = start(work, state, body)
                self._json(result, 400 if "error" in result else 200)
            elif parsed.path == "/api/cancel":
                job = state.get("job")
                if job:
                    job.control("cancel")
                self._json({"cancelled": bool(job)})
            elif parsed.path == "/api/open":
                job = state.get("job")
                target = body.get("path") or (job.request.get("output", "") if job else "")
                try:
                    open_path(target)
                except ValueError as error:
                    self._json({"error": str(error)}, 400)
                    return
                self._json({"opened": target})
            elif parsed.path == "/api/quit":
                # Quitting the window must stop the conversion, not leave it writing
                # into the user's folder after the page is gone. Cancel through the
                # engine's own control channel first, then close the port.
                job = state.get("job")
                stopped = stop_job(job)
                self._json({"bye": True, "cancelled": bool(job and job.state == "cancelled"),
                            "stopped": stopped})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"error": "未知路径"}, 404)

    return Handler


def stop_job(job, timeout: float = 15.0) -> bool:
    """Cancel a running conversion and wait until the engine has really stopped.

    The engine writes its report and closes its staging folder on the way out, so
    killing it immediately would leave half-written state. Terminate only if the
    orderly cancel does not finish in time.
    """
    if job is None or job.state != "running":
        return True
    job.control("cancel")
    try:
        job.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        job.process.terminate()
        try:
            job.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            job.process.kill()
            try:
                job.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return False
    return job.process.poll() is not None


def start(work: Path, state: dict, body: dict) -> dict:
    """Validate the request the same way the engine will, then run it.

    Every rejection here is a sentence the page can show. The engine's own error
    codes exist for programs; someone who typed "abc" into the DPI box needs to be
    told which box is wrong, and that check must happen before a process starts.
    """
    job = state.get("job")
    if job and job.state == "running":
        return {"error": "已有任务在运行，先取消它"}
    raw_inputs = body.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        return {"error": "先加入要转换的文件或文件夹"}
    inputs = []
    for item in raw_inputs:
        if not isinstance(item, str) or not item.strip():
            return {"error": "输入列表里有无法识别的条目"}
        inputs.append(str(Path(item).expanduser()))
    operation = body.get("operation")
    if operation not in {name for name, _ in OPERATIONS}:
        return {"error": "未知转换类型：%s" % operation}
    output = str(Path(str(body.get("output") or "")).expanduser())
    if not output or output == ".":
        return {"error": "先选择输出目录"}
    # A complete config with the CLI's defaults, not only the boxes this page shows.
    # Sending a partial config is what made image conversion fail 100% of the time:
    # the engine reads cfg['format'] for image_convert, and the page never sent it,
    # so every run ended "失败 'format'" without producing anything. One defaulted
    # object also means a new operation cannot break because a key is missing.
    config = {
        "operation": operation, "naming": "clean", "workers": 0,
        "dpi": 150, "format": "png", "quality": 90, "range": "",
        "paper": "original", "margin_mm": 10,
        "pdf_pages": 100, "pdf_bytes": 100_000_000,
        "lossy": False, "max_edge": 0, "shard": "auto",
    }
    if operation in ("pdf_images", "office_images", "image_convert"):
        try:
            dpi = int(body.get("dpi") or 150)
        except (TypeError, ValueError):
            return {"error": "DPI 必须是整数"}
        if not 36 <= dpi <= 600:
            return {"error": "DPI 需在 36-600 之间"}
        fmt = str(body.get("format") or "png").lower()
        if fmt not in ("png", "jpeg", "webp", "tiff", "bmp"):
            return {"error": "不支持的图片格式：%s" % fmt}
        page_range = body.get("range") or ""
        if not isinstance(page_range, str):
            return {"error": "页码范围必须是文本，如 1-3,5"}
        config.update({"dpi": dpi, "format": fmt, "range": page_range})
    elif operation == "image_compress":
        # Two explicit modes, mirroring the CLI: lossless (default, PNG only,
        # pixels preserved) and an opt-in JPEG lossy mode. Nothing is inferred:
        # a lossless request never carries quality/max_edge, and a lossy request
        # always carries both (max_edge=0 means "no limit").
        lossy = body.get("lossy") is True
        given_format = body.get("format")
        if given_format not in (None, ""):
            expected = "jpeg" if lossy else "png"
            if str(given_format).lower() != expected:
                return {"error": "%s模式固定输出 %s，不接受 format=%s"
                                 % ("有损" if lossy else "无损", expected.upper(), given_format)}
        if lossy:
            try:
                quality = int(body.get("quality") if body.get("quality") is not None else 90)
            except (TypeError, ValueError):
                return {"error": "JPEG 质量必须是整数"}
            if not 1 <= quality <= 100:
                return {"error": "JPEG 质量需在 1-100 之间"}
            try:
                max_edge = int(body.get("max_edge") if body.get("max_edge") is not None else 0)
            except (TypeError, ValueError):
                return {"error": "最长边必须是整数（0 表示不限制）"}
            if not 0 <= max_edge <= 16000:
                return {"error": "最长边需在 0-16000 之间"}
            config.update({"lossy": True, "format": "jpeg",
                           "quality": quality, "max_edge": max_edge})
        else:
            # Silently dropping these would lie about what the run does; the CLI
            # exposes them only for the lossy mode, and so does this page.
            for key, label in (("quality", "质量"), ("max_edge", "最长边")):
                given = body.get(key)
                if given not in (None, 0, ""):
                    return {"error": "无损压缩不接受%s参数；需要降低质量/尺寸请选择「有损（JPEG）」模式" % label}
            non_png = [name for name in inputs
                       if Path(name).is_file() and Path(name).suffix.lower() != ".png"]
            if non_png:
                return {"error": "无损压缩只优化 PNG（像素不变）；%s 不是 PNG，"
                                 "请选择「有损（JPEG）」模式（不会自动改为有损）"
                                 % Path(non_png[0]).name}
            config.update({"lossy": False, "format": "png"})
            config.pop("quality", None)
            config.pop("max_edge", None)
    elif operation == "pdf_compress":
        # Same contract as the CLI: lossless is the default, and the explicit
        # lossy mode carries its own 150 dpi / JPEG 75 defaults. A lossless
        # request never submits dpi/quality, so neither is guessed here.
        lossy = bool(body.get("lossy"))
        if lossy:
            try:
                dpi = int(body.get("dpi") or 150)
            except (TypeError, ValueError):
                return {"error": "有损压缩 DPI 必须是整数"}
            if not 72 <= dpi <= 600:
                return {"error": "有损压缩 DPI 需在 72-600 之间"}
            try:
                quality = int(body.get("quality") or 75)
            except (TypeError, ValueError):
                return {"error": "JPEG 质量必须是整数"}
            if not 1 <= quality <= 100:
                return {"error": "JPEG 质量需在 1-100 之间"}
            config.update({"lossy": True, "dpi": dpi, "quality": quality})
        else:
            config.update({"lossy": False})
    elif operation in ("pdf_split", "rag"):
        # The split size is a real decision, and the CLI exposes it; the page passes
        # it through when given instead of silently using 100 pages.
        try:
            pdf_pages = int(body.get("pdf_pages") or 100)
        except (TypeError, ValueError):
            return {"error": "每份页数必须是整数"}
        if not 1 <= pdf_pages <= 10000:
            return {"error": "每份页数需在 1-10000 之间"}
        config.update({"pdf_pages": pdf_pages, "pdf_bytes": 100_000_000})
    # gui_session: if this page dies without saying goodbye, the engine must stop
    # instead of continuing to write into the user's folder with nobody watching.
    request = {"protocol": protocol.PROTOCOL, "inputs": inputs, "output": output,
               "gui_session": True, "config": config}
    try:
        protocol.check_request(request)
    except protocol.ProtocolError as error:
        return {"error": error.message}
    work.mkdir(parents=True, exist_ok=True)
    prune_work(work)
    request_path = work / ("ui-request-%d.json" % int(time.time() * 1000))
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    state["job"] = Conversion(request, request_path)
    return {"started": True, "output": output}


def make_server(port: int = 0, work: Path | None = None, token: str | None = None):
    """Bind the local server. Split out so tests and acceptance can drive the same app.

    It binds 127.0.0.1 only: this page converts files on the user's disk, so it must
    not be reachable from the network.
    """
    token = token or secrets.token_urlsafe(16)
    work = work or Path(os.environ.get("CHAMO_UI_WORK") or (Path.home() / ".chamo-ui"))
    state: dict = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(token, work, state))
    server.ui_state = state
    server.ui_work = work
    return server, token


def serve(port: int = 0, browser: bool = True,
          work: Path | None = None, ready: threading.Event | None = None) -> None:
    server, token = make_server(port, work)
    url = "http://127.0.0.1:%d/?token=%s" % (server.server_address[1], token)
    # Printed unconditionally: the token is in it, so it is the only way to reach the
    # page from another browser, and with --no-browser it is the only way at all.
    print("Chamo 本机界面：%s" % url, flush=True)
    if ready is not None:
        ready.set()
    if browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="chamo ui", description="本机界面：选文件→配置→转换→进度/取消→打开结果")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示自动选择（只绑定 127.0.0.1）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)
    print("按 Ctrl-C 退出。所有转换在本机执行，文件不上传。", flush=True)
    try:
        serve(port=args.port, browser=not args.no_browser)
    except OSError as error:
        print("无法启动本机界面：%s" % error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
