"""Pytest suite for semantic-scan.py — --dump's output and its date stamp only.
Pair selection reads the vault's embed cache and the judge is a model call;
neither is exercised here.

Run: <engine>/maintenance/run-tests.sh
"""
from __future__ import annotations
import importlib.util, re
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "semantic-scan.py"


def load(monkeypatch, vault):
    monkeypatch.setenv("WIKI_VAULT", str(vault))   # VAULT_ROOT is read at import
    spec = importlib.util.spec_from_file_location("semantic_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def page(vault, name, updated, body):
    (vault / "wiki" / f"{name}.md").write_text(f"---\nupdated: {updated}\n---\n{body}\n", encoding="utf-8")


def test_dump_writes_the_pairs_and_stamps_the_round(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    page(tmp_path, "alpha", "2026-01-02", "The port is 8080, see [[beta]].")
    page(tmp_path, "beta", "2026-01-03", "The port is 9090.")
    page(tmp_path, "gamma", "2026-01-04", "No links here.")
    scan = load(monkeypatch, tmp_path)
    pairs = [(0.91, "wiki/alpha.md", {"_id": "c-1:0", "raw_text": "The port is 8080"},
              "wiki/beta.md", {"_id": "c-2:0", "raw_text": "The port is 9090"}),
             (0.75, "wiki/beta.md", {"_id": "c-2:1", "raw_text": "x"},
              "wiki/gamma.md", {"_id": "c-3:0", "raw_text": "y"})]
    out = tmp_path / "learn.md"

    assert scan.dump(pairs, 1, out) == 0

    text = out.read_text(encoding="utf-8")
    assert "## 1. cos 0.910 — wiki/alpha.md (updated 2026-01-02, links B: yes)" in text
    assert "## 2." not in text   # past the top --pairs a pair is only listed, and only if unlinked
    assert "- 0.750  wiki/beta.md  <>  wiki/gamma.md  [c-2:1 | c-3:0]" in text
    stamp = (tmp_path / "wiki" / ".lint" / ".last-learning").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\n", stamp)
