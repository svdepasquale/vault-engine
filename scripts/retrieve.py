#!/usr/bin/env python3
"""retrieve.py — hybrid retrieval orchestrator for the Compound Vault.

Pipeline (v1.7):
  query  →  bm25-index.py query (top-K candidates by BM25 over contextualized chunks)
         →  rerank.py        (cosine on nomic-embed-text vectors via ollama,
                              or no-op if ollama unavailable)
         →  drill            (return chunk pages with absolute paths so the
                              caller can Read them and synthesize)

Loads sibling scripts as Python modules (no subprocess overhead). Falls back
gracefully when index or rerank stage is missing:
- If .vault-meta/bm25/index.json is absent     → exit 10 with friendly message;
                                                  caller falls back to v1.6 legacy
                                                  hot→index→drill read order.
- If .vault-meta/chunks/ is empty              → exit 10 (same).
- If rerank stage cannot embed (no ollama)     → no-op rerank, returns BM25 order.

Output schema (JSON to stdout):
{
  "query": "...",
  "strategy": "bm25+rerank:cosine:nomic-embed-text" | "bm25+noop-rerank",
  "top_k": 5,
  "candidates": [
    {
      "chunk_id": "c-000042:3",
      "page_address": "c-000042",
      "page_path": "wiki/concepts/Foo.md",
      "absolute_path": "/abs/path/to/wiki/concepts/Foo.md",
      "chunk_index": 3,
      "bm25_score": 7.12,
      "rerank_score": 0.81,
      "rerank_source": "cosine:nomic-embed-text",
      "snippet": "... first 200 chars of the chunk ..."
    },
    ...
  ]
}

Usage:
  retrieve.py "your query here"           # standard: BM25 top-20, rerank to top-5
  retrieve.py "query" --top 10            # change result count
  retrieve.py "query" --no-rerank         # skip rerank, BM25-only
  retrieve.py "query" --explain           # include per-stage diagnostics
  retrieve.py "query" --chunks --compact  # chunk-text consumption mode: emit the
                                          # full text of top chunks (minified) so
                                          # the caller synthesizes WITHOUT reading
                                          # whole pages; Read a page only when its
                                          # chunk is insufficient

Exit codes:
  0 — success
  2 — usage error
  10 — feature not provisioned (no chunks or no BM25 index); caller falls back
"""

import os
import argparse
import importlib.util
import json
import sys
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT_ROOT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
SCRIPTS_DIR = Path(__file__).resolve().parent
META_DIR = VAULT_ROOT / ".vault-meta"
CHUNKS_DIR = META_DIR / "chunks"
BM25_INDEX = META_DIR / "bm25" / "index.json"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_PROVISIONED = 10

# Cap per-chunk text in --chunks mode. Chunks average ~2.3k chars; anything
# beyond this is a chunking anomaly, not signal worth paying tokens for.
MAX_CHUNK_TEXT = 6000


def log(msg):
    print(msg, file=sys.stderr)


def import_sibling(name, filename):
    """Import a hyphenated sibling .py file as a Python module.

    Wrapped in try/except (v1.7.2; closes audit M5) so a syntax error or
    missing dependency in a sibling helper produces a friendly diagnostic
    instead of a bare Python traceback at the user's first retrieve call.
    """
    target = SCRIPTS_DIR / filename
    if not target.is_file():
        log(f"ERR: sibling helper {filename} not found at {target}")
        log("  Run `bash bin/setup-retrieve.sh --check` to verify the install.")
        sys.exit(EXIT_NOT_PROVISIONED)
    try:
        spec = importlib.util.spec_from_file_location(name, target)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except (ImportError, SyntaxError, AttributeError) as e:
        log(f"ERR: failed to import sibling helper {filename}: {type(e).__name__}: {e}")
        log("  This likely means the helper script is corrupted or has a syntax error.")
        log("  Run `python3 scripts/<helper>.py --help` directly to see the underlying error.")
        log("  If it persists: re-clone the repo or check `git status` for local damage.")
        sys.exit(EXIT_NOT_PROVISIONED)


def chunk_snippet(chunk_data, max_chars=200):
    text = chunk_data.get("raw_text", "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def main():
    parser = argparse.ArgumentParser(description="Hybrid retrieval over the vault.")
    parser.add_argument("query", help="Natural-language query; '-' reads it from stdin "
                             "(keeps prompt text out of the process list — recall.py uses this)")
    parser.add_argument("--top", type=int, default=5, help="Final result count (post-rerank)")
    parser.add_argument("--bm25-top", type=int, default=20,
                        help="Candidate count from BM25 (pre-rerank)")
    parser.add_argument("--no-rerank", action="store_true",
                        help="Skip the rerank stage; return BM25-only")
    parser.add_argument("--chunks", action="store_true",
                        help="Include full chunk text in output so the caller can "
                             "synthesize from chunks instead of reading whole pages "
                             "(token-saving consumption mode; keeps multiple chunks "
                             "per page instead of collapsing to one).")
    parser.add_argument("--compact", action="store_true",
                        help="Minified JSON with minimal fields (chunk_id, page, "
                             "abs path, score).")
    parser.add_argument("--per-page-cap", type=int, default=2,
                        help="Max chunks from any single page in the final result "
                             "(default 2; 0 disables). Stops one long page from "
                             "monopolising top-K — a 44-chunk archive page used to "
                             "take 3 of 5 slots and bury the page that answered "
                             "the query.")
    parser.add_argument("--explain", action="store_true",
                        help="Include per-stage diagnostics in output")
    parser.add_argument("--allow-remote-ollama", action="store_true",
                        help="Forwarded to rerank.py")
    parser.add_argument("--scores", action="store_true",
                        help="With --compact, also emit bm25 and the raw per-model "
                             "cosines (absolute relevance signal for recall.py)")
    args = parser.parse_args()
    if args.query == "-":
        args.query = sys.stdin.read().strip()

    if not BM25_INDEX.is_file():
        log(f"ERR: no BM25 index at {BM25_INDEX}. Run `bash bin/setup-retrieve.sh` "
            "to provision, or fall back to legacy hot→index→drill.")
        return EXIT_NOT_PROVISIONED
    if not CHUNKS_DIR.is_dir() or not any(CHUNKS_DIR.iterdir()):
        log(f"ERR: no chunks at {CHUNKS_DIR}. Run "
            "`python3 scripts/contextual-prefix.py --all` first.")
        return EXIT_NOT_PROVISIONED

    bm25 = import_sibling("bm25_index", "bm25-index.py")
    reranker = import_sibling("rerank", "rerank.py")

    bm25_hits = bm25.query(args.query, top_k=args.bm25_top)
    log(f"bm25: {len(bm25_hits)} hits")

    candidates = []
    for h in bm25_hits:
        chunk_path = VAULT_ROOT / h["path"]
        try:
            chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        candidates.append({
            "chunk_id": h["chunk_id"],
            "page_address": chunk.get("page_address"),
            "page_path": chunk.get("page_path"),
            "absolute_path": str((VAULT_ROOT / chunk.get("page_path", "")).resolve()),
            "chunk_index": chunk.get("chunk_index"),
            "bm25_score": h["score"],
            "path": h["path"],
            "snippet": chunk_snippet(chunk),
            "_text": chunk.get("raw_text", ""),
        })

    if args.no_rerank:
        final = candidates[:args.top]
        strategy = "bm25-only"
        for c in final:
            c["rerank_score"] = c["bm25_score"]
            c["rerank_source"] = "skipped"
    else:
        # Rerank the FULL candidate set, not just top-K: the dedupe and
        # per-page-cap stages below need material to promote from. Truncating
        # here would make the cap shrink the result set instead of replacing a
        # capped chunk with the next-best page. Final truncation is at the end.
        final = reranker.rerank(
            args.query, candidates, top_k=len(candidates),
            allow_remote=args.allow_remote_ollama,
        )
        # Derive strategy from first candidate's rerank_source
        first_src = (final[0].get("rerank_source") if final else "unknown")
        strategy = f"bm25+rerank:{first_src}"

    # Dedupe by page (we may have multiple chunks of the same page; collapse to
    # best). In --chunks mode dedupe by chunk_id instead: distinct chunks of the
    # same page are distinct evidence and the caller synthesizes from them
    # directly, so collapsing would discard exactly the text we want to return.
    by_key = {}
    for c in final:
        if args.chunks:
            key = c.get("chunk_id") or id(c)
        else:
            # Fall back to chunk_id when page_address is missing, else every
            # address-less chunk collapses under a single None key and all but
            # one are silently dropped from the result set.
            key = c.get("page_address") or c.get("chunk_id") or id(c)
        if key not in by_key or c.get("rerank_score", 0) > by_key[key].get("rerank_score", 0):
            by_key[key] = c
    deduped = list(by_key.values())
    deduped.sort(key=lambda c: c.get("rerank_score", 0), reverse=True)

    # Per-page cap: in --chunks mode dedupe is by chunk_id, so a single long
    # page can legitimately fill the whole result set. That trades breadth for
    # depth exactly when breadth is what makes retrieval hit. Capping at 2
    # raised distinct-pages-per-query 2.7 -> 3.8 and R@3 0.95 -> 1.00 on the
    # 40-query gold set (2026-07-29).
    if args.per_page_cap and args.per_page_cap > 0:
        seen = {}
        capped = []
        for c in deduped:
            page = c.get("page_path") or c.get("page_address")
            n = seen.get(page, 0)
            if n >= args.per_page_cap:
                continue
            seen[page] = n + 1
            capped.append(c)
        deduped = capped

    results = []
    for c in deduped[:args.top]:
        if args.compact:
            rec = {
                "chunk_id": c.get("chunk_id"),
                "page": c.get("page_path"),
                "abs": c.get("absolute_path"),
                "score": round(c.get("rerank_score") or c.get("bm25_score") or 0, 3),
            }
            if args.scores:
                rec["bm25"] = round(c.get("bm25_score") or 0, 3)
                rec["cosine"] = c.get("cosine", {})
            if not args.chunks:
                rec["snippet"] = c.get("snippet")
        else:
            rec = {k: v for k, v in c.items() if not k.startswith("_")}
        if args.chunks:
            text = c.get("_text", "")
            if len(text) > MAX_CHUNK_TEXT:
                text = text[:MAX_CHUNK_TEXT].rstrip() + "…[truncated]"
            rec["text"] = text
            rec.pop("snippet", None)
        results.append(rec)

    out = {
        "query": args.query,
        "strategy": strategy,
        "top_k": args.top,
        "candidates": results,
    }
    if args.explain:
        out["explain"] = {
            "bm25_candidate_count": len(bm25_hits),
            "post_rerank_count": len(final),
            "deduped_count": len(deduped),
            "bm25_top_param": args.bm25_top,
        }

    if args.compact:
        print(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    else:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
