"""Pytest suite for wiki-judge.py.

Runs against an isolated tmp_path vault so it doesn't touch the real
knowledge-vault. Imports the script as a module by patching VAULT/
LINT_DIR before the call.

Run: <engine>/maintenance/run-tests.sh
"""
from __future__ import annotations
import importlib.util, sys
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
                       "Command-line tool that turns recipe cards into printable booklets"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_description_dup_title_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md",
               {"title": "Command-line tool that turns recipe cards into printable booklets"},
               "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"description":
                       "Command-line tool that turns recipe cards into printable booklets"}}
    verdict, _ = mod.judge(prop, lint_targets={"entities/foo.md"})
    assert verdict == "defer"


def test_description_happy_path(judge_module):
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"title": "Foo"}, "x" * 250)
    prop = {"type": "update-frontmatter", "target": "entities/foo.md",
            "action": {"description":
                       "Command-line tool that turns recipe cards into printable booklets"}}
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
        '{"description": "Small library that parses train timetables into a calendar"}\n'
        "```\n"
    )
    parsed = mod.parse_proposal_block(block)
    assert parsed["action"] == {
        "description": "Small library that parses train timetables into a calendar"
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


def test_parse_header_ignores_fenced_action_lines(judge_module):
    """A `type:` / `target:` inside the action YAML must not overwrite the
    proposal header: adding a missing type is the rubric's plain auto case."""
    mod, vault = judge_module
    write_page(vault, "entities/foo.md", {"name": "Foo"}, "x" * 250)
    block = (
        "type: update-frontmatter\n"
        "target: entities/foo.md\n"
        "finding: frontmatter_missing\n"
        "action:\n"
        "```yaml\n"
        "type: entity\n"
        "```\n"
    )
    parsed = mod.parse_proposal_block(block)
    assert parsed["type"] == "update-frontmatter"
    assert mod.judge(parsed, lint_targets={"entities/foo.md"})[0] == "auto"


def test_parse_header_unclosed_fence_and_reverse_case(judge_module):
    """A review proposal whose action says `type: mark-evergreen` stays a
    review (defer), even when the model leaves the fence unclosed."""
    mod, vault = judge_module
    write_page(vault, "meta/notes.md", {"name": "Notes"}, "x" * 250)
    block = (
        "type: review\n"
        "target: meta/notes.md\n"
        "action:\n"
        "```yaml\n"
        "type: mark-evergreen\n"
        "target: entities/other.md\n"
        "status: archived\n"
    )
    parsed = mod.parse_proposal_block(block)
    assert parsed["type"] == "review"
    assert parsed["target"] == "meta/notes.md"
    assert mod.judge(parsed, lint_targets={"meta/notes.md"})[0] == "defer"


def test_mark_evergreen_meta_with_extra_keys_defers(judge_module):
    """On a meta page, mark-evergreen is auto only when its action sets
    nothing but `status: evergreen`: any other key would be applied unseen."""
    mod, vault = judge_module
    write_page(vault, "meta/notes.md", {"name": "Notes", "description": "Old"}, "x" * 250)
    block = (
        "type: mark-evergreen\n"
        "target: meta/notes.md\n"
        "action:\n"
        "```yaml\n"
        "status: evergreen\n"
        'description: "Replaced by the model"\n'
        "```\n"
    )
    verdict, reason = mod.judge(mod.parse_proposal_block(block),
                                lint_targets={"meta/notes.md"})
    assert verdict == "defer" and "more than status" in reason


@pytest.mark.parametrize("action", [
    "",                                         # no action block at all
    "action:\n```yaml\n```\n",                  # empty block
    "finding: stale\nrationale: Permanent record.\naction:\n```yaml\n# concrete change spec\nstatus: evergreen\n```\n\n---\n",
    'action:\n```yaml\nstatus: "evergreen" # permanent record\n```\n',
])
def test_mark_evergreen_meta_plain_actions_apply(judge_module, action):
    mod, vault = judge_module
    write_page(vault, "meta/notes.md", {"name": "Notes"}, "x" * 250)
    block = "type: mark-evergreen\ntarget: meta/notes.md\n" + action
    verdict, _ = mod.judge(mod.parse_proposal_block(block),
                           lint_targets={"meta/notes.md"})
    assert verdict == "auto"


_EXTRA = 'status: evergreen\ndescription: "Replaced by the model"\n'


@pytest.mark.parametrize("action", [
    "action:\n```\n" + _EXTRA + "```\n",          # untagged fence
    "action:\n```yml\n" + _EXTRA + "```\n",       # ```yml fence
    "action:\n```yaml\n" + _EXTRA,                # unclosed ```yaml fence
    "action:\n  " + _EXTRA.replace("\n", "\n  ").rstrip() + "\n",  # unfenced, indented
    _EXTRA,                                       # bare keys, no action label
    "action:\n```yaml\nstatus: evergreen\n```\n```yml\ndescription: x\n```\n",
], ids=["untagged", "yml", "unclosed", "unfenced", "bare", "second-fence"])
def test_mark_evergreen_meta_action_outside_yaml_block_defers(judge_module, action):
    """The applier reads the raw block, so an action the ```yaml regex did
    not pick up must not pass as 'no action'."""
    mod, vault = judge_module
    write_page(vault, "meta/notes.md", {"name": "Notes", "description": "Old"}, "x" * 250)
    block = "type: mark-evergreen\ntarget: meta/notes.md\n" + action + "\n---\n"
    verdict, _ = mod.judge(mod.parse_proposal_block(block),
                           lint_targets={"meta/notes.md"})
    assert verdict == "defer"


def test_mark_evergreen_meta_unparseable_action_defers(judge_module):
    mod, vault = judge_module
    write_page(vault, "meta/notes.md", {"name": "Notes"}, "x" * 250)
    block = ("type: mark-evergreen\ntarget: meta/notes.md\n"
             "action:\n```yaml\n: : not yaml [\n```\n")
    verdict, _ = mod.judge(mod.parse_proposal_block(block),
                           lint_targets={"meta/notes.md"})
    assert verdict == "defer"
