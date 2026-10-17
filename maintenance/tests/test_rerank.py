"""Pytest suite for scripts/rerank.py's dense candidates and cross-encoder arm (fakes, no model)."""
from __future__ import annotations
import importlib.util, json, stat
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "rerank.py"

# Stands in for llama-embedding: records its argv, scores each pair by the
# length of its document, and prints the rows the way --pooling rank does —
# n_embd wide, the pair's score first.
FAKE = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
open(sys.argv[0] + ".args", "w").write(json.dumps(args))
text = open(args[args.index("-f") + 1], encoding="utf-8").read()
sep, cls = args[args.index("--embd-separator") + 1], args[args.index("--cls-separator") + 1]
print(json.dumps([[float(len(p.split(cls)[1]))] + [0.0] * 7 for p in text.split(sep)]))
'''


def load(monkeypatch, tmp_path, mode):
    fake = tmp_path / "llama-embedding"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ce.gguf").write_bytes(b"")
    monkeypatch.setenv("WIKI_LLAMA_EMBEDDING", str(fake))
    monkeypatch.setenv("WIKI_GGUF_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("WIKI_CE_GGUF", "ce.gguf")
    monkeypatch.setenv("WIKI_CE", mode)
    monkeypatch.setenv("WIKI_VAULT", str(tmp_path / "vault"))
    spec = importlib.util.spec_from_file_location("rerank_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, fake


def test_off_by_default(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path, "off")
    assert not mod.ce_enabled()


def test_score_is_the_first_value_of_each_row(monkeypatch, tmp_path):
    mod, fake = load(monkeypatch, tmp_path, "fuse")
    assert mod.ce_enabled()
    assert mod.cross_encode("query", ["a", "bbb", "cc"]) == [1.0, 3.0, 2.0]
    args = json.loads(Path(str(fake) + ".args").read_text())
    assert args[args.index("--pooling") + 1] == "rank"
    assert args[args.index("--embd-normalize") + 1] == "-1"


def test_separators_in_a_chunk_cannot_split_a_pair(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path, "only")
    doc = "x" + mod.LOCAL_SEP + "y" + mod.CE_SEP + "z"
    assert mod.cross_encode("q", [doc, ""]) == [5.0, 1.0]  # "x y z"; an empty chunk is sent as " "


def test_dense_pick_takes_the_nearest_chunks_the_caller_missed(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path, "off")
    tag, scheme = mod.LOCAL_TAG, mod.EMBED_SCHEME
    vecs = {"m": {"h1": [1.0, 0.0], "h2": [0.8, 0.6], "h3": [0.0, 1.0]},
            "n": {"h1": [1.0, 0.0], "h2": [0.0, 1.0], "h3": [0.6, 0.8]}}
    cache = {f"{tag}{m}:{scheme}:{h}": v for m, by in vecs.items() for h, v in by.items()}
    pool = {f"c{i}": {"path": f"p{i}", "body_hash": f"h{i}"} for i in (1, 2, 3)}
    q = [1.0, 0.0]
    assert mod.dense_pick({"m": q}, cache, tag, pool, {"p1"}, 1) == [("c2", "p2")]
    assert mod.dense_pick({"m": q}, cache, tag, pool, set(), 2) == [("c1", "p1"), ("c2", "p2")]
    # k counts all models together, taking their nearest lists in turn, never one already taken
    assert mod.dense_pick({"m": q, "n": [0.6, 0.8]}, cache, tag, pool, set(), 2) == [("c1", "p1"), ("c3", "p3")]
    assert mod.dense_pick({"m": q, "n": [0.6, 0.8]}, cache, tag, pool, set(), 3) == [
        ("c1", "p1"), ("c3", "p3"), ("c2", "p2")]


def fake_backend(monkeypatch, mod, vecs, ce_scores=None):
    chunks = {p: {"body_hash": p, "contextualized_text": p} for p in vecs}
    cache = {f"{mod.LOCAL_TAG}m:{mod.EMBED_SCHEME}:{p}": v for p, v in vecs.items()}
    monkeypatch.setattr(mod, "pick_backend", lambda allow_remote: ("local", ["m"], None))
    monkeypatch.setattr(mod, "prefix_scheme", lambda model: ("", ""))
    monkeypatch.setattr(mod, "load_chunk", lambda path: chunks.get(path))
    monkeypatch.setattr(mod, "load_cache", lambda: (dict(cache), 0))
    monkeypatch.setattr(mod, "save_cache", lambda c: None)
    monkeypatch.setattr(mod, "local_embed_batch", lambda model, texts: [[1.0, 0.0]])
    if ce_scores:
        monkeypatch.setattr(mod, "cross_encode", lambda q, docs: [ce_scores[d] for d in docs])


def test_rerank_adds_dense_candidates(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path, "off")
    fake_backend(monkeypatch, mod, {"a": [0.0, 1.0], "b": [1.0, 0.0]})
    out = mod.rerank("q", [{"path": "a"}], top_k=None, dense_k=5,
                     dense_pool={"x:0": {"path": "b", "body_hash": "b"}},
                     make_candidate=lambda cid, path: {"chunk_id": cid, "path": path})
    assert [(c["path"], c.get("dense", False)) for c in out] == [("b", True), ("a", False)]


def test_cross_encoder_reorders_only_the_fused_top(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path, "only")
    monkeypatch.setattr(mod, "CE_TOP", 2)
    fake_backend(monkeypatch, mod, {"a": [1.0, 0.0], "b": [0.9, 0.1], "c": [0.0, 1.0]},
                 ce_scores={"a": 0.0, "b": 5.0, "c": 9.0})
    out = mod.rerank("q", [{"path": p} for p in "abc"], top_k=None)
    # b overtakes a inside the shortlist; c, outside it, cannot climb whatever its score
    assert [c["path"] for c in out] == ["b", "a", "c"]
