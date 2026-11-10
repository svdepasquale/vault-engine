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
