"""The Settings window's translations have to be complete before they ship.

The window looks up strings by key from a generated table. Two failure modes are
invisible at runtime and trivial to introduce: a view asks for a key the table does
not have (the window shows "!settings.foo"), and a translation drops a {count}
placeholder (the window prints the literal braces). Both are caught here, on
Linux, without a macOS runner.

The generated Swift file is checked for being in sync rather than trusted: a table
edited in one place and not the other is exactly what a generator exists to
prevent.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "localization" / "strings.json"
GENERATED = ROOT / "Sources" / "LocalizationStrings.swift"
GENERATOR = ROOT / "scripts" / "gen_localization.py"
VIEWS = sorted((ROOT / "Sources").glob("*.swift"))
KEY_USE = re.compile(r'(?:L10n\.text|LText)\("([^"]+)"')
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _generator():
    spec = importlib.util.spec_from_file_location("chamo_gen_localization", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def test_every_string_has_every_language_and_the_same_placeholders():
    data = _table()
    languages = list(data["languages"])
    for key, entry in data["strings"].items():
        for language in languages:
            assert entry.get(language, "").strip(), "%s: %s is empty" % (key, language)
        tokens = {language: set(PLACEHOLDER.findall(entry[language])) for language in languages}
        first = languages[0]
        for language in languages:
            assert tokens[language] == tokens[first], (
                "%s: placeholders differ between %s and %s" % (key, first, language))


def test_the_generated_swift_table_is_in_sync():
    completed = subprocess.run([sys.executable, str(GENERATOR), "--check"],
                               capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_generating_twice_produces_the_same_bytes(tmp_path):
    """A generator that reorders its output would make every diff look touched."""
    generator = _generator()
    data = generator.load(SOURCE)
    first = generator.render(data)
    second = generator.render(data)
    assert first == second
    assert GENERATED.read_text(encoding="utf-8") == first


def test_every_key_a_view_asks_for_exists():
    known = set(_table()["strings"])
    missing = []
    for path in VIEWS:
        for key in KEY_USE.findall(path.read_text(encoding="utf-8")):
            if key not in known:
                missing.append("%s: %s" % (path.name, key))
    assert missing == [], "views reference keys the table does not have: %s" % missing


def test_every_translated_key_is_used():
    """A key nobody looks up is a translation nobody can read - and a typo detector."""
    known = set(_table()["strings"])
    used = set()
    for path in VIEWS:
        used.update(KEY_USE.findall(path.read_text(encoding="utf-8")))
    assert known - used == set(), "unused keys: %s" % sorted(known - used)


def test_the_language_setting_is_stored_under_one_key():
    """The picker and the lookups must agree on the UserDefaults key."""
    localization = (ROOT / "Sources" / "Localization.swift").read_text(encoding="utf-8")
    assert 'static let defaultsKey = "ui.language"' in localization
    app = (ROOT / "Sources" / "ChamoApp.swift").read_text(encoding="utf-8")
    assert "L10n.defaultsKey" in app, "the Settings picker must use the shared key"
