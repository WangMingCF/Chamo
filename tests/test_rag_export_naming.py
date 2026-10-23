"""The strict RAG export must keep the source's name, not rename it.

The strict export is "the source itself": a byte-identical copy of what the user
opened. It used to be published as 透明图片.png_part000001.png - identical bytes
under a name that is not the user's file. That renamed the deliverable, and it
also made the benchmark's own name-based check (check_rag) report a correct export
as missing, so the rag.strict case could never pass on any platform.

A real split is different: a PDF cut into parts, or an Office file turned into page
images, is not the source file, so those parts keep the part number.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))


@pytest.fixture(scope="module")
def output():
    # The package uses relative imports, so it is imported as a package, the way
    # the engine imports it.
    return pytest.importorskip("cloud_kb_pack.output")


def _published(work: Path) -> list:
    return [json.loads(line)["file"]
            for line in (work / "parts.jsonl").read_text(encoding="utf-8").splitlines()]


def _sink(output, work: Path, source: Path):
    # The library's own default caps, so a change to the defaults is exercised too.
    return output.Sink(work, source, output.Limits(), tree_relative=Path(source.name))


def test_a_byte_identical_copy_keeps_the_source_name(tmp_path, output):
    """The deliverable is the file the user opened, name included."""
    source = tmp_path / "透明图片.png"
    source.write_bytes(b"png bytes")
    work = tmp_path / "work"
    work.mkdir()
    sink = _sink(output, work, source)
    # verified= skips the format parse (which needs Pillow); this test is about the
    # published name, not about validation, which has its own tests.
    sink.emit(source, "image", {"operation": "copy_native"},
              verified={"bytes": source.stat().st_size})
    sink.close()

    assert _published(work) == ["files/透明图片.png"]
    assert (work / "files" / "透明图片.png").is_file()
    assert not list(work.rglob("*_part*"))


def test_a_real_split_still_says_which_part_it_is(tmp_path, output):
    """A part is not the source file, so it must keep its part number."""
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx bytes")
    work = tmp_path / "work"
    work.mkdir()
    sink = _sink(output, work, source)
    for index in (1, 2):
        part = tmp_path / ("structured-%d.pdf" % index)
        # Escape the percent in the PDF magic: bytes %-formatting reads "%P" as a
        # conversion and raises. (This is what CI caught that my hand-run did not:
        # the hand-run used a different byte string.)
        part.write_bytes(b"%%PDF-1.4 part %d" % index)
        sink.emit(part, "document", {"operation": "page_window"},
                  verified={"bytes": part.stat().st_size, "pages": 1})
    sink.close()

    assert _published(work) == ["files/deck.pptx_part000001.pdf",
                                "files/deck.pptx_part000002.pdf"]


def test_the_copy_keeps_the_whole_name_including_the_extension(tmp_path, output):
    """a.pdf and a.docx in one folder must not collide.

    The old scheme used the whole name plus a part number precisely so that
    a.pdf and a.docx stayed distinct; dropping the part number for copies must not
    reintroduce that collision.
    """
    work = tmp_path / "work"
    work.mkdir()
    for name in ("a.pdf", "a.docx"):
        source = tmp_path / name
        source.write_bytes(name.encode())
        sink = _sink(output, work, source)
        sink.emit(source, "document", {"operation": "copy_native"},
                  verified={"bytes": source.stat().st_size})
        sink.close()

    assert (work / "files" / "a.pdf").is_file()
    assert (work / "files" / "a.docx").is_file()


def test_the_benchmark_check_now_accepts_a_copy_under_its_own_name(tmp_path, output):
    """The reason this matters beyond naming: check_rag pairs by name.

    It requires the export to contain the source's name with the source's bytes.
    Before this change every correct strict export failed that check.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "chamo_bench_quality", ROOT / "scripts" / "bench_quality.py")
    quality = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quality)

    source = tmp_path / "透明图片.png"
    source.write_bytes(b"png bytes")
    work = tmp_path / "work"
    work.mkdir()
    sink = _sink(output, work, source)
    sink.emit(source, "image", {"operation": "copy_native"},
              verified={"bytes": source.stat().st_size})
    sink.close()

    export = work / "files"
    assert quality.check_rag([source], export) == [], quality.check_rag([source], export)

def test_a_source_whose_name_looks_like_a_part_does_not_collide(tmp_path, output):
    """Both files must survive when two names land on the same path.

    A strict export used to give every file a _partN marker, so no two files could
    collide. Keeping the source's own name for a byte-identical copy removes that
    guarantee: a source literally called "a.pdf_part000001.pdf" (a previously
    exported part folder fed back in as a RAG source) publishes to the same path as
    a split part of a.pdf. That surfaced as FileExistsError, so the file failed.
    Both must be kept, in either emission order.
    """
    for order in (("split", "copy"), ("copy", "split")):
        work = tmp_path / ("work-" + "-".join(order))
        work.mkdir()
        split_source = tmp_path / "a.pdf"
        split_source.write_bytes(b"the source pdf")
        part = tmp_path / "structured-0.pdf"
        part.write_bytes(b"part one")

        def do_split():
            sink = _sink(output, work, split_source)
            sink.emit(part, "document", {"operation": "split"},
                      verified={"bytes": part.stat().st_size, "pages": 1})
            sink.close()

        def do_copy():
            odd = tmp_path / "a.pdf_part000001.pdf"
            odd.write_bytes(b"a source whose name looks like a part")
            sink = _sink(output, work, odd)
            sink.emit(odd, "document", {"operation": "copy_native"},
                      verified={"bytes": odd.stat().st_size})
            sink.close()

        {"split": do_split, "copy": do_copy}[order[0]]()
        {"split": do_split, "copy": do_copy}[order[1]]()   # must not raise

        written = {path.name: path.read_bytes()
                   for path in (work / "files").iterdir() if path.is_file()}
        assert b"part one" in written.values(), (order, sorted(written))
        assert b"a source whose name looks like a part" in written.values(), (order, sorted(written))
        assert len(written) == 2, (order, sorted(written))
