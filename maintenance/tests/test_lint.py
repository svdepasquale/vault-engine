from __future__ import annotations
import importlib.util, io, json, sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "wiki-lint.py"


@pytest.fixture
def lint_module(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wiki_lint", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    return mod


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def run(mod, monkeypatch) -> list[dict]:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    mod.main()
    return json.loads(buf.getvalue())["findings"]


PAGE = "---\nname: {n}\ndescription: d\ntype: entity\n---\n{body}\n"


def test_path_style_link_is_not_orphan(lint_module, tmp_path, monkeypatch):
    # Regression 2026-09-26: [[runbooks/x]] resolved for dead-link purposes
    # but did not count as an inbound link, so x was reported as orphan.
    write(tmp_path / "entities/a.md", PAGE.format(n="a", body="see [[runbooks/x]] and [[b]]"))
    write(tmp_path / "entities/b.md", PAGE.format(n="b", body="back to [[a]]"))
    write(tmp_path / "runbooks/x.md", PAGE.format(n="x", body="body"))
    findings = run(lint_module, monkeypatch)
    orphans = {f["path"] for f in findings if f["type"] == "orphan"}
    assert "runbooks/x.md" not in orphans
    assert not [f for f in findings if f["type"] == "dead_link"]


def test_unlinked_page_is_still_orphan(lint_module, tmp_path, monkeypatch):
    write(tmp_path / "entities/a.md", PAGE.format(n="a", body="nothing"))
    write(tmp_path / "runbooks/x.md", PAGE.format(n="x", body="[[a]]"))
    findings = run(lint_module, monkeypatch)
    orphans = {f["path"] for f in findings if f["type"] == "orphan"}
    assert orphans == {"runbooks/x.md"}
