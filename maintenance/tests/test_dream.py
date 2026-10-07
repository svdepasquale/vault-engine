"""Pytest suite for dream.py — the deterministic stages only (seed selection,
answer parsing, quote checks, ledger, rendering). The model call is not
exercised here; the pilot run is its test.

Run: <vault>/maintenance/run-tests.sh
"""
from __future__ import annotations
import importlib.util, json, random, sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "dream.py"


@pytest.fixture(scope="module")
def dream():
    spec = importlib.util.spec_from_file_location("dream", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dream"] = mod
    spec.loader.exec_module(mod)
    return mod


def idea(**kw):
    base = {"kind": "synergy", "title": "Reuse the NAS as backup landing zone",
            "idea": "Two sentences.", "first_step": "Measure throughput.",
            "quote_a": "the NAS has an NVMe volume", "quote_b": "the VPS backup needs a landing zone"}
    base.update(kw)
    return base


# ---------- strip_struck ----------

def test_strip_struck_replaces_spans_also_across_lines(dream):
    text = "keep ~~old plan~~ and ~~two\nlines~~ end"
    assert dream.strip_struck(text) == "keep [superseded] and [superseded] end"


# ---------- parse_ideas ----------

def test_parse_ideas_two_ideas_after_prose_with_stray_brace(dream):
    content = ('Sure {here} you go:\n```json\n{"ideas": [' + json.dumps(idea()) + ", "
               + json.dumps(idea(title="Second")) + "]}\n```")
    got = dream.parse_ideas(content)
    assert [i["title"] for i in got] == ["Reuse the NAS as backup landing zone", "Second"]


def test_parse_ideas_empty_list_is_an_answer(dream):
    assert dream.parse_ideas('{"ideas": []}') == []


def test_parse_ideas_wrong_shape_is_none(dream):
    assert dream.parse_ideas("no json at all") is None
    assert dream.parse_ideas('{"relation": "none"}') is None
    assert dream.parse_ideas('{"ideas": [') is None


def test_parse_ideas_drops_non_dict_items(dream):
    assert dream.parse_ideas('{"ideas": ["text", {"title": "x"}]}') == [{"title": "x"}]


# ---------- check_idea ----------

A = "On **the NAS** has an `NVMe volume` mounted. Other text."
B = "> Note: the VPS backup needs a landing zone, measured later."


def test_check_idea_quotes_are_markdown_insensitive(dream):
    i = idea(quote_a="the NAS has an NVMe volume", quote_b="the VPS backup needs a landing zone")
    assert dream.check_idea(i, A, B) is None


def test_check_idea_drops_fabricated_quote(dream):
    assert dream.check_idea(idea(quote_b="the VPS has a second disk"), A, B) == "quote_b not in passage B"


def test_check_idea_quotes_must_come_from_their_own_side(dream):
    i = idea(quote_a="the VPS backup needs a landing zone", quote_b="the NAS has an NVMe volume")
    assert dream.check_idea(i, A, B) == "quote_a not in passage A"


def test_check_idea_shape(dream):
    assert dream.check_idea(idea(first_step=""), A, B) == "missing first_step"
    assert dream.check_idea(idea(kind="refactor"), A, B) == "kind 'refactor'"
    assert dream.check_idea(idea(quote_a="NVMe"), A, B) == "quote_a too short"


# ---------- seed_pages / select_pairs ----------

def test_seed_pages_skips_dead_profile_and_archive_pages(dream):
    v = [1.0, 0.0]
    chunks = [("wiki/entities/a.md", {}, v), ("wiki/entities/old.md", {}, v),
              ("wiki/meta/profile/feedback-x.md", {}, v), ("wiki/folds/fold-k1.md", {}, v),
              ("wiki/overview.md", {}, v)]
    meta = {p: ("active", "entity", "", "?") for p, _, _ in chunks}
    meta["wiki/entities/old.md"] = ("archived", "entity", "", "?")
    assert list(dream.seed_pages(chunks, meta)) == ["wiki/entities/a.md"]


def vecs(n):
    """n unit vectors on a quarter circle: neighbours are similar, ends are not."""
    import math
    return {f"wiki/entities/p{k}.md": [math.cos(k * math.pi / (2 * (n - 1))),
                                       math.sin(k * math.pi / (2 * (n - 1)))] for k in range(n)}


def test_select_pairs_skips_linked_and_ledger_pairs(dream):
    pv = vecs(4)
    links = {"wiki/entities/p0.md": {"p1"}}
    done = {("wiki/entities/p2.md", "wiki/entities/p3.md")}
    pairs, _ = dream.select_pairs(pv, links, 10, (0, 100), random.Random(1), done=done)
    keys = {(a, b) for _, a, b in pairs}
    assert ("wiki/entities/p0.md", "wiki/entities/p1.md") not in keys
    assert ("wiki/entities/p2.md", "wiki/entities/p3.md") not in keys


def test_select_pairs_uses_each_page_once(dream):
    pairs, _ = dream.select_pairs(vecs(6), {}, 10, (0, 100), random.Random(3))
    pages = [p for _, a, b in pairs for p in (a, b)]
    assert len(pages) == len(set(pages)) and len(pairs) == 3


def test_select_pairs_focus_page_is_in_every_pair_and_reusable(dream):
    focus = "wiki/entities/p2.md"
    pairs, _ = dream.select_pairs(vecs(6), {}, 10, (0, 100), random.Random(3), focus=focus)
    assert len(pairs) == 5 and all(focus in (a, b) for _, a, b in pairs)


def test_select_pairs_band_and_seed(dream):
    pv = vecs(8)
    p1, band = dream.select_pairs(pv, {}, 3, (25, 75), random.Random(7))
    p2, _ = dream.select_pairs(pv, {}, 3, (25, 75), random.Random(7))
    assert p1 == p2
    assert all(band[0] - 1e-3 <= c <= band[1] + 1e-3 for c, _, _ in p1)


# ---------- passage ----------

def test_passage_keeps_bridge_chunk_and_strips_struck(dream, tmp_path):
    d = tmp_path / "c-1"
    d.mkdir()
    texts = ["before " * 50, "BRIDGE ~~dead~~ text", "after " * 50]
    for k, t in enumerate(texts):
        (d / f"chunk-{k:03d}.json").write_text(json.dumps({"raw_text": t}))
    chunk = {"raw_text": texts[1], "_path": d / "chunk-001.json"}
    out = dream.passage(chunk, 120)
    assert "BRIDGE [superseded] text" in out and len(out) <= 120
    assert "dead" not in dream.passage(chunk, 10_000)


# ---------- ledger ----------

def test_load_ledger_skips_blank_and_broken_lines(dream, tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text('{"title": "a", "verdict": "rejected", "pages": ["x", "y"]}\n\nnot json\n')
    assert [e["title"] for e in dream.load_ledger(p)] == ["a"]
    assert dream.load_ledger(tmp_path / "missing.jsonl") == []


# ---------- render ----------

def test_render_sorts_by_novelty_and_states_the_review_protocol(dream, tmp_path):
    rec = lambda t, cos: {"cos": 0.4, "page_a": "wiki/entities/a.md", "page_b": "wiki/entities/b.md",
                          "idea": idea(title=t),
                          "near_vault": {"cos": cos, "page": "wiki/entities/c.md", "chunk": "c-1/chunk-000",
                                         "snippet": "s"},
                          "near_ledger": None}
    header = {"date": "2026-10-03", "model": "Qwen3.8-27B", "backend": "evo", "seed": 1, "pairs": 2,
              "band": ["50", "90"], "band_cos": [0.37, 0.51], "vault_sha": "abc"}
    out = tmp_path / "dream.md"
    dropped = [{"page_a": "wiki/entities/a.md", "page_b": "wiki/entities/b.md", "title": "x",
                "reason": "quote_a not in passage A"}]
    dream.render([rec("known", 0.8), rec("new", 0.3)], dropped, header, out)
    text = out.read_text()
    assert text.index("— new") < text.index("— known")
    assert "backend: evo · model: Qwen3.8-27B" in text
    assert "Claude reviews before anything is approved" in text
    assert "dream-ledger.jsonl" in text
    assert "## Dropped by the checks" in text and "quote_a not in passage A" in text


# ---------- review findings (ultrareview of PR #1, 2026-10-04) ----------

def test_load_chunks_without_cache_returns_empty(dream, monkeypatch, tmp_path):
    # R1: a fresh clone has the tracked chunks but not the gitignored embed cache.
    monkeypatch.setattr(dream._scan, "CACHE", tmp_path / "missing" / "embed-cache.json")
    assert dream.load_chunks() == []


def test_frontmatter_status_is_lowercased_and_dead_matches_prefix_script(dream, monkeypatch, tmp_path):
    # R4: statuses compare case-insensitively, and DEAD includes deprecated/erased.
    monkeypatch.setattr(dream, "VAULT_ROOT", tmp_path)
    page = tmp_path / "wiki" / "entities" / "x.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nname: x\nstatus: Archived\n---\nbody\n")
    assert dream.frontmatter("wiki/entities/x.md")[0] == "archived"
    assert {"deprecated", "erased"} <= dream.DEAD
    v = [1.0, 0.0]
    chunks = [("wiki/entities/x.md", {}, v), ("wiki/entities/y.md", {}, v), ("wiki/entities/z.md", {}, v)]
    meta = {"wiki/entities/x.md": ("archived", "entity", "", "?"),
            "wiki/entities/y.md": ("erased", "entity", "", "?"),
            "wiki/entities/z.md": ("deprecated", "entity", "", "?")}
    assert dream.seed_pages(chunks, meta) == {}


def test_embed_survives_os_errors(dream, monkeypatch):
    # R2: an OSError from the embedder (exec format error, ENOSPC) must not
    # escape and lose the proposals file.
    monkeypatch.setattr(dream._rerank, "local_models", lambda: [dream.QWEN])
    def boom(model, texts):
        raise OSError(8, "Exec format error")
    monkeypatch.setattr(dream._rerank, "local_embed_batch", boom)
    assert dream.embed(["an idea"]) is None


def test_embed_without_backend_says_so(dream, monkeypatch, capsys):
    # R3: no silent None.
    monkeypatch.setattr(dream._rerank, "local_models", lambda: [])
    assert dream.embed(["an idea"]) is None
    assert "no local qwen3 embedder" in capsys.readouterr().err


def test_render_header_says_when_novelty_was_not_computed(dream, tmp_path):
    # R3: the header must not claim cosines that were never computed.
    rec = {"cos": 0.4, "page_a": "wiki/entities/a.md", "page_b": "wiki/entities/b.md",
           "idea": idea(), "near_vault": None, "near_ledger": None}
    header = {"date": "2026-10-04", "model": "m", "backend": "evo", "seed": 1, "pairs": 1,
              "band": ["50", "90"], "band_cos": [0.37, 0.51], "vault_sha": "abc"}
    out = tmp_path / "d.md"
    dream.render([rec], [], header, out)
    text = out.read_text()
    assert "NOT computed" in text and "are printed, not used" not in text


def test_replay_ignores_a_stale_focus(dream, monkeypatch, tmp_path, capsys):
    # Cloud review 2026-10-04: --focus was validated even with --replay, whose
    # help says it is ignored, so a stale --focus aborted the replay (exit 2).
    pages = ["wiki/entities/a.md", "wiki/entities/b.md"]
    chunks = [(p, {"_id": f"{p}/0", "_path": tmp_path / "x.json", "raw_text": "t"}, v)
              for p, v in zip(pages, ([1.0, 0.0], [0.0, 1.0]))]
    monkeypatch.setattr(dream, "load_chunks", lambda: chunks)
    monkeypatch.setattr(dream, "frontmatter", lambda p: ("active", "entity", "", "?"))
    monkeypatch.setattr(dream._scan, "page_meta", lambda p: ("?", set()))
    monkeypatch.setattr(dream, "load_ledger", lambda path=None: [])
    replay = tmp_path / "pairs.json"
    replay.write_text(json.dumps({"pairs": [{"page_a": pages[0], "page_b": pages[1]}]}))
    monkeypatch.setattr(sys, "argv", ["dream.py", "--dry", "--replay", str(replay), "--focus", "gone-page"])
    assert dream.main() == 0
    assert "wiki/entities/a.md" in capsys.readouterr().out
