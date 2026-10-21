"""The native GUI's image-compression payload must match the engine's rules.

The engine allows quality on lossy JPEG and WebP only, and refuses PNG + quality. The
macOS UI cannot be driven inside CI, so these are source-level assertions in the
same style as tests/test_cli.py::test_gui_keys_are_all_accepted: they pin the two
decisions that caused the regression (the lossy format list and whether config()
sends quality) plus the preset that made WebP a real path (极致压缩 = WebP 70 · 1600).

The payload itself is also captured from a real build in artifacts/native-lossy/.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = (ROOT / "Sources" / "Model.swift").read_text(encoding="utf-8")


def block(fragment: str) -> str:
    """The Swift declaration that starts at fragment, braces balanced."""
    start = MODEL.index(fragment)
    opening = MODEL.index("{", start)
    depth = 0
    for position in range(opening, len(MODEL)):
        char = MODEL[position]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return MODEL[start:position + 1]
    raise AssertionError("unbalanced braces after %r" % fragment)


def test_lossy_image_formats_are_jpeg_and_webp_only():
    """PNG in the lossy format list is the combination the engine rejects."""
    formats = block("var formatOptions")
    line = next(line for line in formats.splitlines() if 'case "image_compress"' in line)
    assert '"jpeg"' in line and '"webp"' in line, line
    assert '"png"' not in line, (
        "有损档不应把 PNG 作为输出格式（引擎拒绝 PNG+quality）：%r" % line)


def test_quality_is_sent_only_for_lossy_jpeg_or_webp():
    """The request must not carry quality for the lossless PNG path."""
    config = block("func config() -> [String: Any]")
    base = config[:config.index('if operation == "pdf_compress" {')]
    assert '"quality"' not in base, (
        "config() 的基础字典不能再无条件发送 quality：%r" % base)

    region = config[config.index('} else if operation == "image_compress" {'):]
    # Anchor on the newline so the 8-space outer "else" cannot match inside a
    # 12-space indented line (both strings are substrings of one another).
    lossy_start = region.index("\n            if lossy {") + 1
    lossless_marker = region.index("\n            } else {") + 1
    lossless_end = region.index("\n        } else {") + 1
    lossy = region[lossy_start:lossless_marker]
    lossless = region[lossless_marker:lossless_end]
    assert 'c["quality"] = quality' in lossy, lossy
    assert '["jpeg","webp"].contains(format)' in lossy, (
        "quality 必须限定在 JPEG/WebP：%r" % lossy)
    assert 'c["max_edge"] = maxEdge' in lossy, (
        "max_edge 对有损的任意格式都应发送：%r" % lossy)
    assert 'c["quality"]' not in lossless, (
        "无损档不能发送 quality：%r" % lossless)
    assert 'c["max_edge"]' not in lossless, (
        "无损档不能发送 max_edge（引擎拒绝无损缩放）：%r" % lossless)
    assert 'c["format"] = "png"' in lossless, (
        "无损档必须把格式钉为 PNG：%r" % lossless)


def test_extreme_preset_stays_webp_70_1600():
    """The 极致压缩 preset is WebP 70 · 1600 and must keep working."""
    assert 'Preset(id: "tiny", title: "极致压缩", detail: "WebP 70 · 1600px"' in MODEL
    assert ('apply: { $0.lossy = true; $0.format = "webp"; $0.quality = 70; $0.maxEdge = 1600 }'
            in MODEL), "极致压缩预设的参数被改了：它必须仍是 WebP 70 · 1600"
    assert ('matches: { $0.lossy && $0.format == "webp" && $0.quality == 70 && $0.maxEdge == 1600 }'
            in MODEL)


def test_blockers_refuse_impossible_combinations_before_start():
    """前置拒绝：有损非 JPEG/WebP，以及无损 + 缩放。"""
    blockers = block("var blockers: [String]")
    assert "有损压缩的输出格式需为 JPEG 或 WebP" in blockers, blockers
    assert '["jpeg","webp"].contains(format)' in blockers, blockers
    assert "无损优化不支持缩小尺寸" in blockers, blockers


def test_shows_quality_follows_the_same_jpeg_webp_rule():
    shows = block("var showsQuality: Bool")
    line = next(line for line in shows.splitlines() if 'case "image_compress"' in line)
    assert "lossy" in line and '["jpeg","webp"].contains(format)' in line, line
