"""Unit tests for the sharding cost model that decides shard=auto.

The model replaced two fixed thresholds (96 pages / 60 MPix) with arithmetic
that compares the predicted one-process export with the predicted sharded one.
It is a pure function, so it is pinned here; the acceptance on the real engine
lives in scripts/verify_shard.py --mode decision; its constants come from
forced-sharding measurements on the benchmark shapes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

workbench = pytest.importorskip("workbench")


def a4_pixels(pages: int, dpi: float = 150.0) -> float:
    """Rendered pixels of A4 (595x842 pt) scan pages at the export DPI."""
    return 595.0 * 842.0 * (dpi / 72.0) ** 2 * pages


def test_measured_dense_document_shards():
    """48 A4 pages at 150 dpi = 104 MPix: the old 96-page rule refused it."""
    pages, pixels = 48, a4_pixels(48)
    shards, gain = workbench.shard_count(pages, pixels, 6, 10)
    # Forced sharding measured 1.20x / 1.35x / 1.27x at 2/3/4 shards.
    assert shards == 3
    assert workbench.SHARD_MIN_GAIN <= gain <= 1.5


def test_measured_medium_document_shards():
    """96 A4 pages at 150 dpi = 209 MPix. Measured 1.37/1.67/1.66x."""
    shards, gain = workbench.shard_count(96, a4_pixels(96), 6, 10)
    assert shards == 4
    assert gain >= workbench.SHARD_MIN_GAIN


def test_measured_large_document_shards():
    """240 A4 pages at 150 dpi = 522 MPix. Measured 1.64/2.24/2.24x."""
    shards, gain = workbench.shard_count(240, a4_pixels(240), 6, 10)
    assert shards == 6
    assert gain >= 2.0


def test_small_page_work_is_refused():
    """96 pages of 200x300 pt at 240 dpi = 64 MPix: the old 60-MPix floor sharded them."""
    pixels = 200.0 * 300.0 * (240.0 / 72.0) ** 2 * 96
    shards, gain = workbench.shard_count(96, pixels, 6, 10)
    assert shards == 1
    assert gain < workbench.SHARD_MIN_GAIN


def test_tiny_work_is_refused():
    """96 x 0.26 MPix: an old fixed rule sharded this and measured 0.74x."""
    pixels = 200.0 * 300.0 * (150.0 / 72.0) ** 2 * 96
    shards, gain = workbench.shard_count(96, pixels, 6, 10)
    assert shards == 1
    assert gain < 1.0


def test_few_pages_never_share_a_document():
    """Huge pages but 20 of them: MIN_PAGES_PER_SHARD must still refuse."""
    pixels = 595.0 * 842.0 * (600.0 / 72.0) ** 2 * 20
    assert workbench.shard_count(20, pixels, 6, 10)[0] == 1


def test_budget_cores_and_pages_are_all_ceilings():
    pixels = a4_pixels(240)
    assert workbench.shard_count(240, pixels, 1, 10)[0] == 1
    assert workbench.shard_count(240, pixels, 2, 10)[0] == 2
    assert workbench.shard_count(240, pixels, 8, 2)[0] == 2
    for pages, budget, cores in ((48, 6, 10), (96, 3, 10), (240, 6, 3), (240, 6, 10)):
        shards, _gain = workbench.shard_count(pages, a4_pixels(pages), budget, cores)
        assert shards <= min(budget, pages // workbench.MIN_PAGES_PER_SHARD, cores)


def test_gain_bounds_and_direction():
    pixels = a4_pixels(240)
    assert workbench.shard_gain(1, 10, pixels) == 1.0
    assert workbench.shard_gain(2, 10, 0) == 1.0
    assert workbench.shard_gain(2, 10, pixels) > workbench.shard_gain(1, 10, pixels)
    # More work makes the same process count look better (fixed cost amortised)...
    assert (workbench.shard_gain(3, 10, pixels)
            > workbench.shard_gain(3, 10, a4_pixels(96)))
    # ...but past the core count extra processes only add their own cost.
    assert (workbench.shard_gain(8, 4, pixels)
            < workbench.shard_gain(4, 4, pixels))


def test_refusal_reports_the_best_gain_in_numbers():
    """The job record must explain a refusal with a number, not an adjective."""
    shards, gain = workbench.shard_count(96, 20_000_000, 6, 10)
    assert shards == 1
    assert 0.0 < gain < 1.0


def test_constants_are_pinned_to_the_measured_values():
    """The published model must not drift silently.

    Review showed SHARD_MIN_GAIN could move from 1.10 down to 1.01
    while every model test and both decision gates stayed green - the advertised
    "only shard with >=10% predicted gain" promise was not actually fixed. These
    assertions make the published constants and predictions part of the contract:
    changing them deliberately means re-measuring forced sharding on the
    benchmark shapes and updating these numbers.
    """
    assert workbench.SHARD_ENGINE_FIXED == 0.33
    assert workbench.SHARD_PROCESS_COST == 0.10
    assert workbench.SHARD_PIXEL_RATE == 160_000_000
    assert workbench.SHARD_MIN_GAIN == 1.10
    assert workbench.MIN_PAGES_PER_SHARD == 16
    # Predictions from the benchmark measurements.
    assert workbench.shard_count(48, a4_pixels(48), 6, 10) == (
        3, pytest.approx(1.159, abs=0.002))
    assert workbench.shard_count(96, a4_pixels(96), 6, 10) == (
        4, pytest.approx(1.548, abs=0.002))
    assert workbench.shard_count(240, a4_pixels(240), 6, 10) == (
        6, pytest.approx(2.437, abs=0.002))
