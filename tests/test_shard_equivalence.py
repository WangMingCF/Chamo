"""Independent, fail-capable acceptance gates for content-aware sharded rendering.

These tests are the acceptance gate for the "shard=auto" pdf_images feature.
They were written independently of the engine implementation and they are built
to FAIL when the feature is absent, broken, or "optimised" by cheating:

  * equivalence.image / .text / .range / .big_over_999_pages: shard=off and
    shard=auto must produce the identical relative path set, identical SHA-256
    for every file, the same page count and the same file count. Dropping a page,
    lowering the DPI, renaming with a different zero-padding width or scattering
    one job over several folders all turn these red.
  * non-vacuity: every equivalence test also asserts the sharded arm really took
    the sharded path (process-tree sampling OR report.json's shard decision, so
    either observable is enough to make the comparison meaningful). A comparison
    between two unsharded runs cannot pass. The positive decision gate below is
    stricter: when sampling is available it requires the process table to agree
    with the report, because a report can claim sharding while rendering one
    process.
  * content.text: a born-digital text PDF must NOT be sharded and must not
    regress by more than 5%.
  * failure.shard_failure: one failing shard must fail the whole job and publish
    zero pages; a partial "success" would silently lose pages.
  * the comparator itself has a unit test proving it goes red for a missing file,
    an extra file, a changed byte, a wrong page count and a scattered folder.

Environment:
  CI=true                -> the machine-dependent >=1.5x speed test is skipped
                            with a printed reason (allowed by the acceptance
                            contract). It is never silently green: pytest reports
                            it as skipped.
  CHAMO_SHARD_PERF=0/1     -> force the speed test off/on.
  CHAMO_SHARD_TEST_REPS=N  -> timing repetitions (default 3; paired runs, median ratio).

Run:  ./third_party/python/bin/python3 -m pytest tests/test_shard_equivalence.py -q
Full standalone matrix (more repetitions, mutation-friendly --engine): 
      ./third_party/python/bin/python3 scripts/verify_shard.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = ROOT / "scripts" / "verify_shard.py"
SPEEDUP_MIN = 1.5


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("chamo_verify_shard", GATE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vs = _load_gate_module()


def assert_gate(result):
    """Turn a gate result into a pytest assertion with expected/actual detail."""
    assert result["status"] == "PASS", vs.format_check(result)


@pytest.fixture(scope="session")
def gate(tmp_path_factory):
    """One gate session per pytest run so each engine run happens once."""
    work = tmp_path_factory.mktemp("shard-gates")
    repetitions = os.environ.get("CHAMO_SHARD_TEST_REPS", "3")
    args = vs.parse_args(["--workdir", str(work), "--reps", repetitions])
    return vs.GateSession(args)


# --------------------------------------------------------------------------
# The comparator must be able to fail (negative control of the gate itself)
# --------------------------------------------------------------------------
def _fake(relpath):
    return "sha-" + relpath


def test_equivalence_comparator_is_not_toothless():
    """compare_outputs() must report every realistic way a sharded run differs.

    Without this, the equivalence gates could be green because the comparator is
    blind; this test feeds it the exact failure modes the feature could have.
    """
    off = {"scan/scan_0001.png": _fake("1"), "scan/scan_0002.png": _fake("2")}
    assert vs.compare_outputs(off, dict(off), expected_pages=2) == []

    missing = {"scan/scan_0001.png": _fake("1")}
    problems = vs.compare_outputs(off, missing, expected_pages=2)
    assert any("missing from shard=auto" in p for p in problems), problems
    assert any("file count differs" in p for p in problems), problems
    assert any("page count" in p and "shard=auto produced 1" in p for p in problems), problems

    extra = dict(off)
    extra["scan/scan_0003.png"] = _fake("3")
    problems = vs.compare_outputs(off, extra, expected_pages=2)
    assert any("unexpected relative paths" in p for p in problems), problems

    changed = dict(off)
    changed["scan/scan_0001.png"] = "different-bytes"
    problems = vs.compare_outputs(off, changed, expected_pages=2)
    assert any("SHA-256 differs" in p for p in problems), problems

    padded = {"scan/scan_001.png": _fake("1"), "scan/scan_002.png": _fake("2")}
    problems = vs.compare_outputs(off, padded, expected_pages=2)
    assert any("missing from shard=auto" in p for p in problems), problems
    assert any("unexpected relative paths" in p for p in problems), problems

    scattered = {"scan/scan_0001.png": _fake("1"),
                 "scan (2)/scan_0002.png": _fake("2")}
    problems = vs.compare_outputs(off, scattered, expected_pages=2)
    assert any("span 2 output folders" in p for p in problems), problems


# --------------------------------------------------------------------------
# Contract: the frozen config key and CLI flag
# --------------------------------------------------------------------------
def test_cli_exposes_shard_auto_off(gate):
    """engine/chamo.py --shard {auto,off} must exist; anything else is exit code 2."""
    assert_gate(vs.gate_contract_cli(gate))


def test_default_without_shard_key_is_auto(gate):
    """The GUI never sends the key, so a request without it must behave as auto."""
    assert_gate(vs.gate_contract_default_auto(gate))


# --------------------------------------------------------------------------
# Gate 1: equivalence (the anti-cheat gate)
# --------------------------------------------------------------------------
def test_equivalence_image_sharded_vs_unsharded(gate):
    """An image-dominant PDF: shard=auto must be byte-identical to shard=off."""
    assert_gate(vs.gate_equivalence(gate, "scan", "equivalence.image",
                                    expect_sharded=True))


def test_equivalence_text_sharded_vs_unsharded(gate):
    """A text PDF: the two arms must be identical and auto must not be sharded."""
    assert_gate(vs.gate_equivalence(gate, "text", "equivalence.text",
                                    expect_sharded=False))


def test_equivalence_non_contiguous_range(gate):
    """Sharding must slice the SELECTED page list, not raw page numbers.

    Range "1-40,50-89" leaves pages 41-49 out; a renderer that shards by raw
    page index across the file would lose or duplicate pages around the gap.
    """
    assert_gate(vs.gate_equivalence(gate, "scan", "equivalence.range",
                                    expect_sharded=True, range_spec=vs.RANGE_SPEC))


def test_equivalence_over_999_pages_zero_padding(gate):
    """>999 pages: per-shard naming would use 3 digits while the job needs 4.

    This case is invisible below 1000 pages, which is why it gets its own
    fixture (1200 pages).
    """
    assert_gate(vs.gate_equivalence(gate, "big", "equivalence.big_over_999_pages",
                                    expect_sharded=True))


# --------------------------------------------------------------------------
# Gate 1b: the sharding cost model decides both directions on real documents
# --------------------------------------------------------------------------
def test_decision_model_shards_dense_work(gate):
    """48 A4 pages (~104 MPix): an old 96-page rule refused this shape.

    Forced sharding measured 1.20-1.35x on this fixture shape, so shard=auto must
    take the sharded path, the process table must show the worker processes when
    sampling is available, and the report must carry the model's own numbers
    (total_pixels, predicted_gain, gain_floor). This test is red on an engine
    that refuses the fixture by page count alone, and on a mutant whose
    report claims sharding while the renderer stays single-process.
    """
    results = {result["name"]: result for result in vs.gate_decision_model(gate)}
    assert_gate(results["decision.dense_work_shards"])


def test_decision_model_refuses_cheap_work(gate):
    """96 small pages at 240 dpi (~64 MPix): an old 60-MPix floor admitted this.

    That raster work cannot repay several interpreter starts, so shard=auto must
    stay single-process and produce byte-identical output. This test is red on an
    engine whose cost model always shards.
    """
    results = {result["name"]: result for result in vs.gate_decision_model(gate)}
    assert_gate(results["decision.sparse_work_not_sharded"])


# --------------------------------------------------------------------------
# Gate 2: content decision (text must not be sharded, must not regress >5%)
# --------------------------------------------------------------------------
def test_content_text_is_not_sharded_and_not_slower(gate):
    """Text PDFs must not be sharded and shard=auto must cost <5% on them.

    Three checks, all fail-capable:
      * not_sharded - the report and the process table agree it stayed single;
      * sample_cost - the CPU shard=auto adds on a text PDF (import pageclass +
        classify, measured in fresh interpreters) is <= 5% of a shard=off run, on
        every platform;
      * timing - the end-to-end auto/off ratio, best-of-N on the engine tree's CPU
        seconds (wall time where a host reports none), re-measured while over
        budget; see verify_shard.text_timing_verdict.
    The end-to-end ratio is judged on Linux and Windows (10/10 there). On macOS it
    is measured and REPORTed but not judged, by the maintainer's decision in issue
    #10: identical work on macos-latest costs more or fewer CPU seconds from run to
    run, so even best-of-12 pairs landed in 0.89-1.11 (one encode thread too), while
    42 interleaved triples showed no real slowdown. sample_cost keeps the 5% budget
    enforced there. This is not a skip: the check runs and prints its numbers.
    """
    results = vs.gate_content_text(gate)
    by_name = {result["name"]: result for result in results}
    assert set(by_name) == {"content.text.not_sharded", "content.text.sample_cost",
                            "content.text.timing"}, results
    assert_gate(by_name["content.text.not_sharded"])
    assert_gate(by_name["content.text.sample_cost"])
    timing = by_name["content.text.timing"]
    if vs.TEXT_END_TO_END_JUDGED:
        assert_gate(timing)
    else:
        assert timing["status"] == "REPORT", vs.format_check(timing)
        assert timing["meta"]["judged"] is False, timing
        print(vs.format_check(timing))


def test_end_to_end_text_timing_is_judged_everywhere_but_macos():
    """Pin the platform rule from issue #10 so it cannot spread silently."""
    assert vs.TEXT_END_TO_END_JUDGED is (sys.platform != "darwin")


# --------------------------------------------------------------------------
# Frozen contract 6: a failed shard can never look like a partial success
# --------------------------------------------------------------------------
def test_one_failed_shard_fails_the_whole_job(gate):
    """A shard that cannot render its page must fail the job with 0 published pages.

    The fixture is a scan with one oversized page: only the shard that owns that
    page fails. A partial publish would silently lose every other page too, which
    is exactly the data-safety failure this gate exists to prevent.
    """
    assert_gate(vs.gate_shard_failure_visible(gate))


# --------------------------------------------------------------------------
# Gate 3: speed (machine-dependent; skipped in CI with a printed reason)
# --------------------------------------------------------------------------
def test_tree_cpu_clock_counts_a_worker_in_its_own_group():
    """The timing gates' CPU clock must include the engine's workers.

    The engine starts each worker with platform_process.spawn_group: a new session
    on POSIX, a job object of its own on Windows. A clock that only saw the
    supervisor would report a fraction of the work and could hide a slowdown, so
    a grandchild started exactly that way burns about 0.6 s of CPU here and the
    clock must see it. Runs on every platform, including Windows, where the clock
    is the Job Object accounting rather than RUSAGE_CHILDREN.
    """
    import subprocess
    import sys

    child = (
        "import subprocess, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import platform_process\n"
        "burn = 'import time\\nend = time.process_time() + 0.6\\n"
        "while time.process_time() < end: pass\\n'\n"
        "proc = platform_process.spawn_group([sys.executable, '-c', burn])\n"
        "sys.exit(proc.wait())\n")
    proc, tree_cpu = vs.start_measured([sys.executable, "-c", child, str(ROOT / "engine")])
    try:
        code = proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        seconds = tree_cpu()  # also releases the Windows job handle
    assert code == 0
    assert seconds is not None, "this platform reported no process-tree CPU time"
    assert seconds >= 0.55, (
        "the clock saw %.2f s of CPU, but the worker alone burned 0.6 s: "
        "work in the worker's own group/job is not counted" % seconds)


def test_text_timing_verdict_ignores_host_noise_but_not_a_slowdown():
    """Best-of-N per arm, against the same 5% budget, on either clock.

    The noisy cases are real CI failures of identical work under the older
    median-of-3-pairs verdict:
      * windows-latest wall time at ab4a2b2 (docs-only commit): 8.7%;
      * macos-latest child CPU seconds, load average 13: 6.8%.
    Each arm's fastest measurement is within the budget in both.
    """
    assert vs.TEXT_REGRESSION_MAX == 0.05
    wall_off, wall_auto = [6.17, 6.20, 6.22], [6.36, 6.80, 7.51]
    noisy = vs.text_timing_verdict(wall_off, wall_auto)
    assert noisy["status"] == "PASS", noisy
    assert abs(noisy["regression"] - (1 - 6.17 / 6.36)) < 1e-9, noisy
    cpu_off, cpu_auto = [7.72, 10.58, 11.55], [8.28, 9.96, 12.41]
    assert vs.text_timing_verdict(cpu_off, cpu_auto)["status"] == "FAIL"
    # ...and the macOS case passes once one more pair includes a quiet auto run.
    assert vs.text_timing_verdict(cpu_off + [9.80], cpu_auto + [7.90])["status"] == "PASS"

    # A real slowdown is in every auto run, including the fastest one.
    off = [6.17, 6.20, 6.22]
    assert vs.text_timing_verdict(off, [x * 1.06 for x in off])["status"] == "FAIL"
    # Noise on top of a real 6% slowdown still fails.
    assert vs.text_timing_verdict(off, [6.17 * 1.06, 6.9, 7.4])["status"] == "FAIL"
    # Inside the budget passes.
    assert vs.text_timing_verdict(off, [x * 1.04 for x in off])["status"] == "PASS"
    # No usable pair is a failure, not a pass.
    assert vs.text_timing_verdict([], [])["status"] == "FAIL"


def test_content_sample_probe_measures_the_real_classifier(tmp_path):
    """The probe runs the engine's own pageclass and reads only its prefixed lines."""
    pdf = tmp_path / "text.pdf"
    vs.make_text_pdf(pdf, 12)
    for clock in ("cpu", "wall"):
        seconds, kinds, problems, basis = vs.measure_content_sample_cpu(
            ROOT / "engine", pdf, reps=1, clock=clock)
        assert problems == [] and kinds == ["text"], (problems, kinds)
        # Never 0: a CPU reading below the clock's resolution falls back to wall.
        assert 0 < seconds[0] < vs.TEXT_SAMPLE_MAX_SECONDS, (clock, seconds, basis)
        assert basis[0] in (("cpu", "wall-bound") if clock == "cpu" else ("wall",)), basis


def test_a_cpu_reading_below_resolution_never_counts_as_zero():
    """Windows reads a few-ms sample as 0.0 CPU (15.6 ms ticks); 0 must not pass for free."""
    assert vs.pick_sample_seconds(0.0, 0.012, 0.015625, "cpu") == (0.012, "wall-bound")
    assert vs.pick_sample_seconds(0.015625, 0.020, 0.015625, "cpu") == (0.020, "wall-bound")
    assert vs.pick_sample_seconds(0.0625, 0.070, 0.015625, "cpu") == (0.0625, "cpu")
    assert vs.pick_sample_seconds(0.021, 0.025, 1e-6, "cpu") == (0.021, "cpu")
    assert vs.pick_sample_seconds(0.021, 0.025, 1e-6, "wall") == (0.025, "wall")


def _stub_session(off_times, auto_times, clock="cpu"):
    """A GateSession stand-in: the gate logic, not the machine, is under test."""
    class Args:
        reps = 3

    def measured(value):
        if clock == "cpu":
            return {"rc": 0, "wall": 1.0, "cpu": value}
        return {"rc": 0, "wall": value, "cpu": None}

    class Session:
        args = Args()
        engine = ROOT / "engine" / "workbench.py"
        calls = []

        def fixture(self, key):
            return ROOT / "unused.pdf"

        def run(self, key, shard, rep=0, sample=False, range_spec=""):
            return {"rc": 0, "wall": 1.0, "cpu": None,
                    "report_results": [{"shard": {"used": False, "shards": 1,
                                                  "kind": "text", "mode": shard}}]}

        def paired_reps(self, key, range_spec="", count=None):
            self.calls.append(count)
            return [(measured(off_times[i]), measured(auto_times[i]))
                    for i in range(count)]
    return Session()


def _gate_checks(session, monkeypatch, sample=([0.004, 0.003, 0.003], ["text"] * 3, []),
                 judge=True):
    monkeypatch.setattr(vs, "measure_content_sample_cpu", lambda *_a, **_k: sample)
    return {r["name"]: r for r in vs.gate_content_text(session, judge_end_to_end=judge)}


def test_content_sample_cost_check_can_fail(monkeypatch):
    """sample_cost is the judged 5% on macOS, so it must go red when it should."""
    off = [6.0] * 14
    quiet = _gate_checks(_stub_session(off, off), monkeypatch)
    assert quiet["content.text.sample_cost"]["status"] == "PASS", quiet
    costly = _gate_checks(_stub_session(off, off), monkeypatch,
                          sample=([0.40, 0.35, 0.36], ["text"] * 3, []))
    assert costly["content.text.sample_cost"]["status"] == "FAIL", costly
    misread = _gate_checks(_stub_session(off, off), monkeypatch,
                           sample=([0.004] * 3, ["image"] * 3, []))
    assert misread["content.text.sample_cost"]["status"] == "FAIL", misread
    broken = _gate_checks(_stub_session(off, off), monkeypatch,
                          sample=([], [], ["probe failed"]))
    assert broken["content.text.sample_cost"]["status"] == "FAIL", broken
    # Under the 5% share but over the absolute cap: 0.28 s of a 60 s run.
    long_runs = [60.0] * 14
    capped = _gate_checks(_stub_session(long_runs, long_runs), monkeypatch,
                          sample=([0.28, 0.27, 0.27], ["text"] * 3, []))
    assert capped["content.text.sample_cost"]["status"] == "FAIL", capped


@pytest.mark.parametrize("clock", ["cpu", "wall"])
def test_content_sample_cost_uses_the_clock_of_the_off_runs(clock, monkeypatch):
    """CPU seconds are never divided by wall seconds, and failed off runs are ignored."""
    seen = []

    def probe(*_args, **kwargs):
        seen.append(kwargs.get("clock"))
        return [0.004] * 3, ["text"] * 3, []
    monkeypatch.setattr(vs, "measure_content_sample_cpu", probe)
    session = _stub_session([6.0] * 14, [6.0] * 14, clock)
    checks = {r["name"]: r for r in vs.gate_content_text(session, judge_end_to_end=True)}
    assert seen == [clock], seen
    assert checks["content.text.sample_cost"]["status"] == "PASS", checks


def test_report_only_timing_never_fails_and_is_not_remeasured(monkeypatch):
    """macOS: an 8% end-to-end difference is printed, not judged; no extra pairs."""
    off = [6.0] * 14
    session = _stub_session(off, [x * 1.08 for x in off])
    checks = _gate_checks(session, monkeypatch, judge=False)
    timing = checks["content.text.timing"]
    assert timing["status"] == "REPORT", timing
    assert timing["meta"]["judged"] is False
    assert session.calls == [3], session.calls
    # ...while the same numbers on a judged platform fail.
    judged = _gate_checks(_stub_session(off, [x * 1.08 for x in off]), monkeypatch)
    assert judged["content.text.timing"]["status"] == "FAIL", judged


@pytest.mark.parametrize("clock", ["cpu", "wall"])
def test_text_timing_gate_remeasures_noise_and_still_fails_a_slowdown(clock, monkeypatch):
    """The gate measures more pairs while over budget, then decides.

    A stub session stands in for the engine: the gate logic, not the machine, is
    under test. With noise only, a quiet pair arrives and the gate passes; with a
    real 8% slowdown the gate runs out of pairs and FAILS - it never skips. Both
    clocks: CPU seconds where the platform reports them, else wall time.
    """
    def session_with(off_times, auto_times):
        return _stub_session(off_times, auto_times, clock)

    def timing(session):
        return _gate_checks(session, monkeypatch)["content.text.timing"]

    off = [6.17, 6.20, 6.22, 6.18, 6.19, 6.21, 6.20] * 2
    noisy = session_with(off, [6.80, 6.90, 7.51, 6.20, 6.95, 7.00, 7.10] * 2)
    result = timing(noisy)
    assert result["status"] == "PASS", result
    assert noisy.calls[-1] == 4, noisy.calls
    expected_metric = "child CPU seconds" if clock == "cpu" else "wall time"
    assert result["meta"]["metric"] == expected_metric, result

    slow = session_with(off, [x * 1.08 for x in off])
    result = timing(slow)
    assert result["status"] == "FAIL", result
    assert slow.calls[-1] == vs.TEXT_MAX_PAIRS, slow.calls


def test_speed_image_sharding_at_least_1_5x(gate):
    """>= 1.5x end to end on a 240-page image-dominant PDF (local/perf runs only)."""
    forced = os.environ.get("CHAMO_SHARD_PERF")
    if forced == "0":
        pytest.skip("speed gate disabled by CHAMO_SHARD_PERF=0 (explicit skip, NOT a pass)")
    if forced != "1" and os.environ.get("CI"):
        pytest.skip(
            "speed gate skipped because CI is set: the >=1.5x threshold is "
            "machine-dependent (acceptance contract allows this skip on CI, and "
            "it is NOT a pass). Run on a quiet machine: "
            "python scripts/verify_shard.py --mode speed --perf on")
    assert_gate(vs.gate_speed_image(gate, reps=2))
    assert SPEEDUP_MIN == 1.5
