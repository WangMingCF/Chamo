"""Sharding must never ask for more processes than the machine has cores.

The cap is small but it decides whether a "faster" feature is actually slower:
on a 2-core shared CI runner, four shards over 104 MPix measured 0.949x and
0.941x (i.e. slower than not sharding), while the same four shards on a 10-core
machine measured 1.52x. These tests pin the rule itself, because it is cheap to
delete and impossible to notice from a benchmark on a big machine.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

workbench = pytest.importorskip("workbench")


def budgets(workers, jobs, cores):
    original = workbench.os.cpu_count
    workbench.os.cpu_count = lambda: cores
    try:
        return workbench.shard_budgets("pdf_images", workers, jobs)
    finally:
        workbench.os.cpu_count = original


def test_two_core_machine_never_gets_four_shards():
    assert [1] == workbench.shard_budgets("pdf_images", 1, 1)
    assert [2] == budgets(4, 1, 2)
    assert [2] == budgets(16, 1, 2)


def test_big_machine_keeps_the_requested_split():
    assert [4] == budgets(4, 1, 10)
    assert [6] == budgets(6, 1, 10)
    assert [4, 3] == budgets(7, 2, 10)


def test_cpu_count_unknown_falls_back_to_the_request():
    assert [3] == budgets(3, 1, None)


def test_non_shardable_operations_are_untouched():
    # No spare capacity (one process per job) or an operation that never shards.
    assert [1, 1] == budgets(2, 2, 10)
    assert [1, 1] == workbench.shard_budgets("office_pdf", 8, 2)
    assert [1, 1] == workbench.shard_budgets("images_pdf", 8, 2)


def test_single_file_never_exceeds_cores_even_when_asked():
    # The cap is what keeps --workers 8 on a 2-core laptop from starting 8
    # interpreters that then take turns on two cores.
    assert [2] == budgets(8, 1, 2)
    assert [1] == budgets(1, 1, 2)
