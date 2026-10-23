"""Copy pinned LibreOffice into the bundle, leaving out parts this product
never uses.

Every removed directory is for end-user features we do not expose: offline
help, the extension manager, the clip-art gallery, the Java bridge, document
templates, wizards and the embedded LibreOffice Python. Office->PDF rendering
is unaffected; a benchmark verified byte-identical output before and after
removal on real fixtures.
"""
from pathlib import Path
import shutil

# Verified removable: 253 MB, with identical converted output.
PRUNE = [
    "Contents/Resources/help",
    "Contents/Resources/extensions",
    "Contents/Resources/gallery",
    "Contents/Resources/java",
    "Contents/Resources/template",
    "Contents/Resources/wizards",
    "Contents/Resources/xpdfimport",
    "Contents/Frameworks/LibreOfficePython.framework",
]

# Icon themes are only used by the graphical shell. Removing them was verified
# safe; removing Contents/Resources/config/soffice.cfg was NOT: LibreOffice then
# exits 0 and silently produces no PDF ("source file could not be loaded").
PRUNE_GLOB = [
    "Contents/Resources/config/images_*.zip",
]


def prune(bundle: Path) -> int:
    """Remove unused LibreOffice components; return megabytes reclaimed."""
    freed = 0
    for rel in PRUNE:
        target = bundle / rel
        if not target.exists():
            continue
        freed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        shutil.rmtree(target)
    for pattern in PRUNE_GLOB:
        for target in bundle.glob(pattern):
            if target.is_file():
                freed += target.stat().st_size
                target.unlink()
    return freed // (1024 * 1024)


def resign(bundle: Path, run) -> None:
    """Re-seal the pruned bundle.

    Removing resources invalidates The Document Foundation's signature, so the
    copy can no longer be described as "unmodified LibreOffice". It is re-signed
    ad-hoc inside our own bundle; LICENSES.txt states this explicitly and the
    MPL-2.0 source-availability requirement is unaffected because we do not
    modify LibreOffice source or binaries, only delete unused resource folders.
    """
    run(["codesign", "--force", "--deep", "--sign", "-", bundle])


# LibreOffice writes __pycache__ next to its bundled UNO Python modules when it
# runs, which invalidates the vendor seal of the copy we ship. These caches are
# regenerated on demand, so they are dropped rather than shipped.
STALE_CACHES = "**/__pycache__"


def strip_caches(bundle: Path) -> int:
    freed = 0
    for cache in bundle.glob(STALE_CACHES):
        if cache.is_dir():
            freed += sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
            shutil.rmtree(cache)
    return freed


def pending(bundle: Path) -> bool:
    """True when the bundle still contains at least one prunable component.

    A normal build prunes while this is true; a CHAMO_NO_PRUNE control build keeps
    an untouched copy and therefore refuses one that is already pruned, i.e. it
    refuses when this returns False. The value is existential: one leftover item
    makes the whole copy "pending", and an empty result is not proof that the copy
    was never touched (see the STOCK-02 note under .development/verification/).
    """
    if any((bundle / rel).exists() for rel in PRUNE):
        return True
    # any() over the glob generators themselves is always true - a generator object
    # is truthy - so this used to answer True for every copy. The symptom differed
    # per caller: a normal build re-pruned and re-signed an already pruned bundle,
    # and the control build's "already pruned" refusal could never fire, so
    # CHAMO_NO_PRUNE silently accepted a pruned LibreOffice.
    return any(match for pattern in PRUNE_GLOB for match in bundle.glob(pattern))


def prune_and_seal(bundle: Path, run) -> int:
    """Prune an existing copy and re-seal it. Returns MB reclaimed."""
    # prune() reports megabytes, strip_caches() reports bytes; convert before summing.
    freed_bytes = prune(bundle) * 1024 * 1024 + strip_caches(bundle)
    resign(bundle, run)
    return freed_bytes // (1024 * 1024)


def install(source: Path, dest: Path, run) -> int:
    """Copy LibreOffice once, prune it, and re-seal. Returns MB reclaimed."""
    if dest.exists():
        return 0
    shutil.copytree(source, dest, symlinks=True)
    return prune_and_seal(dest, run)


if __name__ == "__main__":
    import sys
    print(install(Path(sys.argv[1]), Path(sys.argv[2])), "MB reclaimed")
