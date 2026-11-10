"""Pytest suite for consolidation-due.py (synthetic git repo only)."""
from __future__ import annotations
import importlib.util, os, subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "consolidation-due.py"


@pytest.fixture
def due(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("consolidation_due", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return mod, tmp_path


def commit(repo: Path, rel: str, text: str, when: str):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
    subprocess.run(git + ["add", rel], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "c"], check=True, env=env)


def test_baseline_is_the_consolidating_commit_body(due):
    """The consolidated version is committed the day after the date it
    declares: the baseline must be that version's body, not the journal that
    was still on disk at the end of the declared day."""
    mod, repo = due
    rel = "wiki/entities/p.md"
    commit(repo, rel, "---\nname: p\n---\n" + "journal line\n" * 1500, "2026-01-05T20:00:00")
    body = "current state\n" * 400
    fm = "---\nname: p\ndescription: " + "d" * 200 + "\nconsolidated: '2026-01-05'\n---\n"
    commit(repo, rel, fm + body, "2026-01-06T07:00:00")
    commit(repo, rel, fm + body + "one more line\n", "2026-01-08T10:00:00")
    assert mod.size_at(rel, "2026-01-05") == len(body.encode())


def test_falls_back_to_the_day_when_no_commit_wrote_the_line(due):
    mod, repo = due
    rel = "wiki/entities/q.md"
    commit(repo, rel, "---\nname: q\n---\nbody\n", "2026-01-05T20:00:00")
    assert mod.size_at(rel, "2026-01-05") == len(b"body\n")
    assert mod.size_at(rel, "2025-12-31") == 0
