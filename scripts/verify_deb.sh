#!/usr/bin/env bash
# Install the built Linux package and convert with it.
#
# Used by Linux CI and the release job that publishes the
# artifact, so there is one definition of "the package works". Each step prints a
# PASS/FAIL line, because a bare exit code does not say which promise broke.
#
# Expected layout: the .deb under release/ or dist/, the checkout at the working
# directory, and a system LibreOffice available.
set -u
export DEBIAN_FRONTEND=noninteractive
# The Docker wrapper mounts the checkout at /w; the CI job runs in the checkout
# itself and sets CHAMO_ROOT=$PWD.
ROOT=${CHAMO_ROOT:-/w}
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1 ($2)"; else echo "FAIL $1: got $2 want $3"; fail=1; fi; }

# Select the package matching config/release.json, never "some .deb I found".
# A wildcard sorted alphabetically puts 0.9.0 before 0.10.0, so a stale artifact
# sitting next to the new one was installed and verified while the run reported
# on the new version - the failure only showed up because the new flag was
# missing. Verifying the wrong file is worse than verifying nothing.
VERSION=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
          "$ROOT/config/release.json" | head -1)
if [ -z "$VERSION" ]; then echo "FAIL cannot read version from config/release.json"; exit 1; fi
# Same trap one level down: the release folder can hold an amd64 and an arm64 build
# of the same version, and "first alphabetically" installs amd64 on an arm64 host
# (dpkg then fails and every later check reads 127). Select the package built for
# the architecture we actually run on.
ARCH=$(dpkg --print-architecture 2>/dev/null | head -1)
if [ -n "$ARCH" ]; then
  DEB=$(ls release/chamo-cli_"$VERSION"_"$ARCH".deb \
           dist/chamo-cli_"$VERSION"_"$ARCH".deb 2>/dev/null | head -1)
else
  DEB=$(ls release/chamo-cli_"$VERSION"_*.deb dist/chamo-cli_"$VERSION"_*.deb 2>/dev/null | head -1)
fi
if [ -z "$DEB" ]; then
  echo "FAIL no .deb for version $VERSION on architecture ${ARCH:-unknown} (found: $(ls release/*.deb dist/*.deb 2>/dev/null | tr '\n' ' '))"
  exit 1
fi
echo "installing $(basename "$DEB") for version $VERSION on $ARCH"
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
$SUDO dpkg -i "$DEB" >/dev/null 2>&1; check "install" "$?" "0"
check "chamo on PATH" "$(command -v chamo)" "/usr/bin/chamo"
# The venv's C extensions are cpython-3X builds and bin/python3 is a symlink to the
# host interpreter, so the declared Depends must name the same minor version. A
# loose ">= 3.10" installed on a python3.11 host and then failed on first use with
# "ImportError: cannot import name '_imaging' from 'PIL'". Read both facts from the
# archive itself rather than from the build tree.
MINOR=$(dpkg-deb -c "$DEB" 2>/dev/null | sed -n 's#.*lib/python\(3\.[0-9]*\)/site-packages.*#\1#p' | head -1)
if [ -z "$MINOR" ]; then echo "FAIL cannot read the bundled Python version out of $DEB"; fail=1; fi
DEPENDS=$(dpkg-deb -f "$DEB" Depends 2>/dev/null)
check "declared Python minor matches the wheels (${MINOR:-unknown})" \
      "$(printf '%s' "$DEPENDS" | grep -c "python3 (>= ${MINOR:-none})")" "1"
T=$(mktemp -d)
chamo $ROOT/tests/fixtures/示例讲义.pdf -o "$T/pdf" -t pdf-images --range 1-3 --quiet 2>/dev/null
check "pdf to images" "$?" "0"
V=/opt/chamo/venv/bin/python3
# The package's own documents must name the version they were built for: a guide
# titled with an older version must not ship next to newer notes. Read them from the .deb
# itself: Debian/Ubuntu images commonly set path-exclude=/usr/share/doc/* in
# dpkg.cfg.d/excludes, so the installed copy can legitimately be absent even
# though the archive carries it - the shipped bytes are what must be checked.
DOCS=$T/deb-docs/usr/share/doc/chamo-cli
dpkg-deb -x "$DEB" "$T/deb-docs" >/dev/null 2>&1; check "extract packaged documents" "$?" "0"
"$V" "$ROOT/scripts/verify_docs_version.py" --root "$ROOT" \
     --packaged-guide "$DOCS/USER_GUIDE.txt" --packaged-notes "$DOCS/RELEASE_NOTES.txt"
check "packaged documents name this version" "$?" "0"
PAGES=$($V -c "from pathlib import Path; print(len(sorted(Path(\"$T/pdf\").rglob(\"*.png\"))))")
check "rendered pages" "$PAGES" "3"

chamo $ROOT/tests/fixtures/透明图片.png -o "$T/ipdf" -t images-pdf --quiet 2>/dev/null
check "images to pdf" "$?" "0"
MPAGES=$($V -c "import glob,pypdf; print(len(pypdf.PdfReader(sorted(glob.glob(\"$T/ipdf/*.pdf\"))[0]).pages))")
check "merged pdf pages" "$MPAGES" "1"

chamo $ROOT/tests/fixtures/示例课件.pptx -o "$T/office" -t office-pdf --quiet 2>/dev/null
check "office to pdf" "$?" "0"
OPAGES=$($V -c "import glob,pypdf; print(len(pypdf.PdfReader(sorted(glob.glob(\"$T/office/*.pdf\"))[0]).pages))")
check "office pdf pages" "$OPAGES" "2"

chamo $ROOT/tests/fixtures/示例讲义.pdf -o "$T/rag" -t rag --quiet 2>/dev/null
check "rag preprocess" "$?" "0"

# PDF compression: the installed venv must carry pikepdf, and the lossless
# rewrite must really publish <stem>_压缩.pdf. A source-tree test run imports the
# developer's pikepdf; only this check proves the wheel is in the package, which is
# exactly how pdf_compress would first fail on a user's machine (ImportError).
PIKEPDF_PIN=$(sed -n 's/.*"pikepdf==\([0-9.]*\)".*/\1/p' "$ROOT/scripts/engine_deps.py" | head -1)
if [ -z "$PIKEPDF_PIN" ]; then PIKEPDF_PIN=missing; fi
check "pikepdf pinned for the package" "$PIKEPDF_PIN" "10.17.0"
check "pikepdf importable in package venv" "$($V -c 'import pikepdf;print(pikepdf.__version__)' 2>/dev/null)" "$PIKEPDF_PIN"
chamo $ROOT/tests/fixtures/示例讲义.pdf -o "$T/compress" -t pdf-compress --json > "$T/compress.jsonl" 2>/dev/null
check "pdf compress lossless exit" "$?" "0"
$V - "$T/compress.jsonl" "$T/compress" "$ROOT/tests/fixtures/示例讲义.pdf" <<'PY'
import json
import sys
from pathlib import Path

events = [json.loads(line) for line in
          Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
          if line.strip().startswith("{")]
scan = next((event for event in events if event.get("type") == "scan"), None)
finished = next((event for event in reversed(events) if event.get("type") == "finished"), None)
assert scan and scan.get("report"), "scan event without a run record path"
assert finished and finished.get("state") == "complete" and finished.get("success") == 1, finished
out = Path(sys.argv[2])
source = Path(sys.argv[3])
entries = sorted(path.name for path in out.iterdir())
assert entries == ["示例讲义_压缩.pdf"], entries
product = out / entries[0]
import pypdf
assert len(pypdf.PdfReader(str(product)).pages) == 6, "product page count"
assert len(pypdf.PdfReader(str(source)).pages) == 6, "fixture page count"
assert product.stat().st_size <= source.stat().st_size, (
    "lossless product must never be larger than the original")
report = json.loads((Path(scan["report"]) / "report.json").read_text(encoding="utf-8"))
record = (report.get("results") or [{}])[0]
info = record.get("compress") or {}
assert info.get("mode") == "lossless", info
assert info.get("source_bytes") == source.stat().st_size, info
assert info.get("output_bytes") == product.stat().st_size, info
assert info.get("pages") == 6, info
assert record.get("fidelity") != "lossy_user_selected", record.get("fidelity")
print("compressed pdf: %d B -> %d B, pages=%d"
      % (info["source_bytes"], info["output_bytes"], info["pages"]))
PY
check "pdf compress artifact and byte record" "$?" "0"

chamo $ROOT/tests/fixtures/透明图片.png -o "$T/u" -t image-convert --dpi 9999 --quiet 2>/dev/null
check "usage error code" "$?" "2"
chamo /nonexistent.pdf -o "$T/m" -t pdf-images --quiet 2>/dev/null
check "missing input code" "$?" "2"
chamo $ROOT/tests/fixtures/透明图片.png -o "$T/g" -t image-convert --quiet 2>/dev/null
check "ok code" "$?" "0"

# LibreOffice is a Recommends: PDF and image work must survive without it, and
# Office work must fail honestly rather than crash.
$SUDO mv /usr/lib/libreoffice /usr/lib/libreoffice.hidden 2>/dev/null
$SUDO mv /usr/bin/soffice /usr/bin/soffice.hidden 2>/dev/null
chamo $ROOT/tests/fixtures/示例讲义.pdf -o "$T/nolo" -t pdf-images --range 1 --quiet 2>/dev/null
check "pdf works without LibreOffice" "$?" "0"
chamo $ROOT/tests/fixtures/示例课件.pptx -o "$T/nolo2" -t office-pdf --quiet >/dev/null 2>&1
check "office fails without renderer" "$?" "1"
$SUDO mv /usr/lib/libreoffice.hidden /usr/lib/libreoffice 2>/dev/null
$SUDO mv /usr/bin/soffice.hidden /usr/bin/soffice 2>/dev/null
chamo $ROOT/tests/fixtures/示例课件.pptx -o "$T/back" -t office-pdf --quiet 2>/dev/null
check "office works again" "$?" "0"

# Sharding re-executes the bundled engine for every shard, which a broken venv,
# a pruned runtime or a launcher that drops arguments would break while all the
# checks above still passed. Verify the installed package, not just the build.
$V "$ROOT/scripts/verify_package_shard.py" --chamo /usr/bin/chamo --work "$T/shard" \
    --json "$T/shard.json" > "$T/shard.log" 2>&1
check "packaged sharding acceptance" "$?" "0"
tail -3 "$T/shard.log"

# The browser UI is a second front end, not a second converter: start it from the
# installed launcher, drive the same HTTP API the page uses, and check the disk. A
# missing module in the bundled venv, a launcher that drops arguments or a state
# folder leaking into the output directory only shows up here.
$V "$ROOT/scripts/verify_ui.py" --chamo /usr/bin/chamo --work "$T/ui" > "$T/ui.log" 2>&1
check "installed local UI acceptance" "$?" "0"
tail -3 "$T/ui.log"

# Page-level resume in the installed package: cache writes/renames in /opt
# are exercised here, and the rerun must reuse the pages the cancelled run finished.
$V "$ROOT/scripts/verify_page_resume.py" --chamo /usr/bin/chamo \
   --work "$T/resume" --json "$T/resume.json" > "$T/resume.log" 2>&1
check "installed page-resume acceptance" "$?" "0"
tail -3 "$T/resume.log"

if [ "$fail" = "0" ]; then echo "Linux package verified"; else echo "Linux package FAILED"; fi
exit $fail
