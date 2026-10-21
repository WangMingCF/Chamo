import math
from pathlib import Path

from PIL import Image, ImageOps

from .output import Sink


def fit(image: Image.Image, sink: Sink) -> Image.Image:
    limits = sink.limits
    image = ImageOps.exif_transpose(image)
    # Composite transparency onto white, preserving all visible content for JPEG uploads.
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    background.alpha_composite(rgba)
    image = background.convert("RGB")
    w, h = image.size
    scale = min(1.0, limits.image_max_edge / max(w, h))
    if scale < 1:
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    w, h = image.size
    # Pad extreme aspect ratios rather than crop or distort source content.
    short = max(limits.image_min_edge, math.floor(max(w, h) / limits.image_max_ratio) + 1)
    if min(w, h) < short:
        sink.notice("不满足短边/长宽比的图片使用白边补齐；没有裁掉原图内容")
        image = ImageOps.pad(image, (max(w, short), max(h, short)), color="white", method=Image.Resampling.LANCZOS)
    return image


def encode(image: Image.Image, work: Path, sink: Sink, locator: dict):
    image = fit(image, sink)
    path = work / "image.jpg"
    for _ in range(48):
        for quality in (92, 85, 75, 65, 50, 35):
            image.save(path, "JPEG", quality=quality, optimize=True)
            if path.stat().st_size <= sink.limits.image_bytes:
                sink.emit(path, "image", {**locator, "jpeg_quality": quality})
                return
        w, h = image.size
        if w == h == sink.limits.image_min_edge:
            break
        scale = min(0.8, math.sqrt(sink.limits.image_bytes / path.stat().st_size) * 0.9)
        image = image.resize((max(sink.limits.image_min_edge, int(w * scale)),
                              max(sink.limits.image_min_edge, int(h * scale))), Image.Resampling.LANCZOS)
    raise ValueError("图片在最小尺寸和最低质量下仍超限，请增加 image_bytes")


def pack(source: Path, work: Path, sink: Sink, options: dict):
    # Keep Pillow's decompression-bomb checks: source size alone cannot bound decoded RAM.
    with Image.open(source) as image:
        frames = getattr(image, "n_frames", 1)
        w, h = image.size
        limits = sink.limits
        compatible = source.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif"}
        if frames == 1 and compatible and source.stat().st_size <= limits.image_bytes and min(w, h) >= limits.image_min_edge and max(w, h) <= limits.image_max_edge and max(w, h) / min(w, h) < limits.image_max_ratio:
            sink.emit(source, "image", {"frame": 1, "operation": "copy"})
            return
        sink.notice("需压缩的图片导出为 JPEG，压缩有损，透明背景合成为白色；多帧图片逐帧导出")
        for index in range(frames):
            image.seek(index)
            encode(image.copy(), work, sink, {"frame": index + 1, "operation": "compress"})
