"""Where the engine keeps its own bookkeeping, outside the user's output folder.

A conversion must leave the output folder clean: only the converted files. Until
now every run created "_Chamo记录" next to the user's PNGs with a request
log, per-item progress, journals and the run report (13 files for one PDF), plus
an empty "_Chamo暂存". Those files are still needed - resume, history and the
shard decision in report.json all read them - so they move here, into a per-user
state directory keyed by the output folder. A batch conversion (more than one
input task) additionally writes one human-readable "清单.json" into the output
folder: a list of what was produced is part of the deliverable when the user
converts a folder of files. A single-file conversion writes nothing extra.

Resolution order (first writable wins):
  1. request["state_root"] - the GUI may pass its own Application Support folder;
  2. CHAMO_STATE_DIR - tests and scripts use it to keep runs isolated;
  3. the platform default (Application Support / LOCALAPPDATA / XDG state home);
  4. a folder under the system temp dir, so a read-only home directory cannot
     turn a conversion that would otherwise succeed into a failure.

The staging folder is the one exception that stays next to the output: publishing
is an atomic rename, which only works on the same filesystem. It is dot-prefixed
and removed when the run ends, so a finished conversion still shows nothing but
its outputs.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

MANIFEST_NAME = "清单.json"

# Bookkeeping is not valuable enough to grow without a bound.
RUN_KEEP = 20                 # run folders kept per output folder
RECEIPT_KEEP = 500            # resume receipts kept per output folder
SLOT_MAX_AGE = 30 * 24 * 3600  # forget output folders untouched for a month


def default_root() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Chamo"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "Chamo"
        return home / "Chamo"
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else home / ".local" / "state") / "chamo"


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / (".write-test-" + uuid.uuid4().hex)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


_ROOT_CACHE: dict = {}


def _override(request):
    """The explicitly requested state root, from a request dict or a bare path.

    The GUI hands the engine a request dict, but the supervisor passes each worker
    the already-extracted value, which is a string. Accepting only the dict once
    made the documented "state_root" request field fail every conversion that used
    it ("'str' object has no attribute 'get'"), which no test caught because they
    all set CHAMO_STATE_DIR instead.
    """
    if isinstance(request, dict):
        candidate = request.get("state_root")
    elif isinstance(request, str):
        candidate = request
    else:
        candidate = None
    return candidate or None


def root(request=None) -> Path:
    """The state directory, created if needed. Never writes to the output folder.

    Memoised because a batch asks for a receipt path once per file and resolving
    the root probes write access; one probe per process is enough.
    """
    override = _override(request) or os.environ.get("CHAMO_STATE_DIR")
    cache_key = str(Path(override).expanduser()) if override else ""
    cached = _ROOT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(default_root())
    for candidate in candidates:
        if _writable(candidate):
            _ROOT_CACHE[cache_key] = candidate
            return candidate
    # Last resort: a fresh temporary folder per process. Losing resume is better
    # than failing a conversion because the user's home directory is read-only.
    fallback = Path(tempfile.gettempdir()) / "chamo-state"
    if _writable(fallback):
        _ROOT_CACHE[cache_key] = fallback
        return fallback
    raise OSError("无法创建任务状态目录")


def slot(output, request=None) -> Path:
    """The state folder for one output folder (stable across runs)."""
    key = str(Path(output).resolve())
    if os.name == "nt":
        key = key.lower()  # Windows paths are case-insensitive
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return root(request) / "outputs" / digest


def records(output, request=None) -> Path:
    return slot(output, request) / "records"


def new_record(output, request=None) -> Path:
    path = records(output, request) / ("run-" + uuid.uuid4().hex)
    path.mkdir(parents=True)
    # A slot's own mtime does not move when only its child folders change, so
    # "created" would be mistaken for "last used" and prune would eventually
    # delete the records of an output folder that is still in use.
    try:
        os.utime(path.parent.parent, None)
    except OSError:
        pass
    return path


def receipt(output, fingerprint, request=None) -> Path:
    return slot(output, request) / "receipts" / (fingerprint + ".json")


def office_profiles(request=None) -> Path:
    return root(request) / "office-profiles"


def staging(output) -> Path:
    """Staging next to the output, dot-prefixed: atomic renames need one filesystem."""
    return Path(output) / ".Chamo暂存"


def drop_staging(output) -> None:
    try:
        staging(output).rmdir()  # only when empty: a concurrent run keeps its own
    except OSError:
        pass


def _newest_first(paths):
    def mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(paths, key=mtime, reverse=True)


def _last_used(folder: Path) -> float:
    """When this slot was last written to, judged by its newest run or receipt.

    The slot directory's own mtime is set when it is created and does not change
    again: runs and receipts are written into records/ and receipts/. Using it as
    "last used" deleted slots that were being used right up to the 30-day mark.
    """
    try:
        newest = folder.stat().st_mtime
    except OSError:
        return 0.0
    for name in ("records", "receipts"):
        base = folder / name
        try:
            newest = max(newest, base.stat().st_mtime)
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    return newest


# ---- page-level resume cache (pdf_images) ---------------------------------
# Finished pages are cached under the state root so a cancelled or killed export
# can reuse them on its next run. There is deliberately no cache manifest: every
# page is committed on its own (page file, then sidecar), which is what makes a
# kill -9 lose at most the page that was being written. See engine/workbench.py
# for the read/write protocol; this module only owns the location, limits and
# the sweep.
PAGE_CACHE_MARKER = ".active"
PAGE_CACHE_DEFAULT_MAX_BYTES = 2 * 1024 ** 3
PAGE_CACHE_DEFAULT_MAX_AGE = 7 * 24 * 3600
# A cache folder counts as in use while its marker is younger than this. A
# running export refreshes the marker as it writes pages, so a sweep in another
# process skips it instead of deleting pages that are being produced right now.
PAGE_CACHE_ACTIVE_WINDOW = 6 * 3600
PAGE_CACHE_STALE_TMP_AGE = 3600
PAGE_CACHE_DISABLED_VALUES = {"0", "off", "false", "no"}


def page_cache_enabled() -> bool:
    value = os.environ.get("CHAMO_PAGE_CACHE", "").strip().lower()
    return value not in PAGE_CACHE_DISABLED_VALUES


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def page_cache_limits():
    """(max_bytes, max_age_seconds); both overridable for users and tests."""
    return (_env_int("CHAMO_PAGE_CACHE_MAX_BYTES", PAGE_CACHE_DEFAULT_MAX_BYTES),
            _env_int("CHAMO_PAGE_CACHE_MAX_AGE", PAGE_CACHE_DEFAULT_MAX_AGE))


def page_cache_root(request=None) -> Path:
    override = os.environ.get("CHAMO_PAGE_CACHE_DIR")
    if override and override.strip():
        return Path(override).expanduser()
    return root(request) / "page-cache"


def _tree_size(folder: Path) -> int:
    total = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += _tree_size(Path(entry.path))
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _remove_tree(folder: Path) -> bool:
    shutil.rmtree(folder, ignore_errors=True)
    try:
        return not folder.exists()
    except OSError:
        return True


def _sweep_stale_tmp(folder: Path, now: float) -> int:
    """Remove half-written temp files a killed writer left behind (never fresh)."""
    removed = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False) or ".tmp-" not in entry.name:
                        continue
                    if now - entry.stat(follow_symlinks=False).st_mtime > PAGE_CACHE_STALE_TMP_AGE:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
    except OSError:
        pass
    return removed


def _cache_active(folder: Path, now: float) -> bool:
    try:
        marker = folder / PAGE_CACHE_MARKER
        return marker.is_file() and now - marker.stat().st_mtime < PAGE_CACHE_ACTIVE_WINDOW
    except OSError:
        return False


def sweep_page_cache(request=None) -> dict:
    """Expire the page cache: too old first, then over the size limit (LRU).

    Called once at the start of a run, before any worker starts writing. A cache
    set a running export is still writing is protected by its .active marker, so
    this sweep never collects pages another process is producing right now. The
    folder is keyed by output-independent hashes, so a sweep sees every user's
    cached pages: the bounds below are what keeps that disk use finite.

    Every failure here is cosmetic and swallowed: a cache problem must never
    fail a conversion, and a disabled cache (CHAMO_PAGE_CACHE=0) is not touched.
    """
    swept = {"dirs": 0, "bytes": 0, "tmp": 0, "markers": 0, "errors": 0}
    if not page_cache_enabled():
        return swept
    try:
        base = page_cache_root(request) / "pdf_images"
        if not base.is_dir():
            return swept
        max_bytes, max_age = page_cache_limits()
        now = time.time()
        entries = []
        try:
            sources = [item for item in base.iterdir() if item.is_dir()]
        except OSError:
            return swept
        for source in sources:
            try:
                configs = [item for item in source.iterdir() if item.is_dir()]
            except OSError:
                continue
            for config in configs:
                try:
                    swept["tmp"] += _sweep_stale_tmp(config, now)
                    stat = config.stat()
                except OSError:
                    continue
                last_used = stat.st_mtime
                marker = config / PAGE_CACHE_MARKER
                marker_age = None
                try:
                    if marker.is_file():
                        marker_age = now - marker.stat().st_mtime
                        last_used = max(last_used, marker.stat().st_mtime)
                except OSError:
                    marker_age = None
                active = marker_age is not None and marker_age < PAGE_CACHE_ACTIVE_WINDOW
                if marker_age is not None and not active:
                    # A killed writer never got to remove its marker. Dropping the
                    # stale marker is what keeps the set governable: otherwise the
                    # set would look "in use" forever and neither the age nor the
                    # size bound could ever collect it.
                    try:
                        marker.unlink(missing_ok=True)
                        swept["markers"] += 1
                    except OSError:
                        pass
                entries.append({"path": config, "mtime": last_used,
                                "active": active,
                                "size": _tree_size(config)})
        # 1) Age: a page set nobody should start a new run from again.
        for entry in list(entries):
            if entry["active"] or now - entry["mtime"] <= max_age:
                continue
            if _remove_tree(entry["path"]):
                entries.remove(entry)
                swept["dirs"] += 1
                swept["bytes"] += entry["size"]
        # 2) Size: evict least recently used sets, newest last. Active sets are
        #    skipped; when the limit is still exceeded the next sweep retries.
        total = sum(entry["size"] for entry in entries)
        if total > max_bytes:
            for entry in sorted(entries, key=lambda item: item["mtime"]):
                if total <= max_bytes:
                    break
                if entry["active"]:
                    continue
                if _remove_tree(entry["path"]):
                    total -= entry["size"]
                    swept["bytes"] += entry["size"]
                    swept["dirs"] += 1
                    entry["size"] = 0
        # 3) Drop source folders that no longer hold a page set.
        for source in sources:
            try:
                if not any(True for _ in source.iterdir()):
                    source.rmdir()
            except OSError:
                continue
    except Exception:
        swept["errors"] += 1
    return swept


def prune(output, request=None) -> dict:
    """Bound the bookkeeping: newest runs and receipts only, old slots removed.

    Called once per run. It never touches the slot being written right now, even
    when that output folder sat idle for months before this resume.
    """
    removed = {"runs": 0, "receipts": 0, "slots": 0}
    state_root = root(request)
    outputs_root = state_root / "outputs"
    try:
        slots = [p for p in outputs_root.iterdir() if p.is_dir()]
    except OSError:
        return removed
    current = slot(output, request)
    for item in slots:
        stale = time.time() - _last_used(item) > SLOT_MAX_AGE
        if stale and item != current:
            shutil.rmtree(item, ignore_errors=True)
            removed["slots"] += 1
    for name, key, keep in (("records", "runs", RUN_KEEP), ("receipts", "receipts", RECEIPT_KEEP)):
        folder = current / name
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for leftover in _newest_first(entries)[keep:]:
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                try:
                    leftover.unlink()
                except OSError:
                    continue
            removed[key] += 1
    return removed
