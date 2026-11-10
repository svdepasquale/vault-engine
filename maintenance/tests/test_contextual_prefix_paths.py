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
