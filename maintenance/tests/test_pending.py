"""Pytest suite for wiki-pending.py (schema-2 per-target staleness gate).

Runs against an isolated tmp_path vault so it never touches the real
knowledge-vault.

Run: <vault>/maintenance/run-tests.sh
"""
from __future__ import annotations
import importlib.util, json, sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "wiki-pending.py"


@pytest.fixture
def pending(tmp_path):
    """Load wiki-pending.py plus an empty vault + .lint dir."""
    spec = importlib.util.spec_from_file_location("wiki_pending", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wiki_pending"] = mod
    spec.loader.exec_module(mod)
    lint = tmp_path / ".lint"
    lint.mkdir()
    return mod, tmp_path, lint


def write_page(vault: Path, rel: str, body: str):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def write_proposals(lint: Path, name: str, *targets: str):
    blocks = []
    for i, t in enumerate(targets, 1):
        blocks.append(
            f"### proposal-{i:03d}\n"
            f"type: update-frontmatter\n"
            f"target: {t}\n"
            f"finding: stale\n"
            f"rationale: because\n"
            f"action:\n```yaml\ndescription: x\n```\n"
        )
    (lint / name).write_text("\n".join(blocks), encoding="utf-8")


def read_pending(lint: Path) -> dict:
    return json.loads((lint / "PENDING").read_text())


# ---------- parsing ----------

def test_parses_target_from_each_block(pending):
    mod, _, _ = pending
    text = (
        "### proposal-001\ntarget: entities/a.md\n\n"
        "### proposal-002\ntarget: sources/b.md\n"
    )
    assert mod.parse_targets(text) == {"entities/a.md", "sources/b.md"}


def test_ignores_target_inside_fenced_action(pending):
    """A `target:` line inside the action YAML is not a proposal target."""
    mod, _, _ = pending
    text = (
        "### proposal-001\ntarget: entities/real.md\n"
        "action:\n```yaml\ntarget: entities/decoy.md\n```\n"
    )
    assert mod.parse_targets(text) == {"entities/real.md"}


def test_frontmatter_is_not_a_target(pending):
    """The raw proposals file opens with YAML frontmatter, not a block."""
    mod, _, _ = pending
    text = "---\ngenerated: now\nreport: .lint/report.json\n---\n\n# Proposals\n"
    assert mod.parse_targets(text) == set()


def test_archive_dir_is_not_globbed(pending):
    mod, vault, lint = pending
    (lint / "archive").mkdir()
    write_proposals(lint / "archive", "proposals-2020-01-01.md", "entities/old.md")
    write_proposals(lint, "proposals-2026-08-30.md", "entities/new.md")
    targets, _ = mod.collect_targets(lint)
    assert targets == {"entities/new.md"}


# ---------- write ----------

def test_write_records_schema_and_hashes(pending):
    mod, vault, lint = pending
    write_page(vault, "entities/a.md", "hello")
    write_proposals(lint, "proposals-2026-08-30.md", "entities/a.md")
    assert mod.cmd_write(lint, vault) == 0
    state = read_pending(lint)
    assert state["schema"] == 2
    assert state["sources"] == ["proposals-2026-08-30.md"]
    assert state["targets"]["entities/a.md"] is not None


def test_write_records_null_for_missing_target(pending):
    mod, vault, lint = pending
    write_proposals(lint, "proposals-2026-08-30.md", "entities/ghost.md")
    mod.cmd_write(lint, vault)
    assert read_pending(lint)["targets"] == {"entities/ghost.md": None}


def test_write_unions_auto_and_defer_buckets(pending):
    mod, vault, lint = pending
    write_page(vault, "entities/a.md", "a")
    write_page(vault, "entities/b.md", "b")
    write_proposals(lint, "proposals-auto-2026-08-30.md", "entities/a.md")
    write_proposals(lint, "proposals-defer-2026-08-30.md", "entities/b.md")
    mod.cmd_write(lint, vault)
    assert set(read_pending(lint)["targets"]) == {"entities/a.md", "entities/b.md"}


def test_write_without_proposals_errors(pending):
    mod, vault, lint = pending
    assert mod.cmd_write(lint, vault) == 2
    assert not (lint / "PENDING").exists()


# ---------- check ----------

def test_check_fresh(pending):
    mod, vault, lint = pending
    write_page(vault, "entities/a.md", "hello")
    write_proposals(lint, "proposals-2026-08-30.md", "entities/a.md")
    mod.cmd_write(lint, vault)
    assert mod.cmd_check(lint, vault) == 0


def test_check_detects_modified_target(pending):
    mod, vault, lint = pending
    write_page(vault, "entities/a.md", "hello")
    write_proposals(lint, "proposals-2026-08-30.md", "entities/a.md")
    mod.cmd_write(lint, vault)
    write_page(vault, "entities/a.md", "hello, edited")
    assert mod.cmd_check(lint, vault) == 3


def test_unrelated_page_churn_does_not_trip_the_gate(pending):
    """The whole point of schema 2: hot.md/log.md churn is not drift.

    Under schema 1 this exact scenario blocked /wiki-apply at nearly
    every session start.
    """
    mod, vault, lint = pending
    write_page(vault, "entities/a.md", "hello")
    write_page(vault, "hot.md", "# Hot\nv1")
    write_proposals(lint, "proposals-2026-08-30.md", "entities/a.md")
    mod.cmd_write(lint, vault)
    write_page(vault, "hot.md", "# Hot\nv2 — rewritten at session end")
    write_page(vault, "log.md", "- 2026-08-30 · something happened")
    write_page(vault, "meta/unrelated.md", "a whole new page")
    assert mod.cmd_check(lint, vault) == 0


def test_check_detects_deleted_target(pending, capsys):
    mod, vault, lint = pending
    write_page(vault, "entities/a.md", "hello")
    write_proposals(lint, "proposals-2026-08-30.md", "entities/a.md")
    mod.cmd_write(lint, vault)
    capsys.readouterr()
    (vault / "entities/a.md").unlink()
    assert mod.cmd_check(lint, vault) == 3
    out = json.loads(capsys.readouterr().out)
    assert "deleted" in out["stale"][0]["reason"]


def test_missing_target_stays_fresh_until_it_appears(pending, capsys):
    mod, vault, lint = pending
    write_proposals(lint, "proposals-2026-08-30.md", "entities/ghost.md")
    mod.cmd_write(lint, vault)
    capsys.readouterr()
    assert mod.cmd_check(lint, vault) == 0
    capsys.readouterr()
    write_page(vault, "entities/ghost.md", "materialised")
    assert mod.cmd_check(lint, vault) == 3
    out = json.loads(capsys.readouterr().out)
    assert "appeared" in out["stale"][0]["reason"]


def test_legacy_pending_reports_migration(pending, capsys):
    mod, vault, lint = pending
    (lint / "PENDING").write_text("de72718c934fa03c\n")
    assert mod.cmd_check(lint, vault) == 4
    out = json.loads(capsys.readouterr().out)
    assert out["legacy"] is True and out["schema"] == 1


def test_missing_pending_errors(pending):
    mod, vault, lint = pending
    assert mod.cmd_check(lint, vault) == 2


def test_malformed_pending_errors(pending):
    mod, vault, lint = pending
    (lint / "PENDING").write_text("{not json at all")
    assert mod.cmd_check(lint, vault) == 2


def test_targetless_pending_is_flagged_vacuous(pending, capsys):
    mod, vault, lint = pending
    (lint / "PENDING").write_text(json.dumps({"schema": 2, "targets": {}}))
    assert mod.cmd_check(lint, vault) == 0
    assert "vacuous" in json.loads(capsys.readouterr().out)["warning"]
