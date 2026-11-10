"""Pytest suite for scripts/contextual-prefix.py: the prefix's title and scheme."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "contextual-prefix.py"


def load(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_VAULT", str(tmp_path / "vault"))
    spec = importlib.util.spec_from_file_location("ctxprefix_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def page(tmp_path, name, frontmatter):
    path = tmp_path / "vault" / "wiki" / "things" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n# Heading\n\nThe thing does one job. More text.\n", encoding="utf-8")
    return path


def records(mod, path):
    out = mod.process_page(path, force_synthetic=True)
    chunk_dir = mod.CHUNKS_DIR / out["address"]
    return out, [json.loads(f.read_text(encoding="utf-8")) for f in sorted(chunk_dir.glob("chunk-*.json"))]


def test_a_page_without_title_gets_its_file_name(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    _, recs = records(mod, page(tmp_path, "some-thing", "name: Some thing\ntype: entity"))
    assert recs[0]["prefix"].startswith('This passage is from the wiki page "some-thing".')
    assert "(untitled)" not in recs[0]["contextualized_text"]


def test_an_explicit_title_wins(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    _, recs = records(mod, page(tmp_path, "some-thing", 'name: Some thing\ntitle: "Chosen title"\ntype: entity'))
    assert recs[0]["prefix"].startswith('This passage is from the wiki page "Chosen title".')


def test_a_record_of_another_prefix_scheme_is_rewritten(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    path = page(tmp_path, "some-thing", "name: Some thing\ntype: entity")
    out, recs = records(mod, path)
    assert recs[0]["prefix_scheme"] == mod.PREFIX_SCHEME
    again, _ = records(mod, path)
    assert again["written"] == [] and again["skipped"] == 1          # unchanged page, same scheme
    chunk = mod.CHUNKS_DIR / out["address"] / "chunk-000.json"
    old = json.loads(chunk.read_text(encoding="utf-8"))
    old.pop("prefix_scheme")                                           # a record from before the field
    old["prefix"] = 'This passage is from the wiki page "(untitled)".'
    chunk.write_text(json.dumps(old), encoding="utf-8")
    third, recs = records(mod, path)
    assert third["written"] == ["chunk-000.json"]
    assert "(untitled)" not in recs[0]["prefix"]
