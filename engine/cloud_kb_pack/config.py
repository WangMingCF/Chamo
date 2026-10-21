from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    # MB/GB use decimal units: conservative for providers that actually use MiB/GiB.
    image_bytes: int = 20_000_000
    image_min_edge: int = 16
    image_max_edge: int = 8191
    image_max_ratio: float = 50.0  # strictly less than this value
    table_bytes: int = 10_000_000
    table_rows: int = 100_000
    document_bytes: int = 150_000_000
    document_pages: int = 1000
    # PDF gets a separate conservative RAG cap; Office keeps its native constraints.
    pdf_bytes: int = 100_000_000
    pdf_pages: int = 100
    text_bytes: int = 10_000_000
    audio_bytes: int = 2_000_000_000
    video_bytes: int = 2_000_000_000
    batch_files: int = 50

    @property
    def pdf_byte_cap(self):
        return min(self.pdf_bytes, self.document_bytes)

    @property
    def pdf_page_cap(self):
        return min(self.pdf_pages, self.document_pages)

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "image_max_ratio":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 1:
                    raise ValueError("image_max_ratio 必须是大于 1 的有限数")
            elif isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if self.image_min_edge > self.image_max_edge:
            raise ValueError("image_min_edge 不能大于 image_max_edge")

    @classmethod
    def load(cls, path: Path | None):
        if path is None:
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置必须是 JSON 对象")
        return cls(**raw)

    def save(self, path: Path):
        with path.open("x", encoding="utf-8") as handle:
            json.dump(asdict(self), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
