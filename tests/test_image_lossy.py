"""Acceptance gates for image-compress lossy parameter semantics.

Why this file exists
--------------------
image_compress has two modes and one promise: the parameters the user typed must
describe what actually happens. A regression once ignored two of them on a
lossy run: with no --format the product was a PNG while "lossy" was on, and
--quality was accepted for PNG but never used. A user who asked for a smaller
JPEG got an unchanged PNG and a silent success. These gates make that red:

  * lossy without --format must produce a real JPEG that opens and is smaller;
  * lossy WebP + quality is accepted and encodes with that quality (the native "极致压缩" preset is WebP 70 "1600");
  * lossy --format png --quality is refused before conversion (exit 2, no product);
  * max_edge still works for a PNG lossy request (it is a resize, not an
    encoder knob), so quality is not required to get it;
  * the lossless mode still refuses quality / max_edge instead of ignoring them.

Negative control: point CHAMO_TEST_ENGINE_DIR at an engine without these
parameter checks and run

    CHAMO_TEST_ENGINE_DIR=/tmp/chamo-engine-under-test/engine \
        ./third_party/python/bin/python3 -m pytest tests/test_image_lossy.py -q

expect the four parameter gates to fail with the old behaviour (PNG product,
quality silently accepted) while the max_edge test may already pass.

Environment:
    CHAMO_TEST_ENGINE_DIR   engine directory under test (default: repo engine/)

Run:
    ./third_party/python/bin/python3 -m pytest tests/test_image_lossy.py -q
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENGINE = ROOT / "engine"
TIMEOUT = 300


def engine_dir() -> Path:
    override = os.environ.get("CHAMO_TEST_ENGINE_DIR")
    return Path(override).resolve() if override else DEFAULT_ENGINE.resolve()


def _environment(state: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["CHAMO_STATE_DIR"] = str(state)
    # The page cache belongs to pdf_images; keep this file independent of it.
    env["CHAMO_PAGE_CACHE"] = "0"
    env.pop("CHAMO_PAGE_CACHE_DIR", None)
    return env


def run_cli(tmp_path: Path, *arguments) -> subprocess.CompletedProcess:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-I", "-B", str(engine_dir() / "chamo.py"),
               *[str(item) for item in arguments], "--json"]
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL,
                          env=_environment(state), timeout=TIMEOUT)


def events(result: subprocess.CompletedProcess) -> list:
    parsed = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed.append(json.loads(line))
        except ValueError:
            continue
    return parsed


def finished(result: subprocess.CompletedProcess) -> dict:
    return next((event for event in reversed(events(result))
                 if event.get("type") == "finished"), {})


def report_of(result: subprocess.CompletedProcess) -> dict:
    scan = next((event for event in events(result) if event.get("type") == "scan"), {})
    record = scan.get("report")
    assert record, "the run emitted no scan event with a record directory: %r" % result.stdout[-800:]
    return json.loads((Path(record) / "report.json").read_text(encoding="utf-8"))


def run_engine_request(tmp_path: Path, inputs, output: Path,
                       config: dict) -> subprocess.CompletedProcess:
    """Send the request exactly like the app does: protocol envelope + --request."""
    state = tmp_path / "state-request"
    state.mkdir(parents=True, exist_ok=True)
    request = {"protocol": 1, "inputs": [str(item) for item in inputs],
               "output": str(output), "config": config}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    command = [sys.executable, "-I", "-B", str(engine_dir() / "workbench.py"),
               "--request", str(request_path)]
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL,
                          env=_environment(state), timeout=TIMEOUT)


def fatal_event(result: subprocess.CompletedProcess) -> dict:
    return next((event for event in reversed(events(result))
                 if event.get("type") == "fatal"), {})


def products(out: Path) -> dict:
    if not out.exists():
        return {}
    return {path.relative_to(out).as_posix(): path
            for path in out.rglob("*")
            if path.is_file() and not path.relative_to(out).as_posix().startswith(".Chamo暂存/")}


def noisy_png(path: Path, size=(512, 512), seed: int = 7) -> Path:
    """Deterministic incompressible RGB noise: PNG is large, JPEG q70 much smaller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    image = Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))
    image.save(str(path), "PNG")
    image.close()
    return path


# --------------------------------------------------------------------------
# 1. lossy default: JPEG, not a silently unchanged PNG
# --------------------------------------------------------------------------
def test_lossy_without_format_produces_a_real_smaller_jpeg(tmp_path):
    source = noisy_png(tmp_path / "输入" / "噪声图片.png")
    out = tmp_path / "输出"
    result = run_cli(tmp_path, source, "-o", out, "-t", "image-compress",
                     "--lossy", "--quality", "70")
    assert result.returncode == 0, (
        "lossy image-compress must succeed: rc=%r stderr=%r"
        % (result.returncode, result.stderr[-800:]))
    final = finished(result)
    assert final.get("success") == 1 and final.get("failed") == 0, final

    published = products(out)
    assert list(published) == ["噪声图片.jpg"], (
        "lossy image-compress without --format must default to JPEG, got %r"
        % sorted(published))
    product = published["噪声图片.jpg"]
    with Image.open(product) as opened:
        opened.load()
        assert opened.format == "JPEG", "the default product is %r, not a JPEG" % opened.format
        assert opened.size == (512, 512), opened.size
    assert product.stat().st_size < source.stat().st_size, (
        "a noisy 512x512 PNG should be much larger than its JPEG q70: %d vs %d"
        % (source.stat().st_size, product.stat().st_size))


def test_explicit_jpeg_format_still_works(tmp_path):
    """The new default must not break the explicit spelling."""
    source = noisy_png(tmp_path / "输入" / "照片源.png")
    out = tmp_path / "输出"
    result = run_cli(tmp_path, source, "-o", out, "-t", "image-compress",
                     "--lossy", "--format", "jpeg", "--quality", "60")
    assert result.returncode == 0, result.stderr[-800:]
    assert list(products(out)) == ["照片源.jpg"], sorted(products(out))
    with Image.open(out / "照片源.jpg") as opened:
        opened.load()
        assert opened.format == "JPEG"


# --------------------------------------------------------------------------
# 2. quality for a non-JPEG lossy request is a usage error, never ignored
# --------------------------------------------------------------------------
def test_lossy_png_with_quality_is_refused_before_conversion(tmp_path):
    source = noisy_png(tmp_path / "输入" / "噪声图片.png")
    out = tmp_path / "输出"
    result = run_cli(tmp_path, source, "-o", out, "-t", "image-compress",
                     "--lossy", "--format", "png", "--quality", "70")
    text = result.stdout + "\n" + result.stderr
    assert result.returncode == 2, (
        "quality with --format png must be a usage error (exit 2), got %r; %r"
        % (result.returncode, text[-800:]))
    assert "JPEG" in text, "the refusal must name JPEG as the only quality format: %r" % text[-800:]
    assert "质量" in text or "quality" in text.lower(), (
        "the refusal must name the rejected parameter: %r" % text[-800:])
    assert products(out) == {}, "a refused request must not write a product: %r" % products(out)


def test_lossy_webp_with_quality_encodes_with_that_quality(tmp_path):
    """WebP is a lossy format too: quality is passed to Pillow's WebP encoder.

    A regression once restricted quality to JPEG, which broke the native
    "极致压缩" preset (WebP 70 / 1600). Two qualities must give different files,
    otherwise the parameter is accepted but still silently ignored.
    """
    source = noisy_png(tmp_path / "输入" / "噪声图片.png")
    sizes = {}
    for quality in (30, 90):
        out = tmp_path / ("输出-q%d" % quality)
        result = run_cli(tmp_path, source, "-o", out, "-t", "image-compress",
                         "--lossy", "--format", "webp", "--quality", str(quality))
        text = result.stdout + "\n" + result.stderr
        assert result.returncode == 0, (
            "WebP + quality must be accepted: q=%d rc=%r %r"
            % (quality, result.returncode, text[-800:]))
        published = products(out)
        assert list(published) == ["噪声图片.webp"], (
            "webp product naming: %r" % sorted(published))
        product = published["噪声图片.webp"]
        with Image.open(product) as opened:
            opened.load()
            assert opened.format == "WEBP", opened.format
            assert opened.size == (512, 512), opened.size
        assert product.stat().st_size < source.stat().st_size, (
            "WebP q%d should be smaller than the noisy source PNG" % quality)
        sizes[quality] = product.stat().st_size
        config = report_of(result)["results"][0]["config"]
        assert config["format"] == "webp" and config["quality"] == quality, config
    assert sizes[30] < sizes[90], (
        "quality had no effect on the WebP output sizes: %r" % sizes)


def test_native_webp_70_1600_request_succeeds(tmp_path):
    """The native "极致压缩" preset as the app sends it: webp/70/1600, lossy."""
    source = noisy_png(tmp_path / "输入" / "大图.png", size=(2000, 1500), seed=11)
    out = tmp_path / "输出"
    config = {"operation": "image_compress", "format": "webp", "lossy": True,
              "quality": 70, "max_edge": 1600, "naming": "clean", "workers": 1}
    result = run_engine_request(tmp_path, [source], out, config)
    final = next((event for event in reversed(events(result))
                  if event.get("type") == "finished"), {})
    assert result.returncode == 0 and final.get("state") == "complete" \
            and final.get("success") == 1, (
        "native-shaped WebP lossy request failed: rc=%r stderr=%r stdout=%r"
        % (result.returncode, result.stderr[-800:], result.stdout[-800:]))
    published = products(out)
    assert list(published) == ["大图.webp"], sorted(published)
    product = out / "大图.webp"
    with Image.open(product) as opened:
        opened.load()
        assert opened.format == "WEBP", opened.format
        assert max(opened.size) == 1600, (
            "max_edge=1600 was not applied: %r" % (opened.size,))
    assert product.stat().st_size < source.stat().st_size, (
        "the WebP preset product should be smaller than the 2000x1500 noise source")
    record = report_of(result)["results"][0]
    assert record["config"]["quality"] == 70, record["config"]
    assert record["config"]["max_edge"] == 1600, record["config"]


def test_native_png_quality_request_is_refused_as_invalid_request(tmp_path):
    """An app request with PNG + quality is rejected instead of ignoring quality."""
    source = noisy_png(tmp_path / "输入" / "噪声图片.png")
    out = tmp_path / "输出"
    config = {"operation": "image_compress", "format": "png", "lossy": True,
              "quality": 70, "naming": "clean", "workers": 1}
    result = run_engine_request(tmp_path, [source], out, config)
    fatal = fatal_event(result)
    assert result.returncode != 0 and fatal.get("code") == "invalid_request", (
        "png + quality must be rejected as invalid_request: rc=%r fatal=%r"
        % (result.returncode, fatal))
    detail = fatal.get("detail") or ""
    assert "JPEG" in detail and "WebP" in detail, (
        "the refusal must name both supported quality formats: %r" % detail[-800:])
    assert products(out) == {}, "an invalid request must not write a product"


# --------------------------------------------------------------------------
# 3. max_edge keeps working for a non-JPEG format
# --------------------------------------------------------------------------
def test_png_lossy_accepts_max_edge_without_quality(tmp_path):
    source = noisy_png(tmp_path / "输入" / "噪声图片.png")
    out = tmp_path / "输出"
    result = run_cli(tmp_path, source, "-o", out, "-t", "image-compress",
                     "--lossy", "--format", "png", "--max-edge", "128")
    assert result.returncode == 0, (
        "png + max_edge (no quality) must be allowed: rc=%r stderr=%r"
        % (result.returncode, result.stderr[-800:]))
    published = products(out)
    assert list(published) == ["噪声图片.png"], (
        "png lossy max_edge product name/format: %r" % sorted(published))
    with Image.open(out / "噪声图片.png") as opened:
        opened.load()
        assert opened.format == "PNG", opened.format
        assert max(opened.size) == 128, "max_edge did not apply: %r" % (opened.size,)


# --------------------------------------------------------------------------
# 4. the lossless mode still refuses lossy-only parameters
# --------------------------------------------------------------------------
def test_lossless_still_refuses_quality_and_max_edge(tmp_path):
    source = noisy_png(tmp_path / "输入" / "噪声图片.png")
    cases = (("--quality", "70"), ("--max-edge", "100"))
    for index, flags in enumerate(cases):
        out = tmp_path / ("输出-%d" % index)
        result = run_cli(tmp_path, source, "-o", out, "-t", "image-compress", *flags)
        text = result.stdout + "\n" + result.stderr
        assert result.returncode == 2, (
            "lossless image-compress with %r must be a usage error, got %r; %r"
            % (list(flags), result.returncode, text[-800:]))
        assert "无损" in text, "the refusal must say it is the lossless mode: %r" % text[-800:]
        assert products(out) == {}, "a refused lossless request wrote a product"
