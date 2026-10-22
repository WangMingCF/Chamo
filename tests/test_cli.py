"""The command-line front end and the versioned protocol.

The CLI builds the same request the GUI builds and runs the same supervisor, so
these tests check the contract a script depends on: exit codes, the versioned
event stream, and that a bad request fails before touching the filesystem.
"""
import json
import signal
import time
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# CHAMO_TEST_ENGINE_DIR lets a gate run against an engine tree outside the repo, so
# a gate can be shown to fail on another engine without editing this one.
ENGINE = Path(os.environ.get("CHAMO_TEST_ENGINE_DIR") or (ROOT / "engine")).resolve()
sys.path.insert(0, str(ENGINE))
import protocol
import platform_office

CHAMO_CLI = ENGINE / "chamo.py"
FIXTURES = ROOT / "tests/fixtures"


def run_cli(*args, timeout=180):
    return subprocess.run([sys.executable, "-I", str(CHAMO_CLI), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout)


def test_every_event_carries_the_protocol_version(tmp_path):
    source = FIXTURES / "透明图片.png"
    if not source.exists():
        pytest.skip("fixture missing")
    result = run_cli(str(source), "-o", str(tmp_path / "out"),
                     "-t", "image-convert", "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert events, "no events emitted"
    for event in events:
        assert event.get("protocol") == protocol.PROTOCOL, event
    assert events[-1]["type"] == "finished"


def test_help_and_version_work():
    assert run_cli("--version").stdout.strip() == f"chamo protocol {protocol.PROTOCOL}"
    usage = run_cli("--help")
    assert usage.returncode == 0
    assert "本地文件转换" in usage.stdout


def test_unknown_operation_is_a_usage_error(tmp_path):
    source = FIXTURES / "透明图片.png"
    if not source.exists():
        pytest.skip("fixture missing")
    result = run_cli(str(source), "-o", str(tmp_path / "out"), "-t", "nope")
    assert result.returncode == 2, "argparse usage errors must be distinguishable"


def test_unsupported_protocol_is_rejected():
    request = {"protocol": protocol.PROTOCOL + 1, "inputs": [],
               "output": "/tmp/x", "config": {"operation": "pdf_images"}}
    with pytest.raises(protocol.ProtocolError) as info:
        protocol.check_request(request)
    assert info.value.code == "unsupported_protocol"


def test_unknown_field_is_rejected():
    request = {"inputs": [], "output": "/tmp/x",
               "config": {"operation": "pdf_images", "typo_field": 1}}
    with pytest.raises(protocol.ProtocolError) as info:
        protocol.check_request(request)
    assert info.value.code == "unknown_field"


def test_error_codes_are_stable_and_documented():
    # Codes are part of the contract; tests and callers branch on them instead of
    # parsing Chinese prose that may be reworded.
    for code in ("invalid_request", "unsupported_protocol", "unknown_field",
                 "no_inputs", "output_unwritable", "missing_renderer",
                 "encrypted_pdf", "unsupported_structure", "output_changed",
                 "input_changed", "cancelled", "internal"):
        assert code in protocol.ERROR_CODES
    error = protocol.ProtocolError("cancelled")
    assert error.as_event()["code"] == "cancelled"
    assert protocol.ProtocolError("not-a-code").code == "internal"


def test_workers_zero_means_automatic(tmp_path):
    """The GUI resolves 0 in Swift; the engine must also understand it."""
    source = FIXTURES / "透明图片.png"
    if not source.exists():
        pytest.skip("fixture missing")
    request = {"protocol": protocol.PROTOCOL, "inputs": [str(source)],
               "output": str(tmp_path / "out"),
               "config": {"operation": "image_convert", "format": "png",
                          "quality": 90, "workers": 0, "naming": "clean"}}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", str(ENGINE / "workbench.py"), "--request", str(path)],
        capture_output=True, text=True, encoding="utf-8", timeout=180)
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert events[-1]["type"] == "finished", events[-1]
    assert events[-1]["success"] == 1


def test_launcher_survives_a_path_with_spaces(tmp_path):
    """The app can be installed in a folder whose name contains spaces.

    The shipped launcher is a shell script using $(dirname "$0"), so an unquoted
    path would break here. Skipped when the app has not been built.
    """
    app = ROOT / "build/Chamo.app"
    launcher = app / "Contents/Resources/bin/chamo"
    if not launcher.is_file():
        pytest.skip("app bundle not built")
    # The bundle ships a macOS interpreter; on the Linux gate running it is an
    # exec format error, which says nothing about the launcher quoting.
    if sys.platform != "darwin":
        pytest.skip("bundled interpreter only runs on macOS")
    import shutil
    target = tmp_path / "My Apps Folder" / app.name
    target.parent.mkdir(parents=True)
    shutil.copytree(app, target, symlinks=True)
    result = subprocess.run([str(target / "Contents/Resources/bin/chamo"), "--version"],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    assert f"protocol {protocol.PROTOCOL}" in result.stdout


def test_cli_leaves_no_request_file_in_the_output(tmp_path):
    """A crashed run must not leave engine artefacts in the results folder."""
    source = FIXTURES / "透明图片.png"
    if not source.exists():
        pytest.skip("fixture missing")
    output = tmp_path / "out"
    result = run_cli(str(source), "-o", str(output), "-t", "image-convert", "--quiet")
    assert result.returncode == 0, result.stderr
    leftovers = [p.name for p in output.iterdir() if p.name.startswith("_chamo-")]
    assert leftovers == [], f"request file left behind: {leftovers}"



def test_cli_prints_non_ascii_on_a_legacy_code_page(tmp_path):
    """The CLI itself must not crash printing Chinese paths on Windows.

    The engine was fixed for this first; the front end repeated the bug and CI
    caught it. Simulating the Windows console here keeps it from coming back:
    PYTHONIOENCODING=cp1252 makes stdout a legacy code page even on macOS/Linux.
    """
    source = FIXTURES / "透明图片.png"
    if not source.exists():
        pytest.skip("fixture missing")
    # No -I here: it implies -E, which makes Python ignore PYTHONIOENCODING,
    # so the encoding stays utf-8 and the test could never fail.
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    result = subprocess.run(
        [sys.executable, str(CHAMO_CLI), str(source), "-o", str(tmp_path / "out"),
         "-t", "image-convert"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=180)
    assert result.returncode == 0, f"CLI failed under a legacy code page: {result.stderr[-800:]}"
    assert "Traceback" not in result.stderr


def test_cli_json_stream_survives_a_legacy_code_page(tmp_path):
    source = FIXTURES / "透明图片.png"
    if not source.exists():
        pytest.skip("fixture missing")
    # No -I here: it implies -E, which makes Python ignore PYTHONIOENCODING,
    # so the encoding stays utf-8 and the test could never fail.
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    result = subprocess.run(
        [sys.executable, str(CHAMO_CLI), str(source), "-o", str(tmp_path / "out"),
         "-t", "image-convert", "--json"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=180)
    assert result.returncode == 0, result.stderr[-800:]
    events = [json.loads(l) for l in result.stdout.splitlines() if l.strip()]
    assert events[-1]["type"] == "finished"



def test_gui_keys_are_all_accepted():
    """The protocol whitelist must cover everything Sources/Model.swift sends.

    Without this, adding one config key in Swift would make every GUI run abort
    with "未知配置字段" and nothing would catch it before shipping.
    """
    swift = (ROOT / "Sources/Model.swift").read_text(encoding="utf-8")
    import re
    # config() builds its dictionary in parts (a base literal plus conditional
    # keys such as lossy/dpi/quality), so scan the whole function instead of only
    # the first literal. Slicing to the next declaration keeps other functions''
    # keys out of the set.
    start = swift.find("func config() -> [String: Any] {")
    assert start >= 0, "config() moved; update the extractor"
    end = swift.find("\n    func ", start + 1)
    assert end > start, "cannot find the end of config()"
    body = swift[start:end]
    # A key sits right after `[`, `{`, `,` or `;` and ends with a colon; value
    # strings such as `cleanNaming ? "clean" : "tagged"` are preceded by `?`, so
    # they are not mistaken for keys.
    gui_keys = set(re.findall(r'[\[{,;]\s*"([a-z_]+)"\s*:', body))
    # Conditional entries are written as `c["key"] = value`.
    gui_keys |= set(re.findall(r'c\["([a-z_]+)"\]', body))
    assert "operation" in gui_keys and "soffice" in gui_keys, (
        f"extractor found {sorted(gui_keys)}; config() changed shape")
    missing = gui_keys - protocol.CONFIG_FIELDS
    assert not missing, f"GUI sends config keys the protocol rejects: {sorted(missing)}"


def test_launch_fields_are_all_accepted():
    """gui_session / office_profile_root / inputs / output / config."""
    swift = (ROOT / "Sources/Model.swift").read_text(encoding="utf-8")
    for field in ("gui_session", "office_profile_root"):
        assert field in swift, f"{field} no longer sent; update the protocol whitelist"
        assert field in protocol.REQUEST_FIELDS


def test_engine_classifies_the_errors_it_raises():
    """Codes callers branch on must actually be produced, not just declared."""
    cases = [
        ("PDF 已加密：请先在原软件中用密码另存副本", "encrypted_pdf"),
        ("缺少 Office 渲染引擎：未找到可执行的 LibreOffice", "missing_renderer"),
        ("输出盘不足 2GB，请先腾出空间", "output_unwritable"),
        ("没有可处理文件：音视频默认排除", "no_inputs"),
        ("已完成输出被改变，请选择新的输出目录，避免覆盖", "output_changed"),
    ]
    for message, expected in cases:
        assert protocol.classify(ValueError(message)) == expected, message
    assert protocol.classify(ValueError("something unexpected")) == "internal"


def test_auto_workers_matches_the_gui():
    """\"Automatic\" must mean the same number in both front ends."""
    import workbench
    swift = (ROOT / "Sources/Model.swift").read_text(encoding="utf-8")
    assert "min(6, max(2," in swift, "GUI formula changed; update auto_workers"
    assert workbench.auto_workers() >= 2
    assert workbench.auto_workers() <= 6



def _big_pdf(path: Path, pages: int = 400) -> Path:
    from reportlab.pdfgen import canvas
    sheet = canvas.Canvas(str(path))
    for i in range(pages):
        sheet.drawString(80, 700, f"page {i}")
        sheet.showPage()
    sheet.save()
    return path


def _interrupt_and_time(proc: subprocess.Popen, timeout: float):
    """Send SIGINT and return (exit code, seconds from signal to exit, stderr)."""
    assert proc.poll() is None, "the run finished before it could be interrupted"
    started = time.monotonic()
    proc.send_signal(signal.SIGINT)
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"CLI did not exit within {timeout:g}s after Ctrl-C")
    return proc.returncode, time.monotonic() - started, stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_ctrl_c_stops_the_conversion_and_leaves_no_orphans(tmp_path):
    """Ctrl-C must take the engine down, not orphan it.

    Previously the CLI exited with a traceback while the engine and its workers
    kept writing into the output folder, which looks like a hang and leaves
    half-finished files.
    """
    source = _big_pdf(tmp_path / "big.pdf")
    output = tmp_path / "out"
    proc = subprocess.Popen(
        [sys.executable, "-I", str(CHAMO_CLI), str(source), "-o", str(output),
         "-t", "pdf-images", "--dpi", "300", "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    time.sleep(0.8)
    code, _, stderr = _interrupt_and_time(proc, 30)
    assert code == 3, f"expected EXIT_CANCELLED, got {code}: {stderr[-500:]}"
    assert "Traceback" not in stderr
    # Nothing may still be converting into the output folder.
    time.sleep(1.5)
    listing = subprocess.run(["ps", "-axo", "command"], capture_output=True,
                             text=True).stdout
    leftovers = [line for line in listing.splitlines()
                 if "workbench.py" in line and str(output) in line]
    assert not leftovers, f"engine still running after Ctrl-C: {leftovers}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_ctrl_c_during_a_slow_publish_exits_quickly(tmp_path):
    """The publish section blocks signals, so it needs its own cancel gate.

    Publishing defers SIGTERM/SIGINT (a cancel between a rename and the bookkeeping
    that makes the rollback aware of it leaves residue). That is only acceptable
    because the loop checks for a pending cancel between files: without that safe
    point a Ctrl-C during a long publish waited for the whole publish (reviewer
    measured 30.3 s and 62 leftover entries on a 0.5 s/file injection), and the
    supervisor's long publish grace period kept it alive instead of killing it.

    CHAMO_TEST_SLOW_PUBLISH (engine test hook, default off) widens the window on a
    fast machine. A cancelled run must exit in seconds and leave nothing behind.
    """
    source = _big_pdf(tmp_path / "race.pdf", pages=64)
    output = tmp_path / "out"
    state = tmp_path / "state"
    env = dict(os.environ)
    env["CHAMO_TEST_SLOW_PUBLISH"] = "0.5"
    env["CHAMO_STATE_DIR"] = str(state)
    env["CHAMO_PAGE_CACHE"] = "0"
    proc = subprocess.Popen(
        [sys.executable, "-I", str(CHAMO_CLI), str(source), "-o", str(output),
         "-t", "pdf-images", "--dpi", "150", "--workers", "1", "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env=env)
    # Interrupt as soon as the publish section has started: the first product file
    # is outside the staging folder, and the worker's publish marker is on disk.
    # The staging folder lives inside the output folder, so its pages must not be
    # mistaken for published products.
    staging = output / ".Chamo暂存"
    deadline = time.monotonic() + 90
    published = marker = None
    while time.monotonic() < deadline:
        assert proc.poll() is None, "the run finished before it could be interrupted"
        products = [p for p in output.rglob("*")
                    if p.is_file() and staging not in p.parents] if output.exists() else []
        locks = list(state.rglob("item-*-publish.lock")) if state.exists() else []
        if products and locks:
            published, marker = products[0], locks[0]
            break
        time.sleep(0.02)
    assert published is not None, (
        "the run never published a page outside the staging folder; the fixture or "
        "CHAMO_TEST_SLOW_PUBLISH hook is broken")
    assert marker is not None, (
        "the worker never wrote item-<n>-publish.lock while publishing; the cancel "
        "grace period would then always be the short one")
    code, elapsed, stderr = _interrupt_and_time(proc, 25)
    assert code == 3, f"expected EXIT_CANCELLED, got {code}: {stderr[-500:]}"
    assert "Traceback" not in stderr
    assert elapsed < 5.0, (
        f"Ctrl-C took {elapsed:.1f}s while the run was publishing; the publish loop "
        "must honour a pending cancel between files instead of finishing the publish")
    entries = [p.relative_to(output).as_posix() for p in output.rglob("*")]
    assert entries == [], f"a cancelled publish left entries in the output folder: {entries}"


def test_cancel_grace_is_long_only_inside_the_publish_window(tmp_path):
    """The publish marker is what buys the long grace period - pin the mapping.

    The supervisor kills a cancelled worker after a short grace period so Ctrl-C
    cannot look like a hang, but a SIGKILL inside the publish/receipt section leaves
    renamed files that no receipt covers. That is why the worker writes
    item-<n>-publish.lock there and why only that marker may extend the wait. Both
    halves are easy to break silently, so this pins them against the engine module.
    """
    import importlib.util
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    spec = importlib.util.spec_from_file_location(
        "chamo_workbench_grace_under_test", ENGINE / "workbench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    marker = tmp_path / "item-0-publish.lock"
    entry = {"job": {"critical": str(marker)}}
    # No marker: a cancelled worker is killed after the short grace period.
    assert module._publish_in_progress(entry) is False
    assert module._cancel_grace([entry]) == 5.0, "outside the publish window the grace must be short"
    # Marker present: the worker is publishing, so a hard kill would leave residue.
    marker.write_text("123", encoding="utf-8")
    assert module._publish_in_progress(entry) is True
    assert module._cancel_grace([entry]) == 30.0, "inside the publish window the grace must be long"
    # An entry without the key (older job shape) must never be treated as publishing,
    # and must never resolve the empty path to the current directory.
    assert module._publish_in_progress({"job": {}}) is False
    assert module._publish_in_progress({"job": {"critical": ""}}) is False
    assert module._cancel_grace([{"job": {}}]) == 5.0
    # The mapping is worthless if _execute does not use it: assert the wiring too.
    source = (ENGINE / "workbench.py").read_text(encoding="utf-8")
    assert "grace=_cancel_grace(entries)" in source, (
        "Supervisor._execute must take its cancel grace period from _cancel_grace()")
    assert "grace=30.0" not in source and "grace=5.0" not in source, (
        "the cancel grace period must not be hard-coded at the kill site")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_ctrl_c_does_not_wait_for_queued_page_encodes(tmp_path):
    """Cancellation must not drain the encodes that have not started yet.

    CI failed this: on a slow runner each 300 dpi page took seconds to encode, and
    a cancelled export still ran every encode already queued (2 x threads of them)
    before it could unwind. Together with the supervisor's 30 s grace period that
    turned Ctrl-C into a >30 s wait, i.e. a hang for the user.

    The engine's test hook (CHAMO_TEST_SLOW_ENCODE, default off) makes a laptop behave
    like that runner, and the hook can log each encode start, so the assertion is
    about the contract itself - no encode may *start* after the cancel arrived -
    instead of a wall-clock number that depends on the machine.
    """
    source = _big_pdf(tmp_path / "big.pdf")
    output = tmp_path / "out"
    trace = tmp_path / "encodes.log"
    env = dict(os.environ)
    env["CHAMO_TEST_SLOW_ENCODE"] = "3.0"
    env["CHAMO_TEST_ENCODE_LOG"] = str(trace)
    # A CI runner has few cores, so fewer encodes run at once and more sit in the
    # queue. Pinning the pool size keeps that shape on a many-core machine.
    env["CHAMO_TEST_ENCODE_THREADS"] = "2"
    proc = subprocess.Popen(
        [sys.executable, "-I", str(CHAMO_CLI), str(source), "-o", str(output),
         "-t", "pdf-images", "--dpi", "300", "--workers", "1", "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env=env)
    # Interrupt once the first encode has really started, not after a fixed
    # sleep: on a slow macOS runner start-up and the first 300 dpi render took
    # longer than 1 s, Ctrl-C arrived before any encode, and the precondition
    # below failed. Each encode then sleeps 3 s, so the queue is still full.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and proc.poll() is None:
        if trace.exists() and trace.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.02)
    assert proc.poll() is None, "the run finished before it could be interrupted"
    assert trace.exists() and trace.read_text(encoding="utf-8").strip(), (
        "no page encode started within 60 s; the fixture or the hook is broken")
    cancelled_at = time.time()
    code, elapsed, stderr = _interrupt_and_time(proc, 25)
    assert code == 3, f"expected EXIT_CANCELLED, got {code}: {stderr[-500:]}"
    assert "Traceback" not in stderr
    started = [float(line) for line in trace.read_text(encoding="utf-8").splitlines()
               if line.strip()] if trace.exists() else []
    assert started, "the run never encoded a page; the fixture or the hook is broken"
    late = [stamp for stamp in started if stamp > cancelled_at + 0.5]
    assert late == [], (
        f"{len(late)} page encode(s) started after Ctrl-C (at +"
        f"{min(late) - cancelled_at:.2f}s); queued encodes must be dropped, not drained")
    # Belt and braces for a machine where the hook cannot reproduce the slowness:
    # the fixed engine finishes the running encodes and exits in a couple of seconds.
    assert elapsed < 10.0, (
        f"Ctrl-C took {elapsed:.1f}s with slow page encoding; a cancelled run must "
        "not wait for work that has not started")
    entries = [p.relative_to(output).as_posix()
               for p in output.rglob("*")] if output.exists() else []
    assert entries == [], f"a cancelled run left entries in the output folder: {entries}"



def test_gui_sends_the_protocol_version():
    """Versioning is only real if both front ends declare it."""
    swift = (ROOT / "Sources/Model.swift").read_text(encoding="utf-8")
    assert "engineProtocol" in swift
    assert f"static let engineProtocol = {protocol.PROTOCOL}" in swift, (
        "Sources/Model.swift and engine/protocol.py disagree on the protocol version")
    assert '"protocol": Workbench.engineProtocol' in swift, "GUI must send the envelope version"



def test_missing_input_is_a_usage_error(tmp_path):
    """A mistyped path is the caller's mistake, so it must exit 2, not 4.

    The engine cannot tell "no such path" from "nothing convertible here" and
    answers 4 for both, so the CLI checks the paths before starting it.
    """
    result = run_cli(str(tmp_path / "does-not-exist.pdf"), "-o", str(tmp_path / "out"),
                     "-t", "pdf-images", "--quiet")
    assert result.returncode == 2, result.stderr
    assert "找不到输入" in result.stderr


def test_error_codes_are_distinct(tmp_path):
    """Each documented exit code must be reachable and distinct."""
    source = FIXTURES / "透明图片.png"
    if not source.exists():
        pytest.skip("fixture missing")
    ok = run_cli(str(source), "-o", str(tmp_path / "ok"), "-t", "image-convert", "--quiet")
    assert ok.returncode == 0
    usage = run_cli(str(source), "-o", str(tmp_path / "u"), "-t", "image-convert",
                    "--dpi", "9999", "--quiet")
    assert usage.returncode == 2, usage.stderr
    missing = run_cli(str(tmp_path / "nope.pdf"), "-o", str(tmp_path / "m"),
                      "-t", "pdf-images", "--quiet")
    assert missing.returncode == 2


def test_windows_launcher_is_written_as_crlf_not_crcrlf(tmp_path):
    """The shipped chamo.cmd must not get a second CR on Windows.

    build_windows wrote a body that already contained CRLF through text mode, so on
    Windows the newline translation made every line end CRCRLF. cmd.exe tolerates it,
    which is why it shipped, but the file was wrong and only the ZIP could show it.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "chamo_build_windows", ROOT / "scripts" / "build_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.launcher(tmp_path)
    data = (tmp_path / "chamo.cmd").read_bytes()
    assert b"\r\r\n" not in data, data[:120]
    assert data.startswith(b"@echo off\r\n"), data[:40]
    assert data.endswith(b"\r\n"), data[-40:]

