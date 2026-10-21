"""Shared test isolation.

Every engine subprocess inherits this process environment, so pointing the state
root at the test's temporary directory keeps a full pytest run from writing run
records, receipts and slots into the developer's real
~/Library/Application Support/Chamo.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMO_STATE_DIR", str(tmp_path / "chamo-state"))


@pytest.fixture(scope="session", autouse=True)
def page_cache_off_for_the_existing_suite():
    """Keep the page cache off for the general suite.

    The pdf_images page cache is on by default in the product. The general tests
    assume no cache: with it on, a conversion test would fill the cache and a
    later one could read it back, making disk usage and timing depend on test
    order. tests/test_page_resume.py is the suite that exercises the cache;
    it turns it back on explicitly and pins its own CHAMO_PAGE_CACHE_DIR.
    """
    previous = os.environ.get("CHAMO_PAGE_CACHE")
    previous_dir = os.environ.get("CHAMO_PAGE_CACHE_DIR")
    os.environ["CHAMO_PAGE_CACHE"] = "0"
    os.environ.pop("CHAMO_PAGE_CACHE_DIR", None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CHAMO_PAGE_CACHE", None)
        else:
            os.environ["CHAMO_PAGE_CACHE"] = previous
        if previous_dir is not None:
            os.environ["CHAMO_PAGE_CACHE_DIR"] = previous_dir
