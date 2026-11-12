"""Pytest suite for eval/run-eval.py: a fused rerank that lost a model is
reported, not read as a fused measurement (a stub retrieve.py, no index)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

RUN_EVAL = Path(__file__).resolve().parent.parent.parent / "eval" / "run-eval.py"

STUB = '''import json, sys
print("bm25: 1 hits", file=sys.stderr)
print("model(s) model-b not available \\u2014 reranking with model-a", file=sys.stderr)
print(json.dumps({"strategy": "bm25+rerank:cosine:model-a",
                  "candidates": [{"page": "wiki/x.md", "text": "t", "score": 0.5}]}))
'''


def test_a_fused_run_that_lost_a_model_says_so(tmp_path):
    engine = tmp_path / "engine"
    (engine / "eval").mkdir(parents=True)
    (engine / "scripts").mkdir()
    shutil.copy(RUN_EVAL, engine / "eval" / "run-eval.py")
    (engine / "scripts" / "retrieve.py").write_text(STUB, encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "eval").mkdir(parents=True)
    (vault / "eval" / "goldset.json").write_text(
        json.dumps([{"q": "where is x", "gold": ["wiki/x.md"]}]), encoding="utf-8")
    out = subprocess.run([sys.executable, str(engine / "eval" / "run-eval.py")],
                         env={**os.environ, "WIKI_VAULT": str(vault)},
                         capture_output=True, text=True, check=True)
    assert "WARN: the rerank ran without a configured model" in out.stdout
    assert "model(s) model-b not available" in out.stdout
    assert "strategy=bm25+rerank:cosine:model-a" in out.stdout


def _indexed_vault(tmp_path, pages, gold_sets):
    """A vault whose BM25 index holds one chunk per page, and its gold sets."""
    vault = tmp_path / "vault"
    (vault / "eval").mkdir(parents=True)
    (vault / "wiki").mkdir()
    (vault / ".vault-meta" / "bm25").mkdir(parents=True)
    docs = {}
    for i, (name, created) in enumerate(pages):
        fm = f"---\nname: {name}\n" + (f"created: {created}\n" if created else "") + "---\n\nbody\n"
        (vault / "wiki" / f"{name}.md").write_text(fm, encoding="utf-8")
        cdir = vault / ".vault-meta" / "chunks" / f"syn-{i:06d}"
        cdir.mkdir(parents=True)
        (cdir / "chunk-000.json").write_text(json.dumps({"page_path": f"wiki/{name}.md"}), encoding="utf-8")
        docs[f"syn-{i:06d}:0"] = {"path": f".vault-meta/chunks/syn-{i:06d}/chunk-000.json", "dl": 1, "input_hash": f"h{i}"}
    (vault / ".vault-meta" / "bm25" / "index.json").write_text(json.dumps({"docs": docs}), encoding="utf-8")
    for fname, items in gold_sets.items():
        (vault / "eval" / fname).write_text(json.dumps(items), encoding="utf-8")
    return vault


PAGES = [("x", "2026-01-01"), ("y", "2026-02-01"), ("z", "2026-11-01"), ("w", None), ("v", "2026-11-01")]
SETS = {"goldset.json": [{"q": "where is x", "gold": ["wiki/x.md"]}],
        "goldset-multi.json": [{"id": "m1", "q": "x and y", "required": ["wiki/y.md"]}]}
LINE = "coverage: 1 of 5 indexed pages are a target of this set, 2 of any gold set in eval/"


def test_uncovered_lists_the_pages_no_set_targets_newest_first(tmp_path):
    vault = _indexed_vault(tmp_path, PAGES, SETS)
    out = subprocess.run([sys.executable, str(RUN_EVAL), "--uncovered"],
                         env={**os.environ, "WIKI_VAULT": str(vault)},
                         capture_output=True, text=True, check=True).stdout
    assert out.startswith(LINE + "; 3 pages no set targets")
    listed = [ln.split()[-1] for ln in out.splitlines()[1:]]
    assert listed == ["wiki/v.md", "wiki/z.md", "wiki/w.md"]     # newest first, the undated last
    assert "undated" in out.splitlines()[-1]


def test_every_run_prints_the_coverage_line(tmp_path):
    engine = tmp_path / "engine"
    (engine / "eval").mkdir(parents=True)
    (engine / "scripts").mkdir()
    shutil.copy(RUN_EVAL, engine / "eval" / "run-eval.py")
    (engine / "scripts" / "retrieve.py").write_text(STUB, encoding="utf-8")
    vault = _indexed_vault(tmp_path, PAGES, SETS)
    out = subprocess.run([sys.executable, str(engine / "eval" / "run-eval.py")],
                         env={**os.environ, "WIKI_VAULT": str(vault)},
                         capture_output=True, text=True, check=True).stdout
    assert "  " + LINE in out
