"""Pytest suite for scripts/contextual-prefix.py: which pages an --all run
collects, and where their chunks are filed."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "contextual-prefix.py"


def load(monkeypatch, vault):
    monkeypatch.setenv("WIKI_VAULT", str(vault))
    spec = importlib.util.spec_from_file_location("ctxprefix_paths_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_page(vault, rel, frontmatter, body="The page says one thing. More text."):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n# Heading\n\n{body}\n", encoding="utf-8")
    return path


def run_all(monkeypatch, mod):
    monkeypatch.setattr(sys, "argv", ["contextual-prefix.py", "--all", "--no-llm"])
    return mod.main()


def chunk_records(mod):
    return [json.loads(f.read_text(encoding="utf-8"))
            for f in sorted(mod.CHUNKS_DIR.glob("*/chunk-*.json"))]


def test_a_non_utf8_page_does_not_abort_the_run(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    write_page(vault, "wiki/things/good.md", "name: good\ntype: entity")
    bad = vault / "wiki" / "things" / "bad.md"
    bad.write_bytes(b"---\nname: bad\ntype: entity\n---\n\n# Bad\n\nCaf\xe9 text.\n")
    mod = load(monkeypatch, vault)
    assert run_all(monkeypatch, mod) == 0
    assert {r["page_path"] for r in chunk_records(mod)} == {"wiki/things/good.md", "wiki/things/bad.md"}


def test_a_vault_under_a_dot_directory_still_yields_its_pages(monkeypatch, tmp_path):
    vault = tmp_path / ".hidden" / "vault"
    write_page(vault, "wiki/things/alpha.md", "name: alpha\ntype: entity")
    write_page(vault, "wiki/.drafts/skip.md", "name: skip\ntype: entity")   # a dot-dir inside the vault is still skipped
    mod = load(monkeypatch, vault)
    assert [p.relative_to(mod.VAULT_ROOT).as_posix() for p in mod.collect_pages("--all")] == ["wiki/things/alpha.md"]


def test_a_page_moved_without_an_edit_gets_its_new_path(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    old = write_page(vault, "wiki/entities/alpha.md", "name: alpha\naddress: c-000001\ntype: entity")
    mod = load(monkeypatch, vault)
    assert run_all(monkeypatch, mod) == 0
    new = vault / "wiki" / "sources" / "alpha.md"
    new.parent.mkdir(parents=True)
    old.rename(new)
    assert run_all(monkeypatch, mod) == 0
    assert [r["page_path"] for r in chunk_records(mod)] == ["wiki/sources/alpha.md"]


def test_two_pages_with_one_address_both_stay_indexed(monkeypatch, tmp_path, capsys):
    vault = tmp_path / "vault"
    write_page(vault, "wiki/things/beta.md", "name: beta\naddress: c-000002\ntype: entity", "Penguin colonies.")
    write_page(vault, "wiki/things/gamma.md", "name: gamma\naddress: c-000002\ntype: entity", "Glacier tides.")
    mod = load(monkeypatch, vault)
    assert run_all(monkeypatch, mod) == 0                        # logged, not fatal: the rest keeps re-chunking
    by_path = {r["page_path"]: r["page_address"] for r in chunk_records(mod)}
    synthetic = mod.derive_synthetic_address(vault.resolve() / "wiki" / "things" / "gamma.md")
    assert by_path == {"wiki/things/beta.md": "c-000002", "wiki/things/gamma.md": synthetic}
    err = capsys.readouterr().err
    assert "ERR: wiki/things/gamma.md has address c-000002, already used by wiki/things/beta.md" in err
    assert run_all(monkeypatch, mod) == 0                        # a second run keeps both, GC included
    assert {r["page_path"] for r in chunk_records(mod)} == {"wiki/things/beta.md", "wiki/things/gamma.md"}
