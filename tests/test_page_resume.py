"""Independent, fail-capable acceptance gates for page-level resume.

These gates are written against the page-cache contract, not against the
engine, so they must fail when the cache is absent, when "reuse" re-renders
pages, when a damaged cache page is copied blindly, or when the report disagrees
with what is actually on disk. To prove they can fail, point
CHAMO_TEST_ENGINE_DIR at an engine without the page cache and run this file.

Environment:
    CHAMO_TEST_ENGINE_DIR   engine directory under test (default: repo engine/)
    CHAMO_TEST_KEEP=1       keep the run workspaces (pytest keeps failed ones anyway)

POSIX-only: every gate that stops a run uses POSIX-only primitives (SIGINT /
SIGKILL delivery, os.getpgid/os.killpg, "ps -axo pid,ppid"). On Windows those
gates skip with a printed reason instead of raising "Unsupported signal: 2",
and the complete-run gates still run there: cache fill, warm-cache
reuse + byte equivalence, invalidation, sweeps, isolation, unavailable cache.

Cancellation contract: a cancelled run must leave the output folder
exactly as it was - no files AND no empty folders. A late cancel that interrupts
the publish loop used to leave the empty product folder behind, which made the
resumed run publish into "name (2)"; the gates assert both the residue-free
folder and that a resume keeps the ORIGINAL folder name.

The gates deliberately do not trust the report alone: reuse is proven from
(mtime_ns, size, sha256) snapshots of the cache page files, and every compare
against "a clean run" is a full relative-path + SHA-256 comparison.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENGINE = ROOT / "engine"

PAGES = 128                 # inside the 120-200 page acceptance range
SOURCE_DPI = 150
# ~0.8 MPix per page, so 128 pages is ~102 MPix. That is deliberately above the
# shard cost model's floor: with >=2 cores the model predicts ~1.14x and takes
# the sharded path, which the sharded resume gate requires (a comparison of two
# unsharded runs would prove nothing).
PAGE_W, PAGE_H = 800, 1000
DPI = 150
FORMAT = "jpeg"
QUALITY = 90
CANCEL_AFTER_PAGES = 8
TIMEOUT = 900

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}

# Every gate that stops a run uses POSIX-only primitives: SIGINT/SIGKILL delivery,
# os.getpgid/os.killpg and the "ps -axo pid,ppid" process-tree scan. On Windows
# proc.send_signal(SIGINT) raises ValueError ("Unsupported signal: 2") and
# os.killpg does not exist, so those cases skip with this printed reason instead
# of failing the whole file; the complete-run gates still run there.
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only signals/process-group/ps (SIGINT, SIGKILL, os.killpg, ps); "
           "Windows CI runs the complete-run gates")

not_root_posix = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="a read-only cache directory cannot be enforced on Windows or when running as root")


# --------------------------------------------------------------------------
# engine / process helpers
# --------------------------------------------------------------------------
def engine_dir() -> Path:
    override = os.environ.get("CHAMO_TEST_ENGINE_DIR")
    if override:
        return Path(override).resolve()
    return DEFAULT_ENGINE.resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _state_env(state: Path, extra=None):
    """A hermetic environment: every gate gets its own state root and cache dir.

    The repository conftest turns the page cache off for the pre-existing test
    suite (its behaviour, timing and disk usage stay unchanged). These gates are
    the ones that exercise the cache, so they turn it back on explicitly and pin
    both CHAMO_STATE_DIR and CHAMO_PAGE_CACHE_DIR into the test's own temporary tree.
    """
    env = dict(os.environ)
    for key in ("CHAMO_STATE_DIR", "CHAMO_PAGE_CACHE", "CHAMO_PAGE_CACHE_DIR",
                "CHAMO_PAGE_CACHE_MAX_BYTES", "CHAMO_PAGE_CACHE_MAX_AGE"):
        env.pop(key, None)
    env["CHAMO_STATE_DIR"] = str(state)
    env["CHAMO_PAGE_CACHE"] = "1"
    env["CHAMO_PAGE_CACHE_DIR"] = str(state / "page-cache")
    if extra:
        for key, value in extra.items():
            if value is None:
                env.pop(str(key), None)
            else:
                env[str(key)] = str(value)
    return env


def _command(source: Path, out: Path, workers: int, shard: str, dpi: int,
             fmt: str, quality: int):
    return [sys.executable, "-I", "-B", str(engine_dir() / "chamo.py"),
            str(source), "-o", str(out), "-t", "pdf-images",
            "--dpi", str(dpi), "--format", fmt, "--quality", str(quality),
            "--workers", str(workers), "--shard", shard, "--json"]


def parse_event(line: str):
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class Run:
    def __init__(self, rc, events, text):
        self.rc = rc
        self.events = events
        self.text = text

    def finished(self):
        events = [event for event in self.events if event.get("type") == "finished"]
        return events[-1] if events else None


def describe(run: Run) -> str:
    parts = ["rc=%r" % run.rc]
    finished = run.finished()
    if finished:
        parts.append("finished=%r" % {key: finished.get(key)
                                      for key in ("state", "success", "failed", "outputs")})
    tail = [line for line in run.text.strip().splitlines() if line.strip()][-6:]
    if tail:
        parts.append("tail=" + " | ".join(tail))
    return "; ".join(parts)


def run_pdf_images(source: Path, out: Path, state: Path, *, workers: int = 1,
                   shard: str = "off", dpi: int = DPI, fmt: str = FORMAT,
                   quality: int = QUALITY, env_extra=None, timeout: int = TIMEOUT) -> Run:
    out.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(_command(source, out, workers, shard, dpi, fmt, quality),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=_state_env(state, env_extra),
                          timeout=timeout)
    events = [event for event in (parse_event(line) for line in done.stdout.splitlines())
              if event is not None]
    return Run(done.returncode, events, done.stdout + done.stderr)


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
    """SIGKILL the whole tree: the supervisor and the worker it spawned.

    Killing only the supervisor's process group is not a crash simulation: the
    worker is in its own process group (that is how pausing and cancelling reach
    it), so it would keep rendering, write the cache AND publish the products
    after the "crash" - the run would look successful and the gate would test
    nothing. A real crash/power loss stops every process.
    """
    for target in reversed(process_tree(pid)):
        try:
            os.kill(target, signal.SIGKILL)
        except OSError:
            pass


def kill_pdf_images(source: Path, out: Path, state: Path, *,
                    after_pages: int = CANCEL_AFTER_PAGES, workers: int = 1,
                    shard: str = "off", dpi: int = DPI, fmt: str = FORMAT,
                    quality: int = QUALITY, env_extra=None,
                    timeout: int = TIMEOUT):
    """SIGKILL the whole engine tree mid-render (crash / power-loss).

    SIGINT exercises the graceful cancel path; a crash is the case that leaves
    half-written cache pages and a stale .active marker behind, so it needs its
    own gate. POSIX only (the owning test is marked accordingly).
    """
    out.mkdir(parents=True, exist_ok=True)
    # Kill on the disk state (pages already committed to the cache), not on the
    # throttled progress event stream: a short run can finish publishing before a
    # progress line is even flushed, which would kill it after the publish and
    # test nothing. The cache page appears as soon as one page is rendered, so
    # this stops the writer in the middle of the render, like a power loss.
    proc = subprocess.Popen(_command(source, out, workers, shard, dpi, fmt, quality),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=_state_env(state, env_extra),
                            start_new_session=True)
    killed = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if len(cache_pages(state)) >= after_pages:
            kill_tree_hard(proc.pid)
            killed = True
            break
        time.sleep(0.005)
    if proc.poll() is None and not killed:
        kill_tree_hard(proc.pid)
    rc = proc.wait(timeout=60)
    if time.monotonic() > deadline:
        raise AssertionError("the engine run exceeded %ss" % timeout)
    return Run(rc, [], ""), killed


def cancel_pdf_images(source: Path, out: Path, state: Path, *,
                      after_pages: int = CANCEL_AFTER_PAGES, workers: int = 1,
                      shard: str = "off", dpi: int = DPI, fmt: str = FORMAT,
                      quality: int = QUALITY, env_extra=None,
                      timeout: int = TIMEOUT):
    """Start a real run and send SIGINT once the engine reports N rendered pages.

    The cancel point is page progress, not wall time, so the test is stable on a
    slow CI runner and still cancels mid-render on a fast laptop. Returns
    (Run, cancelled); cancelled is False when the run finished before the signal,
    which is itself a gate failure (the caller asserts it).
    """
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(_command(source, out, workers, shard, dpi, fmt, quality),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=_state_env(state, env_extra))
    events = []
    lines = []
    cancelled = False
    deadline = time.monotonic() + timeout
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
            raise AssertionError("the engine run exceeded %ss" % timeout)
    rc = proc.wait(timeout=60)
    return Run(rc, events, "".join(lines)), cancelled


def cancel_at_last_page(source: Path, out: Path, state: Path, *,
                        env_extra=None, timeout: int = TIMEOUT):
    """SIGINT the moment the renderer reports the last page (the publish window).

    The engine publishes right after that progress event. This is the window in
    which a cancel can interrupt the publish loop: the folder has been created
    but the run's outputs are removed on rollback, leaving an empty directory
    or, on another interleaving, published files. The caller repeats
    the attempt because the window is short.
    """
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(_command(source, out, 1, "off", DPI, FORMAT, QUALITY),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=_state_env(state, env_extra))
    events = []
    lines = []
    triggered = False
    deadline = time.monotonic() + timeout
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
        total = event.get("total")
        if (not triggered and event.get("type") == "progress"
                and isinstance(done, int) and isinstance(total, int)
                and done == total and done > 0 and proc.poll() is None):
            proc.send_signal(signal.SIGINT)
            triggered = True
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait(timeout=60)
            raise AssertionError("the engine run exceeded %ss" % timeout)
    rc = proc.wait(timeout=60)
    return Run(rc, events, "".join(lines)), triggered, entries_under(out)


def run_sources(sources, out: Path, state: Path, *, workers: int = 1,
                env_extra=None, timeout: int = TIMEOUT) -> Run:
    """CLI run over one or more inputs (no signals), for withdraw-hook tests."""
    if isinstance(sources, (str, Path)):
        sources = [sources]
    out.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-I", "-B", str(engine_dir() / "chamo.py")]
    command += [str(source) for source in sources]
    command += ["-o", str(out), "-t", "pdf-images", "--dpi", str(DPI),
                "--format", FORMAT, "--quality", str(QUALITY),
                "--workers", str(workers), "--shard", "off", "--json"]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=_state_env(state, env_extra),
                          timeout=timeout)
    events = [event for event in (parse_event(line)
                                  for line in done.stdout.splitlines())
              if event is not None]
    return Run(done.returncode, events, done.stdout + done.stderr)


def run_operation(sources, out: Path, state: Path, operation: str, *,
                  workers: int = 1, env_extra=None, extra_args=(),
                  timeout: int = TIMEOUT) -> Run:
    """Generic CLI run for single-file operations (no page-folder naming).

    Used by the adopt gate: adopt is only reachable when the product is a single
    file in the output folder. A multi-page pdf_images product always lands in a
    page folder, and page_folder() picks "name (2)" when a folder exists, so the
    adopt branch is unreachable for it by design (see the adopt test).
    """
    if isinstance(sources, (str, Path)):
        sources = [sources]
    out.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-I", "-B", str(engine_dir() / "chamo.py")]
    command += [str(source) for source in sources]
    command += ["-o", str(out), "-t", operation, "--workers", str(workers)]
    command += list(extra_args)
    command += ["--json"]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=_state_env(state, env_extra),
                          timeout=timeout)
    events = [event for event in (parse_event(line)
                                  for line in done.stdout.splitlines())
              if event is not None]
    return Run(done.returncode, events, done.stdout + done.stderr)


def late_cancel_hook_run(sources, out: Path, state: Path, *, workers: int = 1,
                         env_extra=None, timeout: int = TIMEOUT) -> Run:
    """Deterministic withdraw trigger: the engine's test-only CHAMO_TEST_LATE_CANCEL.

    The hook makes the supervisor treat the run as if a cancel had arrived inside
    the finalize snapshot window, so the withdraw path runs without racing a real
    signal. It is default-off: when unset (see the control test) nothing changes.
    """
    extra = {"CHAMO_TEST_LATE_CANCEL": "1"}
    if env_extra:
        extra.update(env_extra)
    return run_sources(sources, out, state, workers=workers,
                       env_extra=extra, timeout=timeout)


# --------------------------------------------------------------------------
# disk helpers: output tree, page cache, run records
# --------------------------------------------------------------------------
STAGING_NAME = ".Chamo暂存"


def published_hashes(root: Path) -> dict:
    """Product files only. After a crash the staging folder may survive (a power
    loss is documented to leave it behind), so it is not a
    published product and must not be compared against the clean-run page set."""
    hashes = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == STAGING_NAME:
            continue
        hashes[relative.as_posix()] = sha256_file(path)
    return hashes


def posix_relative(path: Path, root: Path) -> str:
    """Relative path with '/' separators on every platform.

    Output comparisons must not depend on the host separator: on Windows
    str(relative_to(...)) yields "子目录\\扫描\\扫描_001.png", so a check written as
    startswith("子目录/") or a name built with Path(a) / b never matches (a CI run:
    the folder-input gate failed in its preparation step only on
    Windows). The engine reports relative paths as POSIX (scan() uses as_posix),
    so tests compare in the same form.
    """
    return path.relative_to(root).as_posix()


def tree_hashes(root: Path):
    if not root.exists():
        return {}
    return {posix_relative(path, root): sha256_file(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def entries_under(root: Path):
    """Every entry in the output folder, files and directories alike.

    The cancel contract used to look only at files, so an empty folder left by a
    cancel during publishing passed it and then made the resume publish into
    "name (2)". Directories count as residue.
    """
    if not root.exists():
        return []
    return sorted(posix_relative(path, root) for path in root.rglob("*"))


def published_folders(out: Path):
    if not out.exists():
        return []
    return sorted(path.name for path in out.iterdir()
                  if path.is_dir() and path.name != STAGING_NAME)


def assert_resume_kept_original_folder(out: Path, source: Path):
    stem = Path(source).stem
    folders = published_folders(out)
    assert folders == [stem], (
        "resume must publish into the original folder %r, found %r. A cancel that "
        "left an empty folder makes the engine choose 'name (2)'; that is the defect "
        "this gate exists for, and ' (2)' is not accepted as a pass."
        % (stem, folders))


def cache_roots(state: Path):
    if not state.exists():
        return []
    roots = []
    direct = state / "page-cache"
    if direct.is_dir():
        roots.append(direct)
    for candidate in sorted(state.rglob("page-cache")):
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def cache_pages(state: Path):
    """Rendered page files under the page cache, never sidecars or temp files."""
    pages = []
    for root in cache_roots(state):
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                pages.append(path)
    return sorted(pages)


def cache_total_bytes(state: Path) -> int:
    total = 0
    for root in cache_roots(state):
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    return total


def stat_snapshot(files):
    snapshot = {}
    for path in files:
        info = path.stat()
        snapshot[str(path)] = (info.st_mtime_ns, info.st_size, sha256_file(path))
    return snapshot


def latest_report(state: Path) -> dict:
    paths = [path for path in state.glob("outputs/*/records/run-*/report.json")
             if path.is_file()]
    assert paths, ("no report.json under %s; the engine wrote no run record" % state)
    newest = max(paths, key=lambda path: path.stat().st_mtime_ns)
    return json.loads(newest.read_text(encoding="utf-8"))


def first_result(report: dict) -> dict:
    results = report.get("results") or []
    assert results, "the run record has no results"
    return results[0]


def counts(report: dict):
    result = first_result(report)
    return result.get("pages_reused"), result.get("pages_rendered")


def require_counts(report: dict):
    reused, rendered = counts(report)
    if not isinstance(reused, int) or not isinstance(rendered, int):
        raise AssertionError(
            "the pdf_images result has no integer pages_reused/pages_rendered "
            "(got %r); page-level resume is not reported"
            % {key: first_result(report).get(key)
               for key in ("pages_reused", "pages_rendered")})
    return reused, rendered


def default_state_root() -> Path:
    """The real per-user state root, resolved by the engine's own platform module."""
    engine = engine_dir()
    script = ("import sys; sys.path.insert(0, %r); import platform_state; "
              "print(platform_state.root())" % str(engine))
    env = {key: value for key, value in os.environ.items()
           if key not in ("CHAMO_STATE_DIR", "CHAMO_PAGE_CACHE_DIR", "CHAMO_PAGE_CACHE")}
    done = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, (
        "cannot resolve the engine default state root: %r" % done.stderr)
    return Path(done.stdout.strip())


def paths_under(root: Path):
    if not root.exists():
        return set()
    return {posix_relative(path, root) for path in root.rglob("*")}


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def make_scan_pdf(path: Path, pages: int = PAGES) -> Path:
    """A raster-only multi-page PDF: one full-page image per page.

    Built with Pillow (present in every packaged runtime) so this needs no extra
    dependency. The 'text' is drawn into the bitmap, so the document is a scan
    for the content classifier as well (needed by the sharded gate).
    """
    from PIL import Image, ImageDraw

    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    sheets = []
    for index in range(pages):
        sheet = Image.new("RGB", (PAGE_W, PAGE_H), (246, 246, 244))
        draw = ImageDraw.Draw(sheet)
        for row in range(12):
            draw.text((30, 40 + row * 60), "scan %03d line %02d" % (index + 1, row),
                      fill=(30, 30, 30))
        for column in range(0, PAGE_W, 83):
            draw.line([(column, 0), (column, PAGE_H)], fill=(205, 205, 205))
        item = directory / ("sheet-%03d.jpg" % index)
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


def expected_page_names(source: Path, pages: int = PAGES, fmt: str = FORMAT):
    stem = Path(source).stem
    extension = ".jpg" if fmt == "jpeg" else "." + fmt
    width = max(3, len(str(pages)))
    return {(Path(stem) / ("%s_%0*d%s" % (stem, width, index + 1, extension))).as_posix()
            for index in range(pages)}


def assert_page_set(out: Path, source: Path, pages: int = PAGES, fmt: str = FORMAT):
    published = tree_hashes(out)
    expected = expected_page_names(source, pages, fmt)
    missing = sorted(expected - set(published))[:5]
    extra = sorted(set(published) - expected)[:5]
    assert set(published) == expected, (
        "published file set differs from the naming contract: missing=%r extra=%r"
        % (missing, extra))


def assert_cancel_contract(out: Path, run: Run):
    assert run.rc == 3, "a cancelled run must exit 3, got " + describe(run)
    finished = run.finished()
    assert finished is not None, "the cancelled run emitted no finished event; " + describe(run)
    assert finished.get("state") == "cancelled", (
        "finished state must be cancelled, got %r; " % finished.get("state") + describe(run))
    left = entries_under(out)
    assert left == [], (
        "a cancelled run left files or empty folders in the output folder: %r. "
        "The output folder must be exactly as it was before the run." % left[:10])


@pytest.fixture(scope="session")
def scan_pdf(tmp_path_factory):
    work = tmp_path_factory.mktemp("page-resume-fixture")
    return make_scan_pdf(work / "扫描讲义.pdf")


@pytest.fixture(scope="session")
def reference(tmp_path_factory, scan_pdf):
    """A clean run with the cache disabled and sharding off: the baseline."""
    work = tmp_path_factory.mktemp("page-resume-reference")
    out = work / "out"
    state = work / "state"
    run = run_pdf_images(scan_pdf, out, state, env_extra={"CHAMO_PAGE_CACHE": "0"})
    assert run.rc == 0, "the cache-off baseline run failed: " + describe(run)
    assert_page_set(out, scan_pdf)
    return {"outputs": tree_hashes(out), "out": out, "source": scan_pdf}


# --------------------------------------------------------------------------
# 1. completeness and cache-off invariance (regression guards)
# --------------------------------------------------------------------------
def test_a_clean_run_publishes_the_whole_page_set(reference, scan_pdf):
    assert len(reference["outputs"]) == PAGES
    assert_page_set(reference["out"], scan_pdf)


def test_cache_off_writes_no_page_cache_and_matches_the_clean_run(
        tmp_path, scan_pdf, reference):
    state = tmp_path / "state"
    out = tmp_path / "out"
    run = run_pdf_images(scan_pdf, out, state, env_extra={"CHAMO_PAGE_CACHE": "0"})
    assert run.rc == 0, "cache-off run failed: " + describe(run)
    assert tree_hashes(out) == reference["outputs"], (
        "with the page cache disabled the output must be byte-identical to the baseline")
    assert cache_pages(state) == [], (
        "CHAMO_PAGE_CACHE=0 still wrote page cache files: %r"
        % [str(path) for path in cache_pages(state)[:5]])
    reused, rendered = counts(latest_report(state))
    assert reused in (0, None), "a cache-off run must not report reused pages, got %r" % reused


def test_the_page_cache_never_touches_the_default_user_state_root(tmp_path, scan_pdf):
    """CHAMO_STATE_DIR must redirect the cache too, not just run records."""
    real_root = default_state_root()
    before = {path for path in paths_under(real_root) if "page-cache" in path}
    state = tmp_path / "state"
    out = tmp_path / "out"
    run = run_pdf_images(scan_pdf, out, state, env_extra={"CHAMO_PAGE_CACHE_DIR": None})
    assert run.rc == 0, "the redirected run failed: " + describe(run)
    assert cache_pages(state), (
        "with CHAMO_PAGE_CACHE_DIR unset the cache must live under the redirected "
        "CHAMO_STATE_DIR; found %r" % cache_roots(state))
    after = {path for path in paths_under(real_root) if "page-cache" in path}
    assert after == before, (
        "the run wrote page-cache entries into the developer's real state root: %r"
        % sorted(after - before)[:10])


@posix_only
def test_cancel_contract_still_holds(tmp_path, scan_pdf):
    state = tmp_path / "state"
    out = tmp_path / "out"
    run, cancelled = cancel_pdf_images(scan_pdf, out, state)
    assert cancelled, ("the run finished before the cancel point (%d pages): the engine is "
                       "too fast to cancel here, or SIGINT was ignored" % CANCEL_AFTER_PAGES)
    assert_cancel_contract(out, run)
    assert not (out / "清单.json").exists(), "a cancelled single-file run must not write a manifest"
    assert not (out / ".Chamo暂存").exists(), "staging was left behind after cancel"


@posix_only
def test_a_cancel_in_the_publish_window_leaves_no_residue(tmp_path):
    """A late cancel must not leave an empty folder (or files) in the output folder.

    The renderer reports the last page and the engine then publishes; a cancel
    that arrives in that window used to leave the folder behind (seen in CI:
    "扫描讲义"), which made the next run publish into "扫描讲义 (2)".
    The window is a few milliseconds, so the gate repeats the race: a local probe
    reproduced the residue in 2 of 15 attempts, and a fixed engine is clean on
    every attempt. Only rc=0 attempts (the signal lost the race and the run
    completed) are allowed to publish; any rc=3 attempt must leave nothing.
    """
    source = make_scan_pdf(tmp_path / "race-fixture" / "race.pdf", 64)
    attempts = 24
    for attempt in range(1, attempts + 1):
        out = tmp_path / ("out-%02d" % attempt)
        state = tmp_path / ("state-%02d" % attempt)
        run, triggered, residue = cancel_at_last_page(
            source, out, state, env_extra={"CHAMO_PAGE_CACHE": "0"})
        assert triggered, (
            "attempt %d: the engine never reported the last rendered page, so the "
            "publish window cannot be exercised on this host" % attempt)
        if run.rc in (0, 1):
            # Exit-code contract: the signal lost the race and the run finished,
            # so rc=0/1 with a complete page set is a legitimate branch (not a
            # cancel). Never require rc=3 here.
            finished = run.finished()
            assert finished is not None and finished.get("state") in (
                "complete", "complete_with_exceptions"), (
                "attempt %d: rc=%r must come with a finished run, got %s"
                % (attempt, run.rc, describe(run)))
            assert_page_set(out, source, pages=64)
            continue
        assert run.rc == 3, (
            "attempt %d: a run cancelled in the publish window must exit 3, got %r; %s"
            % (attempt, run.rc, describe(run)))
        finished = run.finished()
        assert finished is not None and finished.get("state") == "cancelled", (
            "attempt %d: rc=3 without finished state=cancelled: %s"
            % (attempt, describe(run)))
        assert residue == [], (
            "attempt %d: cancel during the publish window left files or empty folders "
            "in the output folder: %r. rc=3 with anything in the output folder is "
            "forbidden by the exit-code contract."
            % (attempt, residue[:10]))


# --------------------------------------------------------------------------
# 2. the cache fills, and a resumed run equals a clean run byte for byte
# --------------------------------------------------------------------------
def test_a_complete_run_fills_the_page_cache(tmp_path, scan_pdf):
    state = tmp_path / "state"
    out = tmp_path / "out"
    run = run_pdf_images(scan_pdf, out, state)
    assert run.rc == 0, "cache run failed: " + describe(run)
    pages = cache_pages(state)
    assert len(pages) == PAGES, (
        "the cache should hold one rendered page per output page after a complete "
        "run; found %d page files under %r" % (len(pages), cache_roots(state)))
    for path in pages:
        assert not path.is_relative_to(out), (
            "the page cache must live in the state folder, not inside the output folder: %s" % path)


def test_a_warm_cache_reuses_every_page_and_matches_the_clean_run(
        tmp_path, scan_pdf, reference):
    """Reuse + byte equivalence without any signal, so this also runs on Windows."""
    state = tmp_path / "state"
    warm_out = tmp_path / "out-warm"
    warm = run_pdf_images(scan_pdf, warm_out, state)
    assert warm.rc == 0, "the warming run failed: " + describe(warm)
    assert_page_set(warm_out, scan_pdf)
    snapshot = stat_snapshot(cache_pages(state))
    assert len(snapshot) == PAGES, (
        "the warming run filled %d of %d cache pages" % (len(snapshot), PAGES))

    second_out = tmp_path / "out-second"
    second = run_pdf_images(scan_pdf, second_out, state)
    assert second.rc == 0, "the warm-cache run failed: " + describe(second)
    assert tree_hashes(second_out) == reference["outputs"], (
        "a warm-cache run must publish the same names and bytes as a clean run")
    assert_resume_kept_original_folder(second_out, scan_pdf)
    reused, rendered = require_counts(latest_report(state))
    assert reused == PAGES and rendered == 0, (
        "a complete warm cache must be fully reused, got reused=%r rendered=%r"
        % (reused, rendered))
    assert stat_snapshot(cache_pages(state)) == snapshot, (
        "reused cache pages must not be rewritten (mtime/size/bytes changed)")


def test_the_cache_directory_can_be_overridden(tmp_path, scan_pdf):
    state = tmp_path / "state"
    cache_dir = tmp_path / "custom-page-cache"
    run = run_pdf_images(scan_pdf, tmp_path / "out", state,
                         env_extra={"CHAMO_PAGE_CACHE_DIR": str(cache_dir)})
    assert run.rc == 0, "the cache-override run failed: " + describe(run)
    pages = [path for path in cache_dir.rglob("*")
             if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES]
    assert len(pages) == PAGES, (
        "CHAMO_PAGE_CACHE_DIR was ignored: %r holds %d page files"
        % (cache_dir, len(pages)))


@posix_only
def test_cancelled_then_resumed_outputs_match_a_clean_run(tmp_path, scan_pdf, reference):
    state = tmp_path / "state"
    out = tmp_path / "out"
    cancelled_run, cancelled = cancel_pdf_images(scan_pdf, out, state)
    assert cancelled, "the run finished before the cancel point; cannot exercise resume"
    assert_cancel_contract(out, cancelled_run)

    resumed = run_pdf_images(scan_pdf, out, state)
    assert resumed.rc == 0, "the resumed run failed: " + describe(resumed)
    assert tree_hashes(out) == reference["outputs"], (
        "outputs after cancel + resume differ from a clean run (names or bytes)")
    assert_resume_kept_original_folder(out, scan_pdf)

    reused, rendered = require_counts(latest_report(state))
    assert reused + rendered == PAGES, (
        "reused + rendered must equal the page count (%d), got %d + %d"
        % (PAGES, reused, rendered))
    assert reused > 0, "resume reported no reused pages: the page cache was not used"


# --------------------------------------------------------------------------
# 3. reuse is real: disk snapshot cross-check (catches a lying report)
# --------------------------------------------------------------------------
@posix_only
def test_reuse_is_proven_by_untouched_cache_pages(tmp_path, scan_pdf):
    state = tmp_path / "state"
    out = tmp_path / "out"
    cancelled_run, cancelled = cancel_pdf_images(scan_pdf, out, state)
    assert cancelled, "the run finished before the cancel point; cannot exercise resume"
    assert_cancel_contract(out, cancelled_run)

    before = cache_pages(state)
    assert len(before) >= 4, (
        "cancelling after %d rendered pages left only %d cache page files; "
        "the per-page write path is missing or not observable" % (CANCEL_AFTER_PAGES, len(before)))
    snapshot = stat_snapshot(before)

    resumed = run_pdf_images(scan_pdf, out, state)
    assert resumed.rc == 0, "the resumed run failed: " + describe(resumed)
    assert_resume_kept_original_folder(out, scan_pdf)

    current = stat_snapshot(cache_pages(state))
    unchanged = sorted(path for path, stat in snapshot.items() if current.get(path) == stat)
    changed = sorted(path for path, stat in snapshot.items() if current.get(path) != stat)
    added = sorted(path for path in current if path not in snapshot)

    reused, rendered = require_counts(latest_report(state))
    assert reused == len(unchanged), (
        "the report claims %d reused pages but only %d previously cached pages are "
        "untouched on disk (changed=%d, added=%d); reuse was faked or pages were rewitten"
        % (reused, len(unchanged), len(changed), len(added)))
    assert rendered == len(changed) + len(added), (
        "the report claims %d rendered pages but disk shows %d changed + %d new cache pages"
        % (rendered, len(changed), len(added)))
    assert reused + rendered == PAGES
    assert reused >= 1, "no page was actually reused"


@posix_only
def test_a_killed_run_resumes_from_the_page_cache(tmp_path, scan_pdf, reference):
    """A crash (SIGKILL, not the graceful cancel) must leave reusable pages."""
    state = tmp_path / "state"
    out = tmp_path / "out"
    killed_run, killed = kill_pdf_images(scan_pdf, out, state)
    assert killed, "the run finished before the kill point; cannot exercise the crash path"
    assert published_hashes(out) == {}, (
        "a killed run must publish no product files; found %r"
        % sorted(published_hashes(out))[:5])

    before = cache_pages(state)
    assert len(before) >= 4, (
        "a killed run left %d cache page files; the crash path cannot resume" % len(before))
    snapshot = stat_snapshot(before)

    resumed = run_pdf_images(scan_pdf, out, state)
    assert resumed.rc == 0, "the resumed run after a crash failed: " + describe(resumed)
    assert published_hashes(out) == reference["outputs"], (
        "resume after a crash changed the published page bytes")
    assert_resume_kept_original_folder(out, scan_pdf)

    current = stat_snapshot(cache_pages(state))
    unchanged = sorted(path for path, stat in snapshot.items() if current.get(path) == stat)
    reused, rendered = require_counts(latest_report(state))
    assert reused == len(unchanged), (
        "report says %d reused pages but %d pages cached before the crash are untouched"
        % (reused, len(unchanged)))
    assert rendered == PAGES - len(unchanged)
    assert reused + rendered == PAGES
    assert reused >= 1, "the crash recovery reused no page"


# --------------------------------------------------------------------------
# 4. damaged cache pages must be rendered again, not copied blindly
# --------------------------------------------------------------------------
@posix_only
@pytest.mark.parametrize("damage", ["overwrite", "truncate", "same-size"])
def test_a_damaged_cache_page_is_rendered_again(tmp_path, scan_pdf, reference, damage):
    """Corrupt one cached page and require a re-render plus correct output.

    overwrite/truncate change the byte count, so the sidecar size check alone can
    catch them. same-size flips a byte inside the page and keeps the length, so
    the SHA-256 comparison is the only defense left: a mutation that drops just
    that check must turn this case red (see artifacts/page-resume/mutations/M3).
    """
    state = tmp_path / "state"
    out = tmp_path / "out"
    cancelled_run, cancelled = cancel_pdf_images(scan_pdf, out, state)
    assert cancelled, "the run finished before the cancel point; cannot exercise resume"
    assert_cancel_contract(out, cancelled_run)

    before = cache_pages(state)
    assert len(before) >= 4, "no cache page files to damage (per-page cache missing)"
    snapshot = stat_snapshot(before)
    target = before[0]
    old_size = target.stat().st_size
    if damage == "overwrite":
        target.write_bytes(b"not a rendered page")
    elif damage == "truncate":
        original = target.read_bytes()
        target.write_bytes(original[:max(16, len(original) // 2)])
    else:
        # Same byte count, different bytes: only a content hash can notice this.
        original = bytearray(target.read_bytes())
        original[len(original) // 2] ^= 0xFF
        target.write_bytes(bytes(original))
        assert target.stat().st_size == old_size, (
            "the same-size damage case changed the file size; the sha check "
            "would not be the only defense")

    resumed = run_pdf_images(scan_pdf, out, state)
    assert resumed.rc == 0, "a damaged cache page must not fail the job: " + describe(resumed)
    assert tree_hashes(out) == reference["outputs"], (
        "a damaged cache page changed the published bytes: the engine reused or copied "
        "a corrupt page instead of rendering it again")

    current = stat_snapshot(cache_pages(state))
    unchanged = sorted(path for path, stat in snapshot.items() if current.get(path) == stat)
    assert str(target) not in unchanged, (
        "the damaged cache page was not rewritten on disk: it cannot have been rendered again")
    reused, rendered = require_counts(latest_report(state))
    assert reused == len(unchanged), (
        "report says %d reused pages, disk shows %d untouched cached pages"
        % (reused, len(unchanged)))
    assert rendered == PAGES - len(unchanged)
    assert reused + rendered == PAGES


# --------------------------------------------------------------------------
# 4b. a cancel that lands after publish must withdraw exactly this run's output
# --------------------------------------------------------------------------
# Exit-code contract (Lead decision): rc==3 if and only if the final state is
# cancelled; rc==0/1 means the run finished (complete / complete_with_exceptions)
# with complete products and is NOT a withdraw case. "rc==3 with products in the
# output folder" is forbidden. CHAMO_TEST_LATE_CANCEL=1 is a test-only, default-off
# engine hook that makes the supervisor take the withdraw path deterministically
# instead of racing a real SIGINT.
def test_a_late_cancel_withdraws_the_published_single_file(tmp_path, scan_pdf, reference):
    state = tmp_path / "state"
    out = tmp_path / "out"
    run = late_cancel_hook_run(scan_pdf, out, state)
    assert run.rc == 3, "the late-cancel hook must yield rc=3: " + describe(run)
    finished = run.finished()
    assert finished is not None and finished.get("state") == "cancelled", (
        "rc=3 requires finished state=cancelled: " + describe(run))
    assert entries_under(out) == [], (
        "a withdrawn single-file run left files or empty folders in the output "
        "folder: %r; withdraw must restore it to empty" % entries_under(out)[:10])
    result = first_result(latest_report(state))
    assert result.get("state") == "cancelled", (
        "the run record must show the withdrawn item as cancelled, got %r"
        % result.get("state"))

    # The cache was filled before the publish, so the rerun resumes and must
    # publish into the ORIGINAL folder, not "name (2)".
    resumed = run_pdf_images(scan_pdf, out, state)
    assert resumed.rc == 0, "the rerun after withdraw failed: " + describe(resumed)
    assert tree_hashes(out) == reference["outputs"], (
        "the rerun after a withdrawn publish did not match a clean run")
    assert_resume_kept_original_folder(out, scan_pdf)


def test_the_late_cancel_hook_is_off_by_default(tmp_path, scan_pdf, reference):
    """Control for the test-only hook: without it the run completes normally."""
    state = tmp_path / "state"
    out = tmp_path / "out"
    run = run_sources(scan_pdf, out, state)
    assert run.rc == 0, "without the hook the run must complete with rc=0: " + describe(run)
    finished = run.finished()
    assert finished is not None and finished.get("state") == "complete", (
        "without the hook the finished state must be complete: " + describe(run))
    assert tree_hashes(out) == reference["outputs"]
    assert_resume_kept_original_folder(out, scan_pdf)


def test_a_late_cancel_never_deletes_an_adopted_existing_product(tmp_path):
    """A pre-existing identical product that unique_target ADOPTED must survive.

    Adopt is only reachable for a single-file product: a multi-page pdf_images
    run always publishes into a page folder, and page_folder() deliberately
    picks "name (2)" when a folder already exists, so unique_target's adopt
    branch cannot be hit that way (review found an earlier test proved nothing:
    withdraw saw outputs=8 published=8 and never adopted). This gate therefore
    uses a single-file operation: image-compress lossless publishes <stem>.png
    directly in the output folder and its bytes are deterministic. The second run
    uses a different worker count (= different config fingerprint, so it renders
    again) and gets the same bytes; unique_target adopts the existing file
    instead of publishing "(2)".
    """
    from PIL import Image
    source = tmp_path / "adopt-src" / "图标.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (120, 80), (40, 90, 100, 100)).save(str(source))
    state = tmp_path / "state"
    out = tmp_path / "out"
    first = run_operation(source, out, state, "image-compress",
                          env_extra={"CHAMO_PAGE_CACHE": "0"})
    assert first.rc == 0, "the first run failed: " + describe(first)
    assert sorted(tree_hashes(out)) == ["图标.png"], (
        "image-compress must publish a single file in the output folder, got %r; "
        "a page folder would make adopt unreachable" % sorted(tree_hashes(out)))
    before = tree_hashes(out)
    before_stats = {name: (out / name).stat().st_mtime_ns for name in before}

    second = run_operation(source, out, state, "image-compress", workers=2,
                           env_extra={"CHAMO_PAGE_CACHE": "0",
                                      "CHAMO_TEST_LATE_CANCEL": "1"})
    assert second.rc == 3, "the late-cancel hook must yield rc=3: " + describe(second)
    finished = second.finished()
    assert finished is not None and finished.get("state") == "cancelled"
    assert tree_hashes(out) == before, (
        "a late cancel deleted or changed an adopted pre-existing product; the "
        "withdraw path may remove ONLY files this run published (adopt means the "
        "file was already there)")
    assert entries_under(out) == sorted(before), (
        "a late cancel left extra entries beside the adopted product (e.g. a "
        "'(2)' copy or an empty folder): %r"
        % sorted(set(entries_under(out)) - set(before)))
    for name, mtime in before_stats.items():
        assert (out / name).stat().st_mtime_ns == mtime, (
            "an adopted pre-existing product was rewritten: %s" % name)

    # Prove the adopt branch was really taken (otherwise this test could pass
    # while never exercising unique_target's adopt path): seed a fresh output
    # folder with the identical product, run the same request WITHOUT the hook,
    # and check the run record says it published nothing - the file was adopted.
    control_out = tmp_path / "out-control"
    control_out.mkdir(exist_ok=True)
    shutil.copy2(str(out / "图标.png"), str(control_out / "图标.png"))
    adopted_mtime = (control_out / "图标.png").stat().st_mtime_ns
    control_state = tmp_path / "state-control"
    control = run_operation(source, control_out, control_state, "image-compress",
                            workers=2, env_extra={"CHAMO_PAGE_CACHE": "0"})
    assert control.rc == 0, "the adopt-control run failed: " + describe(control)
    record = first_result(latest_report(control_state))
    assert not (record.get("published_paths") or []), (
        "the control run published the product instead of adopting it; the adopt "
        "branch is unreachable, so this gate proves nothing: published_paths=%r"
        % (record.get("published_paths") or []))
    assert [row.get("path") for row in (record.get("outputs") or [])] == [
        str(control_out / "图标.png")], (
        "the adopt-control output list does not point at the pre-existing file: %r"
        % (record.get("outputs") or []))
    assert (control_out / "图标.png").stat().st_mtime_ns == adopted_mtime, (
        "adopt rewrote the pre-existing identical product")


def test_a_late_cancel_keeps_a_user_precreated_empty_folder(tmp_path):
    """Withdraw removes only folders THIS run created; a user's empty folder stays.

    A folder input mirrors its relative directory into the output folder. The
    user pre-creates out/子目录 (empty) and out/keep; the run publishes into
    out/子目录/扫描/ and records only 扫描 as created. Withdrawing must remove
    the published pages and the 扫描 folder it created, and keep the user's empty
    子目录 and keep folders - a mutation that walks up the parent chain deleting
    every empty folder must turn this red.
    """
    # scan() mirrors relative to the PARENT of a folder input, so pass the
    # subfolder itself: relative becomes 子目录/扫描.pdf -> out/子目录/扫描/.
    input_root = tmp_path / "in" / "子目录"
    source = make_scan_pdf(input_root / "扫描.pdf", 4)
    state = tmp_path / "state"
    out = tmp_path / "out"
    (out / "子目录").mkdir(parents=True)
    (out / "keep").mkdir()
    run = run_operation(input_root, out, state, "pdf-images",
                        env_extra={"CHAMO_PAGE_CACHE": "0"})
    assert run.rc == 0, "the preparation run failed: " + describe(run)
    # tree_hashes() keys are POSIX paths on every platform (posix_relative), so
    # this comparison is host independent; the folder itself is matched by parts.
    published = [name for name in tree_hashes(out)
                 if name.split("/")[0] == "子目录"]
    assert published, "the folder input did not publish under out/子目录"
    out_files = [path for path in (out / "子目录").rglob("*")
                 if path.is_file() and STAGING_NAME not in path.relative_to(out).parts]
    assert out_files, "the preparation run published no product under the user folder"

    # Now the same request with the late-cancel hook: the published pages and the
    # run-created page folder must be withdrawn, the user folders must survive.
    # A fresh output copy keeps the pre-created user folders.
    out2 = tmp_path / "out-cancel"
    (out2 / "子目录").mkdir(parents=True)
    (out2 / "keep").mkdir()
    cancelled = run_operation(input_root, out2, state, "pdf-images",
                              env_extra={"CHAMO_PAGE_CACHE": "0",
                                         "CHAMO_TEST_LATE_CANCEL": "1"})
    assert cancelled.rc == 3, (
        "the late-cancel hook must yield rc=3: " + describe(cancelled))
    finished = cancelled.finished()
    assert finished is not None and finished.get("state") == "cancelled"
    assert (out2 / "子目录").is_dir(), (
        "a late cancel deleted the user's pre-created empty folder out/子目录; "
        "withdraw may remove only folders this run created")
    assert not list((out2 / "子目录").iterdir()), (
        "the run-created page folder was not removed from the user's folder: %r"
        % sorted(posix_relative(path, out2 / "子目录")
                 for path in (out2 / "子目录").iterdir()))
    assert (out2 / "keep").is_dir() and not list((out2 / "keep").iterdir()), (
        "a late cancel removed the user's unrelated empty folder out/keep")
    assert entries_under(out2) == ["keep", "子目录"], (
        "the cancelled run left residue beside the user's empty folders: %r"
        % sorted(set(entries_under(out2)) - {"keep", "子目录"}))
    assert not (out2 / STAGING_NAME).exists(), "staging was left behind after cancel"


def test_a_late_cancel_never_deletes_a_product_the_user_changed(tmp_path):
    """A product edited after publication must be kept, not withdrawn.

    The hook fires at finalize, after the pages were published. A watcher thread
    edits the first published product the moment it appears (publish and the
    finalize/withdraw are separated by the rest of the publish plus teardown, so
    the edit lands before withdraw). Withdraw re-hashes each published file and
    must skip a file whose bytes no longer match what this run wrote - deleting it
    would destroy the user's edit.
    """
    source = make_scan_pdf(tmp_path / "mod-fixture" / "改.pdf", 8)
    edited = b"USER-EDIT" + bytes([1, 2, 3])
    for attempt in range(1, 4):
        state = tmp_path / ("state-%02d" % attempt)
        out = tmp_path / ("out-%02d" % attempt)
        modified = {"path": None, "bytes": None}

        def watch():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                files = []
                if out.exists():
                    for path in out.rglob("*"):
                        if not path.is_file():
                            continue
                        if STAGING_NAME in path.relative_to(out).parts:
                            continue
                        files.append(path)
                if files:
                    target = sorted(files)[0]
                    try:
                        data = target.read_bytes()
                        target.write_bytes(data + edited)
                        modified["path"] = target
                        modified["bytes"] = data + edited
                    except OSError:
                        pass
                    return
                time.sleep(0.001)

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        run = run_operation(source, out, state, "pdf-images",
                            env_extra={"CHAMO_PAGE_CACHE": "0",
                                       "CHAMO_TEST_LATE_CANCEL": "1"})
        thread.join(timeout=10)
        if modified["path"] is None:
            continue  # the run finished before the watcher saw a product; retry
        assert run.rc == 3, "the late-cancel hook must yield rc=3: " + describe(run)
        finished = run.finished()
        assert finished is not None and finished.get("state") == "cancelled"
        target = modified["path"]
        assert target.is_file(), (
            "withdraw deleted a product the user had changed after publication")
        assert target.read_bytes() == modified["bytes"], (
            "withdraw changed a product the user had edited after publication")
        survivors = [path for path in out.rglob("*")
                     if path.is_file() and STAGING_NAME not in path.relative_to(out).parts]
        assert survivors == [target], (
            "withdraw left products beside the user-edited one: %r"
            % sorted(posix_relative(path, out) for path in survivors))
        for directory in out.rglob("*"):
            if directory.is_dir() and directory.relative_to(out).parts[0] != STAGING_NAME:
                assert any(directory.iterdir()), (
                    "withdraw left an empty folder after keeping the edited product: %s"
                    % posix_relative(directory, out))
        return
    raise AssertionError("the watcher never caught a published product in 3 attempts; "
                         "the publish window is too short to exercise the edit")


def test_a_late_cancel_in_a_batch_keeps_the_completed_files(tmp_path):
    """Batch semantics: finished items stay, and no empty folder/staging is left."""
    first_source = make_scan_pdf(tmp_path / "batch-a" / "甲.pdf", 8)
    second_source = make_scan_pdf(tmp_path / "batch-b" / "乙.pdf", 8)
    state = tmp_path / "state"
    out = tmp_path / "out"
    run = late_cancel_hook_run([first_source, second_source], out, state, workers=2,
                               env_extra={"CHAMO_PAGE_CACHE": "0"})
    assert run.rc == 3, "the late-cancel hook must yield rc=3 for a batch: " + describe(run)
    finished = run.finished()
    assert finished is not None and finished.get("state") == "cancelled"
    # The hook fires at finalize, so both small items have completed by then and
    # batch semantics keep their files. Compare per source folder: the page set
    # must be complete and no (2) folder may appear.
    for source in (first_source, second_source):
        stem = Path(source).stem
        folder = out / stem
        assert folder.is_dir(), "a completed batch item lost its folder: %s" % stem
        expected = expected_page_names(source, pages=8)
        # expected_page_names() already returns POSIX paths ("甲/甲_001.jpg"), and
        # tree/entries helpers do too, so compare in the same form on Windows.
        inner = {Path(name).relative_to(stem).as_posix() for name in expected}
        got = {posix_relative(path, out) for path in folder.rglob("*") if path.is_file()}
        wanted = {(Path(stem) / name).as_posix() for name in inner}
        assert got == wanted, (
            "a completed batch item's page set is incomplete or renamed: "
            "missing=%r extra=%r"
            % (sorted(wanted - got)[:5], sorted(got - wanted)[:5]))
    assert not (out / STAGING_NAME).exists(), (
        "a cancelled batch left the staging folder behind: %r"
        % [posix_relative(path, out) for path in (out / STAGING_NAME).iterdir()][:5]
        if (out / STAGING_NAME).exists() else "")
    for directory in out.rglob("*"):
        if directory.is_dir():
            assert any(directory.iterdir()), (
                "a cancelled batch left an empty folder in the output folder: %s"
                % directory.relative_to(out))


# --------------------------------------------------------------------------
# 5. cache key: changed source bytes or render config never reuse old pixels
# --------------------------------------------------------------------------
def _page_images_under(directory: Path):
    return {path.name: sha256_file(path) for path in sorted(directory.rglob("*"))
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES}


def _folders(out: Path):
    return sorted(path.name for path in out.iterdir()
                  if path.is_dir() and path.name != ".Chamo暂存")


@pytest.mark.parametrize("variant,second", [
    ("dpi", {"dpi": 100}),
    ("format", {"fmt": "png"}),
    ("quality", {"quality": 40}),
])
def test_a_changed_render_config_never_reuses_old_pixels(tmp_path, scan_pdf, variant, second):
    state = tmp_path / "state"
    out = tmp_path / "out"
    first = run_pdf_images(scan_pdf, out, state)
    assert first.rc == 0, "first run failed: " + describe(first)
    assert_page_set(out, scan_pdf)
    before = tree_hashes(out)
    before_stat = {str(path): (path.stat().st_mtime_ns, path.stat().st_size)
                   for path in out.rglob("*") if path.is_file()}

    second_run = run_pdf_images(scan_pdf, out, state, **second)
    assert second_run.rc == 0, "second run failed: " + describe(second_run)

    reused, rendered = require_counts(latest_report(state))
    assert reused == 0, ("changed %s but %d pages were reused from the old cache key"
                         % (variant, reused))
    assert rendered == PAGES

    for relative, digest in before.items():
        path = out / relative
        assert path.is_file(), "an existing product was removed by the second run: %s" % relative
        assert sha256_file(path) == digest, "an existing product was overwritten: %s" % relative
        assert (path.stat().st_mtime_ns, path.stat().st_size) == before_stat[str(path)], (
            "an existing product was rewritten even though the bytes did not change: %s" % relative)

    folders = _folders(out)
    stem = scan_pdf.stem
    assert stem in folders, "the first run folder disappeared: %r" % folders
    second_folder = [name for name in folders if name != stem]
    assert len(second_folder) == 1, (
        "a different render config must publish into its own folder, found %r" % folders)
    second_images = _page_images_under(out / second_folder[0])
    assert len(second_images) == PAGES
    if variant == "format":
        assert all(name.endswith(".png") for name in second_images)
    else:
        first_images = _page_images_under(out / stem)
        differing = [name for name in second_images
                     if first_images.get(name) != second_images[name]]
        assert len(differing) >= PAGES - 2, (
            "changed %s but the new pages are identical to the old ones: cache reuse "
            "through a different key" % variant)


def test_a_changed_source_byte_never_reuses_old_pages(tmp_path, scan_pdf):
    source = tmp_path / "源讲义.pdf"
    shutil.copy2(str(scan_pdf), str(source))
    state = tmp_path / "state"
    out = tmp_path / "out"
    first = run_pdf_images(source, out, state)
    assert first.rc == 0, "first run failed: " + describe(first)
    before = tree_hashes(out)
    before_stat = {str(path): (path.stat().st_mtime_ns, path.stat().st_size)
                   for path in out.rglob("*") if path.is_file()}
    first_images = _page_images_under(out / source.stem)

    with open(str(source), "ab") as handle:
        handle.write(b"% source changed by one byte")  # trailing junk keeps the PDF valid

    second = run_pdf_images(source, out, state)
    assert second.rc == 0, "second run failed: " + describe(second)
    reused, rendered = require_counts(latest_report(state))
    assert reused == 0, ("the source SHA changed but %d pages were reused from the old cache"
                         % reused)
    assert rendered == PAGES

    for relative, digest in before.items():
        path = out / relative
        assert path.is_file(), "an existing product was removed: %s" % relative
        assert sha256_file(path) == digest
        assert (path.stat().st_mtime_ns, path.stat().st_size) == before_stat[str(path)], (
            "an existing product was rewritten despite the no-overwrite rule: %s" % relative)

    folders = _folders(out)
    second_folder = [name for name in folders if name != source.stem]
    assert len(second_folder) == 1, "expected one new output folder, found %r" % folders
    second_images = _page_images_under(out / second_folder[0])
    assert second_images == first_images, (
        "the re-rendered pages are not identical to the first run's pages; the cache "
        "key or the renderer changed the output")


# --------------------------------------------------------------------------
# 6. cache sweeps (age and size) and keep-alive of the running job
# --------------------------------------------------------------------------
def test_expired_cache_entries_are_swept(tmp_path, scan_pdf):
    state = tmp_path / "state"
    first = run_pdf_images(scan_pdf, tmp_path / "out1", state)
    assert first.rc == 0, "first run failed: " + describe(first)
    assert cache_pages(state), "the first run wrote no page cache; nothing to expire"
    time.sleep(1.2)
    second = run_pdf_images(scan_pdf, tmp_path / "out2", state,
                            env_extra={"CHAMO_PAGE_CACHE_MAX_AGE": "1"})
    assert second.rc == 0, "the sweep run failed: " + describe(second)
    reused, rendered = require_counts(latest_report(state))
    assert reused == 0, (
        "the cache was older than CHAMO_PAGE_CACHE_MAX_AGE=1s but %d pages were reused"
        % reused)
    assert rendered == PAGES
    assert len(cache_pages(state)) == PAGES, "the second run did not write its own cache"


def test_oversized_cache_entries_are_evicted(tmp_path, scan_pdf):
    state = tmp_path / "state"
    first = run_pdf_images(scan_pdf, tmp_path / "out1", state, dpi=150)
    assert first.rc == 0, "first run failed: " + describe(first)
    older = set(str(path) for path in cache_pages(state))
    assert older, "the first run wrote no page cache; nothing to evict"
    second = run_pdf_images(scan_pdf, tmp_path / "out2", state, dpi=100)
    assert second.rc == 0, "second run failed: " + describe(second)
    assert cache_total_bytes(state) > 64 * 1024, "the cache is implausibly small for the sweep"

    third = run_pdf_images(scan_pdf, tmp_path / "out3", state, dpi=125,
                           env_extra={"CHAMO_PAGE_CACHE_MAX_BYTES": "1024"})
    assert third.rc == 0, "the cache-cap run failed: " + describe(third)
    remaining = set(str(path) for path in cache_pages(state))
    assert not (older & remaining), (
        "the oldest cache directory survived CHAMO_PAGE_CACHE_MAX_BYTES=1024: %d old page "
        "files are still there" % len(older & remaining))


def test_a_sweep_never_deletes_a_cache_directory_marked_active(tmp_path, scan_pdf):
    """A running job's cache directory must survive the start-of-job sweep.

    A stop signal cannot be delivered exactly when a directory is "idle long
    enough to look stale", so the gate creates two expired cache directories
    under the isolated cache root: one carries the .active marker the engine uses
    for a directory a live job is writing, the other does not. The sweep may
    delete the unprotected one but must leave the protected one alone. Both
    directions are asserted, so a sweep that deletes nothing cannot pass.
    """
    state = tmp_path / "state"
    cache_root = state / "page-cache"
    old = time.time() - 10 * 24 * 3600

    def expired_directory(name: str, active: bool):
        directory = cache_root / "pdf_images" / (name * 64) / ("c" * 64)
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

    run = run_pdf_images(scan_pdf, tmp_path / "out", state,
                         env_extra={"CHAMO_PAGE_CACHE_MAX_AGE": "1"})
    assert run.rc == 0, "the sweeping run failed: " + describe(run)
    assert protected.exists(), (
        "the sweep deleted a cache directory marked .active; it belongs to a running job")
    assert not unprotected.exists(), (
        "the sweep did not delete an expired cache directory without .active; "
        "the age cap would never reclaim anything")


def test_a_running_job_keeps_the_cache_it_writes(tmp_path, scan_pdf):
    state = tmp_path / "state"
    # 300 dpi makes the run long enough (relative to the 1s age cap) that a sweep
    # during the run would delete pages this same job just wrote.
    run = run_pdf_images(scan_pdf, tmp_path / "out", state, dpi=300,
                         env_extra={"CHAMO_PAGE_CACHE_MAX_AGE": "1"})
    assert run.rc == 0, "the aggressive-age run failed: " + describe(run)
    pages = cache_pages(state)
    assert len(pages) == PAGES, (
        "a page sweep during the run deleted %d of its own cache pages; cleanup must not "
        "run against the job that is writing" % (PAGES - len(pages)))


# --------------------------------------------------------------------------
# 7. isolation between state roots / configurations
# --------------------------------------------------------------------------
def test_state_roots_keep_separate_caches(tmp_path, scan_pdf):
    first_state = tmp_path / "state-a"
    second_state = tmp_path / "state-b"
    first = run_pdf_images(scan_pdf, tmp_path / "out-a1", first_state)
    assert first.rc == 0, "first run failed: " + describe(first)
    second = run_pdf_images(scan_pdf, tmp_path / "out-b1", second_state)
    assert second.rc == 0, "second run failed: " + describe(second)
    reused_b, rendered_b = require_counts(latest_report(second_state))
    assert reused_b == 0, ("a fresh state root reused %d pages from another state root"
                           % reused_b)
    assert rendered_b == PAGES
    third = run_pdf_images(scan_pdf, tmp_path / "out-a2", first_state)
    assert third.rc == 0, "third run failed: " + describe(third)
    reused_a, rendered_a = require_counts(latest_report(first_state))
    assert reused_a == PAGES and rendered_a == 0, (
        "a complete cache in the same state root should be fully reused, got "
        "reused=%d rendered=%d" % (reused_a, rendered_a))
    assert not (set(str(path) for path in cache_pages(first_state))
                & set(str(path) for path in cache_pages(second_state)))


# --------------------------------------------------------------------------
# 8. an unavailable cache must never fail the job
# --------------------------------------------------------------------------
def test_a_cache_path_that_is_not_a_directory_never_fails_the_job(
        tmp_path, scan_pdf, reference):
    state = tmp_path / "state"
    bogus = tmp_path / "cache-is-a-file"
    bogus.write_bytes(b"not a directory")
    run = run_pdf_images(scan_pdf, tmp_path / "out", state,
                         env_extra={"CHAMO_PAGE_CACHE_DIR": str(bogus)})
    assert run.rc == 0, "an unavailable page cache must not fail the job: " + describe(run)
    assert tree_hashes(tmp_path / "out") == reference["outputs"], (
        "the outputs changed when the page cache was unavailable")
    reused, rendered = require_counts(latest_report(state))
    assert reused == 0
    assert rendered == PAGES


@not_root_posix
def test_a_read_only_cache_root_never_fails_the_job(tmp_path, scan_pdf, reference):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cache_root = state / "page-cache"
    cache_root.mkdir()
    os.chmod(str(cache_root), 0o500)
    try:
        run = run_pdf_images(scan_pdf, tmp_path / "out", state)
        assert run.rc == 0, "a read-only page cache must not fail the job: " + describe(run)
        assert tree_hashes(tmp_path / "out") == reference["outputs"]
        reused, rendered = require_counts(latest_report(state))
        assert reused == 0
        assert rendered == PAGES
    finally:
        os.chmod(str(cache_root), 0o700)


# --------------------------------------------------------------------------
# 9. the sharded rendering path must share the same page cache
# --------------------------------------------------------------------------
@posix_only
def test_a_cancelled_sharded_run_resumes_with_the_same_cache(tmp_path, scan_pdf, reference):
    if (os.cpu_count() or 1) < 2:
        pytest.skip("the sharded path needs at least two cores; this host has one")
    state = tmp_path / "state"
    out = tmp_path / "out"
    cancelled_run, cancelled = cancel_pdf_images(scan_pdf, out, state,
                                                 after_pages=4, workers=4, shard="auto")
    assert cancelled, "the sharded run finished before the cancel point"
    assert_cancel_contract(out, cancelled_run)

    resumed = run_pdf_images(scan_pdf, out, state, workers=4, shard="auto")
    assert resumed.rc == 0, "the resumed sharded run failed: " + describe(resumed)
    report = latest_report(state)
    shard = first_result(report).get("shard") or {}
    assert shard.get("used") is True, (
        "the sharded arm did not take the sharded path, so it proves nothing: %r" % shard)
    assert tree_hashes(out) == reference["outputs"], (
        "cancel + resume on the sharded path changed the output bytes")
    assert_resume_kept_original_folder(out, scan_pdf)
    reused, rendered = require_counts(report)
    assert reused > 0, "the sharded resume reused no pages"
    assert reused + rendered == PAGES


# --------------------------------------------------------------------------
# 10. staging registry: registered cleanup, concurrency, cleanup window, user data
# --------------------------------------------------------------------------
# Staging cleanup is a registry, not "remove my staging folder inside one()": each
# job registers its staging directory in its own run-record folder
# (staging-<fingerprint>.json), and the supervisor's _cleanup_run_staging removes
# exactly the paths named by THIS run's registers after the run (engine/
# workbench.py: _run_staging / _cleanup_run_staging / platform_state.staging).
# Review showed the mechanism had no automated coverage: deleting the whole
# staging root, dropping the fingerprint prefix check, removing the cleanup call,
# or removing the cleanup-window signal mask all stayed green. The gates below
# pin each promise; the mutations live in artifacts/page-resume/mutations/.
STAGING_REGISTER_GLOB = "staging-*.json"


def staging_root_of(out: Path) -> Path:
    return out / STAGING_NAME


def staging_directories(out: Path):
    root = staging_root_of(out)
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def run_record_dirs(state: Path):
    return sorted(state.glob("outputs/*/records/run-*"))


def registers_in(record: Path):
    return sorted(record.glob(STAGING_REGISTER_GLOB))


def latest_record(state: Path) -> Path:
    records = run_record_dirs(state)
    assert records, "the run wrote no record folder under %s" % state
    return max(records, key=lambda path: path.stat().st_mtime_ns)


def engine_importable():
    """True when the engine package next to the CLI can be imported in-process."""
    return (engine_dir() / "workbench.py").is_file()


def run_engine_snippet(code: str, *arguments, timeout: int = 180):
    """Run a snippet in a fresh interpreter with the engine on sys.path.

    The staging registry helpers (_run_staging, _cleanup_run_staging) are
    internal, so their promises are pinned by driving them directly in a
    subprocess: that keeps the gate deterministic instead of relying on a rare
    timing window, and it still exercises the real engine code.
    """
    script = ("import sys\n"
              "sys.path.insert(0, %r)\n"
              "import json, os, signal, time\n"
              "from pathlib import Path\n"
              "import workbench\n" % str(engine_dir()))
    script += code
    done = subprocess.run([sys.executable, "-I", "-B", "-c", script, *arguments],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    assert done.returncode == 0, (
        "the in-process engine probe failed (%r): %s" % (done.returncode, done.stderr[-1500:]))
    return done.stdout


def test_a_completed_run_leaves_no_staging_and_no_registers(tmp_path, scan_pdf):
    """A finished run owns nothing in the output folder or its record folder."""
    state = tmp_path / "state"
    out = tmp_path / "out"
    run = run_pdf_images(scan_pdf, out, state)
    assert run.rc == 0, "the run failed: " + describe(run)
    assert not staging_root_of(out).exists(), (
        "a completed run left the staging root in the output folder: %r"
        % staging_directories(out))
    record = latest_record(state)
    assert registers_in(record) == [], (
        "a completed run left staging register files behind: %r"
        % [path.name for path in registers_in(record)])


@posix_only
def test_a_cancelled_run_leaves_no_staging_and_no_registers(tmp_path, scan_pdf):
    """Cancelling must not leave a staging directory or its register either."""
    state = tmp_path / "state"
    out = tmp_path / "out"
    run, cancelled = cancel_pdf_images(scan_pdf, out, state)
    assert cancelled, "the run finished before the cancel point; cannot exercise cancel"
    assert_cancel_contract(out, run)
    assert not staging_root_of(out).exists(), (
        "a cancelled run left the staging root behind: %r" % staging_directories(out))
    for record in run_record_dirs(state):
        assert registers_in(record) == [], (
            "a cancelled run left staging registers behind in %s: %r"
            % (record.name, [path.name for path in registers_in(record)]))


def test_cleanup_touches_only_this_runs_registered_staging(tmp_path):
    """_cleanup_run_staging removes registered paths only; never the whole root.

    Seeds one staging root with (a) the directory this run registered, (b) a
    concurrent run's directory that no register names, (c) a user file the user
    put there, and (d) a register naming a path OUTSIDE the staging root plus a
    register whose name does not carry its own fingerprint. Only (a) may go:
    (b) and (c) belong to somebody else, and (d) must be refused - that is the
    fingerprint-prefix + under-the-root check (mutation S2 removes it and this
    gate turns red).
    """
    out = tmp_path / "out"
    staging = staging_root_of(out)
    staging.mkdir(parents=True)
    mine = staging / "fpA-aaaa1111"
    mine.mkdir()
    (mine / "page00001.png").write_bytes(b"this run")
    theirs = staging / "fpB-bbbb2222"
    theirs.mkdir()
    (theirs / "page00001.png").write_bytes(b"another run")
    user_file = staging / "我的东西.txt"
    user_file.write_bytes(b"keep me")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"not staging")
    record = tmp_path / "record"
    record.mkdir()
    (record / "staging-fpA.json").write_text(
        json.dumps({"path": str(mine), "fingerprint": "fpA"}), encoding="utf-8")
    # A forged register that names another run's directory: the fingerprint
    # prefix check must reject it.
    (record / "staging-fpA-forged.json").write_text(
        json.dumps({"path": str(theirs), "fingerprint": "fpA"}), encoding="utf-8")
    # A register escaping the staging root: the under-the-root check must reject it.
    (record / "staging-fpA-escape.json").write_text(
        json.dumps({"path": str(outside), "fingerprint": "fpA"}), encoding="utf-8")

    run_engine_snippet(
        "workbench._cleanup_run_staging(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "print(json.dumps({'mine': Path(sys.argv[3]).exists(),"
        " 'theirs': Path(sys.argv[4]).exists(),"
        " 'user': Path(sys.argv[5]).exists(),"
        " 'outside': Path(sys.argv[6]).exists(),"
        " 'registers': sorted(p.name for p in Path(sys.argv[2]).glob('staging-*.json'))}))",
        str(out), str(record), str(mine), str(theirs), str(user_file), str(outside))

    assert not mine.exists(), (
        "the registered staging directory of this run was not removed")
    assert theirs.exists(), (
        "cleanup deleted a concurrent run's staging directory; it must only remove "
        "paths named by this run's own registers")
    assert user_file.exists(), (
        "cleanup deleted a user file that happened to sit in the staging root")
    assert outside.exists(), (
        "cleanup deleted a path outside the staging root; the register must be "
        "rejected by the under-the-root check")
    assert registers_in(record) == [], (
        "cleanup left register files behind: %r"
        % [path.name for path in registers_in(record)])


def test_two_concurrent_runs_never_delete_each_others_staging(tmp_path):
    """Two runs to the same output folder register independently and coexist."""
    first = make_scan_pdf(tmp_path / "a" / "甲.pdf", 48)
    second = make_scan_pdf(tmp_path / "b" / "乙.pdf", 48)
    out = tmp_path / "out"
    out.mkdir()
    state = tmp_path / "state"
    staging = staging_root_of(out)
    env = _state_env(state, {"CHAMO_PAGE_CACHE": "0"})

    def command(source):
        return [sys.executable, "-I", "-B", str(engine_dir() / "chamo.py"), str(source),
                "-o", str(out), "-t", "pdf-images", "--dpi", str(DPI),
                "--format", FORMAT, "--quality", str(QUALITY), "--workers", "1",
                "--shard", "off", "--json"]

    processes = [subprocess.Popen(command(source), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=env)
                 for source in (first, second)]
    peak = 0
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and any(p.poll() is None for p in processes):
        peak = max(peak, len(staging_directories(out)))
        time.sleep(0.01)
    codes = [process.wait(timeout=120) for process in processes]
    assert codes == [0, 0], "a concurrent run failed: exit codes %r" % codes
    assert peak >= 2, (
        "the two runs never had their staging directories at the same time (peak=%d), "
        "so this gate could not observe the isolation it claims" % peak)
    assert not staging.exists(), (
        "the concurrent runs left the staging root behind: %r" % staging_directories(out))
    for record in run_record_dirs(state):
        assert registers_in(record) == [], (
            "a run left staging registers behind: %r"
            % [path.name for path in registers_in(record)])
    assert (out / "甲").is_dir() and (out / "乙").is_dir(), (
        "the concurrent runs did not both publish: %r" % sorted(p.name for p in out.iterdir()))


@posix_only
def test_a_killed_worker_leaves_no_staging_for_the_next_run(tmp_path):
    """A worker SIGKILLed mid-render must be cleaned up by its supervisor.

    This is the deterministic stand-in for "the cleanup itself is interrupted":
    the worker dies inside the staging window, so nothing in the worker can run
    any Python cleanup. The supervisor's finally-block must remove exactly the
    registered directory and drop the staging root, otherwise the next run would
    publish next to a leftover dot-folder.
    """
    source = make_scan_pdf(tmp_path / "kill-fixture" / "扫描.pdf", 64)
    out = tmp_path / "out"
    state = tmp_path / "state"
    env = _state_env(state, {"CHAMO_PAGE_CACHE": "0"})
    command = [sys.executable, "-I", "-B", str(engine_dir() / "chamo.py"), str(source),
               "-o", str(out), "-t", "pdf-images", "--dpi", str(DPI),
               "--format", FORMAT, "--quality", str(QUALITY), "--workers", "1",
               "--shard", "off", "--json"]
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)
    staging = staging_root_of(out)
    killed = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pids = process_tree(proc.pid)
        if len(pids) >= 3 and staging_directories(out):
            worker = pids[-1]
            try:
                os.kill(worker, signal.SIGKILL)
                killed = worker
            except OSError:
                pass
            break
        time.sleep(0.01)
    assert killed is not None, (
        "the worker was never observed inside its staging window; this gate cannot "
        "exercise the supervisor cleanup on this host")
    rc = proc.wait(timeout=180)
    assert rc in (0, 1), (
        "the supervisor must survive a killed worker (rc 0/1), got %r" % rc)
    assert not staging.exists(), (
        "a SIGKILLed worker left the staging root behind (supervisor cleanup did not "
        "run): %r" % staging_directories(out))
    for record in run_record_dirs(state):
        assert registers_in(record) == [], (
            "a SIGKILLed worker left its staging register behind: %r"
            % [path.name for path in registers_in(record)])
    assert published_hashes(out) == {}, (
        "a killed worker published products: %r" % sorted(published_hashes(out))[:5])


@posix_only
def test_a_signal_inside_the_staging_cleanup_window_leaves_nothing(tmp_path):
    """A cancel delivered while the staging folder is being removed leaves nothing.

    _run_staging masks SIGTERM/SIGINT across its cleanup (engine/workbench.py
    :1283-1290) precisely because a handler raising inside rmtree used to leave
    the folder behind. This gate raises the signal from inside the
    removal itself, so it is deterministic instead of a millisecond race: if the
    mask is removed (mutation S4) the exception interrupts the removal and the
    staging directory survives, and the assertion below turns red.
    """
    out = tmp_path / "out"
    staging = staging_root_of(out)
    staging.mkdir(parents=True)
    record = tmp_path / "record"
    record.mkdir()
    output = run_engine_snippet(
        "real_rmtree = workbench.shutil.rmtree\n"
        "seen = {'raised': False}\n"
        "def fake_rmtree(path, *args, **kwargs):\n"
        "    path = Path(path)\n"
        "    if path.parent == Path(sys.argv[1]) and path.name.startswith('fpA-') "
        "and not seen['raised']:\n"
        "        seen['raised'] = True\n"
        "        for child in sorted(p for p in path.iterdir() if p.is_file())[:3]:\n"
        "            child.unlink()\n"
        "        signal.raise_signal(signal.SIGTERM)\n"
        "    return real_rmtree(path, *args, **kwargs)\n"
        "workbench.shutil.rmtree = fake_rmtree\n"
        "signal.signal(signal.SIGTERM, lambda s, f: (_ for _ in ()).throw("
        "KeyboardInterrupt('sigterm')))\n"
        "outcome = 'clean'\n"
        "try:\n"
        "    with workbench._run_staging(Path(sys.argv[1]), 'fpA', sys.argv[2]) as directory:\n"
        "        for index in range(20):\n"
        "            (Path(directory) / ('f%03d' % index)).write_bytes(b'x' * 32)\n"
        "except KeyboardInterrupt:\n"
        "    outcome = 'interrupted'\n"
        "print(json.dumps({'raised_in_cleanup': seen['raised'], 'outcome': outcome,"
        " 'left': sorted(p.name for p in Path(sys.argv[1]).iterdir())"
        " if Path(sys.argv[1]).exists() else [],"
        " 'registers': sorted(p.name for p in Path(sys.argv[2]).glob('staging-*.json'))}))",
        str(staging), str(record))
    data = json.loads(output.strip().splitlines()[-1])
    assert data["raised_in_cleanup"], (
        "the probe never raised the signal inside the staging cleanup; the window "
        "was not exercised")
    # The pending signal surfacing right after the cleanup is fine and expected
    # (the mask is restored at the end of the window, so the cancel still reaches
    # the caller). What must never happen is residue: without the mask the signal
    # raises inside rmtree, the removal is interrupted, and the staging directory
    # survives - that is what mutation S4 does, and this assertion catches it.
    assert data["outcome"] in ("clean", "interrupted"), (
        "unexpected probe outcome %r" % data["outcome"])
    assert data["left"] == [], (
        "a cancel during the staging cleanup left directories behind: %r; the "
        "cleanup window must be protected so the removal completes" % data["left"])
    assert data["registers"] == [], (
        "a cancel during the staging cleanup left registers behind: %r" % data["registers"])


def test_a_user_created_staging_folder_is_not_deleted(tmp_path, scan_pdf):
    """The dot-staging folder can pre-exist (a user's own folder); it is not ours.

    platform_state.drop_staging only removes the root when it is empty, and
    _cleanup_run_staging only removes registered paths, so a user file inside
    .Chamo暂存 must survive a completed run and must not be mistaken for this
    run's staging.
    """
    state = tmp_path / "state"
    out = tmp_path / "out"
    staging = staging_root_of(out)
    staging.mkdir(parents=True)
    user_note = staging / "我的说明.txt"
    user_note.write_bytes(b"a file the user keeps here")
    user_dir = staging / "我的资料"
    user_dir.mkdir()
    (user_dir / "note.txt").write_bytes(b"keep")

    run = run_pdf_images(scan_pdf, out, state)
    assert run.rc == 0, "the run failed: " + describe(run)
    assert user_note.is_file() and user_note.read_bytes() == b"a file the user keeps here", (
        "the run deleted a user file that was inside the pre-existing staging folder")
    assert user_dir.is_dir() and (user_dir / "note.txt").is_file(), (
        "the run deleted a user folder that was inside the pre-existing staging folder")
    assert staging_directories(out) == ["我的资料"], (
        "the run left its own staging directory behind (only the user's folder may "
        "remain): %r" % staging_directories(out))
    assert (out / scan_pdf.stem).is_dir(), (
        "the run published nothing next to the pre-existing staging folder")
