"""Versioned task protocol shared by the GUI, the CLI and the tests.

The engine speaks JSONL: one request in, a stream of events out. Both sides must
agree on the shape, so the shape is versioned here instead of being implied by
whatever the current code happens to emit.

Compatibility rules:
  * every event carries "protocol" so a consumer can refuse a stream it does not
    understand instead of misreading it;
  * adding a field is backward compatible and does not bump the version;
  * removing or renaming a field, or changing its type or meaning, requires a
    new major version;
  * a request may carry the protocol it was written for; the engine rejects an
    unknown one rather than guessing.

Errors use stable codes so callers can branch on them without parsing Chinese
prose that may be reworded.
"""
from __future__ import annotations

PROTOCOL = 1

# Request fields the engine understands. Unknown keys are rejected so a typo in
# a caller does not silently do something different from what was asked.
REQUEST_FIELDS = {"protocol", "inputs", "output", "config", "gui_session",
                  "office_profile_root", "state_root"}

CONFIG_FIELDS = {
    "operation", "naming", "format", "dpi", "quality", "range", "paper",
    "margin_mm", "pdf_pages", "pdf_bytes", "image_bytes", "document_bytes",
    "text_bytes", "table_bytes", "table_rows", "batch_files", "workers",
    "lossy", "max_edge", "soffice", "timeout", "shard",
}

# Stable error codes. The Chinese message stays user-facing; the code is for
# programmatic handling and for tests that must not depend on wording.
ERROR_CODES = {
    "invalid_request": "请求格式无效",
    "unsupported_protocol": "协议版本不受支持",
    "unknown_field": "请求包含未知字段",
    "no_inputs": "没有可处理的文件",
    "output_unwritable": "输出目录不可写或空间不足",
    "missing_renderer": "缺少 Office 渲染引擎",
    "encrypted_pdf": "PDF 已加密",
    "unsupported_structure": "文档结构不受支持",
    "output_changed": "已完成输出被改变",
    "input_changed": "处理期间原件发生变化",
    "cancelled": "任务已取消",
    "internal": "内部错误",
}


class ProtocolError(Exception):
    """An error with a stable code plus a human-readable message."""

    def __init__(self, code: str, message: str = ""):
        self.code = code if code in ERROR_CODES else "internal"
        self.message = message or ERROR_CODES[self.code]
        super().__init__(self.message)

    def as_event(self) -> dict:
        return {"code": self.code, "detail": self.message}


def classify(error) -> str:
    """Map an engine exception to a stable error code.

    The engine raises ValueError with a Chinese message at dozens of sites. It
    matches on distinctive substrings rather than exact text so a reworded
    message keeps its code, and anything unmatched stays "internal" — which is
    the honest answer for an unexpected failure.
    """
    if isinstance(error, ProtocolError):
        return error.code
    text = str(error)
    for code, needles in (
        ("encrypted_pdf", ("已加密",)),
        ("missing_renderer", ("缺少 Office 渲染引擎", "未找到可执行")),
        ("output_unwritable", ("空间不足", "不足 2GB", "不可写")),
        ("output_changed", ("已完成输出被改变",)),
        ("input_changed", ("原件发生变化",)),
        ("unsupported_structure", ("不受支持", "暂不支持", "含表单", "含交互批注",
                                   "多帧", "不能验证保留")),
        ("no_inputs", ("没有可处理文件", "未生成输出文件", "请选择 Office")),
        # Raising these is the caller passing a bad value, not the engine failing.
        ("invalid_request", ("参数超出范围", "未知转换类型", "无效纸张类型",
                              "边距应为", "不支持的输出格式", "无效命名方式",
                              "严格拆分必须保留", "页码范围重复", "页码格式",
                              "页码超出范围", "无效分片模式")),
    ):
        if any(needle in text for needle in needles):
            return code
    return "internal"


def check_request(request) -> None:
    """Validate the envelope before any work starts."""
    if not isinstance(request, dict):
        raise ProtocolError("invalid_request", "请求必须是 JSON 对象")
    version = request.get("protocol")
    if version is not None and version != PROTOCOL:
        raise ProtocolError(
            "unsupported_protocol",
            f"协议版本 {version} 不受支持，本引擎支持 {PROTOCOL}",
        )
    unknown = set(request) - REQUEST_FIELDS
    if unknown:
        raise ProtocolError("unknown_field", f"未知请求字段：{sorted(unknown)}")
    config = request.get("config")
    if not isinstance(config, dict):
        raise ProtocolError("invalid_request", "缺少 config")
    unknown_config = set(config) - CONFIG_FIELDS
    if unknown_config:
        raise ProtocolError("unknown_field", f"未知配置字段：{sorted(unknown_config)}")
