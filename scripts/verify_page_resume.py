#!/usr/bin/env python3
"""Packaged-CLI acceptance for page-level resume (pdf_images).

The pytest gates (tests/test_page_resume.py) run against the checkout. A packaged
DMG / .deb adds failure modes those never touch: a launcher that drops the CHAMO_*
environment, a pruned runtime that lost a module the cache path imports, a cache
directory that cannot be created next to the packaged state root, or a launcher
that turns SIGINT into a different exit code. This script drives the launcher the
user actually gets, cancels a real run, and checks the disk.

It is the only acceptance script that runs with the page cache ON. The shard
gates keep it off (a warm cache would make their auto/off wall-clock arms
incomparable), and the checkout pytest suite sets CHAMO_PAGE_CACHE=0 for the same
reason. Here the cache is the feature under test.

What it proves, all from disk rather than from the report:
  * a cancelled run exits 3 and leaves zero files in the output folder;
  * a SIGKILLed run (crash simulation) publishes nothing and still resumes from
    the pages that had been cached;
  * resuming reuses the cached pages (their (mtime_ns, size, sha256) did not
    change) and publishes a byte-identical page set to a cache-off clean run;
  * report pages_reused agrees with the number of untouched cache pages;
  * a damaged cache page is rendered again and the published bytes stay correct;
  * changed source bytes / changed render config reuse nothing and never
    overwrite existing products;
  * CHAMO_PAGE_CACHE=0 writes no cache and matches the cache-free output;
  * the sharded path also resumes (skipped when the host has <2 cores);
  * age / size sweeps evict stale entries but keep a .active directory;
  * an unusable cache location (a file) or read-only root never fails the job;
  * the cache never appears in the output folder or the developer's default
    state root.

Usage:
    third_party/python/bin/python3 scripts/verify_page_resume.py \
        --chamo build/ChamoGUI验收.app/Contents/Resources/bin/chamo

    # installed package on Linux:
    third_party/python/bin/python3 scripts/verify_page_resume.py --chamo /usr/bin/chamo

Requires an interpreter with Pillow (the fixture is generated with it; every
package ships Pillow too).

Windows: every gate that stops a run needs POSIX signals / process groups / ps
(SIGINT, SIGKILL, os.getpgid, killpg, ps). Those gates print SKIP with a reason
instead of raising "Unsupported signal: 2" and the complete-run
gates still run: baseline, warm-cache reuse and byte equivalence, damaged cache,
invalidation, cache-off control arm, age/size sweeps, active-marker protection,
unavailable cache and default-root pollution.

Cancellation contract: a cancelled run must leave the output folder exactly as
it was - no files AND no empty folders. The publish window is short, so one gate
repeats a late cancel (right after the last-page progress event) and fails on
any residue; the resume gates also require the ORIGINAL folder name, so a
residue folder can never be excused by publishing into "name (2)".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PAGES = 128                       # inside the 120-200 page acceptance range
PAGE_W, PAGE_H = 800, 1000        # ~0.8 MPix per page -> ~102 MPix: shardable
SOURCE_DPI = 150
DPI = 150
FORMAT = "jpeg"
QUALITY = 90
CANCEL_AFTER = 8
DEFAULT_TIMEOUT = 900

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


STAGING_NAME = ".Chamo暂存"


def published_hashes(root: Path) -> dict:
    """Product files only; a crash may leave the staging folder behind."""
    hashes = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == STAGING_NAME:
            continue
        hashes[str(relative)] = sha256_file(path)
    return hashes


def tree_hashes(root: Path):
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): sha256_file(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def entries_under(root: Path):
    """Files and directories alike: an empty folder is cancel residue too."""
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def published_folders(out: Path):
    if not out.exists():
        return []
    return sorted(path.name for path in out.iterdir()
                  if path.is_dir() and path.name != STAGING_NAME)


def assert_original_folder(session: Session, out: Path, source: Path) -> bool:
    stem = Path(source).stem
    folders = published_folders(out)
    ok = folders == [stem]
    session.check("resume.original_folder", ok,
                  ["%s" % stem],
                  folders,
                  facts=["a cancel that leaves an empty folder makes the engine pick "
                         "'name (2)'; that is residue, not a pass"])
    return ok


def parse_event(line: str):
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class Run:
    def __init__(self, rc, events, text, wall):
        self.rc = rc
        self.events = events
        self.text = text
        self.wall = wall

    def finished(self):
        events = [event for event in self.events if event.get("type") == "finished"]
        return events[-1] if events else None

    def tail(self, limit=6):
        lines = [line for line in self.text.strip().splitlines() if line.strip()]
        return " | ".join(line[:400] for line in lines[-limit:])


def make_scan_pdf(path: Path, pages: int = PAGES) -> Path:
    """A raster-only multi-page PDF (one full-page image per page).

    Pillow is used on purpose: it ships inside every package, and the drawn
    'text' is part of the bitmap, so the content classifier sees a scan (needed
    for the sharded gate to take the sharded path).
    """
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    sheets = []
    for index in range(pages):
        sheet = Image.new("RGB", (PAGE_W, PAGE_H), (246, 246, 244))
        draw = ImageDraw.Draw(sheet)
        for row in range(12):
            draw.text((30, 40 + row * 60), "scan %03d line %02d" % (index + 1, row),
                      fill=(30, 30, 30))
        for column in range(0, PAGE_W, 83):
            draw.line([(column, 0), (column, PAGE_H)], fill=(205, 205, 205))
        item = path.parent / ("sheet-%03d.jpg" % index)
        sheet.save(str(item), "JPEG", quality=80)
        sheet.close()
        sheets.append(item)

    def frames():
        from PIL import Image as ImageModule
        for item in sheets:
            with ImageModule.open(str(item)) as handle:
                yield handle.convert("RGB")

    stream = frames()
    first = next(stream)
    first.save(str(path), "PDF", save_all=True, append_images=stream,
               resolution=float(SOURCE_DPI))
    first.close()
    return path


def process_tree(pid: int):
    """pid plus all its descendants, discovered with ps (POSIX only)."""
    try:
        listing = subprocess.run(["ps", "-axo", "pid=,ppid="],
                                 capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return [pid]
    children = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)
    tree = []
    stack = [pid]
    while stack:
        current = stack.pop()
        tree.append(current)
        stack.extend(children.get(current, []))
    return tree


def kill_tree_hard(pid: int) -> None:
    """SIGKILL the supervisor and the worker it spawned.

    The worker is in its own process group (pause/cancel reach it through the
    supervisor), so killing only the launcher's group would let the worker keep
    rendering and publishing after the simulated crash - the run would look
    successful and the gate would prove nothing.
    """
    for target in reversed(process_tree(pid)):
        try:
            os.kill(target, signal.SIGKILL)
        except OSError:
            pass


def expected_page_names(source: Path, pages: int = PAGES, fmt: str = FORMAT):
    stem = Path(source).stem
    extension = ".jpg" if fmt == "jpeg" else "." + fmt
    width = max(3, len(str(pages)))
    return {str(Path(stem) / ("%s_%0*d%s" % (stem, width, index + 1, extension)))
            for index in range(pages)}


def find_engine(launcher: Path, explicit: str = ""):
    """Locate the engine directory next to a packaged launcher, or None.

    Needed to ask the package's own platform_state where the DEFAULT state root
    is: the no-pollution gate checks that the real per-user folder does not gain
    page-cache entries. Layouts: macOS .app has Resources/bin/chamo + Resources/
    engine; the Windows ZIP has chamo.cmd + engine/; the Linux deb launcher is
    /usr/bin/chamo with the engine at /opt/chamo/engine.
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for base in (launcher.parent, launcher.parent.parent,
                 launcher.parent.parent.parent):
        candidates.extend([base / "engine", base / "Resources" / "engine",
                           base / "Contents" / "Resources" / "engine"])
    candidates.append(Path("/opt/chamo/engine"))
    for candidate in candidates:
        try:
            if (candidate / "platform_state.py").is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


class Session:
    def __init__(self, chamo: Path, work: Path, pages: int, timeout: int,
                 engine: Path = None):
        self.chamo = chamo
        self.work = work
        self.pages = pages
        self.timeout = timeout
        self.engine = engine
        self.checks = []
        self.skipped = []

    # -- process plumbing -------------------------------------------------
    def environment(self, state: Path, cache_dir: Path, extra=None, cache_on=True):
        env = dict(os.environ)
        for key in ("CHAMO_STATE_DIR", "CHAMO_PAGE_CACHE", "CHAMO_PAGE_CACHE_DIR",
                    "CHAMO_PAGE_CACHE_MAX_BYTES", "CHAMO_PAGE_CACHE_MAX_AGE"):
            env.pop(key, None)
        env["CHAMO_STATE_DIR"] = str(state)
        env["CHAMO_PAGE_CACHE"] = "1" if cache_on else "0"
        env["CHAMO_PAGE_CACHE_DIR"] = str(cache_dir)
        if extra:
            for key, value in extra.items():
                if value is None:
                    env.pop(str(key), None)
                else:
                    env[str(key)] = str(value)
        return env

    def command(self, source, out, *, shard, workers, dpi, fmt, quality):
        launcher = str(self.chamo)
        prefix = []
        # A .cmd launcher on Windows has to be started through cmd.exe; the
        # POSIX launchers (shell script or binary) are executed directly.
        if os.name == "nt" and Path(launcher).suffix.lower() in (".cmd", ".bat"):
            prefix = ["cmd", "/c", launcher]
            launcher = ""
        return prefix + ([launcher] if launcher else []) + [str(source), "-o", str(out), "-t", "pdf-images",
                "--dpi", str(dpi), "--format", fmt, "--quality", str(quality),
                "--workers", str(workers), "--shard", shard, "--json"]

    def run(self, source, out, state, cache_dir, *, shard="off", workers=1,
            dpi=DPI, fmt=FORMAT, quality=QUALITY, cache_on=True, extra_env=None):
        out.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        done = subprocess.run(
            self.command(source, out, shard=shard, workers=workers, dpi=dpi,
                         fmt=fmt, quality=quality),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=self.environment(state, cache_dir, extra_env, cache_on),
            timeout=self.timeout)
        events = [event for event in (parse_event(line)
                                      for line in done.stdout.splitlines())
                  if event is not None]
        return Run(done.returncode, events, done.stdout + done.stderr,
                   time.monotonic() - started)

    def cancel(self, source, out, state, cache_dir, *, after_pages=CANCEL_AFTER,
               shard="off", workers=1, dpi=DPI, fmt=FORMAT, quality=QUALITY):
        """Cancel a real run once the CLI reports N rendered pages. POSIX only."""
        out.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            self.command(source, out, shard=shard, workers=workers, dpi=dpi,
                         fmt=fmt, quality=quality),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            env=self.environment(state, cache_dir))
        events = []
        lines = []
        cancelled = False
        deadline = time.monotonic() + self.timeout
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            event = parse_event(line)
            if event is None:
                continue
            events.append(event)
            done = event.get("done")
            if (not cancelled and event.get("type") == "progress"
                    and isinstance(done, int) and done >= after_pages
                    and proc.poll() is None):
                proc.send_signal(signal.SIGINT)
                cancelled = True
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=60)
                raise SystemExit("the packaged run exceeded %ss" % self.timeout)
        rc = proc.wait(timeout=60)
        return Run(rc, events, "".join(lines), 0.0), cancelled

    def crash(self, source, out, state, cache_dir, *, after_pages=CANCEL_AFTER):
        """SIGKILL the launcher's process group mid-render (crash simulation).

        The trigger is the disk (pages already committed to the cache), not the
        throttled progress stream: a short run can publish before a progress line
        is flushed, and killing it then would test nothing.
        """
        out.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            self.command(source, out, shard="off", workers=1, dpi=DPI,
                         fmt=FORMAT, quality=QUALITY),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=self.environment(state, cache_dir), start_new_session=True)
        killed = False
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if len(self.cache_pages(cache_dir)) >= after_pages:
                kill_tree_hard(proc.pid)
                killed = True
                break
            time.sleep(0.005)
        if proc.poll() is None and not killed:
            kill_tree_hard(proc.pid)
        rc = proc.wait(timeout=60)
        if time.monotonic() > deadline:
            raise SystemExit("the packaged run exceeded %ss" % self.timeout)
        return Run(rc, [], "", 0.0), killed

    # -- disk helpers -----------------------------------------------------
    def report(self, state: Path) -> dict:
        paths = [path for path in state.glob("outputs/*/records/run-*/report.json")
                 if path.is_file()]
        if not paths:
            raise SystemExit("no report.json under %s" % state)
        newest = max(paths, key=lambda path: path.stat().st_mtime_ns)
        return json.loads(newest.read_text(encoding="utf-8"))

    def result(self, state: Path) -> dict:
        results = self.report(state).get("results") or [{}]
        return results[0]

    def counts(self, state: Path):
        result = self.result(state)
        return result.get("pages_reused"), result.get("pages_rendered")

    def cache_pages(self, cache_dir: Path):
        if not cache_dir.is_dir():
            return []
        return sorted(path for path in cache_dir.rglob("*")
                      if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)

    def cache_top_dirs(self, cache_dir: Path):
        return sorted(path for path in cache_dir.glob("*/*") if path.is_dir())

    # -- reporting --------------------------------------------------------
    def check(self, name, ok, expected, actual, facts=()):
        if ok:
            self.checks.append({"name": name, "status": "PASS"})
            print("[PASS] " + name)
        else:
            self.checks.append({"name": name, "status": "FAIL",
                                "expected": expected, "actual": actual})
            print("[FAIL] " + name)
            print("       expected: " + str(expected))
            print("       actual:   " + str(actual))
        for fact in facts:
            print("       " + str(fact))

    def skip(self, name, reason):
        self.skipped.append({"name": name, "reason": reason})
        print("[SKIP] " + name + " -- " + reason)

    def finish(self):
        passed = sum(1 for check in self.checks if check["status"] == "PASS")
        failed = sum(1 for check in self.checks if check["status"] == "FAIL")
        total = len(self.checks) + len(self.skipped)
        print()
        if failed:
            print("RESULT: FAIL - %d passed, %d failed, %d skipped of %d gates"
                  % (passed, failed, len(self.skipped), total))
            failed_names = [check["name"] for check in self.checks
                            if check["status"] == "FAIL"]
            print("failed gates: " + ", ".join(failed_names))
        else:
            print("RESULT: PASS - %d passed, 0 failed, %d skipped of %d gates"
                  % (passed, len(self.skipped), total))
        return 0 if not failed else 1


def cancelled_contract(session: Session, run: Run, out: Path,
                       name: str = "cancel.contract"):
    finished = run.finished()
    ok = (run.rc == 3
          and finished is not None and finished.get("state") == "cancelled"
          and entries_under(out) == [])
    session.check(name, ok,
                  {"rc": 3, "state": "cancelled", "entries_left_in_output": 0},
                  {"rc": run.rc,
                   "state": (finished or {}).get("state"),
                   "entries_left_in_output": entries_under(out)[:5]},
                  facts=["tail: " + run.tail()])
    return ok


def gate_cancel_and_resume(session: Session, source: Path, reference) -> None:
    # Windows: skip BEFORE calling session.cancel(). proc.send_signal(SIGINT)
    # raises ValueError("Unsupported signal: 2") there, which used to crash the
    # whole packaged acceptance right after baseline.clean_run.
    if not session.posix_cancel:
        session.skip("cancel.contract", session.posix_reason)
        session.skip("resume.equivalence", session.posix_reason)
        session.skip("resume.disk_evidence", session.posix_reason)
        session.skip("resume.original_folder", session.posix_reason)
        return

    state = session.work / "state-resume"
    cache_dir = session.work / "cache-resume"
    out = session.work / "out-resume"

    cancelled_run, cancelled = session.cancel(source, out, state, cache_dir)
    if not cancelled:
        session.check("cancel.contract", False, "a run cancelable mid-render",
                      "the packaged run finished before the cancel point")
        return
    if not cancelled_contract(session, cancelled_run, out):
        return

    before = session.cache_pages(cache_dir)
    snapshot = {}
    for path in before:
        info = path.stat()
        snapshot[str(path)] = (info.st_mtime_ns, info.st_size, sha256_file(path))
    if not before:
        session.check("cancel.contract", False,
                      "at least one rendered page cached before the cancel",
                      "the cache directory is empty after cancelling; "
                      "the per-page write path is missing")
        return

    resumed = session.run(source, out, state, cache_dir)
    session.check("resume.exit", resumed.rc == 0, 0, resumed.rc,
                  facts=["tail: " + resumed.tail()])
    if resumed.rc != 0:
        return
    session.check("resume.equivalence", tree_hashes(out) == reference,
                  "byte-identical page set to the cache-off clean run",
                  "%d files differing" % len(set(tree_hashes(out).items())
                                             ^ set(reference.items())))
    # A cancel that left a folder behind would make this run publish into
    # "name (2)": equivalence alone already fails, but the dedicated gate names
    # the broken promise (do not accept " (2)").
    assert_original_folder(session, out, source)

    current = {}
    for path in session.cache_pages(cache_dir):
        info = path.stat()
        current[str(path)] = (info.st_mtime_ns, info.st_size, sha256_file(path))
    unchanged = [path for path, stat in snapshot.items() if current.get(path) == stat]
    added = [path for path in current if path not in snapshot]
    changed = [path for path in snapshot if current.get(path) != snapshot[path]]
    reused, rendered = session.counts(state)
    session.check("resume.disk_evidence",
                  isinstance(reused, int) and isinstance(rendered, int)
                  and reused == len(unchanged)
                  and rendered == len(changed) + len(added),
                  {"pages_reused": len(unchanged),
                   "pages_rendered": len(changed) + len(added)},
                  {"pages_reused": reused, "pages_rendered": rendered},
                  facts=["%d cached pages were untouched, %d rewritten, %d added"
                         % (len(unchanged), len(changed), len(added))])


def gate_crash_resume(session: Session, source: Path, reference) -> None:
    """A SIGKILLed run (crash / power loss) must resume from the same cache."""
    if not session.posix_cancel:
        session.skip("crash.resume", session.posix_reason)
        return
    state = session.work / "state-crash"
    cache_dir = session.work / "cache-crash"
    out = session.work / "out-crash"
    killed_run, killed = session.crash(source, out, state, cache_dir)
    if not killed:
        session.check("crash.resume", False, "a run killable mid-render",
                      "the packaged run finished before the kill point")
        return
    leftover = published_hashes(out)
    session.check("crash.contract", leftover == {},
                  "no published product files after SIGKILL", sorted(leftover)[:5],
                  facts=["killed rc=%r" % killed_run.rc])

    before = session.cache_pages(cache_dir)
    snapshot = {}
    for path in before:
        info = path.stat()
        snapshot[str(path)] = (info.st_mtime_ns, info.st_size, sha256_file(path))
    if not before:
        session.check("crash.resume", False,
                      "at least one rendered page cached before the kill",
                      "the cache directory is empty after SIGKILL")
        return
    resumed = session.run(source, out, state, cache_dir)
    session.check("crash.exit", resumed.rc == 0, 0, resumed.rc,
                  facts=["tail: " + resumed.tail()])
    if resumed.rc != 0:
        return
    current = {}
    for path in session.cache_pages(cache_dir):
        info = path.stat()
        current[str(path)] = (info.st_mtime_ns, info.st_size, sha256_file(path))
    unchanged = [path for path, stat in snapshot.items() if current.get(path) == stat]
    reused, rendered = session.counts(state)
    assert_original_folder(session, out, source)
    session.check("crash.resume",
                  published_hashes(out) == reference
                  and isinstance(reused, int) and reused > 0
                  and reused + rendered == session.pages
                  and reused == len(unchanged),
                  {"outputs_equal_reference": True, "pages_reused > 0": True,
                   "reused_equals_untouched_cache_pages": True},
                  {"outputs_equal_reference": published_hashes(out) == reference,
                   "pages_reused": reused,
                   "untouched_cache_pages": len(unchanged)})


def gate_damaged_cache(session: Session, source: Path, reference) -> None:
    state = session.work / "state-damaged"
    cache_dir = session.work / "cache-damaged"
    warm = session.run(source, session.work / "out-damaged-warm", state, cache_dir)
    if warm.rc != 0:
        session.check("damaged_cache.rerender", False, 0, warm.rc,
                      facts=["tail: " + warm.tail()])
        return
    before = session.cache_pages(cache_dir)
    if not before:
        session.check("damaged_cache.rerender", False,
                      "a filled cache after a complete run",
                      "cache is empty; the cache was not written")
        return
    target = before[0]
    old = target.stat()
    target.write_bytes(target.read_bytes()[: max(16, old.st_size // 2)])

    fresh_out = session.work / "out-damaged"
    run = session.run(source, fresh_out, state, cache_dir)
    finished = run.finished()
    stats = target.stat() if target.exists() else None
    rewritten = stats is not None and (stats.st_mtime_ns, stats.st_size) != (
        old.st_mtime_ns, old.st_size)
    session.check("damaged_cache.rerender",
                  run.rc == 0 and rewritten and tree_hashes(fresh_out) == reference,
                  {"rc": 0, "damaged_page_rewritten": True,
                   "outputs_equal_reference": True},
                  {"rc": run.rc, "damaged_page_rewritten": rewritten,
                   "outputs_equal_reference": tree_hashes(fresh_out) == reference},
                  facts=["tail: " + run.tail()])


def gate_invalidation(session: Session, source: Path) -> None:
    state = session.work / "state-invalidation"
    cache_dir = session.work / "cache-invalidation"
    first_out = session.work / "out-invalidation-150"
    first = session.run(source, first_out, state, cache_dir)
    if first.rc != 0:
        session.check("invalidation.dpi_no_reuse", False, 0, first.rc)
        return
    first_hashes = tree_hashes(first_out)
    second_out = session.work / "out-invalidation-100"
    second = session.run(source, second_out, state, cache_dir, dpi=100)
    reused, rendered = session.counts(state)
    untouched = all((first_out / name).is_file()
                    and sha256_file(first_out / name) == digest
                    for name, digest in first_hashes.items())
    session.check("invalidation.dpi_no_reuse",
                  second.rc == 0 and reused == 0 and rendered == session.pages
                  and untouched,
                  {"rc": 0, "pages_reused": 0, "pages_rendered": session.pages,
                   "previous_products_untouched": True},
                  {"rc": second.rc, "pages_reused": reused,
                   "pages_rendered": rendered,
                   "previous_products_untouched": untouched},
                  facts=["tail: " + second.tail()])


def gate_cache_off(session: Session, source: Path, reference) -> None:
    state = session.work / "state-cache-off"
    cache_dir = session.work / "cache-cache-off"
    out = session.work / "out-cache-off"
    run = session.run(source, out, state, cache_dir, cache_on=False)
    pages = session.cache_pages(cache_dir)
    session.check("cache_off.matches_reference",
                  run.rc == 0 and tree_hashes(out) == reference and pages == [],
                  {"rc": 0, "outputs_equal_reference": True, "cache_page_files": 0},
                  {"rc": run.rc, "outputs_equal_reference": tree_hashes(out) == reference,
                   "cache_page_files": len(pages)},
                  facts=["tail: " + run.tail()])


def gate_sharded_resume(session: Session, source: Path, reference) -> None:
    if not session.posix_cancel:
        session.skip("sharded.resume", session.posix_reason)
        return
    if (os.cpu_count() or 1) < 2:
        session.skip("sharded.resume", "needs at least two cores for the sharded path")
        return
    state = session.work / "state-sharded"
    cache_dir = session.work / "cache-sharded"
    out = session.work / "out-sharded"
    cancelled_run, cancelled = session.cancel(source, out, state, cache_dir,
                                              after_pages=4, workers=4, shard="auto")
    if not cancelled:
        session.check("sharded.resume", False, "a sharded run cancelable mid-render",
                      "the run finished before the cancel point")
        return
    if not cancelled_contract(session, cancelled_run, out, name="sharded.cancel_contract"):
        return
    resumed = session.run(source, out, state, cache_dir, workers=4, shard="auto")
    shard = session.result(state).get("shard") or {}
    reused, rendered = session.counts(state)
    session.check("sharded.resume",
                  resumed.rc == 0 and shard.get("used") is True
                  and tree_hashes(out) == reference
                  and isinstance(reused, int) and reused > 0
                  and reused + rendered == session.pages,
                  {"rc": 0, "shard_used": True, "outputs_equal_reference": True,
                   "pages_reused > 0": True},
                  {"rc": resumed.rc, "shard_used": shard.get("used"),
                   "outputs_equal_reference": tree_hashes(out) == reference,
                   "pages_reused": reused},
                  facts=["shard: " + json.dumps(shard, ensure_ascii=False),
                         "tail: " + resumed.tail()])


def gate_warm_reuse(session: Session, source: Path, reference) -> None:
    """Portable reuse + byte equivalence (no signals), so Windows runs it too."""
    state = session.work / "state-warm"
    cache_dir = session.work / "cache-warm"
    warm_out = session.work / "out-warm"
    warm = session.run(source, warm_out, state, cache_dir)
    if warm.rc != 0:
        session.check("warm_reuse.equivalence", False, 0, warm.rc,
                      facts=["tail: " + warm.tail()])
        return
    snapshot = {}
    for path in session.cache_pages(cache_dir):
        info = path.stat()
        snapshot[str(path)] = (info.st_mtime_ns, info.st_size, sha256_file(path))
    second_out = session.work / "out-warm-second"
    second = session.run(source, second_out, state, cache_dir)
    reused, rendered = session.counts(state)
    unchanged = {}
    for path in session.cache_pages(cache_dir):
        info = path.stat()
        unchanged[str(path)] = (info.st_mtime_ns, info.st_size, sha256_file(path))
    session.check("warm_reuse.equivalence",
                  second.rc == 0
                  and tree_hashes(second_out) == reference
                  and isinstance(reused, int) and reused == session.pages
                  and rendered == 0,
                  {"rc": 0, "outputs_equal_reference": True,
                   "pages_reused": session.pages, "pages_rendered": 0},
                  {"rc": second.rc,
                   "outputs_equal_reference": tree_hashes(second_out) == reference,
                   "pages_reused": reused, "pages_rendered": rendered},
                  facts=["tail: " + second.tail()])
    assert_original_folder(session, second_out, source)
    session.check("warm_reuse.cache_untouched", unchanged == snapshot,
                  "reused cache page files unchanged (mtime/size/sha)",
                  "changed=%d of %d" % (sum(1 for key in snapshot
                                            if snapshot.get(key) != unchanged.get(key)),
                                        len(snapshot)))


def gate_late_cancel_withdraw(session: Session, source: Path, reference) -> None:
    """Deterministic withdraw via the test-only, default-off engine hook.

    CHAMO_TEST_LATE_CANCEL=1 makes the supervisor take the withdraw path without
    racing a real signal. rc=3 iff cancelled; the output folder must be empty.
    """
    state = session.work / "state-withdraw"
    cache_dir = session.work / "cache-withdraw"
    out = session.work / "out-withdraw"
    run = session.run(source, out, state, cache_dir,
                      extra_env={"CHAMO_TEST_LATE_CANCEL": "1"})
    finished = run.finished()
    ok = (run.rc == 3
          and finished is not None and finished.get("state") == "cancelled"
          and entries_under(out) == [])
    session.check("late_cancel.withdraw", ok,
                  {"rc": 3, "state": "cancelled", "entries_left_in_output": 0},
                  {"rc": run.rc,
                   "state": (finished or {}).get("state"),
                   "entries_left_in_output": entries_under(out)[:5]},
                  facts=["tail: " + run.tail()])
    if not ok:
        return
    stem = Path(source).stem
    resumed = session.run(source, out, state, cache_dir)
    session.check("late_cancel.rerun_original_folder",
                  resumed.rc == 0 and tree_hashes(out) == reference
                  and published_folders(out) == [stem],
                  {"rc": 0, "outputs_equal_reference": True, "folder": [stem]},
                  {"rc": resumed.rc,
                   "outputs_equal_reference": tree_hashes(out) == reference,
                   "folder": published_folders(out)},
                  facts=["tail: " + resumed.tail()])

    # Control: without the hook the engine must behave normally (rc=0, complete).
    control_state = session.work / "state-withdraw-control"
    control_out = session.work / "out-withdraw-control"
    control = session.run(source, control_out, control_state,
                          session.work / "cache-withdraw-control")
    control_finished = control.finished()
    session.check("late_cancel.hook_off_control",
                  control.rc == 0
                  and control_finished is not None
                  and control_finished.get("state") == "complete"
                  and tree_hashes(control_out) == reference,
                  {"rc": 0, "state": "complete", "outputs_equal_reference": True},
                  {"rc": control.rc,
                   "state": (control_finished or {}).get("state"),
                   "outputs_equal_reference": tree_hashes(control_out) == reference},
                  facts=["tail: " + control.tail()])


def gate_sweeps(session: Session, source: Path) -> None:
    state = session.work / "state-sweeps"
    cache_dir = session.work / "cache-sweeps"
    warm = session.run(source, session.work / "out-sweep-warm", state, cache_dir)
    if warm.rc != 0 or not session.cache_pages(cache_dir):
        session.check("sweep.age", False, "a filled cache after a complete run", "no cache")
        session.check("sweep.bytes", False, "a filled cache after a complete run", "no cache")
        return
    time.sleep(1.2)
    aged = session.run(source, session.work / "out-sweep-aged", state, cache_dir,
                       extra_env={"CHAMO_PAGE_CACHE_MAX_AGE": "1"})
    reused, rendered = session.counts(state)
    session.check("sweep.age",
                  aged.rc == 0 and reused == 0 and rendered == session.pages,
                  {"rc": 0, "pages_reused": 0, "pages_rendered": session.pages},
                  {"rc": aged.rc, "pages_reused": reused, "pages_rendered": rendered},
                  facts=["tail: " + aged.tail()])

    older_state = session.work / "state-sweep-bytes"
    older_cache = session.work / "cache-sweep-bytes"
    session.run(source, session.work / "out-sweep-a", older_state, older_cache, dpi=150)
    session.run(source, session.work / "out-sweep-b", older_state, older_cache, dpi=100)
    older = set(str(path) for path in session.cache_pages(older_cache))
    if not older:
        session.check("sweep.bytes", False, "cache entries to evict", "no cache")
        return
    capped = session.run(source, session.work / "out-sweep-c", older_state, older_cache,
                         dpi=125, extra_env={"CHAMO_PAGE_CACHE_MAX_BYTES": "1024"})
    remaining = set(str(path) for path in session.cache_pages(older_cache))
    session.check("sweep.bytes",
                  capped.rc == 0 and not (older & remaining),
                  {"rc": 0, "oldest_cache_entries_kept": 0},
                  {"rc": capped.rc,
                   "oldest_cache_entries_kept": len(older & remaining)},
                  facts=["tail: " + capped.tail()])


def gate_active_marker(session: Session, source: Path) -> None:
    state = session.work / "state-active"
    cache_dir = session.work / "cache-active"
    cache_dir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 10 * 24 * 3600

    def expired_directory(name: str, active: bool):
        directory = cache_dir / "pdf_images" / (name * 64) / ("c" * 64)
        directory.mkdir(parents=True, exist_ok=True)
        page = directory / "page00001.png"
        page.write_bytes(b"stale rendered page")
        os.utime(str(page), (old, old))
        if active:
            (directory / ".active").write_text("running", encoding="utf-8")
            (directory.parent / ".active").write_text("running", encoding="utf-8")
        for parent in (directory, directory.parent):
            os.utime(str(parent), (old, old))
        return page

    protected = expired_directory("a", True)
    unprotected = expired_directory("b", False)
    run = session.run(source, session.work / "out-active", state, cache_dir,
                      extra_env={"CHAMO_PAGE_CACHE_MAX_AGE": "1"})
    session.check("sweep.active_marker",
                  run.rc == 0 and protected.exists() and not unprotected.exists(),
                  {"rc": 0, "active_dir_kept": True, "expired_dir_deleted": True},
                  {"rc": run.rc, "active_dir_kept": protected.exists(),
                   "expired_dir_deleted": not unprotected.exists()},
                  facts=["tail: " + run.tail()])


def gate_unavailable_cache(session: Session, source: Path, reference) -> None:
    state = session.work / "state-unavailable"
    bogus = session.work / "cache-is-a-file"
    bogus.write_bytes(b"not a directory")
    out = session.work / "out-unavailable"
    run = session.run(source, out, state, bogus)
    reused, rendered = session.counts(state)
    session.check("unavailable_cache.never_fails",
                  run.rc == 0 and tree_hashes(out) == reference
                  and reused == 0 and rendered == session.pages,
                  {"rc": 0, "outputs_equal_reference": True,
                   "pages_reused": 0, "pages_rendered": session.pages},
                  {"rc": run.rc, "outputs_equal_reference": tree_hashes(out) == reference,
                   "pages_reused": reused, "pages_rendered": rendered},
                  facts=["tail: " + run.tail()])


def gate_no_pollution(session: Session, source: Path) -> None:
    """CHAMO_STATE_DIR redirects the cache; the real per-user root stays untouched."""
    if session.engine is None:
        session.skip("cache.no_default_pollution",
                     "cannot locate the packaged engine directory to resolve the "
                     "default state root (pass --engine)")
        return
    state = session.work / "state-pollution"
    cache_dir = session.work / "cache-pollution"
    script = ("import sys; sys.path.insert(0, %r); import platform_state; "
              "print(platform_state.root())"
              % str(session.engine))
    env = {key: value for key, value in os.environ.items()
           if key not in ("CHAMO_STATE_DIR", "CHAMO_PAGE_CACHE_DIR", "CHAMO_PAGE_CACHE")}
    real_root = None
    done = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                          capture_output=True, text=True, env=env, timeout=120)
    if done.returncode == 0 and done.stdout.strip():
        real_root = Path(done.stdout.strip())
    before = set()
    if real_root is not None and real_root.exists():
        before = {str(path) for path in real_root.rglob("*") if "page-cache" in str(path)}
    out = session.work / "out-pollution"
    run = session.run(source, out, state, cache_dir)
    pages = session.cache_pages(cache_dir)
    after = set()
    if real_root is not None and real_root.exists():
        after = {str(path) for path in real_root.rglob("*") if "page-cache" in str(path)}
    session.check("cache.no_default_pollution",
                  run.rc == 0 and pages and before == after,
                  {"rc": 0, "isolated_cache_page_files": session.pages,
                   "default_state_root_new_page_cache_paths": 0},
                  {"rc": run.rc, "isolated_cache_page_files": len(pages),
                   "default_state_root_new_page_cache_paths": len(after - before)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Packaged-CLI acceptance for page-level resume")
    parser.add_argument("--chamo", required=True,
                        help="packaged CLI launcher to test (chamo or chamo.cmd)")
    parser.add_argument("--engine", default="",
                        help="packaged engine directory, if it cannot be found next to the launcher")
    parser.add_argument("--work", "--workdir", dest="workdir", default="",
                        help="work directory (default: a fresh temporary directory)")
    parser.add_argument("--pages", type=int, default=PAGES,
                        help="fixture page count (default %d)" % PAGES)
    parser.add_argument("--keep", action="store_true",
                        help="keep the work directory after a passing run")
    parser.add_argument("--json", default="", help="write the machine-readable report here")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="per-run timeout in seconds (default %d)" % DEFAULT_TIMEOUT)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    chamo = Path(args.chamo).expanduser().resolve()
    if not chamo.exists():
        print("FAIL missing packaged launcher: %s" % chamo)
        return 1
    keep = args.keep
    if args.workdir:
        work = Path(args.workdir).expanduser().resolve()
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="dw-page-resume-"))
    engine = find_engine(chamo, args.engine)
    session = Session(chamo, work, args.pages, args.timeout, engine)
    session.posix_cancel = os.name == "posix"
    session.posix_reason = ("SIGINT cancellation is POSIX-only; the Windows package "
                            "job runs the complete-run gates")
    print("packaged CLI: %s" % chamo)
    print("packaged engine: %s" % engine)
    print("work dir: %s" % work)
    print()
    try:
        source = make_scan_pdf(work / "扫描讲义.pdf", args.pages)
        reference_state = work / "state-reference"
        reference_cache = work / "cache-reference"
        reference_out = work / "out-reference"
        print("== clean cache-off run (baseline) ==")
        baseline = session.run(source, reference_out, reference_state, reference_cache,
                               cache_on=False)
        if baseline.rc != 0:
            session.check("baseline.clean_run", False, 0, baseline.rc,
                          facts=["tail: " + baseline.tail()])
            return session.finish()
        required = expected_page_names(source, args.pages)
        session.check("baseline.clean_run",
                      set(tree_hashes(reference_out)) == required,
                      "%d page files matching the naming contract" % args.pages,
                      "%d files" % len(tree_hashes(reference_out)))
        reference = tree_hashes(reference_out)

        gate_warm_reuse(session, source, reference)
        gate_late_cancel_withdraw(session, source, reference)
        gate_cancel_and_resume(session, source, reference)
        gate_crash_resume(session, source, reference)
        gate_damaged_cache(session, source, reference)
        gate_invalidation(session, source)
        gate_cache_off(session, source, reference)
        gate_sharded_resume(session, source, reference)
        gate_sweeps(session, source)
        gate_active_marker(session, source)
        gate_unavailable_cache(session, source, reference)
        gate_no_pollution(session, source)
        return session.finish()
    finally:
        report = {"checks": session.checks, "skipped": session.skipped,
                  "work": str(work), "chamo": str(chamo)}
        if args.json:
            Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        failed = any(check["status"] == "FAIL" for check in session.checks)
        if keep or failed:
            print()
            print("work directory kept for inspection: %s" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
