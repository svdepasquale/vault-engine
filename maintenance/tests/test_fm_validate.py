"""Pytest suite for fm-validate.py (synthetic pages only)."""
from __future__ import annotations
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "fm-validate.py"


@pytest.fixture
def fmv(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("fm_validate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "VAULT_ROOT", tmp_path)
    return mod


def kinds(mod, frontmatter: str) -> list[str]:
    return [f["kind"] for f in mod.check_text(f"---\n{frontmatter}---\nbody\n", "wiki/x.md")]


CLEAN = ('name: "Page"\ndescription: One line about C# and F#, a hash with no space before it\n'
         "type: entity\ntags: [a, b]\nstatus: evergreen\nindex: false\nupdated: 2026-01-31\n"
         "title: 'x'\naddress: c-000042\n"
         'relations:\n  hosted_on: "[[box]]"\n  depends_on: ["[[a]]", "[[b]]"]\n'
         '  documents:\n    - "[[c]]"  # a comment inside the block is fine\n')


def test_clean_page_has_no_findings(fmv):
    assert kinds(fmv, CLEAN) == []


def test_unquoted_hash_cuts_the_value(fmv):
    # The incident class: YAML stops at ` #`, the line readers keep the rest.
    found = fmv.check_text("---\nname: n\ndescription: Notes from mock #3 on rate limits\n---\n", "wiki/x.md")
    assert [f["kind"] for f in found] == ["value_mismatch"]
    assert found[0]["key"] == "description" and "'Notes from mock'" in found[0]["detail"]


@pytest.mark.parametrize("fm", [
    'description: "quoted" # trailing comment\n',
    "updated: 2026-01-31 # bumped by hand\n",
    "description: a value that goes on\n  to a second line\n",
])
def test_other_disagreements_are_reported(fmv, fm):
    assert kinds(fmv, fm) == ["value_mismatch"]


def test_quoted_hash_is_fine(fmv):
    assert kinds(fmv, 'description: "Notes from mock #3"\n') == []


@pytest.mark.parametrize("fm, kind", [
    ("description: a: b\n", "yaml_error"),
    ("name: a\nname: b\n", "duplicate_key"),
    ("- a\n- b\n", "not_mapping"),
    ("tags: just-a-string\n", "bad_shape"),
    ('relations:\n  - "[[a]]"\n', "bad_shape"),
    ("relations:\n  hosted_on: 3\n", "bad_shape"),
])
def test_structural_problems(fmv, fm, kind):
    assert kinds(fmv, fm) == [kind]


def test_page_without_frontmatter(fmv):
    assert [f["kind"] for f in fmv.check_text("# Title\nbody\n", "wiki/x.md")] == ["no_frontmatter"]


def test_main_takes_vault_relative_paths_and_exits_on_findings(fmv, tmp_path, capsys):
    good, bad = tmp_path / "wiki/entities/good.md", tmp_path / "wiki/entities/bad.md"
    good.parent.mkdir(parents=True)
    good.write_text(f"---\n{CLEAN}---\nbody\n", encoding="utf-8")
    bad.write_text("---\nname: n\ndescription: cut #here\n---\nbody\n", encoding="utf-8")
    assert fmv.main(["wiki/entities/good.md"]) == 0
    assert fmv.main(["wiki/entities/bad.md"]) == 1
    assert capsys.readouterr().out.startswith("value_mismatch: wiki/entities/bad.md description:")
    assert fmv.main([]) == 1                         # default: every page under wiki/
    assert fmv.main(["wiki/entities/missing.md"]) == 2
