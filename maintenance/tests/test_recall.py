"""Pytest suite for scripts/recall.py's hook: a failed retrieve is logged."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "recall.py"


def load(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    monkeypatch.setenv("WIKI_VAULT", str(vault))
    monkeypatch.delenv("WIKI_RECALL", raising=False)
    spec = importlib.util.spec_from_file_location("recall_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("failure", [RuntimeError("retrieve.py exit 10"),
                                     subprocess.TimeoutExpired(cmd="retrieve.py", timeout=8)])
def test_a_failed_retrieve_leaves_an_error_line(monkeypatch, tmp_path, capsys, failure):
    mod = load(monkeypatch, tmp_path)

    def boom(query):
        raise failure

    monkeypatch.setattr(mod, "retrieve", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "how is the backup scheduled every night"})))
    assert mod.hook() == 0
    assert capsys.readouterr().out == ""                    # nothing injected, the prompt goes on
    rec = json.loads(mod.LOG_PATH.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["action"] == "error"
    assert rec["reason"] == type(failure).__name__
    assert rec["pages"] == [] and isinstance(rec["ms"], int)


def test_the_eval_prints_the_ranking_ceiling(monkeypatch, tmp_path, capsys):
    """A positive whose expected page ranks third among distinct pages is
    outside the ceiling whatever the threshold; one ranked second is inside."""
    monkeypatch.delenv("WIKI_RECALL_FEATURE", raising=False)
    mod = load(monkeypatch, tmp_path)

    def cand(page, cos):
        return {"page": page, "text": "t", "cosine": {mod.QWEN: cos}}

    pool = [cand("wiki/a.md", 0.60), cand("wiki/a.md", 0.55), cand("wiki/b.md", 0.50), cand("wiki/c.md", 0.40)]
    monkeypatch.setattr(mod, "retrieve", lambda query: {"strategy": "bm25+rerank:cosine:x", "candidates": pool})
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps([
        {"id": "p1", "kind": "positive", "prompt": "where does the alpha service run", "expected_pages": ["wiki/b.md"]},
        {"id": "p2", "kind": "positive", "prompt": "how is the gamma job scheduled", "expected_pages": ["wiki/c.md"]},
    ]), encoding="utf-8")
    assert mod.evaluate(str(gold)) == 0
    out = capsys.readouterr().out
    assert "ranking ceiling (expected page among the top 2 distinct pages, no threshold, feature qwen): 1/2" in out
