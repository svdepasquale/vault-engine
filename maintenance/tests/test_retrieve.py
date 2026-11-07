"""Pytest suite for scripts/retrieve.py's fold prior (synthetic candidates, no index)."""
from __future__ import annotations
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "retrieve.py"


def load(monkeypatch, tmp_path, weight=None):
    monkeypatch.setenv("WIKI_VAULT", str(tmp_path / "vault"))
    if weight is None:
        monkeypatch.delenv("WIKI_FOLD_WEIGHT", raising=False)
    else:
        monkeypatch.setenv("WIKI_FOLD_WEIGHT", weight)
    spec = importlib.util.spec_from_file_location("retrieve_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cand(page, score, source="cosine:test-model", bm25=1.0):
    return {"page_path": page, "rerank_score": score, "rerank_source": source, "bm25_score": bm25}


def test_default_weight_lets_the_current_page_win_a_near_tie(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    assert mod.fold_weight() == 0.95
    cands = [cand("wiki/folds/fold-a.md", 0.538), cand("wiki/runbooks/setup.md", 0.522)]
    mod.apply_fold_weight(cands, mod.fold_weight())
    ranked = sorted(cands, key=lambda c: c["rerank_score"], reverse=True)
    assert [c["page_path"] for c in ranked] == ["wiki/runbooks/setup.md", "wiki/folds/fold-a.md"]
    assert cands[0]["rerank_score"] == pytest.approx(0.538 * 0.95)


def test_a_fold_still_wins_by_a_clear_margin(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    cands = [cand("wiki/folds/fold-a.md", 0.62), cand("wiki/entities/x.md", 0.50)]
    mod.apply_fold_weight(cands, mod.fold_weight())
    assert max(cands, key=lambda c: c["rerank_score"])["page_path"] == "wiki/folds/fold-a.md"


def test_weight_one_leaves_every_score_as_it_was(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path, "1")
    cands = [cand("wiki/folds/fold-a.md", 0.0, source="noop-no-backend", bm25=7.0),
             cand("wiki/entities/x.md", 0.4)]
    mod.apply_fold_weight(cands, mod.fold_weight())
    assert [c["rerank_score"] for c in cands] == [0.0, 0.4]


def test_without_a_reranker_it_ranks_on_the_bm25_score(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    cands = [cand("wiki/folds/fold-a.md", 0.0, source="noop-no-backend", bm25=10.0),
             cand("wiki/entities/x.md", 0.0, source="noop-no-backend", bm25=9.8)]
    mod.apply_fold_weight(cands, mod.fold_weight())
    assert cands[0]["rerank_score"] == pytest.approx(9.5)
    assert cands[1]["rerank_score"] == pytest.approx(9.8)


def test_a_bad_value_falls_back_to_the_default(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path, "abc")
    assert mod.fold_weight() == 0.95
