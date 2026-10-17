#!/usr/bin/env python3
"""run-eval.py — retrieval quality gate for the vault.

Scores page-level recall@k and MRR by driving the SHIPPED scripts/retrieve.py
exactly as CLAUDE.md tells Claude to call it. Deliberately not a unit test and
deliberately not a reimplementation of the pipeline: it measures what a session
would actually get back.

Why this exists: the v1.7.5 round (2026-07-29) found three defects that had
been live since rollout — a prefix-conditioned embedding model used without its
task prefixes, chunk dirs never garbage-collected, and one long page eating
most of top-K. All three looked like tuning preferences until they were
measured, and one candidate fix (RRF fusion) was the textbook recommendation
that turned out to be *worse* on this corpus. Retune against numbers, not vibes.

Usage:
  python3 eval/run-eval.py          # top-3, the documented default
  python3 eval/run-eval.py 5        # compare against a wider window
  python3 eval/run-eval.py --gold goldset-it.json   # Italian paired gold set
  python3 eval/run-eval.py --save /tmp/a.json       # keep each query's rank, labels, scores
  python3 eval/run-eval.py --compare /tmp/a.json    # list the queries that changed since

Uses the same local embedding backend as retrieve.py (Homebrew llama-embedding
on the GGUFs, or the HTTP backend named by WIKI_EMBED_URL); without it the rerank
silently falls back to BM25 order, so the script warns rather than reporting
numbers that look comparable but aren't. A partially-available fused config
degrades to the surviving model and still reports numbers — check stderr, not
just this summary.

Provenance: every run prints the gold set's SHA-256 (first 12 hex), the vault
HEAD (+dirty when tracked files differ from it), a content hash of the BM25
index (each chunk's id, body_hash and length; the index is untracked and any
session's SessionEnd rebuilds it, which HEAD does not show — checked again at the
end of the run) and the engine HEAD. Record them with any baseline row in CLAUDE.md
§Acceptance gate: rows measured on different gold or index hashes are not
comparable line-for-line (the 2026-07-29 baseline below is the example).

Maintaining the gold set: `eval/goldset.json` is a list of
{"q": <realistic question>, "gold": [<page paths that answer it>]}. Pages get
renamed and merged, so a miss may mean the gold set drifted rather than that
retrieval regressed — check the page still exists before chasing a fix.

Knowledge-update items (2026-10-06, `goldset-ku*.json`) add "current" and
optionally "stale": case-insensitive markers matched against each returned
chunk's text after dropping `*` and backticks. Page recall cannot see a stale
fact winning — the stale chunk usually sits on a gold page — so these score
the facts: a chunk with any current marker is C, else one with a stale marker
is S, else -. KU@1 = the top chunk is C; KU-order = a C chunk is in the
window and no S chunk ranks above the first one. When a fact changes, update
its markers (the item is the measurement of the consolidation pass, vault
CLAUDE.md §Consolidate).

Answer markers on the standard sets (2026-10-17, after the exposed-evidence
metric of a sibling project, obsidian-second-brain): items that ask for a fact
carry "current" markers too — the answer as the page states it; a marker may
share words with the question but is never made of question words alone. Page recall cannot see the right page coming back with the wrong
chunk, which costs the caller a full-page read; these can. With no "stale"
marker anywhere in the set, the same labels print as A@1 (the top chunk states
the answer) and A@k (a chunk in the window does). Topic items — a design doc,
an inventory, a whole project — carry none: for them the page is the answer.

Baseline: the CURRENT figures live in exactly one place — vault-root CLAUDE.md
§Acceptance gate (both gold sets, dated). Historical figures below are context,
not the gate.
Historical (2026-08-25 post-maintenance, 40 queries, --top 3, fused nomic+qwen3-0.6b):
  goldset.json     R@1 0.88  R@3 1.00  MRR 0.933  ~1528 tok  2.23 distinct pages
  goldset-it.json  R@1 0.80  R@3 0.95  MRR 0.867  ~1531 tok  2.20 distinct pages

Earlier the same day, pre-maintenance, these read 0.90/1.00/0.946 and
0.82/0.95/0.879. The drop is CONTENT, not configuration: the maintenance pass
deleted a duplicated copy of one page, and that page had been riding its own
duplicate chunks to rank 1 on one of the gold queries. It now sits at rank 3 behind its own sub-page, which is the
honest ranking. Do not "fix" this by re-inflating the page.

The 2026-07-29 single-nomic baseline was R@1 0.88 / R@3 1.00 / MRR 0.925 against
a gold set that has since been corrected (the hot-cache query gained
a profile page that genuinely answers it), so it is NOT comparable
line-for-line. On the corrected set single-nomic scores 0.90 / 1.00 / 0.938 EN
and 0.65 / 0.88 / 0.754 IT.
"""
import os
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def _norm(s):
    return re.sub(r"[*`]", "", s).lower()


def ku_labels(item, cands):
    cur = [_norm(m) for m in item["current"]]
    stale = [_norm(m) for m in item.get("stale", [])]
    out = ""
    for c in cands:
        t = _norm(c.get("text", ""))
        out += "C" if any(m in t for m in cur) else "S" if any(m in t for m in stale) else "-"
    return out

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
# `--gold PATH` selects an alternative gold set; bare positional stays the
# top-K so the documented `run-eval.py [K]` invocation is unchanged. Relative
# paths resolve against the vault's eval/ (the gold sets are vault data), not the cwd.
_args = sys.argv[1:]
if {"-h", "--help"} & set(_args):
    print(__doc__)
    sys.exit(0)
GOLD = VAULT / "eval" / "goldset.json"
ENGINE = Path(__file__).resolve().parent.parent
if "--gold" in _args:
    _i = _args.index("--gold")
    if _i + 1 >= len(_args):
        sys.exit("--gold needs a path")
    GOLD = Path(_args[_i + 1])
    if not GOLD.is_absolute():
        GOLD = VAULT / "eval" / GOLD
    del _args[_i:_i + 2]


def _pop_path(flag):
    """`--save PATH` / `--compare PATH`: per-query records, and the diff
    against a saved run — the misses list alone cannot show a rank 1 → 2 flip."""
    if flag not in _args:
        return None
    i = _args.index(flag)
    if i + 1 >= len(_args):
        sys.exit(f"{flag} needs a path")
    path = Path(_args[i + 1])
    del _args[i:i + 2]
    return path


SAVE, COMPARE = _pop_path("--save"), _pop_path("--compare")
TOP = int(_args[0]) if _args else 3

gold_bytes = GOLD.read_bytes()
gold = json.loads(gold_bytes)
GOLD_SHA = hashlib.sha256(gold_bytes).hexdigest()[:12]


def _git(*args, cwd=VAULT):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


HEAD = (_git("rev-parse", "--short", "HEAD") or "unknown") + (
    "+dirty" if _git("status", "--porcelain", "--untracked-files=no") else "")
ENGINE_HEAD = (_git("rev-parse", "--short", "HEAD", cwd=ENGINE) or "unknown") + (
    "+dirty" if _git("status", "--porcelain", "--untracked-files=no", cwd=ENGINE) else "")
# The index is untracked and any session's SessionEnd rebuilds it: a HEAD change
# does not say whether or when it was rebuilt, and a rebuild from uncommitted
# edits leaves HEAD as it was. Its content hash does say: each chunk's id,
# body_hash and indexed length (not the file bytes, which carry the build time).
_bm25 = VAULT / ".vault-meta" / "bm25" / "index.json"


def _index_sha():
    if not _bm25.is_file():
        return "none"
    try:
        docs = json.loads(_bm25.read_text(encoding="utf-8")).get("docs", {})
    except (json.JSONDecodeError, OSError):
        return "unreadable"
    key = sorted((cid, d.get("body_hash") or d.get("path"), d.get("dl")) for cid, d in docs.items())
    return hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()[:12]


INDEX_SHA = _index_sha()
r1 = r3 = rk = 0
mrr = 0.0
chars = 0
pages_total = 0
misses = []
rows = []  # per query, for --save / --compare
ku = []  # (q, labels) for knowledge-update items
degraded = False
t0 = time.time()

for item in gold:
    out = subprocess.run(
        ["python3", str(ENGINE / "scripts" / "retrieve.py"), item["q"],
         "--top", str(TOP), "--chunks", "--compact"],
        cwd=VAULT, capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        print(f"FAIL rc={out.returncode} q={item['q']}\n{out.stderr[-400:]}")
        continue
    d = json.loads(out.stdout)
    if "noop" in d.get("strategy", ""):
        degraded = True
    cands = d["candidates"]
    pages = [c["page"] for c in cands]
    chars += sum(len(c.get("text", "")) for c in cands)
    pages_total += len(set(pages))
    labels = ku_labels(item, cands) if item.get("current") else None
    if labels is not None:
        ku.append((item["q"], labels))
    g = set(item["gold"])
    rank = next((i + 1 for i, p in enumerate(pages) if p in g), None)
    rows.append({"q": item["q"], "rank": rank, "labels": labels,
                 "scores": [c.get("score") for c in cands]})
    if rank:
        mrr += 1.0 / rank
        if rank <= 1:
            r1 += 1
        if rank <= 3:
            r3 += 1
        rk += 1
    else:
        misses.append((item["q"], item["gold"], pages))

n = len(gold)
if _index_sha() != INDEX_SHA:
    print(f"WARN: the index was rebuilt during this run ({INDEX_SHA} → {_index_sha()}): "
          "the queries did not all see the same corpus.\n")
if degraded:
    print("WARN: rerank fell back to no-op (no embedding backend) "
          "— these numbers are BM25-only and not comparable to the baseline.\n")
print(f"retrieve.py --top {TOP}   gold={GOLD.name} sha256:{GOLD_SHA}   n={n}   vault={HEAD}   "
      f"index={INDEX_SHA}   engine={ENGINE_HEAD}")
print(f"  R@1={r1/n:.2f}  R@3={r3/n:.2f}  R@{TOP}={rk/n:.2f}  MRR={mrr/n:.3f}")
print(f"  avg_chars={chars/n:.0f} (~{chars/n/4:.0f} tokens)  "
      f"avg_distinct_pages={pages_total/n:.2f}")
print(f"  elapsed={time.time()-t0:.0f}s")
if ku:
    k1 = sum(lab[:1] == "C" for _, lab in ku)
    ko = sum("C" in lab and "S" not in lab[:lab.index("C")] for _, lab in ku)
    if any(item.get("stale") for item in gold):
        print(f"  KU@1={k1/len(ku):.2f}  KU-order={ko/len(ku):.2f}  (n={len(ku)}; "
              f"C current, S stale, - neither)")
        for q, lab in ku:
            flag = "" if lab[:1] == "C" else "   <- stale first" if "S" in lab[:(lab + "C").index("C")] else "   <- no current fact on top"
            print(f"    {lab:<5} {q}{flag}")
    else:
        # Answer markers only: with no stale side, KU-order reduces to "a
        # chunk with the answer is in the window". List only the misses.
        print(f"  A@1={k1/len(ku):.2f}  A@{TOP}={ko/len(ku):.2f}  (n={len(ku)} items with "
              f"answer markers; C answer, - none)")
        for q, lab in ku:
            if lab[:1] != "C":
                flag = "answer below the top chunk" if "C" in lab else "answer not in the window"
                print(f"    {lab:<5} {q}   <- {flag}")
if misses:
    print("\n  MISSES:")
    for q, g, got in misses:
        print(f"    {q}\n      want {g}\n      got  {got[:3]}")
prev = json.loads(COMPARE.read_text(encoding="utf-8")) if COMPARE else None  # before --save may overwrite it
if SAVE:
    SAVE.write_text(json.dumps({"gold": GOLD.name, "gold_sha": GOLD_SHA, "vault": HEAD,
                                "index": INDEX_SHA, "engine": ENGINE_HEAD, "top": TOP,
                                "rows": rows}, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
if prev is not None:
    if (prev.get("gold_sha"), prev.get("top")) != (GOLD_SHA, TOP):
        print(f"\nWARN: {COMPARE.name} was gold {prev.get('gold_sha')} top {prev.get('top')}, "
              f"this run gold {GOLD_SHA} top {TOP}; queries are matched by text")
    if prev.get("index") != INDEX_SHA:
        print(f"\nWARN: the index changed since {COMPARE.name} ({prev.get('index')} → {INDEX_SHA}): "
              f"corpus drift is mixed into every difference below")
    before = {r["q"]: r for r in prev.get("rows", [])}
    changed = [(before[r["q"]], r) for r in rows if r["q"] in before
               and (before[r["q"]]["rank"], before[r["q"]]["labels"]) != (r["rank"], r["labels"])]
    print(f"\n  CHANGED vs {COMPARE.name} (vault {prev.get('vault')}, engine {prev.get('engine')}): "
          f"{len(changed)} of {len(rows)} queries")
    for b, r in changed:
        labs = f"   labels {b['labels']} → {r['labels']}" if b["labels"] or r["labels"] else ""
        print(f"    rank {b['rank'] or '-'} → {r['rank'] or '-'}{labs}   {r['q']}\n"
              f"      scores {b['scores']} → {r['scores']}")
    now = {r["q"] for r in rows}
    for q in sorted(set(before) - now):
        print(f"    missing from this run (FAIL above?): {q}")
    for q in sorted(now - set(before)):
        print(f"    not in {COMPARE.name}: {q}")
