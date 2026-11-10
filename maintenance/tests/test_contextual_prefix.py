"""Pytest suite for the chunker's section context (scripts/contextual-prefix.py)."""
from __future__ import annotations
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "contextual-prefix.py"
FILL = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 12   # ~680 chars


def load(monkeypatch, tmp_path, headings=None):
    monkeypatch.setenv("WIKI_VAULT", str(tmp_path / "vault"))
    if headings is None:
        monkeypatch.delenv("WIKI_PREFIX_HEADINGS", raising=False)
    else:
        monkeypatch.setenv("WIKI_PREFIX_HEADINGS", headings)
    spec = importlib.util.spec_from_file_location("ctxprefix_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BODY = "\n\n".join([
    "# Page title", "The page opens here.",
    "## Alpha", FILL, FILL, FILL, FILL,
    "### Alpha one", FILL, FILL, FILL,
    "## Beta", FILL, FILL, FILL, FILL,
])


def test_chunks_are_the_same_with_and_without_paths(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    plain = mod.chunk_body(BODY)
    paired = mod.chunk_body(BODY, with_headings=True)
    assert [c for c, _ in paired] == plain
    assert len(plain) >= 4


def test_path_is_the_section_in_force_where_the_chunk_starts(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    paired = mod.chunk_body(BODY, with_headings=True)
    assert paired[0][1] == ()                      # opens the page: nothing above it
    paths = [p for _, p in paired]
    assert ("Alpha",) in paths                      # a chunk that starts mid-section
    assert ("Alpha", "Alpha one") in paths
    assert ("Beta",) in paths
    for chunk, path in paired:                      # a heading the chunk shows is not repeated
        first_new = chunk.split("\n\n")[0]
        if first_new.startswith("## "):
            assert path == ()


def test_leading_heading_gets_its_ancestors_only(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    t = mod.HeadingTracker()
    assert t.feed("# Title") == ()
    assert t.feed("## Alpha\ntext under alpha") == ()
    assert t.feed("plain text") == ("Alpha",)
    assert t.feed("### Deep\ntext") == ("Alpha",)
    assert t.feed("more") == ("Alpha", "Deep")
    assert t.feed("## Beta") == ()                  # a sibling drops the old section
    assert t.feed("after") == ("Beta",)


def test_fenced_comment_is_not_a_heading_and_markup_is_dropped(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    t = mod.HeadingTracker()
    t.feed("## Setup `cmd` and [[some-page|a link]] ~~old~~")
    assert t.path() == ("Setup cmd and a link",)
    t.feed("```sh\n# not a heading\necho hi")
    t.feed("# still inside the fence\n```")
    assert t.path() == ("Setup cmd and a link",)
    t.feed("# Real top level")
    assert t.path() == ()


def test_prefix_carries_the_section_unless_switched_off(monkeypatch, tmp_path):
    fm = {"title": "Thing", "status": "archived", "replaced_by": ["Other"]}
    on = load(monkeypatch, tmp_path)
    assert on.PREFIX_SCHEME == 2
    pre = on.synthetic_prefix(fm, "Thing does X. More.", "chunk", ("Alpha", "Alpha one"))
    assert pre == ('This passage is from the wiki page "Thing". The page opens: Thing does X. '
                   'Section: Alpha > Alpha one. Page status: archived. Replaced by: Other.')
    assert "Section:" not in on.synthetic_prefix(fm, "Thing does X.", "chunk", ())
    off = load(monkeypatch, tmp_path, headings="0")
    assert off.PREFIX_SCHEME == 1
    assert "Section:" not in off.synthetic_prefix(fm, "Thing does X.", "chunk", ("Alpha",))
