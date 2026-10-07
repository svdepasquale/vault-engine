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


def graph_page(n: str, typ: str = "entity", fm: str = "") -> str:
    return f"---\nname: {n}\ndescription: d\ntype: {typ}\n{fm}---\nbody\n"


def relation_findings(mod, monkeypatch) -> list[dict]:
    return [f for f in run(mod, monkeypatch) if f["type"].startswith("relation_")]


def test_missing_inverse_between_entities_is_flagged(lint_module, tmp_path, monkeypatch):
    write(tmp_path / "entities/a.md", graph_page("a", fm='relations:\n  hosted_on: "[[b]]"\n'))
    write(tmp_path / "entities/b.md", graph_page("b", fm="relations:\n  hosts:\n"))
    assert relation_findings(lint_module, monkeypatch) == [{
        "type": "relation_missing_inverse", "path": "entities/b.md", "predicate": "hosts",
        "target": "a", "declared_by": "entities/a.md", "declared": "hosted_on",
    }]


@pytest.mark.parametrize("hosts", [
    'hosts: ["[[a]]", "[[c]]"]\n',                      # flow list
    'hosts:\n    - "[[a]]"\n    - "[[c]]"\n',           # block list
    'hosts:\n  - "[[entities/a]]"\n  - "[[c]]"\n',      # path-style, items at key indent
])
def test_declared_inverse_passes(lint_module, tmp_path, monkeypatch, hosts):
    write(tmp_path / "entities/a.md", graph_page("a", fm='relations:\n  hosted_on: "[[b]]"\n'))
    write(tmp_path / "entities/c.md", graph_page("c", fm='relations:\n  hosted_on: "[[entities/b]]"\n'))
    write(tmp_path / "entities/b.md", graph_page("b", fm="relations:\n  " + hosts))
    assert relation_findings(lint_module, monkeypatch) == []


def test_no_inverse_needed_outside_entity_and_source(lint_module, tmp_path, monkeypatch):
    # meta/profile pages carry related:, not relations:, so the edge is one-way.
    write(tmp_path / "entities/a.md", graph_page("a", fm='relations:\n  references: "[[m]]"\n'))
    write(tmp_path / "meta/m.md", graph_page("m", typ="meta", fm='relations:\n  implements: "[[a]]"\n'))
    assert relation_findings(lint_module, monkeypatch) == []


def test_inverse_free_predicates_and_placeholders_are_skipped(lint_module, tmp_path, monkeypatch):
    write(tmp_path / "entities/a.md", graph_page("a", fm=(
        'relations:\n  depends_on: "[[b]]"\n  part_of: "[[b]]"\n  related_to: ["[[b]]"]\n'
        '  consumes: "[[not-written-yet]]"\n')))
    write(tmp_path / "entities/b.md", graph_page("b"))
    findings = run(lint_module, monkeypatch)
    assert not [f for f in findings if f["type"].startswith("relation_") or f["type"] == "dead_link"]


def test_unknown_predicate_is_flagged(lint_module, tmp_path, monkeypatch):
    write(tmp_path / "entities/a.md", graph_page("a", fm='relations:\n  hostedon: "[[b]]"\n'))
    write(tmp_path / "entities/b.md", graph_page("b"))
    assert relation_findings(lint_module, monkeypatch) == [
        {"type": "relation_unknown_predicate", "path": "entities/a.md", "predicate": "hostedon"}]


def test_wikilink_in_a_trailing_comment_is_not_an_edge(lint_module, tmp_path, monkeypatch):
    # A YAML comment is not data: "(was [[c]] until last month)" must not read as consumes c.
    write(tmp_path / "entities/a.md", graph_page("a", fm=(
        'relations:\n  consumes: "[[b]]"   # was [[c]] until last month\n')))
    write(tmp_path / "entities/b.md", graph_page("b", fm='relations:\n  consumed_by: "[[a]]"\n'))
    write(tmp_path / "entities/c.md", graph_page("c", fm="relations:\n  consumed_by:\n"))
    assert relation_findings(lint_module, monkeypatch) == []


def test_section_anchor_link_resolves_to_its_page(lint_module, tmp_path, monkeypatch):
    write(tmp_path / "entities/a.md", graph_page("a", fm='relations:\n  documented_in: "[[b#setup]]"\n'))
    write(tmp_path / "sources/b.md", graph_page("b", typ="source", fm="relations:\n  documents:\n"))
    assert [f["path"] for f in relation_findings(lint_module, monkeypatch)] == ["sources/b.md"]


def test_top_level_replaced_by_counts_as_the_inverse(lint_module, tmp_path, monkeypatch):
    write(tmp_path / "entities/a.md", graph_page("a", fm='relations:\n  replaces: "[[b]]"\n'))
    write(tmp_path / "entities/b.md", graph_page("b", fm='status: archived\nreplaced_by: "[[a]]"\n'))
    assert relation_findings(lint_module, monkeypatch) == []


def brief(mod, monkeypatch) -> list[str]:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    mod.main(brief=True)
    return buf.getvalue().splitlines()


def test_brief_lists_write_errors_and_skips_age(lint_module, tmp_path, monkeypatch):
    write(tmp_path / "entities/a.md", graph_page("a", fm='updated: 2020-01-01\nrelations:\n  hosted_on: "[[b]]"\n'))
    write(tmp_path / "entities/b.md", graph_page("b", fm="relations:\n  hosts:\n"))
    lines = brief(lint_module, monkeypatch)
    assert any(line.startswith("relation_missing_inverse: entities/b.md lacks `hosts: [[a]]`") for line in lines)
    assert not [line for line in lines if line.startswith("stale")]


def test_brief_is_empty_on_a_clean_vault(lint_module, tmp_path, monkeypatch):
    write(tmp_path / "entities/a.md", graph_page("a", fm='relations:\n  hosted_on: "[[b]]"\n') + "[[b]]\n")
    write(tmp_path / "entities/b.md", graph_page("b", fm='relations:\n  hosts: "[[a]]"\n') + "[[a]]\n")
    assert brief(lint_module, monkeypatch) == []
