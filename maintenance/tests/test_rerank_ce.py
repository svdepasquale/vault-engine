"""Pytest suite for the cross-encoder arm of scripts/rerank.py (a fake llama-embedding, no model)."""
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
