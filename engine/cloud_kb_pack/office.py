"""Path-based adaptation of WeKnora docreader/parser/excel_convert.py.

Upstream commit: 3e22822c1d2397be730c458c7539d0eb8c113a24 (Tencent, MIT).
Reuse its LibreOffice discovery and isolated-profile conversion approach.
Unlike the upstream bytes interface, conversions stay on disk.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def find_soffice(explicit: str | None = None) -> str:
    candidates = [explicit, os.environ.get("KB_PACK_SOFFICE"), shutil.which("soffice"),
                  "/usr/bin/soffice", "/usr/lib/libreoffice/program/soffice",
                  "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                  r"C:\Program Files\LibreOffice\program\soffice.exe",
                  r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise ValueError("Office 转换需要 LibreOffice，请安装后设置 --soffice 或 KB_PACK_SOFFICE")


def convert(source: Path, directory: Path, target: str, soffice: str | None, timeout: int) -> Path:
    executable = find_soffice(soffice)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kb-pack-lo-") as profile:
        # Separate profile avoids attaching to, or modifying, a user's open Office session.
        args = [executable, f"-env:UserInstallation={Path(profile).as_uri()}",
                "--headless", "--nologo", "--nodefault", "--norestore",
                "--convert-to", target, "--outdir", str(directory), str(source)]
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    expected = directory / f"{source.stem}.{target}"
    if result.returncode or not expected.is_file() or not expected.stat().st_size:
        # Some LibreOffice versions return success even when conversion fails.
        raise ValueError(f"LibreOffice 未生成有效 {target} 文件（退出码 {result.returncode}）")
    return expected
