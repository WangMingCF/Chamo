#!/usr/bin/env python3
"""Command-line front end for the conversion engine.

The GUI drives the engine through a JSON request file. That interface is fine for
a bundled app but useless for scripting, CI and the Linux/Windows builds that have
no GUI yet, so this module is the supported command line: the same engine, the
same rules, the same verification, one flag per decision.

Design notes:
  * the CLI builds the same request object the GUI builds, then runs the same
    supervisor, so behaviour cannot drift between the two front ends;
  * --json emits the raw protocol stream for machines; the default output is
    human-readable and does not require parsing;
  * exit codes are stable: 0 success, 1 some files failed, 2 bad usage,
    3 cancelled, 4 protocol/internal error;
  * nothing is written outside the requested output directory.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import signal
import tempfile
import threading
from pathlib import Path

# Run as a script under "python -I" (isolated mode), so the script directory is
# not on sys.path and the sibling modules would be unimportable. Add it explicitly;
# this is what lets the CLI work from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Same reason as the engine: the CLI prints events that contain Chinese paths and
# messages. On a Windows console sys.stdout is a legacy code page, so printing
# them raises UnicodeEncodeError inside the CLI itself. The engine was fixed for
# this; the front end needs the same treatment, which CI caught here.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import protocol  # noqa: E402

ENGINE = Path(__file__).resolve().parent / "workbench.py"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3
EXIT_INTERNAL = 4

OPERATIONS = {
    "pdf-images": "pdf_images",
    "images-pdf": "images_pdf",
    "pdf-split": "pdf_split",
    "pdf-merge": "pdf_merge",
    "office-pdf": "office_pdf",
    "office-images": "office_images",
    "image-convert": "image_convert",
    "image-compress": "image_compress",
    "pdf-compress": "pdf_compress",
    "rag": "rag",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chamo",
        description="本地文件转换：PDF、Office、图片与 RAG 前置处理（不上传任何文件）",
        epilog="本机界面（选文件→配置→转换→进度/取消→打开结果）：chamo ui",
    )
    parser.add_argument("inputs", nargs="+", help="输入文件或文件夹")
    parser.add_argument("-o", "--output", required=True, help="输出目录")
    parser.add_argument("-t", "--operation", required=True, choices=sorted(OPERATIONS),
                        help="转换类型")
    # None means "not given": image-compress --lossy defaults to JPEG (the
    # browser page does the same), everything else keeps the historical PNG.
    parser.add_argument("--format", default=None,
                        help="图片输出格式：png/jpeg/tiff/bmp/webp（默认 png；图片有损压缩默认 jpeg）")
    # None means "not given on the command line". pdf_compress needs its own
    # documented defaults (150 dpi / JPEG 75 for the lossy mode) while every other
    # operation keeps the historical 150/90, and an explicit --dpi/--quality
    # without --lossy has to be refused instead of silently ignored.
    parser.add_argument("--dpi", type=int, default=None,
                        help="渲染 DPI，36-600（默认 150；PDF 压缩 --lossy 档 72-600）")
    parser.add_argument("--quality", type=int, default=None,
                        help="JPEG/WebP 质量，1-100（默认 90；PDF 压缩 --lossy 档默认 75）")
    parser.add_argument("--range", dest="pages", default="", help="页码范围，如 1-5,8")
    parser.add_argument("--paper", default="original", choices=["original", "a4", "a3", "letter"])
    parser.add_argument("--margin-mm", type=int, default=10, help="图片合成 PDF 的页边距")
    parser.add_argument("--pdf-pages", type=int, default=100, help="严格拆分/ RAG 的每份页数上限")
    parser.add_argument("--pdf-bytes", type=int, default=100_000_000, help="每份字节上限")
    parser.add_argument("--workers", type=int, default=0, help="并发工作进程数，0 为自动")
    parser.add_argument("--lossy", action="store_true",
                        help="允许有损处理（需主动开启；pdf-compress 有损档把每页渲染成 JPEG，文字层不再保留）")
    parser.add_argument("--max-edge", type=int, default=None,
                        help="最长边像素，0 为不缩放（只用于有损压缩）")
    parser.add_argument("--shard", default="auto", choices=["auto", "off"],
                        help="分片渲染：auto 按内容自动决定，off 完全关闭（默认 auto）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSONL 事件流")
    parser.add_argument("--quiet", action="store_true", help="只输出错误")
    parser.add_argument("--version", action="version",
                        version=f"chamo protocol {protocol.PROTOCOL}")
    return parser


def build_request(args) -> dict:
    """Build the same request shape the GUI sends."""
    operation = OPERATIONS[args.operation]
    # pdf_compress documents 150 dpi / JPEG 75 for its lossy mode; image
    # operations keep 150/90. Only an explicit flag overrides either default.
    dpi = args.dpi if args.dpi is not None else 150
    quality = args.quality if args.quality is not None else (75 if operation == "pdf_compress" else 90)
    # An image-compression request with --lossy but no --format means JPEG: the
    # lossy mode used to produce a PNG and silently ignore --quality, which looked
    # configured but changed nothing. The browser page already fixes JPEG here.
    if args.format is not None:
        fmt = str(args.format).lower()
    elif operation == "image_compress" and args.lossy:
        fmt = "jpeg"
    else:
        fmt = "png"
    max_edge = args.max_edge if args.max_edge is not None else 0
    config = {
        "operation": operation,
        "format": fmt,
        "dpi": dpi,
        "quality": quality,
        "range": args.pages,
        "paper": args.paper,
        "margin_mm": args.margin_mm,
        "pdf_pages": args.pdf_pages,
        "pdf_bytes": args.pdf_bytes,
        "workers": args.workers,
        "lossy": args.lossy,
        "max_edge": max_edge,
        "naming": "clean",
        "shard": args.shard,
    }
    # quality has no meaning outside JPEG/WebP: do not send a number the engine
    # would have to ignore. (The engine still refuses an explicit quality from any
    # other front end for a non-JPEG/WebP lossy image-compression request.)
    if operation == "image_compress" and args.lossy and fmt not in ("jpeg", "webp"):
        config.pop("quality", None)
    renderer = None
    try:
        import platform_office
        renderer = platform_office.find(ENGINE.parent)
    except Exception:
        renderer = None
    if renderer:
        config["soffice"] = renderer
    return {
        "protocol": protocol.PROTOCOL,
        "inputs": args.inputs,
        "output": args.output,
        "config": config,
    }


# Set when Ctrl-C arrived, so main can return EXIT_CANCELLED instead of 130.
INTERRUPTED = []


def _interrupt(proc) -> None:
    """Stop the conversion tree, then let the caller unwind."""
    INTERRUPTED.append(True)
    try:
        # The engine terminates its own workers when it receives this; signalling
        # the group would also kill a terminal that shares it, so signal the pid.
        proc.terminate()
    except Exception:
        pass


def run(request: dict):
    """Run the engine and yield its events.

    The request file goes to a temporary directory, not the output directory: a
    crash between writing it and unlinking it would otherwise leave an engine
    artefact in the folder the user asked to receive results in.

    stderr is drained on a thread. Reading stdout to completion while the child
    fills an unread stderr pipe would block it once the 64 KB buffer filled, so a
    long traceback could hang the run instead of being reported.
    """
    with tempfile.TemporaryDirectory(prefix="chamo-") as scratch:
        request_path = Path(scratch) / "request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", str(ENGINE), "--request", str(request_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        # Ctrl-C must take the whole conversion down. Without this the engine and
        # its workers keep writing into the output folder after the CLI has exited,
        # which looks like a hang to the user and leaves half-finished files.
        previous = None
        try:
            previous = signal.signal(signal.SIGINT, lambda *_: _interrupt(proc))
            # SIGTERM too: a script or a supervisor that stops the CLI this way
            # must also stop the conversion, not leave it writing.
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, lambda *_: _interrupt(proc))
        except (ValueError, OSError, AttributeError):
            previous = None
        captured = []
        reader = threading.Thread(
            target=lambda: captured.append(proc.stderr.read()), daemon=True)
        reader.start()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue
        proc.wait()
        # The Ctrl-C/SIGTERM handler intentionally stays installed for the rest of
        # this short-lived command. Restoring the default handler here left a
        # millisecond window where a late signal raised KeyboardInterrupt after the
        # engine had already finished: the CLI exited 3 while the complete products
        # stayed on disk (indistinguishable from a cancelled run with residue).
        reader.join(timeout=5)
        stderr_text = (captured[0] if captured else "").strip()
        if stderr_text and proc.returncode != 0:
            # The engine reports protocol errors as events; a traceback on stderr
            # means it died before it could, so the user must see it. It carries
            # the protocol version like every other event, and the caller sends it
            # to stderr so a --json consumer never has to filter prose out of the
            # machine-readable stream.
            yield {"protocol": protocol.PROTOCOL, "type": "stderr",
                   "detail": stderr_text[-4000:]}


def is_ui_command(argv) -> bool:
    """"chamo ui" is a mode; "chamo ui -o out -t pdf-images" is a conversion of a file named ui.

    Dispatching on argv[0] alone would make a folder called "ui" impossible to
    convert - not hypothetical, "ui" is a normal folder name in a front-end project.
    So the mode is only claimed when no conversion flag is present.

    argparse also accepts the attached short forms (-oout, -tpdf-images), so
    matching only the separated spellings would let a real conversion be mistaken
    for the mode.
    """
    if not argv or argv[0] != "ui":
        return False
    for argument in argv[1:]:
        if argument in ("-o", "--output", "-t", "--operation"):
            return False
        if argument.startswith("--output=") or argument.startswith("--operation="):
            return False
        if len(argument) > 2 and not argument.startswith("--") and argument[:2] in ("-o", "-t"):
            return False
    return True


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Dispatch before argparse: the conversion parser requires inputs and --output,
    # and making the UI satisfy those would mean inventing a request it does not have.
    if is_ui_command(argv):
        import ui
        return ui.main(argv[1:])
    args = build_parser().parse_args(argv)
    # --dpi/--quality are decisions of the pdf_compress lossy mode only. Refusing
    # them before any conversion starts is the honest behaviour: silently ignoring
    # a flag the user typed on a lossless run would be a lie about what happened.
    if args.operation == "pdf-compress" and not args.lossy:
        for flag, given in (("--dpi", args.dpi), ("--quality", args.quality)):
            if given is not None:
                print(f"用法错误：{flag} 只用于有损档（--lossy）；无损压缩不接受该参数。",
                      file=sys.stderr)
                return EXIT_USAGE
    # image_compress: the flags must describe what will actually happen. --quality
    # and --max-edge only exist in the lossy mode, and quality only for the two
    # encoders that use it (JPEG, WebP); a lossy PNG used to accept --quality and
    # silently drop it.
    if args.operation == "image-compress":
        fmt = str(args.format).lower() if args.format is not None else ("jpeg" if args.lossy else "png")
        if not args.lossy:
            for flag, given in (("--quality", args.quality),
                                ("--max-edge", args.max_edge if (args.max_edge or 0) > 0 else None)):
                if given is not None:
                    print(f"用法错误：{flag} 只用于有损压缩（--lossy）；无损优化不接受该参数。",
                          file=sys.stderr)
                    return EXIT_USAGE
        elif args.quality is not None and fmt not in ("jpeg", "webp"):
            print("用法错误：--quality 仅对 JPEG / WebP 生效；有损压缩请使用 --format jpeg 或 webp"
                  f"（当前格式 {fmt}），或改用无损优化。", file=sys.stderr)
            return EXIT_USAGE
    request = build_request(args)
    try:
        protocol.check_request(request)
    except protocol.ProtocolError as error:
        print(f"请求无效：{error.message}", file=sys.stderr)
        return EXIT_USAGE

    # Catch a mistyped path here instead of letting the engine report it as an
    # internal failure. The engine cannot distinguish "this path does not exist"
    # from "this folder holds nothing convertible", so it answers 4; for someone
    # typing a command a wrong path is a usage error, and 2 is the documented code.
    missing = [item for item in args.inputs if not Path(item).expanduser().exists()]
    if missing:
        for item in missing:
            print(f"找不到输入：{item}", file=sys.stderr)
        return EXIT_USAGE

    final = None
    for event in run(request):
        if event.get("type") == "stderr":
            # Always stderr, even with --json: a traceback is not an event and
            # must not appear in the machine-readable stream.
            print(event["detail"], file=sys.stderr)
        elif args.json:
            line = json.dumps(event, ensure_ascii=False)
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                sys.stdout.buffer.write((line + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()
        elif not args.quiet:
            kind = event.get("type")
            if kind == "scan":
                print(f"待处理 {event.get('total')} 项" + (
                    f"，排除音视频 {event.get('excluded')}" if event.get("excluded") else ""))
            elif kind == "progress":
                print(f"  进度 {event.get('done')}/{event.get('total')}", end="\r", flush=True)
            elif kind == "item" and event.get("state") in ("complete", "resumed", "failed", "cancelled"):
                state = {"complete": "完成", "resumed": "复用", "failed": "失败",
                         "cancelled": "已取消"}[event["state"]]
                detail = event.get("detail") or ""
                print(f"  {state}  {event.get('source', '')}" + (f"  {detail}" if detail else ""))
        if event.get("type") in ("finished", "fatal"):
            final = event

    if final is None:
        print("引擎没有返回结果", file=sys.stderr)
        return EXIT_INTERNAL
    if final.get("type") == "fatal":
        print(f"失败：{final.get('detail')}", file=sys.stderr)
        # A request the engine rejects for its contents is a usage error, not an
        # internal failure: the caller passed --dpi 1000, not a broken engine.
        if final.get("code") in ("invalid_request", "unknown_field",
                                 "unsupported_protocol"):
            return EXIT_USAGE
        return EXIT_INTERNAL
    if not args.json and not args.quiet:
        print(f"\n成功 {final.get('success')} / 异常 {final.get('failed')} / 成品 {final.get('outputs')}"
              f"，用时 {final.get('seconds')} 秒")
        print(f"报告：{final.get('report')}")
    if final.get("state") == "cancelled":
        if not args.quiet:
            print("任务已取消；已完成的成品保留，可重新运行继续", file=sys.stderr)
        return EXIT_CANCELLED
    if INTERRUPTED:
        # The signal arrived after the engine had already finished (finished state
        # is complete / complete_with_exceptions). The run is not cancelled: its
        # report and its files agree, so report success and say what happened.
        # Reporting "cancelled" here used to keep the complete products on disk
        # while returning exit code 3, which no caller could tell apart from a
        # half-finished cancelled run.
        if not args.quiet:
            print("取消信号到达前转换已完成；成品已保留", file=sys.stderr)
        return EXIT_PARTIAL if final.get("failed") else EXIT_OK
    return EXIT_PARTIAL if final.get("failed") else EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl-C before the child existed; still a cancellation, not a crash.
        sys.exit(EXIT_CANCELLED)
