#!/usr/bin/env python3
"""semantic-scan.py — local-LLM semantic consistency scan of the vault (pilot).

Pilot started 2026-09-26 ([[2026-09-25-memory-architecture-review]]). Three
stages, run by hand now and then:

1. Candidate pairs (no model): passages from DIFFERENT pages whose cached
   qwen3 vectors are very similar — where duplicates, contradictions and stale
   copies of the same fact live. Archive-like pages (folds, log, hot, indexes,
   dated meta syntheses, lint/tiling reports) are excluded.
2. Judgment: a local generative model (LM Studio, qwen/qwen3.8-27b by default,
   reasoning_effort "none") labels each pair — contradiction / stale /
   duplicate / none — and must quote the lines that prove it. Each passage
   comes with its neighbouring chunks. Missing links are computed without the
   model (similar passages, no link either way).
3. Anti-fabrication filter (no model): a proposal survives only if every quote
   appears in its passage (whitespace- and Markdown-insensitive).

Output: a proposals markdown file for Claude to verify and apply by hand; the
model never edits a page. Nothing is written into wiki/.
"""
import os
import argparse
import json
import math
import re
import sys
import time
import urllib.request
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT_ROOT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
CHUNKS = VAULT_ROOT / ".vault-meta" / "chunks"
CACHE = VAULT_ROOT / ".vault-meta" / "embed-cache.json"
ENGINE_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(ENGINE_SCRIPTS))
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("rerank", ENGINE_SCRIPTS / "rerank.py")
_rerank = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_rerank)
# Same key rerank.py writes for the local backend — derived, not hardcoded, so
# an EMBED_SCHEME or LOCAL_TAG bump cannot silently leave this scan with zero
# vectors (and zero pairs).
QKEY = f"{_rerank.LOCAL_TAG}text-embedding-qwen3-embedding-0.6b:{_rerank.EMBED_SCHEME}:"
SKIP = re.compile(r"^wiki/(folds/|log\.md|hot\.md|index\.md|.*_index\.md|"
                  r"meta/(lint-report|tiling-report|20\d\d-))")
LINK = re.compile(r"\[\[([^\]|#]+)")
API = "http://127.0.0.1:1234/v1/chat/completions"

PROMPT = """You check a technical wiki (a knowledge base curated by Claude) for consistency between two passages from DIFFERENT pages.

Page A: {page_a} (frontmatter updated: {upd_a}){link_a}
<passage_a>
{text_a}
</passage_a>

Page B: {page_b} (frontmatter updated: {upd_b}){link_b}
<passage_b>
{text_b}
</passage_b>

Go through EVERY factual claim in both passages (versions, statuses, paths, hosts, what is installed or enabled, what runs where, "currently"/"now"/"still" statements, dated decisions) and check it against the other passage. A single outdated line is enough. Then report the single most important finding:
- "contradiction": they state incompatible things about the same fact.
- "stale": one passage presents as current something the other passage shows was later changed, removed or superseded (compare the dates in the text and the pages' updated dates).
- "duplicate": the same substantial block of facts is written out in full in both places (a mention or a one-line summary is not a duplicate).
- "none": every claim is consistent.

Return ONLY a JSON object, no prose:
{{"relation": "...", "quote_a": "<text copied from passage A, 20-200 chars, empty for none>", "quote_b": "<text copied from passage B, 20-200 chars, empty for none>", "explanation": "<max 2 sentences>", "action": "<one concrete edit, max 1 sentence, empty for none>"}}
Copy quotes from the passages, keeping their words exactly."""


def norm(s):
    """Whitespace- and Markdown-insensitive form for the verbatim-quote check.
    Pilot 1 (2026-09-27): all 3 rejected quotes were real text with the
    model's Markdown stripped (**bold**, `code`, '> ' callout markers) — 2 of
    them were correct stale findings. Strip those markers on both sides."""
    s = re.sub(r"(?m)^\s*>\s?", "", s)
    s = s.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip().lower()


def load_pairs(limit, min_cos):
    if not CACHE.is_file():                 # fresh clone: the embed cache is untracked
        return []
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    chunks = []
    for p in sorted(CHUNKS.glob("*/*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["_id"] = f"{p.parent.name}/{p.stem}"
        d["_path"] = p
        page = d.get("page_path", "")
        if SKIP.match(page):
            continue
        v = cache.get(QKEY + d.get("body_hash", ""))
        if not v:
            continue
        n = math.sqrt(sum(x * x for x in v))
        chunks.append((page, d, [x / n for x in v]))
    pairs = []
    for i in range(len(chunks)):
        pa, da, va = chunks[i]
        for j in range(i + 1, len(chunks)):
            pb, db, vb = chunks[j]
            if pa == pb:
                continue
            s = sum(a * b for a, b in zip(va, vb))
            if s >= min_cos:
                pairs.append((s, pa, da, pb, db))
    pairs.sort(key=lambda t: t[0], reverse=True)
    out, seen = [], set()
    for t in pairs:                     # one pair per page pair: the strongest
        key = tuple(sorted((t[1], t[3])))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def with_neighbours(d):
    """The chunk plus its previous and next chunk of the same page. Pilot 1:
    in 2 of 6 misses the contradicting line sat in the neighbouring chunk."""
    p = d["_path"]
    parts = []
    for off in (-1, 0, 1):
        m = re.match(r"chunk-(\d+)", p.stem)
        q = p.parent / f"chunk-{int(m.group(1)) + off:03d}.json" if m else p
        if off and (not m or not q.is_file()):
            continue
        dd = d if off == 0 else json.loads(q.read_text(encoding="utf-8"))
        parts.append(dd.get("raw_text", ""))
    return "\n\n".join(parts)


def page_meta(page):
    path = VAULT_ROOT / page
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    m = re.search(r"^updated:\s*(\S+)", text, re.M)
    links = {l.strip().split("/")[-1] for l in LINK.findall(text)}
    return (m.group(1) if m else "?"), links


def ask(prompt, model, max_tokens, reasoning):
    # LM Studio ignores chat_template_kwargs.enable_thinking for qwen3.8 but
    # honours reasoning_effort (measured 2026-09-26: "none" -> 0 reasoning
    # tokens; with reasoning on, one pair took 494 s and ran out of budget).
    payload = {"model": model, "temperature": 0.1, "max_tokens": max_tokens,
               "stream": False, "messages": [{"role": "user", "content": prompt}]}
    if reasoning:
        payload["reasoning_effort"] = reasoning
    body = json.dumps(payload).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        data = json.loads(r.read())
    ch = data["choices"][0]
    usage = data.get("usage", {})
    return {"content": ch["message"].get("content") or "", "finish": ch.get("finish_reason"),
            "secs": round(time.time() - t0, 1),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}


def parse(content):
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairs", type=int, default=30)
    ap.add_argument("--min-cos", type=float, default=0.70)
    ap.add_argument("--model", default="qwen/qwen3.8-27b")
    ap.add_argument("--max-tokens", type=int, default=1200)
    ap.add_argument("--reasoning", default="none",
                    help='reasoning_effort sent to LM Studio ("none", "low", ...; "" to omit)')
    ap.add_argument("--dry", action="store_true", help="list candidate pairs and exit")
    ap.add_argument("--out", default=str(VAULT_ROOT / "wiki" / ".lint" / "semantic-proposals.md"))
    ap.add_argument("--json", default=None, help="raw results JSON path")
    args = ap.parse_args()

    pairs = load_pairs(args.pairs, args.min_cos)
    if not pairs:
        print(f"no candidate pairs: no cached vectors under {QKEY!r}? run "
              "`python3 ~/projects/vault-engine/scripts/rerank.py --embed-all` first", file=sys.stderr)
        return 2
    if args.dry:
        for s, pa, da, pb, db in pairs:
            print(f"{s:.3f}  {pa}  <>  {pb}")
        return 0

    results = []
    for k, (s, pa, da, pb, db) in enumerate(pairs, 1):
        upd_a, links_a = page_meta(pa)
        upd_b, links_b = page_meta(pb)
        slug_a, slug_b = Path(pa).stem, Path(pb).stem
        ta = with_neighbours(da)
        tb = with_neighbours(db)
        linked = slug_b in links_a or slug_a in links_b
        prompt = PROMPT.format(
            page_a=pa, upd_a=upd_a, page_b=pb, upd_b=upd_b, text_a=ta, text_b=tb,
            link_a=f" — links to B: {'yes' if slug_b in links_a else 'no'}",
            link_b=f" — links to A: {'yes' if slug_a in links_b else 'no'}")
        try:
            r = ask(prompt, args.model, args.max_tokens, args.reasoning)
        except Exception as e:
            r = {"content": "", "finish": f"error: {e}", "secs": None}
        verdict = parse(r["content"])
        ok_quotes = None
        if verdict and verdict.get("relation") in ("contradiction", "stale", "duplicate"):
            qa, qb = verdict.get("quote_a", ""), verdict.get("quote_b", "")
            ok_quotes = bool(qa and qb and norm(qa) in norm(ta) and norm(qb) in norm(tb))
        rec = {"n": k, "cos": round(s, 3), "page_a": pa, "page_b": pb,
               "chunk_a": da["_id"], "chunk_b": db["_id"], "linked": linked,
               "verdict": verdict, "quotes_verbatim": ok_quotes, **{k2: r[k2] for k2 in r if k2 != "content"},
               "raw": r["content"][:2000] if not verdict else None}
        results.append(rec)
        rel = verdict.get("relation") if verdict else "UNPARSED"
        print(f"[{k}/{len(pairs)}] {s:.3f} {rel:13} quotes={ok_quotes} {r.get('secs')}s "
              f"finish={r.get('finish')} {pa} <> {pb}", flush=True)
        if args.json:
            Path(args.json).write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")

    keep = [r for r in results if r["verdict"] and
            r["verdict"].get("relation") in ("contradiction", "stale", "duplicate") and r["quotes_verbatim"]]
    # Missing links need no model: very similar passages, and neither page
    # links the other (body or frontmatter). Pilot 1: the model talked itself
    # out of an explicit missing link it had been told about.
    links = [r for r in results if not r["linked"]]
    lines = [f"# Semantic scan proposals ({time.strftime('%Y-%m-%d')}, {args.model}, "
             f"{len(keep)} of {len(results)} pairs)", "",
             "Local-model proposals — verify each against both pages before applying. "
             "Quotes were checked verbatim; judgments were not.", ""]
    for r in keep:
        v = r["verdict"]
        lines += [f"## {r['n']}. {v['relation']} — [[{Path(r['page_a']).stem}]] ↔ [[{Path(r['page_b']).stem}]] (cos {r['cos']})", ""]
        if v.get("quote_a"):
            lines.append(f"- A: \"{v['quote_a']}\"")
        if v.get("quote_b"):
            lines.append(f"- B: \"{v['quote_b']}\"")
        lines += [f"- Why: {v.get('explanation', '')}", f"- Action: {v.get('action', '')}", ""]
    if links:
        lines += ["## Missing links (deterministic: similar passages, no link either way)", ""]
        lines += [f"- [[{Path(r['page_a']).stem}]] ↔ [[{Path(r['page_b']).stem}]] (cos {r['cos']})" for r in links]
        lines.append("")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    secs = [r["secs"] for r in results if r.get("secs")]
    rels = {}
    for r in results:
        rel = r["verdict"].get("relation") if r["verdict"] else "UNPARSED"
        rels[rel] = rels.get(rel, 0) + 1
    print(json.dumps({"pairs": len(results), "relations": rels, "kept": len(keep),
                      "missing_links": len(links),
                      "fabricated_quotes": sum(1 for r in results if r["quotes_verbatim"] is False),
                      "secs_total": round(sum(secs), 1),
                      "secs_per_pair": round(sum(secs) / len(secs), 1) if secs else None,
                      "out": args.out}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
