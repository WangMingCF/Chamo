import json
import math
import csv
from pathlib import Path
import shutil
import subprocess

from .output import Sink


def executable(name):
    path = shutil.which(name)
    if not path:
        raise ValueError(f"音视频处理需要 {name}，请先安装 FFmpeg")
    return path


def probe(source: Path):
    result = subprocess.run([executable("ffprobe"), "-v", "error", "-show_format", "-show_streams",
                             "-of", "json", str(source)], capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError("ffprobe 无法读取音视频")
    info = json.loads(result.stdout)
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, ValueError):
        raise ValueError("音视频缺少有效时长，可能未生成完整帧") from None
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("音视频时长无效")
    kinds = {stream["codec_type"] for stream in info["streams"]}
    return {"duration": duration, "audio": "audio" in kinds, "video": "video" in kinds}


def pack(source: Path, work: Path, sink: Sink, options: dict, category: str):
    info = probe(source)
    duration = info["duration"]
    cap = getattr(sink.limits, f"{category}_bytes")
    if source.stat().st_size <= cap:
        sink.emit(source, category, {"time_start": 0, "time_end": duration, "operation": "copy"})
        return
    ffmpeg = executable("ffmpeg")

    def emit_range(start, end):
        sink.notice("需要重编码的片段从原始文件导出 MP4/H.264/AAC 或 M4A/AAC；有损，只保留主视频及第一音轨")
        candidate = work / ("media.mp4" if category == "video" else "media.m4a")
        # Re-encode from the original for each range; stream-copy cuts at keyframes can overlap.
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.9f}", "-i", str(source), "-t", f"{end - start:.9f}"]
        if category == "video":
            args += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "fast",
                     "-crf", "23", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "128k"]
        else:
            args += ["-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "128k"]
        args += ["-movflags", "+faststart", str(candidate)]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=options["timeout"])
        if result.returncode:
            raise ValueError("FFmpeg 重编码失败，请检查编码器/输入格式")
        if candidate.stat().st_size <= cap:
            sink.emit(candidate, category, {"time_start": start, "time_end": end, "operation": "transcode"})
        elif end - start > 0.25:
            middle = (start + end) / 2
            emit_range(start, middle)
            emit_range(middle, end)
        else:
            raise ValueError("最短音视频片段仍超限，请提高大小限制")

    # Use source bitrate only as an initial estimate; actual encoded bytes decide compliance.
    window = min(options["media_seconds"], max(0.25, duration * cap / source.stat().st_size * 0.8))
    if options.get("media_mode", "auto") == "auto":
        segmented = work / "segments"
        segmented.mkdir(exist_ok=True)
        listing = segmented / "ranges.csv"
        extension = "mp4" if category == "video" else "m4a"
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
        args += ["-map", "0:v:0", "-map", "0:a:0?"] if category == "video" else ["-map", "0:a:0"]
        args += ["-c", "copy", "-f", "segment", "-segment_time", str(window),
                 "-reset_timestamps", "1", "-segment_list", str(listing),
                 "-segment_list_type", "csv", str(segmented / f"part%09d.{extension}")]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=options["timeout"])
        if not result.returncode and listing.is_file():
            emitted = False
            with listing.open(newline="") as handle:
                for name, start, end in csv.reader(handle):
                    candidate = segmented / Path(name).name
                    if candidate.stat().st_size <= cap:
                        sink.emit(candidate, category, {"time_start": float(start), "time_end": float(end), "operation": "remux"})
                    else:
                        emit_range(max(0, float(start)), min(duration, float(end)))
                    candidate.unlink()
                    emitted = True
            if emitted:
                sink.notice("无损分段按关键帧边界进行；时间范围来自 FFmpeg 索引，字幕/附件/额外音轨不保留")
                return
        sink.notice("输入编码无法封装为 MP4/M4A 或无损分段失败，改用重编码")
    # Equal ranges avoid a near-zero tail (shorter than a frame) and floating-point drift.
    count = math.ceil(duration / window)
    for index in range(count):
        emit_range(duration * index / count, duration * (index + 1) / count)
