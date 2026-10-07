#!/usr/bin/env python3
"""dream.py — local-LLM ideation over the vault ("dream"), on demand (pilot).

Started 2026-10-03. The divergent sibling of semantic-scan.py: the scan pairs
very SIMILAR passages to find inconsistencies; the dream pairs pages that are
related but not linked and asks for ideas neither page contains. Stages:

1. Seeds (no model): one vector per page (mean of its cached qwen3 chunk
   vectors); page pairs that do not link each other, with a cosine inside a
   percentile band of all such pairs, sampled at random (--seed), each page at
   most once per run. Measured 2026-10-03: mid-band CHUNK pairs read as
   fragment noise (a config dump next to a SaaS audit line), page pairs with
   their descriptions did not. Archived/closed pages, profile pages and
   archive-like pages are never seeds; ~~struck-through~~ text is replaced by
   "[superseded]" so a dead plan is not built on.
2. Dream: a local generative model (a remote llama-server or LM Studio, Qwen3.8
   27B, thinking off unless --think) gets each page's description plus the
   chunk that bridges to the other page, and returns 0-2 ideas as JSON, each
   quoting both passages.
3. Checks (no model): an idea survives only if both quotes appear in the text
   the model was shown (Markdown-insensitive, as in semantic-scan.py). Every
   survivor gets its nearest vault chunk and nearest ledger entry with the raw
   cosine — printed, never used to drop anything (no threshold measured yet).

Output: a proposals file for Claude to review and the user to approve; the
model never edits a page. Verdicts go to maintenance/dream-ledger.jsonl.
"""
import os
import argparse
import importlib.util as _ilu
import json
import math
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT_ROOT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
LEDGER = VAULT_ROOT / "maintenance" / "dream-ledger.jsonl"
# Who the wiki belongs to, for the prompt: private, so it lives in the vault, not here.
_CTX = VAULT_ROOT / "maintenance" / "dream-context.md"
CONTEXT = (_CTX.read_text(encoding="utf-8").strip() if _CTX.is_file() else
           "one person's homelab and tooling notes. Single user, hobby scale, curated by Claude.")
# norm, with_neighbours, page_meta, SKIP, QKEY, CHUNKS and CACHE are the
# semantic scan's; loading them from there keeps the Markdown-insensitive quote
# check and the derived cache key in one place.
_spec = _ilu.spec_from_file_location("semantic_scan", Path(__file__).resolve().parent / "semantic-scan.py")
_scan = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_scan)
_rerank = _scan._rerank
QWEN = "text-embedding-qwen3-embedding-0.6b"

BACKENDS = {
    "lmstudio": ("http://127.0.0.1:1234", "qwen/qwen3.8-27b"),
    # URL from WIKI_DREAM_EVO_URL or WIKI_LINT_EVO_URL (the same box), e.g. http://<llm-box>:8080
    "evo": (os.environ.get("WIKI_DREAM_EVO_URL") or os.environ.get("WIKI_LINT_EVO_URL") or "", "Qwen3.8-27B"),
}
DEAD = {"archived", "decommissioned", "superseded", "closed", "deprecated", "erased"}  # = contextual-prefix.py STALE_STATUSES
SEED_SKIP = re.compile(r"^wiki/(meta/profile/|overview\.md)")
STRUCK = re.compile(r"~~.+?~~", re.S)
KINDS = ("automation", "simplification", "synergy", "experiment", "question")
THINK_CAP = 8192    # the evo server's reasoning-budget for Qwen3.8-27B (its models.ini)
MIN_QUOTE = 15

PROMPT = """You read two pages of a personal knowledge wiki and propose ORIGINAL ideas that come from putting them side by side.

Whose wiki: {context}

What a good idea looks like:
- It needs BOTH pages: something neither page says, which follows from a fact on page A meeting a fact on page B.
- Specific: it names the concrete components, files or steps from the passages.
- Worth it for one person: it removes work, removes a part, reuses something already there, or settles an open question with a cheap test.

Rejected on sight:
- Enterprise patterns: HA, multi-region, on-call, SLAs, compliance, extra environments, "at scale".
- Anything whose price is recurring manual upkeep, or that degrades silently when forgotten.
- Generic advice: "add monitoring", "document it", "write tests", "follow best practices", "consider using X".
- Premises taken from history: "[superseded]" marks deleted text; anything marked cancelled, closed, removed, replaced or "do NOT action" is a past state, not a current fact.
- Reversing a settled decision: text marked "deliberately", "user decision", "decided", "rejected", "accepted risk" or "NOT" records a choice already made with its reason. It is a constraint for your idea, never the problem your idea solves.
- Ideas either page already does, plans or rejected.

Page A: {page_a} (type {type_a}, status {status_a}, updated {upd_a})
Description: {desc_a}
<passage_a>
{text_a}
</passage_a>

Page B: {page_b} (type {type_b}, status {status_b}, updated {upd_b})
Description: {desc_b}
<passage_b>
{text_b}
</passage_b>

Give at most 2 ideas, only ones that clear the bar above; an empty list is a good answer when nothing does. Return ONLY this JSON object, no prose:
{{"ideas": [{{"kind": "automation|simplification|synergy|experiment|question", "title": "<max 80 chars>", "idea": "<2-3 sentences>", "first_step": "<one concrete action>", "quote_a": "<20-200 chars copied exactly from passage A that the idea builds on>", "quote_b": "<20-200 chars copied exactly from passage B>"}}]}}"""


def strip_struck(text):
    """Replace ~~struck~~ spans: the vault keeps dead plans struck through as
    history, and a quote check cannot tell a dead premise from a live one."""
    return STRUCK.sub("[superseded]", text)


def frontmatter(page):
    """(status, type, description, updated) from a page's frontmatter."""
    path = VAULT_ROOT / page
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    fm = m.group(1) if m else ""

    def field(name):
        f = re.search(rf"^{name}:\s*(.*)$", fm, re.M)
        return f.group(1).strip().strip("\"'") if f else ""
    return field("status").lower() or "-", field("type") or "-", field("description"), field("updated") or "?"


def unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def load_chunks():
    """[(page, chunk_dict, unit_vector)] for every chunk with a cached qwen3 vector."""
    if not _scan.CACHE.is_file():           # fresh clone: chunks are tracked, the cache is not
        return []
    cache = json.loads(_scan.CACHE.read_text(encoding="utf-8"))
    out = []
    for p in sorted(_scan.CHUNKS.glob("*/*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        v = cache.get(_scan.QKEY + d.get("body_hash", ""))
        if v:
            d["_path"] = p
            d["_id"] = f"{p.parent.name}/{p.stem}"
            out.append((d.get("page_path", ""), d, unit(v)))
    return out


def seed_pages(chunks, meta):
    """{page: [(chunk, vec)]} for pages allowed as seeds; meta[page] = frontmatter()."""
    pages = {}
    for page, d, v in chunks:
        if _scan.SKIP.match(page) or SEED_SKIP.match(page) or meta[page][0] in DEAD:
            continue
        pages.setdefault(page, []).append((d, v))
    return pages


def select_pairs(page_vecs, links, n, band, rng, focus=None, done=frozenset()):
    """Random page pairs from a cosine percentile band, at most once per page.

    page_vecs: {page: unit vector}; links: {page: set of linked slugs}; band:
    (low, high) percentiles of the cosines of all unlinked candidate pairs;
    focus: a page path every pair must contain; done: page-pair keys already
    in the ledger. Returns [(cos, page_a, page_b)] and the band's (lo, hi)
    cosines."""
    names = sorted(page_vecs)
    cands = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if focus and focus not in (a, b):
                continue
            if Path(b).stem in links.get(a, ()) or Path(a).stem in links.get(b, ()):
                continue
            if tuple(sorted((a, b))) in done:
                continue
            cands.append((dot(page_vecs[a], page_vecs[b]), a, b))
    if not cands:
        return [], (None, None)
    cos = sorted(c[0] for c in cands)
    lo = cos[int(band[0] / 100 * (len(cos) - 1))]
    hi = cos[int(band[1] / 100 * (len(cos) - 1))]
    pool = [c for c in cands if lo <= c[0] <= hi]
    rng.shuffle(pool)    # names are sorted: a seed repeats a selection until the ledger grows
    used, out = set(), []
    for c in pool:
        if (c[1] in used and c[1] != focus) or (c[2] in used and c[2] != focus):
            continue
        used.update((c[1], c[2]))
        out.append(c)
        if len(out) >= n:
            break
    return out, (round(lo, 3), round(hi, 3))


def bridge(chunks_a, vec_b):
    """The chunk of page A closest to page B's vector."""
    return max(chunks_a, key=lambda c: dot(c[1], vec_b))[0]


def passage(d, cap):
    """Bridge chunk plus neighbours (semantic-scan's), struck text replaced,
    cut at cap chars — the bridge chunk is never cut by its neighbours."""
    full = strip_struck(_scan.with_neighbours(d))
    own = strip_struck(d.get("raw_text", ""))
    if len(full) <= cap:
        return full
    if len(own) >= cap:
        return own[:cap]
    start = max(0, full.find(own[:200]) - (cap - len(own)) // 2)
    return full[start:start + cap]


SCHEMA = {"type": "object", "required": ["ideas"], "properties": {"ideas": {
    "type": "array", "maxItems": 2, "items": {
        "type": "object", "required": ["kind", "title", "idea", "first_step", "quote_a", "quote_b"],
        "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                       **{f: {"type": "string"} for f in
                          ("title", "idea", "first_step", "quote_a", "quote_b")}}}}}}


def ask(url, model, prompt, max_tokens, temperature, think=None):
    # think=None: thinking off with both switches, as in wiki-maintenance.sh —
    # llama-server honours chat_template_kwargs.enable_thinking, LM Studio
    # ignores it for qwen3.8 and honours reasoning_effort "none" (measured
    # 2026-09-26 on LM Studio, 2026-10-03 on evo: 0 reasoning tokens either
    # way). think="low"/"medium": the qwen3.8 template's effort level (it
    # rejects anything but xhigh|medium|low). The only hard cap is evo's
    # server-side reasoning-budget 8192 — a per-request reasoning_budget is
    # ignored (2026-10-03: 2224/2224 tokens spent reasoning with 1024 asked)
    # — so the answer gets its max_tokens on top of those 8192.
    payload = {"model": model, "temperature": temperature, "top_p": 0.8,
               "max_tokens": max_tokens + (THINK_CAP if think else 0), "stream": False,
               "chat_template_kwargs": {"enable_thinking": bool(think)},
               "reasoning_effort": think or "none",
               "response_format": {"type": "json_schema",
                                   "json_schema": {"name": "ideas", "strict": True, "schema": SCHEMA}},
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(f"{url}/v1/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        data = json.loads(r.read())
    ch = data["choices"][0]
    usage = data.get("usage", {})
    return {"content": ch["message"].get("content") or "", "finish": ch.get("finish_reason"),
            "secs": round(time.time() - t0, 1),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_chars": len(ch["message"].get("reasoning_content") or "")}


def backend_check(url, model):
    """None when `model` is listed by the server, else the reason. /health
    alone goes green in router mode with no model loaded (the 2026-07-17
    lesson), so the model list is what counts."""
    try:
        with urllib.request.urlopen(f"{url}/v1/models", timeout=5) as r:
            ids = [m.get("id") for m in json.loads(r.read()).get("data", [])]
    except Exception as e:
        return f"{url} unreachable ({e})"
    return None if model in ids else f"{url} does not serve {model!r} (has: {', '.join(map(str, ids))})"


def parse_ideas(content):
    """The ideas list from {"ideas": [...]}, or None when the answer is not
    that object. raw_decode, not a greedy {...} regex: two ideas plus any
    stray brace in prose would fuse into one unparseable span."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", content):
        try:
            obj, _ = dec.raw_decode(content, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("ideas"), list):
            return [i for i in obj["ideas"] if isinstance(i, dict)]
    return None


def check_idea(idea, text_a, text_b):
    """Reason the idea is dropped, or None. Shape first, then both quotes
    verbatim (Markdown-insensitive) in the passages the model was shown."""
    for f in ("title", "idea", "first_step", "quote_a", "quote_b"):
        if not isinstance(idea.get(f), str) or not idea[f].strip():
            return f"missing {f}"
    if idea.get("kind") not in KINDS:
        return f"kind {idea.get('kind')!r}"
    for side, text in (("a", text_a), ("b", text_b)):
        q = _scan.norm(idea[f"quote_{side}"])
        if len(q) < MIN_QUOTE:
            return f"quote_{side} too short"
        if q not in _scan.norm(text):
            return f"quote_{side} not in passage {side.upper()}"
    return None


def load_ledger(path=LEDGER):
    out = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def embed(texts):
    """qwen3 vectors for texts, document side (no query instruction): an idea
    compared with a chunk is a 'does this already exist' question, not a
    search. Ideas are embedded bare, while the cached chunk vectors include
    each chunk's contextual prefix, so the cosines rank but are not
    chunk-to-chunk comparable. None if no local backend."""
    if not texts:
        return None
    if QWEN not in _rerank.local_models():
        print("novelty: no local qwen3 embedder (llama-embedding + GGUF) — nearest pages not computed",
              file=sys.stderr)
        return None
    try:
        vecs = _rerank.local_embed_batch(QWEN, texts)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
        print(f"novelty: embedding failed ({e}) — nearest pages not computed", file=sys.stderr)
        return None
    return [unit(v) if v else None for v in vecs]


def nearest(vec, items):
    """(cos, item) of the best match among [(unit_vec, item)], or None."""
    best = None
    for v, item in items:
        if v is None:
            continue
        s = dot(vec, v)
        if best is None or s > best[0]:
            best = (s, item)
    return best


def idea_text(i):
    return f"{i['title']}. {i['idea']}"


def render(ideas, dropped, header, out_path):
    """Markdown proposals file. Ideas sorted by nearest-vault cosine, lowest
    first: the top of the file is what the vault knows least about."""
    ideas = sorted(ideas, key=lambda r: (r["near_vault"] or {}).get("cos", 0.0))
    lines = [
        f"# Dream proposals ({header['date']}, {header['model']} via {header['backend']}, "
        f"{len(ideas)} ideas from {header['pairs']} page pairs)", "",
        f"backend: {header['backend']} · model: {header['model']} · think: {header.get('think', 'off')} · "
        f"seed: {header['seed']} · "
        + (f"replay: {header['replay']} · " if header.get("replay") else
           f"band: p{header['band'][0]}-p{header['band'][1]} "
           f"(cos {header['band_cos'][0]}-{header['band_cos'][1]}) · ")
        + f"vault: {header['vault_sha']}", "",
        "Local-model ideas. **Claude reviews before anything is approved** — for each idea:",
        "1. Check every factual premise in the idea against the live pages, not only the two quotes "
        "(quotes prove the text exists, not that it is current).",
        "2. Read the nearest vault chunk: already done, planned, or rejected? Then it is not new.",
        "3. Apply the user's value filter: single user, no enterprise patterns, no recurring manual upkeep.",
        "4. Present the endorsed ideas to the user in one message, verdict + one-line reason each, "
        "discarded ones listed with the reason. File only what the user approves, on the page it concerns.",
        "5. Append every verdict to `maintenance/dream-ledger.jsonl`.", "",
        ("Nearest-chunk cosines are printed, not used to drop anything: no threshold is measured yet."
         if any(r.get("near_vault") for r in ideas) or not ideas else
         "Nearest-chunk cosines were NOT computed (no local embedder): do step 2 with retrieve.py."), "",
    ]
    for k, r in enumerate(ideas, 1):
        i = r["idea"]
        lines += [f"## {k}. {i['kind']} — {i['title']}", "",
                  f"- Pages: [[{Path(r['page_a']).stem}]] × [[{Path(r['page_b']).stem}]] (page cos {r['cos']})",
                  f"- Idea: {i['idea']}",
                  f"- First step: {i['first_step']}",
                  f"- A: \"{i['quote_a']}\"",
                  f"- B: \"{i['quote_b']}\""]
        nv = r["near_vault"]
        if nv:
            lines.append(f"- Nearest in vault: [[{Path(nv['page']).stem}]] `{nv['chunk']}` "
                         f"(cos {nv['cos']}) \"{nv['snippet']}\"")
        nl = r["near_ledger"]
        if nl:
            lines.append(f"- Nearest in ledger: \"{nl['title']}\" — {nl['verdict']} "
                         f"(cos {nl['cos']})")
        lines.append("")
    if dropped:
        lines += ["## Dropped by the checks", ""]
        lines += [f"- [[{Path(d['page_a']).stem}]] × [[{Path(d['page_b']).stem}]]: "
                  f"\"{d['title']}\" — {d['reason']}" for d in dropped]
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backend", choices=sorted(BACKENDS), default="lmstudio",
                    help="a remote llama-server (WIKI_DREAM_EVO_URL) or LM Studio on this Mac; no fallback between them")
    ap.add_argument("--url", default=None, help="override the backend URL")
    ap.add_argument("--model", default=None, help="override the backend's default model id")
    ap.add_argument("--pairs", type=int, default=12)
    ap.add_argument("--band", type=float, nargs=2, default=None, metavar=("LOW", "HIGH"),
                    help="percentile band of unlinked page-pair cosines (default 50 90; 70 100 with --focus)")
    ap.add_argument("--focus", default=None, help="only pairs with this page (slug or wiki/ path)")
    ap.add_argument("--seed", type=int, default=None,
                    help="random seed (default: time-based, printed); exact repeats need --replay")
    ap.add_argument("--replay", default=None, metavar="JSON",
                    help="reuse all page pairs of an earlier --json output (A/B of a prompt or model); "
                         "--pairs and --focus are ignored")
    ap.add_argument("--max-chars", type=int, default=4000, help="passage cap per side")
    ap.add_argument("--max-tokens", type=int, default=1200)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--think", choices=("low", "medium"), default=None,
                    help="thinking on at this effort (evo only, capped server-side at 8192 tokens); default off")
    ap.add_argument("--dry", action="store_true", help="list the selected pairs and exit")
    ap.add_argument("--out", default=str(VAULT_ROOT / "wiki" / ".lint" / "dream-proposals.md"))
    ap.add_argument("--json", default=None, help="raw results JSON path")
    args = ap.parse_args()

    if args.band is None:
        # With --focus, p50-p90 paired the NAS with a service-mesh design page;
        # p70-p100 gave its nearest unlinked pages (2026-10-03, one run).
        args.band = (70, 100) if args.focus else (50, 90)
    url, model = BACKENDS[args.backend]
    if not url:
        sys.exit("--backend evo needs WIKI_DREAM_EVO_URL (or WIKI_LINT_EVO_URL), e.g. http://<llm-box>:8080")
    url, model = args.url or url, args.model or model
    seed = args.seed if args.seed is not None else int(time.time()) % 100000
    rng = random.Random(seed)

    chunks = load_chunks()
    meta = {page: frontmatter(page) for page in {c[0] for c in chunks}}
    pages = seed_pages(chunks, meta)
    if not pages:
        print(f"no seed pages: no cached vectors under {_scan.QKEY!r}? run "
              "`python3 ~/projects/vault-engine/scripts/rerank.py --embed-all` first", file=sys.stderr)
        return 2
    page_vecs = {p: unit([sum(v[k] for _, v in cs) for k in range(len(cs[0][1]))])
                 for p, cs in pages.items()}
    links = {p: _scan.page_meta(p)[1] for p in pages}
    focus = None
    if args.focus and not args.replay:      # --replay ignores --focus, so don't validate it
        hits = [p for p in pages if p == args.focus or Path(p).stem == args.focus]
        if not hits:
            print(f"--focus {args.focus!r}: not a seed page (archived, excluded or unknown)", file=sys.stderr)
            return 2
        focus = hits[0]
    ledger = load_ledger()
    done = {tuple(sorted(e["pages"])) for e in ledger if len(e.get("pages", [])) == 2}
    if args.replay:
        old = json.loads(Path(args.replay).read_text(encoding="utf-8"))["pairs"]
        pairs = [(dot(page_vecs[p["page_a"]], page_vecs[p["page_b"]]), p["page_a"], p["page_b"])
                 for p in old if p["page_a"] in pages and p["page_b"] in pages]
        band_cos = (None, None)
    else:
        pairs, band_cos = select_pairs(page_vecs, links, args.pairs, args.band, rng, focus, done)
    if not pairs:
        print("no candidate pairs in the band", file=sys.stderr)
        return 2
    if args.dry:
        print(f"seed {seed} · band p{args.band[0]:g}-p{args.band[1]:g} = cos {band_cos[0]}-{band_cos[1]}")
        for s, a, b in pairs:
            print(f"{s:.3f}  {a}  <>  {b}  [{bridge(pages[a], page_vecs[b])['_id']} | "
                  f"{bridge(pages[b], page_vecs[a])['_id']}]")
        return 0

    if args.think and args.backend != "evo":
        print("--think relies on evo's server-side reasoning-budget cap; LM Studio has none "
              "and reasoned for 494 s without answering (2026-09-26)", file=sys.stderr)
        return 2
    why = backend_check(url, model)
    if why:
        print(f"backend {args.backend}: {why}", file=sys.stderr)
        return 3

    results, ideas, dropped = [], [], []
    for k, (s, a, b) in enumerate(pairs, 1):
        da, db = bridge(pages[a], page_vecs[b]), bridge(pages[b], page_vecs[a])
        ta, tb = passage(da, args.max_chars), passage(db, args.max_chars)
        (st_a, ty_a, de_a, up_a), (st_b, ty_b, de_b, up_b) = meta[a], meta[b]
        prompt = PROMPT.format(context=CONTEXT, page_a=a, type_a=ty_a, status_a=st_a, upd_a=up_a, desc_a=de_a or "-",
                               text_a=ta, page_b=b, type_b=ty_b, status_b=st_b, upd_b=up_b,
                               desc_b=de_b or "-", text_b=tb)
        try:
            r = ask(url, model, prompt, args.max_tokens, args.temperature, args.think)
        except Exception as e:
            r = {"content": "", "finish": f"error: {e}", "secs": None}
        got = parse_ideas(r["content"]) if r.get("finish") == "stop" else None
        kept = 0
        for idea in got or []:
            reason = check_idea(idea, ta, tb)
            rec = {"cos": round(s, 3), "page_a": a, "page_b": b,
                   "chunk_a": da["_id"], "chunk_b": db["_id"], "idea": idea}
            if reason:
                dropped.append({**rec, "title": str(idea.get("title", "?"))[:80], "reason": reason})
            else:
                ideas.append(rec)
                kept += 1
        results.append({"n": k, "cos": round(s, 3), "page_a": a, "page_b": b,
                        "ideas": len(got) if got is not None else None, "kept": kept,
                        **{x: r.get(x) for x in ("finish", "secs", "prompt_tokens", "completion_tokens",
                                                   "reasoning_chars")},
                        "raw": None if got is not None else r["content"][:2000]})
        print(f"[{k}/{len(pairs)}] {s:.3f} ideas={len(got) if got is not None else 'UNPARSED'} "
              f"kept={kept} {r.get('secs')}s finish={r.get('finish')} {a} <> {b}", flush=True)
        if args.json:
            Path(args.json).write_text(json.dumps({"pairs": results, "ideas": ideas, "dropped": dropped},
                                                  indent=1, ensure_ascii=False), encoding="utf-8")

    # Novelty context, one embedding batch: new ideas + ledger entries.
    led = [e for e in ledger if e.get("title")]
    vecs = embed([idea_text(r["idea"]) for r in ideas] + [f"{e['title']}. {e.get('idea', '')}" for e in led])
    vault_items = [(v, (p, d)) for p, d, v in chunks]
    for n, r in enumerate(ideas):
        r["near_vault"] = r["near_ledger"] = None
        if not vecs or vecs[n] is None:
            continue
        nv = nearest(vecs[n], vault_items)
        if nv:
            p, d = nv[1]
            snip = re.sub(r"\s+", " ", strip_struck(d.get("raw_text", ""))).strip()[:160]
            r["near_vault"] = {"cos": round(nv[0], 3), "page": p, "chunk": d["_id"], "snippet": snip}
        nl = nearest(vecs[n], list(zip(vecs[len(ideas):], led)))
        if nl:
            r["near_ledger"] = {"cos": round(nl[0], 3), "title": nl[1]["title"],
                                "verdict": nl[1].get("verdict", "?")}

    sha = subprocess.run(["git", "-C", str(VAULT_ROOT), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "?"
    header = {"date": time.strftime("%Y-%m-%d"), "model": model, "backend": args.backend,
              "think": args.think or "off", "replay": args.replay and Path(args.replay).name,
              "seed": seed, "pairs": len(results), "band": [f"{x:g}" for x in args.band],
              "band_cos": band_cos, "vault_sha": sha}
    render(ideas, dropped, header, Path(args.out))
    if args.json:
        Path(args.json).write_text(json.dumps({"header": header, "pairs": results, "ideas": ideas,
                                               "dropped": dropped}, indent=1, ensure_ascii=False),
                                   encoding="utf-8")
    secs = [r["secs"] for r in results if r.get("secs")]
    print(json.dumps({"pairs": len(results), "seed": seed,
                      "unparsed": sum(1 for r in results if r["ideas"] is None),
                      "ideas_raw": sum(r["ideas"] or 0 for r in results),
                      "ideas_kept": len(ideas), "dropped": len(dropped),
                      "secs_total": round(sum(secs), 1),
                      "secs_per_pair": round(sum(secs) / len(secs), 1) if secs else None,
                      "out": args.out}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
