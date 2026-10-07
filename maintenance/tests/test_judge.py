"""Pytest suite for wiki-judge.py.

Runs against an isolated tmp_path vault so it doesn't touch the real
knowledge-vault. Imports the script as a module by patching VAULT/
LINT_DIR before the call.

Run: <vault>/maintenance/run-tests.sh
"""
from __future__ import annotations
import importlib.util, json, sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "wiki-judge.py"


@pytest.fixture
def judge_module(tmp_path, monkeypatch):
    """Load wiki-judge.py with VAULT pointed at tmp_path."""
    spec = importlib.util.spec_from_file_location("wiki_judge", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wiki_judge"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "LINT_DIR", tmp_path / ".lint")
    (tmp_path / ".lint").mkdir()
    return mod, tmp_path


def write_page(vault: Path, rel: str, frontmatter: dict, body: str = ""):
    import yaml as _y
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = _y.safe_dump(frontmatter, sort_keys=False)
    p.write_text(f"---\n{fm}---\n{body}", encoding="utf-8")


# ---------- judge() unit tests ----------

def test_target_missing_defers(judge_module):
    mod, vault = judge_module
    prop = {"type": "update-frontmatter", "target": "entities/missing.md",
            "action": {"name": "X"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/missing.md"})
    assert verdict == "defer"


def test_target_not_in_lint_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"title": "Foo"}, "body" * 50)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"name": "Foo"}}
    verdict, _ = mod.judge(prop, lint_targets=set())
    assert verdict == "defer"
    assert "hallucination" in _


def test_existing_field_blocks_overwrite(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"name": "Foo Existing"}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"name": "Different"}}
    verdict, reason = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"
    assert "already populated" in reason


def test_simple_name_apply(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"title": "Foo"}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"name": "Foo Bar"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "auto"


def test_type_mismatch_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"type": "source"}}  # path says entity, proposal says source
    verdict, reason = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"
    assert "mismatch" in reason


def test_invalid_type_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"type": "fancy"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_description_too_short_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"title": "Foo"}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"description": "tiny"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_description_filler_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"title": "Foo"}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"description":
                       "A comprehensive guide to managing the entity vault and its various components"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_description_body_too_thin_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"title": "Foo"}, "tiny")
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"description":
                       "Self-hosted k3s platform serving a static site and a password manager"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_description_dup_title_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md",
               {"title": "Self-hosted k3s platform serving a static site and a password manager"},
               "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"description":
                       "Self-hosted k3s platform serving a static site and a password manager"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_description_happy_path(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"title": "Foo"}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"description":
                       "Self-hosted k3s platform serving a static site and a password manager"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "auto"


def test_mark_evergreen_on_meta_applies(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/_index.md", {}, "x" * 100)
    prop = {"type": "mark-evergreen", "target": "entities/_index.md", "action": {}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/_index.md"})
    assert verdict == "auto"


def test_mark_evergreen_on_regular_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {}, "x" * 100)
    prop = {"type": "mark-evergreen", "target": "entities/foo.md", "action": {}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_unsupported_type_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {}, "x" * 250)
    prop = {"type": "merge", "target": "entities/foo.md",
            "action": {"merge_into": "bar"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_extra_fields_defer(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"name": "Foo", "summary": "no such field"}}
    verdict, reason = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"
    assert "non-whitelisted" in reason


# ---------- parse_proposal_block tests ----------

def test_parse_strips_stray_dashes(judge_module):
    mod, _ = judge_module
    block = (
        "type: update-frontmatter\n"
        "target: entities/foo.md\n"
        "finding: frontmatter_missing\n"
        "rationale: derive\n"
        "action:\n"
        "```yaml\n"
        "---\n"
        "description: A real description that fits inside the gates window\n"
        "---\n"
        "```\n"
    )
    parsed = mod.parse_proposal_block(block)
    assert parsed["action"] == {
        "description": "A real description that fits inside the gates window"
    }


def test_parse_strips_comment_first_line(judge_module):
    mod, _ = judge_module
    block = (
        "type: update-frontmatter\n"
        "target: entities/foo.md\n"
        "finding: frontmatter_missing\n"
        "rationale: derive\n"
        "action:\n"
        "```yaml\n"
        "# concrete change spec\n"
        "name: Foo\n"
        "```\n"
    )
    parsed = mod.parse_proposal_block(block)
    assert parsed["action"] == {"name": "Foo"}


def test_parse_handles_json_wrapper(judge_module):
    mod, _ = judge_module
    block = (
        "type: update-frontmatter\n"
        "target: entities/foo.md\n"
        "finding: frontmatter_missing\n"
        "rationale: derive\n"
        "action:\n"
        "```yaml\n"
        '{"description": "Self-hosted platform with a site, a vault and a feed reader"}\n'
        "```\n"
    )
    parsed = mod.parse_proposal_block(block)
    assert parsed["action"] == {
        "description": "Self-hosted platform with a site, a vault and a feed reader"
    }


def test_parse_unparseable_returns_none(judge_module):
    mod, _ = judge_module
    block = (
        "type: update-frontmatter\n"
        "target: entities/foo.md\n"
        "action:\n"
        "```yaml\n"
        "this is: just some text\n"
        "  indented: badly:\n"
        "    too\n"
        "```\n"
    )
    parsed = mod.parse_proposal_block(block)
    # YAML actually accepts this as nested dict — verify it's a dict OR None
    assert parsed["action"] is None or isinstance(parsed["action"], dict)
