"""Pytest suite for scripts/catalog.py (a synthetic vault, no index)."""
from __future__ import annotations
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "catalog.py"


def page(root: Path, rel: str, fm: str, body: str = "Body.\n") -> None:
    path = root / "vault" / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body}", encoding="utf-8")


def load(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_VAULT", str(tmp_path / "vault"))
    spec = importlib.util.spec_from_file_location("catalog_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def vault(tmp_path):
    page(tmp_path, "things/alpha.md", 'name: alpha\ndescription: "First thing: with a colon"\ntype: entity')
    page(tmp_path, "things/beta.md", "name: beta\ndescription: >-\n  A folded\n  description\ntags:\n  - x\nstatus: archived\nindex: false")
    page(tmp_path, "things/_index.md", "name: nav\ndescription: section index")
    page(tmp_path, "notes/deep/gamma.md", "name: gamma\ntype: meta")
    page(tmp_path, "index.md", "name: index\ndescription: master")
    page(tmp_path, "hot.md", "name: hot\ndescription: cache")
    page(tmp_path, ".lint/report.md", "name: r\ndescription: tool output")
    page(tmp_path, "overview.md", "name: overview\ndescription: What this is\nstatus: evergreen")


def test_lists_every_knowledge_page_with_its_description(monkeypatch, tmp_path):
    vault(tmp_path)
    mod = load(monkeypatch, tmp_path)
    rows = mod.entries()
    assert [r[1] for r in rows] == ["overview", "notes/deep/gamma", "things/alpha", "things/beta"]
    by_page = {r[1]: r for r in rows}
    assert by_page["things/alpha"][2] == "First thing: with a colon"
    assert by_page["things/beta"][2] == "A folded description"
    assert by_page["notes/deep/gamma"][2] == "(no description)"


def test_marks_pages_that_are_not_live_or_not_indexed(monkeypatch, tmp_path):
    vault(tmp_path)
    mod = load(monkeypatch, tmp_path)
    by_page = {r[1]: r[3] for r in mod.entries()}
    assert by_page["things/beta"] == ["archived", "not indexed"]
    assert by_page["things/alpha"] == []
    assert by_page["overview"] == []


def test_section_filter_and_rendering(monkeypatch, tmp_path):
    vault(tmp_path)
    mod = load(monkeypatch, tmp_path)
    rows = mod.entries(["things/"])
    assert {r[0] for r in rows} == {"things"}
    out = mod.render(rows)
    assert out.splitlines()[0] == "## things (2)"
    assert "- things/beta — A folded description [archived] [not indexed]" in out
    assert mod.render(rows, names_only=True).splitlines()[1] == "- things/alpha"
    assert mod.entries(["nowhere"]) == []


def test_root_pages_form_their_own_section(monkeypatch, tmp_path):
    vault(tmp_path)
    mod = load(monkeypatch, tmp_path)
    assert [r[1] for r in mod.entries(["(root)"])] == ["overview"]
