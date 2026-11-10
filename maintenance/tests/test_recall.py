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
