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
    """Text PDFs must not be sharded and the content sample must cost <5%.

    The timing half is judged on child CPU seconds where the platform reports
    them, because wall time on a shared CI runner is dominated by other tenants.
    On a platform without child CPU time (Windows) it is judged on each arm's
    fastest wall time, re-measured up to TEXT_WALL_MAX_PAIRS pairs while over
    budget; see verify_shard.text_wall_verdict. Both halves must PASS.
    """
    results = vs.gate_content_text(gate)
    assert {result["name"] for result in results} == {
        "content.text.not_sharded", "content.text.timing"}, results
    for result in results:
        assert_gate(result)


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
def test_text_timing_verdicts_keep_the_5_percent_bar():
    """Both clocks judge the same 5% budget and both can fail.

    Child CPU time: median of the per-pair ratios. One CI run measured 6.1% with
    5.7% pairwise spread on an overloaded host; that failure stands as a
    host-limited observation and must not be reclassified into a skip.
    """
    assert vs.TEXT_REGRESSION_MAX == 0.05
    assert vs.text_timing_verdict([0.94, 0.93, 1.04])["status"] == "FAIL"
    assert vs.text_timing_verdict([1.00, 1.01, 0.99])["status"] == "PASS"
    assert vs.text_timing_verdict([0.80, 0.81, 0.80])["status"] == "FAIL"
    # No usable pair is a failure, not a pass.
    assert vs.text_timing_verdict([])["status"] == "FAIL"


def test_text_wall_verdict_ignores_host_noise_but_not_a_slowdown():
    """Wall time: each arm's fastest run, against the same 5% budget.

    The first case is the windows-latest run that failed main at ab4a2b2 on a
    docs-only commit: identical work, auto/off median 0.913 and 8.7% "regression"
    from the median of three pairs. Best-of-3 is 3.0%, inside the budget.
    """
    off = [6.17, 6.20, 6.22]
    noisy_auto = [6.36, 6.80, 7.51]
    noisy = vs.text_wall_verdict(off, noisy_auto)
    assert noisy["status"] == "PASS", noisy
    assert abs(noisy["regression"] - (1 - 6.17 / 6.36)) < 1e-9, noisy

    # A real slowdown is in every auto run, including the fastest one.
    slow_auto = [x * 1.06 for x in off]
    assert vs.text_wall_verdict(off, slow_auto)["status"] == "FAIL"
    # Noise on top of a real 6% slowdown still fails.
    assert vs.text_wall_verdict(off, [6.17 * 1.06, 6.9, 7.4])["status"] == "FAIL"
    # Inside the budget passes.
    assert vs.text_wall_verdict(off, [x * 1.04 for x in off])["status"] == "PASS"
    # No usable pair is a failure, not a pass.
    assert vs.text_wall_verdict([], [])["status"] == "FAIL"


def test_text_timing_gate_remeasures_noise_and_still_fails_a_slowdown():
    """The wall-time gate measures more pairs while over budget, then decides.

    A stub session stands in for the engine: the gate logic, not the machine, is
    under test. With noise only, a quiet pair arrives and the gate passes; with a
    real 8% slowdown the gate runs out of pairs and FAILS - it never skips.
    """
    def session_with(auto_walls, off_walls):
        class Args:
            reps = 3

        class Session:
            args = Args()
            calls = []

            def run(self, key, shard, rep=0, sample=False, range_spec=""):
                return {"rc": 0, "wall": 1.0, "cpu": None,
                        "report_results": [{"shard": {"used": False, "shards": 1,
                                                      "kind": "text", "mode": shard}}]}

            def paired_reps(self, key, range_spec="", count=None):
                self.calls.append(count)
                return [({"rc": 0, "wall": off_walls[i], "cpu": None},
                         {"rc": 0, "wall": auto_walls[i], "cpu": None})
                        for i in range(count)]
        return Session()

    def timing(session):
        return {r["name"]: r for r in vs.gate_content_text(session)}["content.text.timing"]

    off = [6.17, 6.20, 6.22, 6.18, 6.19, 6.21]
    noisy = session_with([6.80, 6.90, 7.51, 6.20, 6.95, 7.00], off)
    result = timing(noisy)
    assert result["status"] == "PASS", result
    assert noisy.calls[-1] == 4, noisy.calls

    slow = session_with([x * 1.08 for x in off], off)
    result = timing(slow)
    assert result["status"] == "FAIL", result
    assert slow.calls[-1] == vs.TEXT_WALL_MAX_PAIRS, slow.calls


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
