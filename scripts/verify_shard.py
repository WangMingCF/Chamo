#!/usr/bin/env python3
"""Independent, fail-capable acceptance gates for content-aware sharded PDF rendering.

Why this file exists
--------------------
"shard=auto" (a pdf_images config key) may render ONE image-dominant PDF in
several worker processes. That is only acceptable if it changes nothing except
speed: same files, same names, same bytes, every page. These gates are written
so the shortcuts that would fake a speedup (drop a page, skip the last shard,
lower the DPI, widen/narrow the zero padding, scatter one job's pages over
several folders, shard a text PDF) FAIL loudly.

A second, equally important rule: a gate must not pass vacuously. An
"equivalence" comparison between two unsharded runs proves nothing, so every
equivalence gate also checks that the sharded arm really did take the sharded
path. That is observed two independent ways:
  * the process table: peak number of concurrent descendant worker processes;
  * the engine's machine-readable decision: report.json results[i].shard and the
    item event's shard field ({mode, used, shards, kind, pages, sampled,
    image_pages, selected, total_pixels, cores, predicted_gain, gain_floor,
    reason}).
If EITHER observable says "sharded", the gate treats it as sharded. If neither
can be observed, the gate FAILs: unverified is not passed.

The decision gates check the sharding cost model in both directions on real
documents: decision.dense_work_shards (48 A4 pages, ~104 MPix, which an old fixed
96-page threshold refused and forced sharding measured 1.15-1.40x faster) and
decision.sparse_work_not_sharded (96 small pages at 240 dpi, ~64 MPix, which an
old 60-MPix floor admitted but which cannot repay several processes). Both
compare shard=off and shard=auto byte for byte.

Recorded project lessons applied here
* Every gate prints PASS/FAIL/SKIP with expected and actual values.
* A gate that cannot observe its subject FAILs; the one exception is the
  machine-dependent speed gate, which may be skipped in CI but must print why
  and is never reported as a pass.
* Exit code 1 when any gate FAILs; skips alone keep 0 but are named on the
  result line.
* engine/ is never modified: mutation runs copy the engine tree elsewhere and
  point --engine/--chamo at the copy.

Usage
-----
  ./third_party/python/bin/python3 scripts/verify_shard.py
  ./third_party/python/bin/python3 scripts/verify_shard.py --mode equivalence,content
  ./third_party/python/bin/python3 scripts/verify_shard.py --pdf /path/scan.pdf --pages 240
  ./third_party/python/bin/python3 scripts/verify_shard.py --mode speed --perf on --reps 3
  ./third_party/python/bin/python3 scripts/verify_shard.py --engine /tmp/mutant/workbench.py \
      --chamo /tmp/mutant/chamo.py            # mutation testing against a copy

Exit codes: 0 every gate passed (skips allowed unless --strict), 1 a gate FAILED,
2 bad usage.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENGINE = ROOT / "engine" / "workbench.py"
DEFAULT_CHAMO = ROOT / "engine" / "chamo.py"

# Directories the engine writes next to the user's outputs (receipts and
# staging). They are bookkeeping, not deliverables, so equivalence compares the
# deliverables only.
# Folders older builds wrote next to the outputs. Current builds keep their
# bookkeeping outside the output folder entirely, so only a batch conversion
# adds one file: its manifest.
RECORD_DIRS = {"_Chamo记录", "_Chamo暂存"}
MANIFEST_NAME = "清单.json"

SPEEDUP_MIN = 1.5            # gate 3: image-dominant PDF must be >= 1.5x faster
TEXT_REGRESSION_MAX = 0.05   # gate 2: text PDF must not regress by more than 5%
# The engine reports the gain floor it used in the job report; it is part of the
# advertised contract ("only shard when predicted gain >= 10%"), so a gate pins
# it. Review showed the constant could drift from 1.10 down to 1.01 while
# every gate and model unit test stayed green, i.e. the promise was unenforced.
PINNED_GAIN_FLOOR = 1.10

# Synthetic fixtures. The image fixtures carry enough raster work that the cost
# model in engine/workbench.py predicts a real speed-up, so the sharded arm is
# genuinely exercised. The big fixture is >999 pages so the clean_names
# zero-padding width (4 digits at 1000+ pages) is covered: a per-shard namer
# would produce 3-digit names for early shards.
#
# Two fixtures exist for the sharding decision itself, and both are cases the
# old fixed thresholds judged wrongly (see gate_decision_model):
#   dense  48-page A4 scan -> ~104 MPix. The 96-page rule refused it, but forced
#          sharding measured 1.15-1.40x; the cost model must admit it.
#   sparse 96 pages of 200x300 pt at 240 dpi -> ~64 MPix. The 60-MPix rule
#          admitted it, but that is not enough work to repay the processes; the
#          cost model must refuse it.
FIXTURES = {
    "scan":  {"kind": "scan", "pages": 120,  "source_dpi": 120, "render_dpi": 150},
    "dense": {"kind": "scan", "pages": 48,   "source_dpi": 120, "render_dpi": 150},
    "sparse": {"kind": "small", "pages": 96, "source_dpi": 120, "render_dpi": 240},
    # Timing fixture: same shape as the reference benchmark (240 pages, 200-dpi scan
    # source, 150-dpi render). A cheap 120-dpi source renders fast enough that
    # process start-up dominates and a real 2.1x gain shows up as ~1.3x, so the
    # speed gate uses this fixture, not the cheap one used for equivalence.
    "perf":  {"kind": "scan", "pages": 240,  "source_dpi": 200, "render_dpi": 150},
    "text":  {"kind": "text", "pages": 120,  "source_dpi": None, "render_dpi": 150},
    "big":   {"kind": "scan", "pages": 1200, "source_dpi": 72,  "render_dpi": 72},
    "cli":   {"kind": "scan", "pages": 4,    "source_dpi": 36,  "render_dpi": 72},
    # built from the scan fixture plus one oversized page (see make_oversized_page_pdf)
    "fail":  {"kind": "fail", "pages": 121,  "source_dpi": None, "render_dpi": 150},
}
RANGE_SPEC = "1-40,50-89"   # non-contiguous selection against the 120-page scan
RANGE_PAGES = 120
FAIL_PAGE_INDEX = 60        # 0-based insertion point of the oversized page
FAIL_CONTROL_RANGE = "1-60" # avoids the oversized page; same file must succeed


# --------------------------------------------------------------------------
# Process observation: is the run really using several worker processes?
# --------------------------------------------------------------------------
# psutil is not a dependency of this project, so it is a bonus path only. The
# real paths are /proc on Linux, ps on POSIX, and the Toolhelp32 snapshot API on
# Windows (ctypes: no extra dependency and fast enough to sample at 5-7 Hz;
# spawning powershell per sample would be too slow to see short-lived workers).

_PROC_TABLE_CACHE: dict = {}


def _table_from_psutil():
    try:
        import psutil  # noqa: PLC0415 - optional accelerator
    except Exception:
        return None
    try:
        table = {}
        for proc in psutil.process_iter(["pid", "ppid"]):
            try:
                table[int(proc.info["pid"])] = int(proc.info["ppid"] or 0)
            except (TypeError, ValueError):
                continue
        return table or None
    except Exception:
        return None


def _table_from_procfs():
    root = Path("/proc")
    if not root.is_dir():
        return None
    table = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # "pid (comm) state ppid ..." - comm may contain spaces and parentheses,
        # so split from the LAST ')' instead of by whitespace.
        close = stat.rfind(")")
        if close < 0:
            continue
        fields = stat[close + 2:].split()
        if len(fields) >= 2 and fields[1].isdigit():
            table[int(entry.name)] = int(fields[1])
    return table or None


def _table_from_ps():
    try:
        result = subprocess.run(["ps", "-Ao", "pid=,ppid="], capture_output=True,
                                text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    table = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = int(parts[1])
    return table or None


def _table_from_toolhelp32():
    """Windows: in-process process snapshot via ctypes (fast, no subprocess)."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE or not snapshot:
        return None
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table or None


def _table_from_powershell():
    """Last-resort Windows path when the ctypes snapshot fails."""
    command = ("Get-CimInstance Win32_Process | ForEach-Object "
               "{ $_.ProcessId.ToString() + ' ' + $_.ParentProcessId.ToString() }")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    table = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = int(parts[1])
    return table or None


def process_table():
    """Return {pid: ppid} for the whole system, or None if unavailable.

    The working mechanism is detected once and cached; every platform the CI
    matrix uses has at least one of them (Linux /proc, macOS ps, Windows
    Toolhelp32), so None means the harness cannot observe concurrency - which
    the gates treat as a FAIL, not a pass.
    """
    if _PROC_TABLE_CACHE.get("table") is not None:
        return _PROC_TABLE_CACHE["table"]()
    if _PROC_TABLE_CACHE.get("unavailable"):
        return None
    candidates = [_table_from_psutil, _table_from_procfs]
    if os.name == "nt":
        candidates += [_table_from_toolhelp32, _table_from_powershell]
    if os.name == "posix":
        candidates += [_table_from_ps]
    for candidate in candidates:
        try:
            table = candidate()
        except Exception:
            table = None
        if table:
            _PROC_TABLE_CACHE["table"] = candidate
            return table
    _PROC_TABLE_CACHE["unavailable"] = True
    return None


def _descendant_count(table, root_pid):
    children: dict = {}
    for pid, ppid in table.items():
        children.setdefault(ppid, []).append(pid)
    seen = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return len(seen)


class DescendantSampler(threading.Thread):
    """Samples how many descendant processes the engine tree has at once."""

    def __init__(self, root_pid: int, interval: float = 0.15):
        super().__init__(daemon=True)
        self.root_pid = root_pid
        self.interval = interval
        self.max_children = 0
        self.samples = 0
        self.available = None  # None = never sampled, True/False afterwards
        self._halt = threading.Event()  # not _stop: that name is Thread's own method

    def run(self):
        while not self._halt.is_set():
            table = process_table()
            if table is None:
                self.available = False
                return
            self.available = True
            count = _descendant_count(table, self.root_pid)
            self.max_children = max(self.max_children, count)
            self.samples += 1
            self._halt.wait(self.interval)

    def stop(self):
        self._halt.set()


# --------------------------------------------------------------------------
# Synthetic fixtures (no user data, no external drives)
# --------------------------------------------------------------------------
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_page_image(source_dpi: int, width: float, height: float, variant: int):
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    pixel_w = max(8, int(width / 72.0 * source_dpi))
    pixel_h = max(8, int(height / 72.0 * source_dpi))
    image = Image.new("L", (pixel_w, pixel_h), 246)
    draw = ImageDraw.Draw(image)
    rows = max(4, min(30, pixel_h // 48))
    for row in range(rows):
        draw.text((16, 16 + row * 24),
                  "Scanned page variant %d line %d" % (variant + 1, row), fill=30)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=82)
    buffer.seek(0)
    image.close()
    return ImageReader(buffer)


def make_scan_pdf(path: Path, pages: int, source_dpi: int = 120,
                  width: float = 595.0, height: float = 842.0,
                  variants: int = 8) -> Path:
    """An image-dominant PDF: every page is exactly one raster image, no text.

    That is what pageclass.classify() must call "image". A few distinct page
    images are reused as shared XObjects so a 1200-page fixture generates in
    about a second instead of minutes; page identity is not needed because the
    renderer names outputs by absolute page number.
    """
    from reportlab.pdfgen import canvas

    canvas_obj = canvas.Canvas(str(path), pagesize=(width, height))
    readers = [_scan_page_image(source_dpi, width, height, variant)
               for variant in range(max(1, min(variants, pages)))]
    for index in range(pages):
        canvas_obj.drawImage(readers[index % len(readers)], 0, 0,
                             width=width, height=height)
        canvas_obj.showPage()
    canvas_obj.save()
    return path


def make_text_pdf(path: Path, pages: int,
                  width: float = 595.0, height: float = 842.0) -> Path:
    """A born-digital text PDF: no image objects, lots of text objects."""
    from reportlab.pdfgen import canvas

    canvas_obj = canvas.Canvas(str(path), pagesize=(width, height))
    for index in range(pages):
        canvas_obj.setFont("Helvetica", 10)
        for row in range(55):
            canvas_obj.drawString(
                60, 800 - row * 13.5,
                "Page %d line %d: the quick brown fox jumps over the lazy dog 0123456789"
                % (index + 1, row))
        canvas_obj.showPage()
    canvas_obj.save()
    return path


def make_oversized_page_pdf(path: Path, base: Path, page_index: int = 60,
                            page_pt: float = 5000.0) -> Path:
    """base scan + one page too large to render at the run's DPI.

    The shard that owns that page must fail, which must fail the whole job with
    nothing published (frozen contract 6: a failed shard can never look like a
    successful job with fewer pages). At 150 dpi 5000 pt = 10417 px per side =
    108 megapixels, over the engine's 64M-pixel per-page cap.
    """
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    huge_path = path.with_name(path.stem + "-huge-one.pdf")
    canvas_obj = canvas.Canvas(str(huge_path), pagesize=(page_pt, page_pt))
    canvas_obj.drawImage(_scan_page_image(10, page_pt, page_pt, 0), 0, 0,
                         width=page_pt, height=page_pt)
    canvas_obj.showPage()
    canvas_obj.save()

    writer = PdfWriter()
    writer.append(PdfReader(str(base)))
    writer.insert_page(PdfReader(str(huge_path)).pages[0], page_index)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def pdf_page_count(path: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(path)).pages)


def selected_page_count(spec: str, total: int) -> int:
    """Mirror of engine.workbench.pages() counting, without importing engine/."""
    if not spec.strip():
        return total
    count = 0
    for part in spec.split(","):
        bounds = part.strip().split("-")
        if len(bounds) == 1:
            count += 1
        else:
            count += int(bounds[-1]) - int(bounds[0]) + 1
    return count


# --------------------------------------------------------------------------
# Runs: engine request / CLI invocation, with output collection
# --------------------------------------------------------------------------
def collect_outputs(out_dir: Path) -> dict:
    """{relative posix path: sha256} for the deliverables, records excluded."""
    files = {}
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return files
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(out_dir)
        if relative.parts and relative.parts[0] in RECORD_DIRS:
            continue
        if relative.name == MANIFEST_NAME or relative.parts[0].startswith("."):
            continue  # batch manifest / in-flight staging: not a converted deliverable
        files[relative.as_posix()] = sha256(path)
    return files


def _parse_events(stdout: str) -> list:
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _fatal_of(events: list):
    for event in reversed(events):
        if event.get("type") == "fatal":
            return event
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Best-effort teardown after a timeout; the supervisor also stops workers."""
    try:
        if os.name == "posix":
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.SubprocessError:
        pass


def _find_record_dir(events: list):
    for event in events:
        if event.get("type") == "scan" and event.get("report"):
            return Path(event["report"])
    return None


def run_engine(engine: Path, pdf: Path, out_dir: Path, config: dict,
               sample: bool = False, timeout: int = 900) -> dict:
    """Run engine/workbench.py on one PDF; measure wall time and concurrency."""
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    request = {"inputs": [str(Path(pdf).resolve())], "output": str(out_dir),
               "config": config}
    request_path = out_dir.parent / (out_dir.name + "-request.json")
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    start_kwargs: dict = {}
    if os.name == "posix":
        start_kwargs["start_new_session"] = True
    command = [sys.executable, "-I", "-B", str(engine), "--request", str(request_path)]
    environment = dict(os.environ)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    # The page cache is on by default and would be shared across the repeated
    # auto/off arms of one gate session: the first cold rep fills it, every later
    # rep reads it back, and the timing comparison stops measuring what it claims
    # to measure. This suite measures rendering, not resume, so it opts out.
    environment.setdefault("CHAMO_PAGE_CACHE", "0")
    cpu_started = _children_cpu()
    started = time.monotonic()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", env=environment, **start_kwargs)
    sampler = DescendantSampler(proc.pid) if sample else None
    if sampler:
        sampler.start()
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        stdout, stderr = "", ""
    finally:
        wall = time.monotonic() - started
        cpu_finished = _children_cpu()
        if sampler:
            sampler.stop()
            sampler.join(timeout=5)
    events = _parse_events(stdout)
    scan_event = next((e for e in events if e.get("type") == "scan"), None)
    report_dir = _find_record_dir(events)
    report = None
    if report_dir is not None:
        try:
            report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
    finished = next((e for e in reversed(events) if e.get("type") == "finished"), None)
    return {
        "command": command,
        "request": request,
        "rc": proc.returncode,
        "wall": wall,
        # CPU seconds spent by the engine and everything it spawned. Timing gates
        # prefer this over wall time because wall time on a shared runner measures
        # the host's other tenants too; None means the platform cannot report it.
        "cpu": (None if cpu_started is None or cpu_finished is None
                else max(0.0, cpu_finished - cpu_started)),
        "timed_out": timed_out,
        "events": events,
        "fatal": _fatal_of(events),
        "finished": finished,
        "scan_total": (scan_event or {}).get("total"),
        "report_dir": str(report_dir) if report_dir else None,
        "report": report,
        "report_results": list((report or {}).get("results") or []),
        "outputs": collect_outputs(out_dir),
        "out_dir": str(out_dir),
        "max_children": sampler.max_children if sampler else None,
        "sampler_samples": sampler.samples if sampler else None,
        "sampler_available": sampler.available if sampler else None,
        "stderr_tail": (stderr or "").strip()[-1500:],
    }


def run_cli(chamo: Path, pdf: Path, out_dir: Path, shard: str,
            timeout: int = 300) -> dict:
    """Run the supported CLI (engine/chamo.py) so the --shard flag itself is tested."""
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-I", "-B", str(chamo), str(pdf), "-o", str(out_dir),
               "-t", "pdf-images", "--dpi", "72", "--workers", "2",
               "--shard", shard, "--json"]
    environment = dict(os.environ)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    # Same reason as run_engine: a warm page cache would make the second arm of a
    # paired timing comparison cheaper than the first.
    environment.setdefault("CHAMO_PAGE_CACHE", "0")
    started = time.monotonic()
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, env=environment)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        rc, stdout, stderr = None, "", "timeout"
    events = _parse_events(stdout)
    report_dir = _find_record_dir(events)
    report = None
    if report_dir is not None:
        try:
            report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
    return {
        "command": command,
        "rc": rc,
        "wall": time.monotonic() - started,
        "events": events,
        "fatal": _fatal_of(events),
        "finished": next((e for e in reversed(events) if e.get("type") == "finished"), None),
        "report": report,
        "report_results": list((report or {}).get("results") or []),
        "stdout_tail": (stdout or "").strip()[-1500:],
        "stderr_tail": (stderr or "").strip()[-1500:],
        "outputs": collect_outputs(out_dir),
        "out_dir": str(out_dir),
    }


# --------------------------------------------------------------------------
# The engine's own shard decision: read it, but never trust it alone
# --------------------------------------------------------------------------
def shard_decision(run: dict):
    """The machine-readable shard decision, from report.json or the item event."""
    for record in run.get("report_results") or []:
        if isinstance(record, dict) and isinstance(record.get("shard"), dict):
            return record["shard"]
    for event in run.get("events") or []:
        if event.get("type") == "item" and isinstance(event.get("shard"), dict):
            return event["shard"]
    return None


def is_sharded(run: dict):
    """True/False when observable, None when neither observable works.

    Conservative by construction: if EITHER the process sampler or the engine's
    own report says the sharded path ran, the gate treats the run as sharded.
    This is the right question for the equivalence gates ("was the sharded path
    exercised at all?"). It is NOT strong enough for a gate that claims real
    multi-process work: a report can lie while the renderer stays single-process.
    gate_decision_model therefore additionally requires the process table to
    agree (when sampling is available) before it accepts ">=2 processes".
    """
    signals = []
    decision = shard_decision(run)
    if decision is not None:
        signals.append(bool(decision.get("used")) and int(decision.get("shards") or 1) >= 2)
    if run.get("sampler_available") is True:
        signals.append((run.get("max_children") or 0) >= 2)
    if not signals:
        return None
    return any(signals)


def shard_evidence(run: dict) -> str:
    """A short string describing every shard observable that exists for a run."""
    parts = []
    decision = shard_decision(run)
    if decision is not None:
        parts.append("report: mode=%s used=%s shards=%s kind=%s pages=%s selected=%s "
                     "pixels=%s cores=%s predicted_gain=%s gain_floor=%s"
                     % (decision.get("mode"), decision.get("used"),
                        decision.get("shards"), decision.get("kind"),
                        decision.get("pages"), decision.get("selected"),
                        decision.get("total_pixels"), decision.get("cores"),
                        decision.get("predicted_gain"), decision.get("gain_floor")))
    else:
        parts.append("report: no shard decision recorded")
    if run.get("sampler_available") is True:
        parts.append("process sampler: max concurrent descendants=%s over %s samples"
                     % (run.get("max_children"), run.get("sampler_samples")))
    elif run.get("sampler_available") is False:
        parts.append("process sampler: unavailable")
    else:
        parts.append("process sampler: not sampled")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Equivalence comparison (the anti-cheat core)
# --------------------------------------------------------------------------
def _summarize(items, limit=6):
    items = list(items)
    if len(items) <= limit:
        return items
    return items[:limit] + ["... and %d more" % (len(items) - limit)]


def compare_outputs(off_files: dict, auto_files: dict,
                    expected_pages=None) -> list:
    """Return a list of human-readable differences; empty list means identical.

    Compares the four things that make the sharded render equivalent: relative
    path set, per-file SHA-256, page count, and total file count. Also requires a
    single input file's pages to land in one output folder and (when given) the
    count to match the source PDF page count - so two runs that both drop the
    same page cannot pass.
    """
    problems = []
    off_paths, auto_paths = set(off_files), set(auto_files)
    if len(off_files) != len(auto_files):
        problems.append(
            "file count differs: shard=off=%d, shard=auto=%d" % (len(off_files), len(auto_files)))
    if expected_pages is not None:
        if len(off_files) != expected_pages:
            problems.append("shard=off produced %d files, source page count is %d"
                            % (len(off_files), expected_pages))
        if len(auto_files) != expected_pages:
            problems.append("shard=auto produced %d files, source page count is %d"
                            % (len(auto_files), expected_pages))
    missing = sorted(off_paths - auto_paths)
    if missing:
        problems.append("relative paths missing from shard=auto (%d): %s"
                        % (len(missing), _summarize(missing)))
    extra = sorted(auto_paths - off_paths)
    if extra:
        problems.append("unexpected relative paths in shard=auto (%d): %s"
                        % (len(extra), _summarize(extra)))
    differing = [name for name in sorted(off_paths & auto_paths)
                 if off_files[name] != auto_files[name]]
    if differing:
        problems.append("SHA-256 differs for %d file(s): %s"
                        % (len(differing), _summarize(differing)))
    off_folders = sorted({str(Path(name).parent.as_posix()) for name in off_paths})
    auto_folders = sorted({str(Path(name).parent.as_posix()) for name in auto_paths})
    if len(auto_folders) > 1:
        problems.append("one job's pages span %d output folders in shard=auto: %s"
                        % (len(auto_folders), _summarize(auto_folders)))
    if len(off_folders) > 1:
        problems.append("one job's pages span %d output folders in shard=off: %s"
                        % (len(off_folders), _summarize(off_folders)))
    return problems


def describe_fatal(run: dict) -> str:
    fatal = run.get("fatal") or {}
    if fatal:
        return "engine rejected the request: %s %s" % (
            fatal.get("code") or "", fatal.get("detail") or "")
    if run.get("timed_out"):
        return "engine run timed out after %.0fs" % run["wall"]
    if run.get("rc") not in (0, None):
        return "engine exited %s: %s" % (run.get("rc"), run.get("stderr_tail", ""))
    return "engine did not report a fatal event but left rc=%s" % run.get("rc")


def describe_unsuccessful(run: dict) -> str:
    """Why a run produced no usable output, even when the engine exited 0.

    The engine returns 0 for a request whose files all failed -- exit codes are the
    CLI's job and the GUI reads the event stream. So rc cannot tell a completed run
    from a broken one, and a gate that only checks rc reports "0 files" with no
    reason, which is useless when the failure is platform specific. The per-file
    error is what names the cause.
    """
    reason = describe_fatal(run)
    if run.get("fatal") or run.get("timed_out") or run.get("rc") not in (0, None):
        return reason
    for result in run.get("report_results") or []:
        if result.get("state") not in ("complete", "resumed"):
            return "job failed: %s" % (result.get("error") or result.get("state"))
    finished = run.get("finished") or {}
    if finished.get("failed"):
        return "run finished as %s with %s failed item(s)" % (
            finished.get("state"), finished.get("failed"))
    return ""


def compare_runs(off: dict, auto: dict, expected_pages=None) -> list:
    """compare_outputs plus the reasons a run cannot be compared at all."""
    problems = []
    for label, run in (("shard=off", off), ("shard=auto", auto)):
        reason = describe_unsuccessful(run)
        if reason:
            problems.append("%s did not complete: %s" % (label, reason))
    if problems:
        return problems
    return compare_outputs(off["outputs"], auto["outputs"], expected_pages)


# --------------------------------------------------------------------------
# Timing-gate helpers
# --------------------------------------------------------------------------
def _loadavg_text() -> str:
    """One-line load average for diagnostics; timing gates fail on noisy hosts."""
    try:
        values = os.getloadavg()
    except (AttributeError, OSError):
        return "n/a"
    return "%.2f/%.2f/%.2f" % values


try:
    import resource as _RESOURCE
except ImportError:  # Windows has no RUSAGE_CHILDREN
    _RESOURCE = None


def _children_cpu() -> float | None:
    """CPU seconds of all reaped child processes (POSIX), else None.

    Wall-clock time on a shared CI runner is dominated by whatever else the host
    is doing: a macOS GitHub runner at load average 28 turned two identical runs
    into a reported 9% "regression" (per-pair ratios 0.67/0.91/1.07). Child CPU
    time is not inflated by other tenants, so a 5% budget can actually be
    resolved there. Windows has no RUSAGE_CHILDREN, so callers fall back to wall
    time and must then decide whether the host can resolve the budget at all.
    """
    resource = _RESOURCE
    if resource is None:
        return None
    try:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    except (OSError, ValueError):
        return None
    return float(usage.ru_utime) + float(usage.ru_stime)


# --------------------------------------------------------------------------
# Gate result objects and printing
# --------------------------------------------------------------------------
def check(name, status, expected, actual, details=None, meta=None) -> dict:
    assert status in ("PASS", "FAIL", "SKIP")
    return {"name": name, "status": status, "expected": expected,
            "actual": actual, "details": list(details or []),
            "meta": dict(meta or {})}


def format_check(result: dict) -> str:
    lines = ["[%s] %s" % (result["status"], result["name"])]
    if result["expected"]:
        lines.append("       expected: %s" % result["expected"])
    if result["actual"]:
        lines.append("       actual:   %s" % result["actual"])
    for detail in result["details"]:
        lines.append("       - %s" % detail)
    return "\n".join(lines)


def print_check(result: dict) -> None:
    print(format_check(result), flush=True)


# --------------------------------------------------------------------------
# Gate session: fixture cache, run cache, gate implementations
# --------------------------------------------------------------------------
class GateSession:
    def __init__(self, args):
        self.args = args
        self.engine = Path(args.engine).resolve()
        self.chamo = Path(args.chamo).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="dw-shard-gate-"))
        if args.workdir:
            self.root = Path(args.workdir).resolve()
            self.root.mkdir(parents=True, exist_ok=True)
        self.run_root = self.root / ("runs-" + uuid.uuid4().hex[:8])
        self.run_root.mkdir(parents=True, exist_ok=True)
        self._fixtures: dict = {}
        self._runs: dict = {}
        self._run_index = 0
        self.run_log: list = []

    # -- fixtures ----------------------------------------------------------
    def fixture(self, key: str) -> Path:
        if key in self._fixtures:
            return self._fixtures[key]
        if key in ("scan", "fail"):
            if self.args.pdf:
                path = Path(self.args.pdf).expanduser().resolve()
                if not path.is_file():
                    raise SystemExit("--pdf not found: %s" % path)
                if key == "fail":
                    built = self.root / "fixture-fail-from-external.pdf"
                    if not built.exists():
                        make_oversized_page_pdf(built, path, FAIL_PAGE_INDEX)
                    self._fixtures[key] = built
                    return built
                self._fixtures[key] = path
                return path
        if key == "text" and self.args.text_pdf:
            path = Path(self.args.text_pdf).expanduser().resolve()
            self._fixtures[key] = path
            return path
        spec = dict(FIXTURES[key])
        if key in ("scan", "perf") and self.args.pages:
            spec["pages"] = int(self.args.pages)
        if key == "text" and self.args.text_pages:
            spec["pages"] = int(self.args.text_pages)
        if key == "big" and self.args.big_pages:
            spec["pages"] = int(self.args.big_pages)
        path = self.root / ("fixture-%s-%d.pdf" % (key, spec["pages"]))
        if key == "fail":
            base = self.fixture("scan")
            if not path.exists():
                started = time.monotonic()
                make_oversized_page_pdf(path, base, FAIL_PAGE_INDEX)
                print("[fixture] fail: %d pages + 1 oversized page (index %d) built in %.2fs: %s"
                      % (pdf_page_count(base), FAIL_PAGE_INDEX,
                         time.monotonic() - started, path), flush=True)
        elif not path.exists():
            started = time.monotonic()
            if spec["kind"] == "scan":
                make_scan_pdf(path, spec["pages"], source_dpi=spec["source_dpi"])
            elif spec["kind"] == "small":
                make_scan_pdf(path, spec["pages"], source_dpi=spec["source_dpi"],
                              width=200.0, height=300.0)
            else:
                make_text_pdf(path, spec["pages"])
            print("[fixture] %s: %d pages generated in %.2fs: %s"
                  % (key, spec["pages"], time.monotonic() - started, path), flush=True)
        else:
            print("[fixture] %s: reusing %s" % (key, path), flush=True)
        self._fixtures[key] = path
        return path

    def expected_pages(self, key: str, range_spec: str = "") -> int:
        total = pdf_page_count(self.fixture(key))
        return selected_page_count(range_spec, total)

    # -- runs --------------------------------------------------------------
    def engine_config(self, key: str, range_spec: str = "") -> dict:
        render_dpi = FIXTURES.get(key, FIXTURES["scan"])["render_dpi"]
        return {"operation": "pdf_images", "format": "png", "dpi": render_dpi,
                "workers": self.args.workers, "naming": "clean", "quality": 90,
                "range": range_spec}

    def run(self, key: str, shard, rep: int = 0, sample: bool = False,
            range_spec: str = "") -> dict:
        cache_key = (key, shard, rep, sample, range_spec)
        if cache_key in self._runs:
            return self._runs[cache_key]
        self._run_index += 1
        suffix = "%s-%s-r%d%s" % (key, shard or "default", rep,
                                  "-sampled" if sample else "")
        out_dir = self.run_root / ("out-%03d-%s" % (self._run_index, suffix))
        config = self.engine_config(key, range_spec)
        if shard is not None:
            config["shard"] = shard
        result = run_engine(self.engine, self.fixture(key), out_dir, config,
                            sample=sample, timeout=self.args.timeout)
        result.update({"fixture": key, "shard": shard, "rep": rep,
                       "range": range_spec, "sampled": sample})
        self.run_log.append(result)
        self._runs[cache_key] = result
        return result

    def cli_run(self, shard: str) -> dict:
        cache_key = ("__cli__", shard)
        if cache_key in self._runs:
            return self._runs[cache_key]
        self._run_index += 1
        out_dir = self.run_root / ("out-cli-%03d-%s" % (self._run_index, shard))
        result = run_cli(self.chamo, self.fixture("cli"), out_dir, shard,
                         timeout=self.args.timeout)
        result.update({"fixture": "cli", "shard": shard, "sampled": False})
        self.run_log.append(result)
        self._runs[cache_key] = result
        return result

    def reps(self, key: str, shard, range_spec: str = "", count=None) -> list:
        total = int(count if count is not None else self.args.reps)
        return [self.run(key, shard, rep=rep, sample=False, range_spec=range_spec)
                for rep in range(max(1, total))]

    def paired_reps(self, key: str, range_spec: str = "", count=None) -> list:
        """Interleaved (off, auto) runs: adjacent in time, alternating order.

        A timing gate that runs all shard=off repetitions and then all shard=auto
        repetitions measures machine-load drift between the arms, not the
        feature: on a loaded host that turned an identical 1.10s/1.10s text pair
        into a 29.5% "regression". Pairing the arms and alternating their order
        makes both arms see the same conditions, and the gate judges the MEDIAN
        of the per-pair ratios (thresholds unchanged), which ignores one-off
        load spikes instead of turning them into a verdict.
        """
        total = max(1, int(count if count is not None else self.args.reps))
        pairs = []
        for rep in range(total):
            if rep % 2 == 0:
                off = self.run(key, "off", rep=rep, range_spec=range_spec)
                auto = self.run(key, "auto", rep=rep, range_spec=range_spec)
            else:
                auto = self.run(key, "auto", rep=rep, range_spec=range_spec)
                off = self.run(key, "off", rep=rep, range_spec=range_spec)
            pairs.append((off, auto))
        return pairs


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------
def gate_contract_cli(session: GateSession) -> dict:
    """engine/chamo.py must expose --shard {auto,off} and reject anything else."""
    name = "contract.cli_shard_flag"
    expected = ("--shard off and --shard auto accepted (exit 0, identical outputs); "
                "--shard bogus rejected with exit code 2")
    off = session.cli_run("off")
    auto = session.cli_run("auto")
    bogus = session.cli_run("bogus")
    problems = []
    for label, run in (("--shard off", off), ("--shard auto", auto)):
        if run["rc"] != 0:
            problems.append("%s exited %s (expected 0); stderr: %s"
                            % (label, run["rc"], run["stderr_tail"] or run["stdout_tail"]))
        elif not run["outputs"]:
            problems.append("%s produced no output files" % label)
    if bogus["rc"] != 2:
        problems.append("--shard bogus exited %s (expected 2 = argparse usage error); stderr: %s"
                        % (bogus["rc"], bogus["stderr_tail"]))
    if not problems:
        problems.extend(compare_outputs(off["outputs"], auto["outputs"]))
    details = ["off: rc=%s files=%d; auto: rc=%s files=%d; bogus: rc=%s"
               % (off["rc"], len(off["outputs"]), auto["rc"],
                  len(auto["outputs"]), bogus["rc"])]
    return check(name, "FAIL" if problems else "PASS", expected,
                 "identical output trees across the two flag values"
                 if not problems else "see details", problems + details)


def gate_contract_default_auto(session: GateSession) -> dict:
    """No shard key in the request must behave exactly like shard=auto."""
    name = "contract.default_is_auto"
    expected = ("request without a shard key behaves as auto: actually sharded and "
                "outputs identical to shard=auto")
    explicit = session.run("scan", "auto", rep=0, sample=True)
    default = session.run("scan", None, rep=0, sample=True)
    expected_pages = session.expected_pages("scan")
    problems = []
    explicit_reason = describe_unsuccessful(explicit)
    if explicit_reason:
        problems.append("explicit shard=auto run failed: %s" % explicit_reason)
    default_reason = describe_unsuccessful(default)
    if default_reason:
        problems.append("default (no shard key) run failed: %s" % default_reason)
    if not problems:
        problems.extend(compare_outputs(explicit["outputs"], default["outputs"],
                                       expected_pages))
        sharded = is_sharded(default)
        if sharded is None:
            problems.append("cannot observe whether the default run sharded: neither "
                            "report.json shard evidence nor process sampling is available")
        elif not sharded:
            problems.append("default run was NOT sharded (expected the default to be auto)")
        decision = shard_decision(default) or {}
        if decision and decision.get("mode") != "auto":
            problems.append("default run reports shard mode=%r (expected 'auto')"
                            % decision.get("mode"))
    return check(name, "FAIL" if problems else "PASS", expected,
                 "identical to shard=auto and sharded" if not problems else "see details",
                 problems + ["explicit auto: " + shard_evidence(explicit),
                             "default (no key): " + shard_evidence(default)])


def gate_equivalence(session: GateSession, key: str, label: str,
                     expect_sharded: bool, range_spec: str = "") -> dict:
    """Outputs of shard=off and shard=auto must be identical, and non-vacuous.

    A sharded arm that never sharded makes the comparison meaningless, so the
    gate also demands that the auto arm took the sharded path (expect_sharded),
    or did not take it (text PDFs). Both directions are observable failures.
    """
    expected_pages = session.expected_pages(key, range_spec)
    off = session.run(key, "off", rep=0, sample=True, range_spec=range_spec)
    auto = session.run(key, "auto", rep=0, sample=True, range_spec=range_spec)
    problems = compare_runs(off, auto, expected_pages)
    if not problems:
        auto_sharded = is_sharded(auto)
        if auto_sharded is None:
            problems.append(
                "cannot observe whether shard=auto sharded: no process-table backend "
                "worked and the engine recorded no shard decision, so the equivalence "
                "comparison would pass vacuously. Treating as FAIL.")
        elif expect_sharded and not auto_sharded:
            problems.append(
                "shard=auto did not shard this image-dominant fixture; the equivalence "
                "comparison would be vacuous (check the cost model / classification)")
        elif not expect_sharded and auto_sharded:
            problems.append(
                "shard=auto sharded a fixture it should not have sharded")
        off_sharded = is_sharded(off)
        if off_sharded is True:
            problems.append("control arm shard=off used the sharded path (expected it not to)")
    expected = ("shard=off vs shard=auto: identical relative path set, per-file "
                "SHA-256, page count and file count (%d pages)%s"
                % (expected_pages, "; auto arm must be sharded" if expect_sharded
                   else "; auto arm must NOT be sharded"))
    if problems:
        actual = ("off files=%d, auto files=%d; auto: %s"
                  % (len(off.get("outputs") or {}), len(auto.get("outputs") or {}),
                     shard_evidence(auto)))
    else:
        actual = ("identical: %d files, %d SHA-256, %d pages per arm; off [%s]; auto [%s]"
                  % (len(off["outputs"]), len(off["outputs"]), expected_pages,
                     shard_evidence(off), shard_evidence(auto)))
    return check(label, "FAIL" if problems else "PASS", expected, actual, problems)


def gate_decision_model(session: GateSession) -> list:
    """The cost model must shard real work and refuse work that cannot repay it.

    Fixed thresholds could not do both. The dense fixture is a 48-page A4 scan
    (~104 MPix): an old 96-page rule refused it, yet forced sharding measured
    1.15-1.40x. The sparse fixture is 96 small pages at 240 dpi (~64 MPix): an
    old 60-MPix rule admitted it, but that is not enough work to repay several
    interpreter starts. A model that always shards fails the second check; a
    model that always refuses fails the first.

    Each direction also compares the two arms byte for byte, so "it sharded" can
    never be shown by producing different output, and the sharded arm must report
    the model's own numbers (total_pixels, cores, predicted_gain, gain_floor).
    The positive direction further requires the process table to agree with the
    report: review showed a mutant that kept the report's used=true while
    rendering single-process passed the earlier version of this gate.
    """
    results = []
    dense_off = session.run("dense", "off", rep=0, sample=True)
    dense_auto = session.run("dense", "auto", rep=0, sample=True)
    dense_pages = session.expected_pages("dense")
    problems = compare_runs(dense_off, dense_auto, dense_pages)
    decision = shard_decision(dense_auto) or {}
    if not problems:
        sharded = is_sharded(dense_auto)
        if sharded is None:
            problems.append("cannot observe whether the dense fixture sharded: no "
                            "report decision and no process sampler")
        elif not sharded:
            problems.append("dense fixture was NOT sharded: 48 A4 pages (~104 MPix) "
                            "measured 1.15-1.40x under forced sharding, so the cost "
                            "model must admit it")
        shards = int(decision.get("shards") or 0)
        if shards < 2:
            problems.append("dense fixture reports shards=%r (expected at least 2)" % shards)
        # is_sharded() deliberately accepts "report OR process table" because the
        # equivalence gates only need to know that the sharded path was exercised.
        # This positive gate is different: a report that claims used=true while
        # the renderer actually stayed single-process must not pass, so when the
        # sampler is available it has to agree. The real engine spawns one child
        # per shard, and the sampler has always observed them (max_children>=2).
        if dense_auto.get("sampler_available") is True and (dense_auto.get("max_children") or 0) < 2:
            problems.append(
                "dense report says sharded but the process sampler saw at most %r "
                "concurrent worker process(es): the run was not really sharded"
                % (dense_auto.get("max_children"),))
        gain, floor = decision.get("predicted_gain"), decision.get("gain_floor")
        if not isinstance(gain, (int, float)) or not isinstance(floor, (int, float)):
            problems.append("dense report lacks cost-model fields: predicted_gain=%r "
                            "gain_floor=%r" % (gain, floor))
        elif gain < floor:
            problems.append("dense report predicted_gain=%.3f is below its own "
                            "gain_floor=%.3f, yet it sharded" % (gain, floor))
        if isinstance(floor, (int, float)) and abs(float(floor) - PINNED_GAIN_FLOOR) > 1e-9:
            problems.append(
                "dense report gain_floor=%r does not match the documented %.2f "
                "contract (the floor may not drift silently)" % (floor, PINNED_GAIN_FLOOR))
        if dense_auto.get("sampler_available") is not True:
            problems.append("dense run could not be process-sampled (%r), so '>=2 "
                            "processes' is unverified; unverified is not passed"
                            % (dense_auto.get("sampler_available"),))
        if not decision.get("total_pixels"):
            problems.append("dense report lacks total_pixels, the model's work unit")
    results.append(check(
        "decision.dense_work_shards",
        "FAIL" if problems else "PASS",
        "48 A4 pages (~104 MPix): shard=auto is sharded (>=2 processes observed by "
        "the process table), the output is byte-identical to shard=off, and the report "
        "carries predicted_gain >= gain_floor == %.2f" % PINNED_GAIN_FLOOR,
        ("%d files, identical per-file SHA-256; auto %s"
         % (len(dense_auto.get("outputs") or {}), shard_evidence(dense_auto)))
        if not problems else "see details",
        problems + ["shard=off: " + shard_evidence(dense_off),
                    "shard=auto: " + shard_evidence(dense_auto)]))

    sparse_off = session.run("sparse", "off", rep=0, sample=True)
    sparse_auto = session.run("sparse", "auto", rep=0, sample=True)
    sparse_pages = session.expected_pages("sparse")
    problems = compare_runs(sparse_off, sparse_auto, sparse_pages)
    decision = shard_decision(sparse_auto) or {}
    if not problems:
        sharded = is_sharded(sparse_auto)
        if sharded is None:
            problems.append("cannot observe whether the sparse fixture sharded: no "
                            "report decision and no process sampler")
        elif sharded:
            problems.append("sparse fixture WAS sharded: 96 small pages at 240 dpi "
                            "carry only ~64 MPix, not enough work to repay the processes")
        if decision.get("kind") != "image":
            problems.append("sparse fixture classified kind=%r (expected an "
                            "image-only scan)" % decision.get("kind"))
        gain, floor = decision.get("predicted_gain"), decision.get("gain_floor")
        # The report rounds predicted_gain to 3 decimals. A true gain of e.g.
        # 1.0995 is refused by the engine and displays as 1.100 with a floor of
        # 1.10, so only a gain clearly above the floor is a contradiction.
        if (isinstance(gain, (int, float)) and isinstance(floor, (int, float))
                and gain >= floor + 0.0005):
            problems.append("sparse report predicted_gain=%.3f is at or above "
                            "gain_floor=%.3f, yet the run refused to shard: the "
                            "report contradicts the decision" % (gain, floor))
    results.append(check(
        "decision.sparse_work_not_sharded",
        "FAIL" if problems else "PASS",
        "96 small pages at 240 dpi (~64 MPix): shard=auto must NOT shard and must keep "
        "the single-process output byte-identical to shard=off",
        ("single process, %d files identical per-file SHA-256"
         % len(sparse_auto.get("outputs") or {})) if not problems else "see details",
        problems + ["shard=off: " + shard_evidence(sparse_off),
                    "shard=auto: " + shard_evidence(sparse_auto)]))
    return results


def text_timing_verdict(ratios, use_cpu):
    """Decide the text-PDF timing gate from the per-pair ratios.

    Rules, unchanged from the original gate (extracted so they have a unit test):
      * inside the 5% budget -> PASS;
      * on wall time, when the host's own pairs disagree by more than the budget,
        the bar is that demonstrated noise, but an unresolvable measurement is an
        explicit SKIP ("this host cannot resolve 5%"), never a pass;
      * child CPU time keeps the plain 5% bar: a larger measured difference FAILS.
        A host-limited CPU observation is reported as a failure and re-measured on a
        quiet machine; it is not reclassified into a skip, because that would trade
        an acceptance requirement for a green build.
    """
    if not ratios:
        return {"status": "FAIL", "noise": 0.0, "ratio": 0.0, "regression": 0.0,
                "allowed": TEXT_REGRESSION_MAX,
                "inconclusive": "no valid text timing pair was measured"}
    ratio = statistics.median(ratios)
    regression = 1.0 - ratio
    # Half the spread of the per-pair ratios is this host's demonstrated repeatability.
    noise = ((max(ratios) - min(ratios)) / 2.0 / ratio
             if len(ratios) > 1 and ratio else 0.0)
    allowed = TEXT_REGRESSION_MAX
    inconclusive = None
    if not use_cpu and noise > TEXT_REGRESSION_MAX:
        allowed = noise
        inconclusive = (
            "this host cannot resolve a %.1f%% budget from wall time: its own "
            "repeated pairs differ by +-%.1f%%, and the platform reports no "
            "child CPU time" % (TEXT_REGRESSION_MAX * 100, noise * 100))
    if regression > allowed:
        status = "FAIL"
    elif inconclusive:
        # The measurement could not resolve the budget. Keeping it out of PASS is
        # the point: "not proven slower" is not "proven not slower".
        status = "SKIP"
    else:
        status = "PASS"
    return {"status": status, "noise": noise, "ratio": ratio,
            "regression": regression, "allowed": allowed,
            "inconclusive": inconclusive}


def gate_content_text(session: GateSession, reps=None) -> list:
    """Gate 2: a text PDF must not be sharded and must not regress >5%."""
    results = []
    # 2a. Observable: neither the process tree nor the engine report may say the
    # text PDF was sharded.
    text_auto = session.run("text", "auto", rep=0, sample=True)
    text_off = session.run("text", "off", rep=0, sample=True)
    problems = []
    if text_auto.get("fatal") or text_auto.get("rc") not in (0, None):
        problems.append("text shard=auto run failed: %s" % describe_fatal(text_auto))
    sharded = is_sharded(text_auto)
    if sharded is None:
        problems.append(
            "cannot observe whether the text PDF was sharded: no process-table backend "
            "worked and no shard decision was recorded. Unverified is not passed.")
    elif sharded:
        problems.append("text PDF WAS sharded (expected no sharding): "
                        + shard_evidence(text_auto))
    decision = shard_decision(text_auto) or {}
    if decision and decision.get("kind") and decision.get("kind") != "text":
        problems.append("content classifier called the text PDF kind=%r (expected 'text')"
                        % decision.get("kind"))
    if decision and decision.get("mode") != "auto":
        problems.append("text shard=auto run reports mode=%r (expected 'auto')"
                        % decision.get("mode"))
    results.append(check(
        "content.text.not_sharded",
        "FAIL" if problems else "PASS",
        "text PDF must not be sharded: report used=false/kind=text and max concurrent "
        "worker processes = 1",
        shard_evidence(text_auto) if not problems else "see details",
        problems + ["control shard=off: " + shard_evidence(text_off)]))

    # 2b. Timing: the bounded content sample must cost <5% on a text PDF.
    # Paired runs with the median of per-pair ratios; see paired_reps().
    #
    # Which clock: on a shared CI host wall time mostly measures other tenants.
    # A macOS runner at load average 28 produced per-pair ratios 0.67/0.91/1.07
    # for two arms that do the same work, i.e. a 9% "regression" out of noise.
    # Child CPU seconds are not inflated by neighbours, so where the platform
    # reports them (POSIX) the gate judges CPU time against the same 5% budget.
    # Where it does not (Windows) the gate judges wall time but refuses to make
    # a claim the host cannot resolve: if repeated pairs disagree by more than
    # the budget itself, the result is an explicit inconclusive SKIP carrying the
    # measured numbers, never a silent pass and never an invented regression.
    pairs = session.paired_reps("text", count=reps)
    problems = []
    for off_run, auto_run in pairs:
        for label, run in (("shard=off", off_run), ("shard=auto", auto_run)):
            if run.get("fatal") or run.get("rc") not in (0, None):
                problems.append("text %s timing arm failed: %s"
                                % (label, describe_fatal(run)))
    usable = [(off_run, auto_run) for off_run, auto_run in pairs
              if not off_run.get("timed_out") and not auto_run.get("timed_out")]
    wall_ratios = [off_run["wall"] / auto_run["wall"] for off_run, auto_run in usable
                   if auto_run.get("wall")]
    cpu_pairs = [(off_run["cpu"], auto_run["cpu"]) for off_run, auto_run in usable
                 if off_run.get("cpu") and auto_run.get("cpu")]
    cpu_ratios = [off_cpu / auto_cpu for off_cpu, auto_cpu in cpu_pairs]
    use_cpu = bool(usable) and len(cpu_ratios) == len(usable)
    ratios = cpu_ratios if use_cpu else wall_ratios
    metric = "child CPU seconds" if use_cpu else "wall time"
    verdict = text_timing_verdict(ratios, use_cpu)
    ratio, noise = verdict["ratio"], verdict["noise"]
    regression, inconclusive = verdict["regression"], verdict["inconclusive"]
    if verdict["status"] == "FAIL":
        problems.append(
            "text shard=auto is %.1f%% slower than shard=off (median of %d paired "
            "runs, %s; max allowed %.1f%%)"
            % (regression * 100, len(ratios), metric, verdict["allowed"] * 100))
    off_times = ["%.2f" % off_run["wall"] for off_run, _auto in pairs]
    auto_times = ["%.2f" % auto_run["wall"] for _off, auto_run in pairs]
    off_cpu_text = ["%.2f" % off_run["cpu"] if off_run.get("cpu") else "n/a"
                    for off_run, _auto in pairs]
    auto_cpu_text = ["%.2f" % auto_run["cpu"] if auto_run.get("cpu") else "n/a"
                     for _off, auto_run in pairs]
    results.append(check(
        "content.text.timing",
        "FAIL" if problems else verdict["status"],
        "text PDF shard=auto must not regress more than %.1f%% vs shard=off "
        "(median of %d interleaved pairs, measured in %s)"
        % (TEXT_REGRESSION_MAX * 100, len(pairs), metric),
        "auto/off ratio %.3fx in %s (noise +-%.1f%%); wall off %s / auto %s; "
        "cpu off %s / auto %s; paired %s; regression %+.1f%%; loadavg=%s%s"
        % (ratio, metric, noise * 100, off_times, auto_times,
           off_cpu_text, auto_cpu_text, ["%.2f" % r for r in ratios],
           -regression * 100, _loadavg_text(),
           "" if not inconclusive else "; " + inconclusive),
        problems,
        meta={"reason": "timing_noise", "metric": metric,
              "noise": round(noise, 6), "ratio": round(ratio, 6)}
        if inconclusive else {"metric": metric, "noise": round(noise, 6)}))
    return results


def gate_speed_image(session: GateSession, reps=None) -> dict:
    """Gate 3: image-dominant large PDF must be >= 1.5x faster with shard=auto.

    Paired runs with the median of the per-pair speedups. The threshold is the
    contract's 1.5x; pairing only removes the "the other arm ran while the
    machine was busy" artefact, it does not relax the bar.
    """
    name = "speed.image"
    expected_pages = session.expected_pages("perf")
    pairs = session.paired_reps("perf", count=reps)
    problems = []
    for off_run, auto_run in pairs:
        for label, run in (("shard=off", off_run), ("shard=auto", auto_run)):
            if run.get("fatal") or run.get("rc") not in (0, None):
                problems.append("%s run failed: %s" % (label, describe_fatal(run)))
            elif len(run["outputs"]) != expected_pages:
                problems.append("%s produced %d files, expected %d pages"
                                % (label, len(run["outputs"]), expected_pages))
    ratios = [off_run["wall"] / auto_run["wall"]
              for off_run, auto_run in pairs
              if auto_run.get("wall") and not auto_run.get("timed_out")
              and not off_run.get("timed_out")]
    speedup = statistics.median(ratios) if ratios else 0.0
    min_based = (min(off_run["wall"] for off_run, _ in pairs)
                 / min(auto_run["wall"] for _, auto_run in pairs)) if pairs else 0.0
    if not ratios:
        problems.append("no valid speed pair was measured")
    elif speedup < SPEEDUP_MIN:
        problems.append("median speedup %.3fx < required %.1fx (per-pair %s)"
                        % (speedup, SPEEDUP_MIN,
                           ["%.2f" % r for r in ratios]))
    sharded = is_sharded(pairs[-1][1]) if pairs else None
    if sharded is False:
        problems.append("speed arm was not sharded, so the number cannot be a sharding gain")
    off_times = ["%.2f" % off_run["wall"] for off_run, _auto in pairs]
    auto_times = ["%.2f" % auto_run["wall"] for _off, auto_run in pairs]
    return check(
        name,
        "FAIL" if problems else "PASS",
        "image-dominant %d-page PDF: shard=auto >= %.1fx end-to-end vs shard=off "
        "(median of %d interleaved pairs)" % (expected_pages, SPEEDUP_MIN, len(pairs)),
        "speedup %.3fx median (off runs %s; auto runs %s; per-pair %s; "
        "min-based %.3fx); auto: %s; loadavg=%s"
        % (speedup, off_times, auto_times, ["%.2f" % r for r in ratios],
           min_based, shard_evidence(pairs[-1][1]) if pairs else "no run",
           _loadavg_text()),
        problems)


def gate_shard_failure_visible(session: GateSession) -> dict:
    """Frozen contract 6: one failed shard must fail the job and publish nothing.

    No partial success is allowed: a job that failed must not leave a subset of
    its pages in the output folder, because that is exactly how a sharded
    renderer would silently lose pages.
    """
    name = "failure.shard_failure_is_visible"
    expected = ("a job whose shard cannot render fails as a whole: final event "
                "failed>=1/state=complete_with_exceptions, report results[0].state="
                "'failed', and zero page files published; the same file without the "
                "bad page must still succeed")
    fail_run = session.run("fail", "auto", rep=0, sample=True)
    control = session.run("fail", "auto", rep=0, sample=True,
                          range_spec=FAIL_CONTROL_RANGE)
    problems = []
    # Non-vacuity: the failing run must actually have taken the sharded path.
    if fail_run.get("fatal") or fail_run.get("timed_out") or fail_run.get("rc") not in (0, None):
        problems.append("failing run did not complete normally: %s" % describe_fatal(fail_run))
    sharded = is_sharded(fail_run)
    if sharded is not True:
        problems.append(
            "the failure did not happen on the sharded path (sharded=%r), so this gate "
            "proves nothing about shard failure handling" % (sharded,))
    finished = fail_run.get("finished") or {}
    if fail_run.get("report_results"):
        states = [str(record.get("state")) for record in fail_run["report_results"]]
        if "failed" not in states:
            problems.append("report.json results states=%s, expected one 'failed'" % states)
        failed_record = next((record for record in fail_run["report_results"]
                              if record.get("state") == "failed"), {})
        if failed_record and failed_record.get("outputs"):
            problems.append("the failed job still reports %d published outputs"
                            % len(failed_record["outputs"]))
    else:
        problems.append("no report.json results: cannot prove the job was marked failed")
    if finished:
        if int(finished.get("failed") or 0) < 1:
            problems.append("final event failed=%s (expected >=1)" % finished.get("failed"))
        if int(finished.get("success") or 0) != 0:
            problems.append("final event success=%s (expected 0)" % finished.get("success"))
        if int(finished.get("outputs") or 0) != 0:
            problems.append("final event outputs=%s (expected 0)" % finished.get("outputs"))
    else:
        problems.append("no 'finished' event: the supervisor did not report a result")
    leaked = sorted((fail_run.get("outputs") or {}).keys())
    if leaked:
        problems.append("page files were published by the failed job (%d): %s"
                        % (len(leaked), _summarize(leaked)))
    # Causality control: same file, range avoids the oversized page -> success.
    if control.get("fatal") or control.get("rc") not in (0, None):
        problems.append("control run (range %s) failed: %s"
                        % (FAIL_CONTROL_RANGE, describe_fatal(control)))
    else:
        expected_control = session.expected_pages("fail", FAIL_CONTROL_RANGE)
        if len(control["outputs"]) != expected_control:
            problems.append("control run published %d pages, expected %d"
                            % (len(control["outputs"]), expected_control))
        if is_sharded(control) is not True:
            problems.append("control run was not sharded, so it does not show the "
                            "failure came from the bad page")
    decision = shard_decision(fail_run) or {}
    details = ["failing run: %s" % shard_evidence(fail_run),
               "control run (range %s): %s; files=%d"
               % (FAIL_CONTROL_RANGE, shard_evidence(control),
                  len(control.get("outputs") or {})),
               "failed record error: %s" % (
                   (next((record.get("error") for record in fail_run.get("report_results") or []
                          if record.get("state") == "failed"), "") or "")[:300])]
    return check(name, "FAIL" if problems else "PASS", expected,
                 ("failed as a whole with 0 published pages; control succeeded"
                  if not problems else "see details"), problems + details)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _parse_modes(value: str) -> list:
    valid = {"all", "contract", "equivalence", "decision", "content", "speed", "failure"}
    items = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in items if item not in valid]
    if unknown:
        raise argparse.ArgumentTypeError("unknown mode(s): %s (choose from %s)"
                                         % (", ".join(unknown), ", ".join(sorted(valid))))
    if "all" in items:
        return ["contract", "equivalence", "decision", "content", "speed", "failure"]
    return items


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_shard.py",
        description="Independent acceptance gates for content-aware sharded PDF rendering.")
    parser.add_argument("--pdf", default="",
                        help="use this PDF as the image-dominant fixture instead of a synthetic one")
    parser.add_argument("--text-pdf", default="",
                        help="use this PDF as the text fixture instead of a synthetic one")
    parser.add_argument("--pages", type=int, default=0,
                        help="page count for the synthetic image-dominant fixture (default 120)")
    parser.add_argument("--text-pages", type=int, default=0,
                        help="page count for the synthetic text fixture (default 120)")
    parser.add_argument("--big-pages", type=int, default=0,
                        help="page count for the >999 page naming fixture (default 1200)")
    parser.add_argument("--mode", type=_parse_modes, default=_parse_modes("all"),
                        help="comma separated: all, contract, equivalence, decision, "
                             "content, speed, failure")
    parser.add_argument("--workers", type=int, default=6, help="workers in the test config")
    parser.add_argument("--reps", type=int, default=3,
                        help="repetitions for timing gates (best run of N is compared)")
    parser.add_argument("--perf", choices=["auto", "on", "off"], default="auto",
                        help="speed gate: auto = run locally and skip in CI; on = always run; "
                             "off = explicit skip")
    parser.add_argument("--strict", action="store_true",
                        help="treat every SKIP as a failure (release gating)")
    parser.add_argument("--engine", default=str(DEFAULT_ENGINE),
                        help="engine entry point (use a copy for mutation testing)")
    parser.add_argument("--chamo", default=str(DEFAULT_CHAMO),
                        help="CLI entry point (use a copy for mutation testing)")
    parser.add_argument("--workdir", default="",
                        help="directory for fixtures and outputs; default is a temp dir")
    parser.add_argument("--keep", action="store_true",
                        help="keep the work directory even when every gate passes")
    parser.add_argument("--json", default="", help="write a machine-readable report here")
    parser.add_argument("--timeout", type=int, default=900, help="per-engine-run timeout (s)")
    return parser


def parse_args(argv=None):
    return build_parser().parse_args(argv)


def perf_decision(args) -> tuple:
    """Decide whether the machine-dependent speed gate runs, and why not."""
    if args.perf == "off":
        return False, ("explicit --perf off: the speed gate was disabled by the "
                       "operator; this is NOT a pass")
    if args.perf == "on":
        return True, ""
    if os.environ.get("CI"):
        return False, ("CI is set: the >=1.5x threshold is machine-dependent and this "
                       "check is allowed to be skipped on CI, but it is NOT a pass. "
                       "Run scripts/verify_shard.py --mode speed --perf on on a quiet "
                       "machine for the real number")
    return True, ""


def main(argv=None) -> int:
    args = parse_args(argv)
    engine = Path(args.engine)
    if not engine.is_file():
        print("engine not found: %s" % engine, file=sys.stderr)
        return 2
    session = GateSession(args)
    checks = []
    started = time.monotonic()
    try:
        if "contract" in args.mode:
            checks.append(gate_contract_cli(session))
            checks.append(gate_contract_default_auto(session))
        if "equivalence" in args.mode:
            checks.append(gate_equivalence(session, "scan", "equivalence.image",
                                           expect_sharded=True))
            checks.append(gate_equivalence(session, "text", "equivalence.text",
                                           expect_sharded=False))
            checks.append(gate_equivalence(session, "scan", "equivalence.range",
                                           expect_sharded=True, range_spec=RANGE_SPEC))
            checks.append(gate_equivalence(session, "big", "equivalence.big_over_999_pages",
                                           expect_sharded=True))
        if "decision" in args.mode:
            checks.extend(gate_decision_model(session))
        if "content" in args.mode:
            checks.extend(gate_content_text(session))
        if "speed" in args.mode:
            should_run, reason = perf_decision(args)
            if should_run:
                checks.append(gate_speed_image(session))
            else:
                checks.append(check(
                    "speed.image",
                    "SKIP",
                    "image-dominant PDF: shard=auto >= %.1fx end-to-end vs shard=off"
                    % SPEEDUP_MIN,
                    "NOT RUN: " + reason,
                    ["This skip is printed on purpose and is not a pass."]))
        if "failure" in args.mode:
            checks.append(gate_shard_failure_visible(session))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        checks.append(check("harness", "FAIL", "complete run", "interrupted"))
    except Exception as error:  # noqa: BLE001 - the report must stay honest
        import traceback
        traceback.print_exc()
        checks.append(check("harness", "FAIL", "gates run to completion",
                            "%s: %s" % (type(error).__name__, error)))
    duration = time.monotonic() - started

    print()
    for result in checks:
        print_check(result)
    passed = sum(1 for result in checks if result["status"] == "PASS")
    failed = [result["name"] for result in checks if result["status"] == "FAIL"]
    skipped = [result["name"] for result in checks if result["status"] == "SKIP"]
    total = len(checks)
    print()
    print("RESULT: " + ("FAIL" if failed else "PASS_WITH_SKIPS" if skipped else "PASS")
          + " - %d passed, %d failed, %d skipped of %d gates, %.1fs"
          % (passed, len(failed), len(skipped), total, duration))
    if failed:
        print("failed gates: " + ", ".join(failed))
    if skipped:
        print("skipped gates (NOT passes): " + ", ".join(skipped))
    print("reproduce: " + " ".join([sys.executable, "scripts/verify_shard.py"]
                                   + list(argv if argv is not None else sys.argv[1:])))

    report = {
        "engine": str(engine), "args": vars(args), "duration_seconds": round(duration, 2),
        "checks": checks,
        "runs": [{
            "fixture": run.get("fixture"), "shard": run.get("shard"),
            "rep": run.get("rep"), "sampled": run.get("sampled"),
            "rc": run.get("rc"), "wall": round(run.get("wall") or 0, 3),
            "timed_out": run.get("timed_out"),
            "max_children": run.get("max_children"),
            "sampler_samples": run.get("sampler_samples"),
            "sampler_available": run.get("sampler_available"),
            "files": len(run.get("outputs") or {}),
            "shard_decision": shard_decision(run),
            "fatal": (run.get("fatal") or {}).get("code") if run.get("fatal") else None,
            "fatal_detail": (run.get("fatal") or {}).get("detail") if run.get("fatal") else None,
            "command": run.get("command"),
        } for run in session.run_log],
    }
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print("report: %s" % args.json)
    failed_any = bool(failed) or (args.strict and bool(skipped))
    if failed_any and not args.workdir:
        print("work directory kept for inspection: %s" % session.root)
    elif not args.keep and not args.workdir:
        shutil.rmtree(session.root, ignore_errors=True)
    else:
        print("work directory: %s" % session.root)
    return 1 if failed_any else 0


if __name__ == "__main__":
    sys.exit(main())
