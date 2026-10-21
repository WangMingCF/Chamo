"""Persistent output layout, independent from immutable source-file provenance."""
import json
from pathlib import Path


def checked_relative(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('布局路径必须是安全的相对路径')
    return path


def remap(value, prefixes):
    path = checked_relative(value)
    for old in sorted(prefixes, key=lambda p: len(Path(p).parts), reverse=True):
        before = checked_relative(old)
        if path.is_relative_to(before):
            return checked_relative(prefixes[old]) / path.relative_to(before)
    return path


def load(root):
    path = root / '_处理记录/output-layout.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    prefixes = data['prefixes']
    for old, new in prefixes.items():
        checked_relative(old); checked_relative(new)
        if Path(old) == Path('.'):
            raise ValueError('布局源前缀不能是根目录')
    return prefixes
