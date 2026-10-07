"""Pytest suite for wiki-autofix.py."""
from __future__ import annotations
import importlib.util, sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "wiki-autofix.py"


@pytest.fixture
def autofix_module(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wiki_autofix", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wiki_autofix"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    return mod, tmp_path


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_adds_name_from_title(autofix_module):
    mod, vault = autofix_module
    f = vault / "entities/foo.md"
    write(f, "---\ntitle: My Page\n---\nbody\n")
    added = mod.fix_page(f)
    assert "name" in added
    text = f.read_text()
    assert "name: My Page" in text


def test_adds_name_from_slug_when_no_title(autofix_module):
    mod, vault = autofix_module
    f = vault / "entities/some-page.md"
    write(f, "---\ntype: entity\n---\nbody\n")
    added = mod.fix_page(f)
    assert "name" in added
    assert "name: Some Page" in f.read_text()


def test_adds_type_from_path_entity(autofix_module):
    mod, vault = autofix_module
    f = vault / "entities/foo.md"
    write(f, "---\nname: Foo\n---\nbody\n")
    added = mod.fix_page(f)
    assert "type" in added
    assert "type: entity" in f.read_text()


def test_adds_type_from_path_source(autofix_module):
    mod, vault = autofix_module
    f = vault / "sources/bar.md"
    write(f, "---\nname: Bar\n---\nbody\n")
    added = mod.fix_page(f)
    assert "type: source" in f.read_text()


def test_adds_type_from_feedback_prefix(autofix_module):
    mod, vault = autofix_module
    f = vault / "meta/feedback-something.md"
    write(f, "---\nname: Foo\n---\nbody\n")
    mod.fix_page(f)
    assert "type: feedback" in f.read_text()


def test_skip_if_no_frontmatter(autofix_module):
    mod, vault = autofix_module
    f = vault / "entities/foo.md"
    write(f, "no frontmatter here\n")
    assert mod.fix_page(f) == []


def test_skip_skip_pages(autofix_module):
    mod, vault = autofix_module
    f = vault / "log.md"
    write(f, "---\n---\nbody\n")
    assert mod.fix_page(f) == []


def test_idempotent(autofix_module):
    mod, vault = autofix_module
    f = vault / "entities/foo.md"
    write(f, "---\ntitle: Foo\n---\nbody\n")
    mod.fix_page(f)
    second = mod.fix_page(f)
    assert second == []


def test_multiline_field_not_duplicated(autofix_module):
    """Regression: regex parser would have missed multiline `description: |`
    style and re-added the key. PyYAML handles it."""
    mod, vault = autofix_module
    f = vault / "entities/foo.md"
    text = (
        "---\n"
        "title: Foo\n"
        "name: Foo\n"
        "type: entity\n"
        "tags: [a, b]\n"
        "description: |\n"
        "  multi\n"
        "  line\n"
        "---\n"
        "body\n"
    )
    write(f, text)
    added = mod.fix_page(f)
    assert added == []


def test_malformed_yaml_skipped(autofix_module):
    """Don't touch files with broken YAML — leave for human."""
    mod, vault = autofix_module
    f = vault / "entities/foo.md"
    write(f, "---\nname: : : bad\n---\nbody\n")
    added = mod.fix_page(f)
    assert added == []


def test_adds_tags_when_missing(autofix_module):
    mod, vault = autofix_module
    f = vault / "entities/foo.md"
    write(f, "---\nname: Foo\ntype: entity\n---\nbody\n")
    added = mod.fix_page(f)
    assert "tags" in added
    assert "tags: []" in f.read_text()


def test_unknown_path_skips_type(autofix_module):
    """Path doesn't match any known prefix — type not inferred."""
    mod, vault = autofix_module
    f = vault / "stray/foo.md"
    write(f, "---\nname: Foo\n---\nbody\n")
    added = mod.fix_page(f)
    assert "type" not in added


def test_post_write_yaml_parses(autofix_module):
    """Sanity: after fix, frontmatter is valid YAML."""
    import yaml
    mod, vault = autofix_module
    f = vault / "entities/foo.md"
    write(f, "---\ntitle: Foo\n---\nbody\n")
    mod.fix_page(f)
    text = f.read_text()
    fm = text.split("---\n")[1]
    parsed = yaml.safe_load(fm)
    assert isinstance(parsed, dict)
    assert parsed["name"] == "Foo"
    assert parsed["type"] == "entity"
    assert parsed["tags"] == []
