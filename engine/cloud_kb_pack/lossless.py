"""Lossless entropy compression; no resizing, quantization, or metadata stripping."""
import hashlib
import mmap
from pathlib import Path
import shutil
import struct
import subprocess
import zlib


def chunks(path):
    with path.open("rb") as handle:
        if handle.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("PNG 签名错误")
        while header := handle.read(8):
            if len(header) != 8:
                raise ValueError("PNG chunk header 损坏")
            length, kind = struct.unpack(">I4s", header)
            offset = handle.tell()
            handle.seek(length + 4, 1)
            if handle.tell() > path.stat().st_size:
                raise ValueError("PNG chunk 截断")
            yield kind, offset, length
            if kind == b"IEND":
                break


def png(source: Path, work: Path):
    compressed = work / "idat.zlib"
    decompress = zlib.decompressobj()
    compress = zlib.compressobj(level=9)
    original_raw = hashlib.sha256()
    with source.open("rb") as src, compressed.open("wb") as dest:
        for kind, offset, length in chunks(source):
            if kind == b"dSIG":
                raise ValueError("签名 PNG 不在改变文件编码的情况下压缩，保留原件")
            if kind != b"IDAT":
                continue
            src.seek(offset)
            remaining = length
            while remaining:
                data = src.read(min(65536, remaining))
                remaining -= len(data)
                while data:
                    raw = decompress.decompress(data, 65536)
                    data = decompress.unconsumed_tail
                    original_raw.update(raw)
                    dest.write(compress.compress(raw))
        if not decompress.eof or decompress.unused_data:
            raise ValueError("PNG IDAT 压缩流损坏或存在附加数据")
        dest.write(compress.flush())
    target = work / "lossless.png"
    wrote = False
    with source.open("rb") as src, target.open("wb") as dest:
        dest.write(b"\x89PNG\r\n\x1a\n")
        for kind, offset, length in chunks(source):
            if kind == b"IDAT":
                if wrote:
                    continue
                size = compressed.stat().st_size
                if size > 2**31 - 1:
                    raise ValueError("PNG 单个压缩块仍过大")
                dest.write(struct.pack(">I4s", size, b"IDAT"))
                crc = zlib.crc32(b"IDAT")
                with compressed.open("rb") as handle:
                    while data := handle.read(65536):
                        dest.write(data)
                        crc = zlib.crc32(data, crc)
                dest.write(struct.pack(">I", crc))
                wrote = True
            else:
                # Copy every original ancillary chunk and CRC, including ICC/EXIF/XMP.
                src.seek(offset - 8)
                remaining = length + 12
                while remaining:
                    data = src.read(min(65536, remaining))
                    dest.write(data)
                    remaining -= len(data)
        # Bytes after IEND can contain original auxiliary data; retain them too.
        shutil.copyfileobj(src, dest, length=65536)
    check = hashlib.sha256()
    decode = zlib.decompressobj()
    with compressed.open("rb") as handle:
        while data := handle.read(65536):
            while data:
                raw = decode.decompress(data, 65536)
                data = decode.unconsumed_tail
                check.update(raw)
    if check.digest() != original_raw.digest() or not decode.eof:
        raise ValueError("PNG 解压后的像素扫描数据完整性验证失败")
    return target, "png_idat_scanlines_identical_all_ancillary_chunks_preserved"


def jpeg_markers(path):
    records = []
    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            raise ValueError("JPEG 签名错误")
        while True:
            if handle.read(1) != b"\xff":
                raise ValueError("JPEG 标记损坏")
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if marker in {b"\xda", b"\xd9"}:
                break
            length = struct.unpack(">H", handle.read(2))[0]
            data = handle.read(length - 2)
            if len(data) != length - 2:
                raise ValueError("JPEG 标记截断")
            if marker == b"\xfe" or 0xE0 <= marker[0] <= 0xEF:
                records.append((marker, data))
    return records


def baseline_single_scan(path):
    with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        position = 2
        scans = 0
        entropy = False
        while position < len(data):
            if entropy:
                position = data.find(b"\xff", position)
                if position < 0:
                    break
            if data[position] != 255:
                raise ValueError("JPEG 标记格式错误")
            while position < len(data) and data[position] == 255:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker == 0 or 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xD9:
                if position != len(data) or scans != 1:
                    raise ValueError("JPEG 有额外数据或多个扫描，不能确认全部信息保留")
                return
            if entropy:
                raise ValueError("JPEG 扫描后有额外元数据，不能确认全部信息保留")
            if marker in {0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                raise ValueError("JPEG 非基线单扫描编码，暂不做无损改写")
            length = int.from_bytes(data[position:position+2], "big")
            if length < 2 or position+length > len(data):
                raise ValueError("JPEG 标记截断")
            position += length
            if marker == 0xDA:
                scans += 1
                entropy = True
        raise ValueError("JPEG 缺少完整结束标记")


def jpeg(source: Path, work: Path):
    command = shutil.which("jpegtran")
    if not command:
        raise ValueError("JPEG 无损压缩需要 jpegtran；禁止使用有损重编码替代")
    baseline_single_scan(source)
    target = work / "lossless.jpg"
    subprocess.run([command, "-copy", "all", "-optimize", "-outfile", str(target), str(source)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
    if sorted(jpeg_markers(source)) != sorted(jpeg_markers(target)):
        raise ValueError("JPEG 压缩前后 EXIF/ICC/APP/注释不一致，保留原件")
    from PIL import Image
    with Image.open(source) as a, Image.open(target) as b:
        if a.size != b.size or a.mode != b.mode or a.tobytes() != b.tobytes():
            raise ValueError("JPEG 压缩前后解码像素不一致，保留原件")
    return target, "jpeg_pixels_and_all_pre_scan_app_comment_markers_identical"
