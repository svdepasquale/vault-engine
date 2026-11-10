"""Pytest suite for scripts/retrieve.py: the fold prior (synthetic candidates)
and the --no-rerank path (a small synthetic index)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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


def test_a_negative_fused_score_is_still_demoted(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    cands = [cand("wiki/folds/fold-a.md", -0.39, source="cosine:model-a+model-b"),
             cand("wiki/entities/x.md", -0.39, source="cosine:model-a+model-b")]
    mod.apply_fold_weight(cands, mod.fold_weight())
    assert cands[0]["rerank_score"] < cands[1]["rerank_score"] == -0.39
    assert cands[0]["rerank_score"] == pytest.approx(-0.39 - 0.05 * 0.39)


def test_a_bad_value_falls_back_to_the_default(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path, "abc")
    assert mod.fold_weight() == 0.95


def test_no_rerank_fills_a_capped_slot_from_the_next_page(tmp_path):
    vault = tmp_path / "vault"
    chunks = vault / ".vault-meta" / "chunks"
    texts = {("c-000001", i): f"walrus walrus walrus tusks part {i}" for i in range(4)}
    texts[("c-000002", 0)] = "walrus seen once on a beach"
    for (addr, i), text in texts.items():
        d = chunks / addr
        d.mkdir(parents=True, exist_ok=True)
        page = "wiki/things/long.md" if addr == "c-000001" else "wiki/things/short.md"
        (d / f"chunk-{i:03d}.json").write_text(json.dumps({
            "page_path": page, "page_address": addr, "chunk_index": i,
            "raw_text": text, "contextualized_text": text}), encoding="utf-8")
    env = {**os.environ, "WIKI_VAULT": str(vault)}
    scripts = SCRIPT.parent
    subprocess.run([sys.executable, str(scripts / "bm25-index.py"), "build"], env=env,
                   check=True, capture_output=True)
    out = subprocess.run([sys.executable, str(SCRIPT), "walrus", "--no-rerank", "--top", "3",
                          "--chunks", "--compact"], env=env, check=True, capture_output=True, text=True)
    pages = [c["page"] for c in json.loads(out.stdout)["candidates"]]
    assert pages == ["wiki/things/long.md", "wiki/things/long.md", "wiki/things/short.md"]


@pytest.mark.parametrize("flag", ["--allow-remote-backend", "--allow-remote-ollama"])
def test_without_an_index_it_names_the_build_command(tmp_path, flag):
    env = {**os.environ, "WIKI_VAULT": str(tmp_path / "vault")}
    out = subprocess.run([sys.executable, str(SCRIPT), "anything", flag],
                         env=env, capture_output=True, text=True, check=False)
    assert out.returncode == 10                                  # the old flag still parses
    assert "contextual-prefix.py --all && python3 scripts/bm25-index.py build" in out.stderr
    assert "setup-retrieve" not in out.stderr
