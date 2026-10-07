#!/usr/bin/env python3
"""recall.py — per-prompt vault recall for a Claude Code UserPromptSubmit hook.

Experiment started 2026-09-26 (see [[2026-09-25-memory-architecture-review]]).
On every user prompt the hook runs the normal local retrieval (retrieve.py:
BM25 + on-demand llama-embedding rerank, ~1 s, nothing resident) and injects a
SHORT pointer — page, section, one matching line — only when a chunk clears an
absolute similarity threshold. Below it, nothing is injected (0 tokens). The
fused rerank score is a per-query z-score and cannot say "nothing here is
relevant", so the decision uses the raw qwen3 cosine exposed by
`retrieve.py --scores`.

Modes:
  recall.py --hook                 read the hook JSON on stdin, print the
                                   UserPromptSubmit envelope (or nothing)
  recall.py "<prompt>"             debug: show the decision
  recall.py --eval GOLD.json [--cache .git/recall-eval-cache.json]
                                   sweep thresholds on a labelled prompt set

The hook never blocks a prompt: any error means no injection, exit 0.
The log (.git/wiki-recall.log, untracked) stores a hash and the length of
the prompt, never its text.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT_ROOT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
RETRIEVE = Path(__file__).resolve().parent / "retrieve.py"
LOG_PATH = VAULT_ROOT / ".git" / "wiki-recall.log"

QWEN = "text-embedding-qwen3-embedding-0.6b"
NOMIC = "text-embedding-nomic-embed-text-v1.5@f16"
# Calibrated 2026-09-26 on eval/recall-gold.json (73 real prompts: 42 needing
# a vault page, 19 not, 12 trivial): at 0.46 the right page is injected for 34
# of 42 and 5 of 21 irrelevant/trivial prompts get a (mostly borderline)
# pointer; precision 0.81. Numbers and the sweep: vault CLAUDE.md
# §Per-prompt recall. Override for experiments only.
THRESHOLD = float(os.environ.get("WIKI_RECALL_THRESHOLD", "0.46"))
FEATURE = os.environ.get("WIKI_RECALL_FEATURE", "qwen")   # qwen | nomic | mean
MAX_POINTERS = 2
MAX_QUERY_CHARS = 600
EXCERPT_CHARS = 220
RETRIEVE_TIMEOUT_SEC = 8   # the hook itself is capped at 10 s in settings.json

TRIVIAL_RE = re.compile(
    r"^\s*(ok(ay)?|s[iì]|yes|no|va bene|vai|procedi|continua|retry|riprova|"
    r"fai tu|concludi|perfetto|grazie|thanks|go|fatto|chiudiamo)\b[\s.!,]*$",
    re.IGNORECASE)
HARNESS_RE = re.compile(r"<(command-name|local-command-[a-z]+|system-reminder|task-notification)>")
# Some users type a hyphen for the Italian elision apostrophe ("l-indice",
# "dell-archivio"); BM25 keeps hyphenated tokens whole, so "l-indice" never
# matched "indice". Split those, for the retrieval query only.
ELISION_RE = re.compile(
    r"\b(l|un|dell|all|nell|dall|sull|quell|quest|c|d|s|m|t|v|n|po|qual|com|dov|anch)-(?=\w)",
    re.IGNORECASE)
WORD_RE = re.compile(r"[\wÀ-ÿ][\wÀ-ÿ'\-]{3,}", re.UNICODE)


def is_trivial(prompt):
    p = prompt.strip()
    if not p or p.startswith("/") or HARNESS_RE.search(p):
        return True
    if TRIVIAL_RE.match(p):
        return True
    return len(p) < 12 or len(p.split()) < 3


def query_for(prompt):
    return ELISION_RE.sub(r"\1 ", prompt.strip())[:MAX_QUERY_CHARS]


def retrieve(query):
    # The query goes over stdin, not argv: argv is visible to every process
    # (ps) and to endpoint-security agents that log command lines, and this
    # is raw user prompt text.
    proc = subprocess.run(
        [sys.executable, str(RETRIEVE), "-", "--top", "5", "--chunks",
         "--compact", "--scores"],
        input=query.encode("utf-8"),
        capture_output=True, timeout=RETRIEVE_TIMEOUT_SEC, cwd=str(VAULT_ROOT))
    if proc.returncode != 0:
        raise RuntimeError(f"retrieve.py exit {proc.returncode}")
    return json.loads(proc.stdout)


def feature(cand, which=None):
    cos = cand.get("cosine") or {}
    which = which or FEATURE
    if which == "nomic":
        return cos.get(NOMIC)
    if which == "mean":
        vals = [v for v in (cos.get(QWEN), cos.get(NOMIC)) if v is not None]
        return sum(vals) / len(vals) if vals else None
    return cos.get(QWEN)


def excerpt(text, prompt):
    """The line of the chunk sharing most words with the prompt, trimmed."""
    words = {w.lower() for w in WORD_RE.findall(prompt)}
    best, best_n = "", -1
    for line in text.splitlines():
        line = line.strip().lstrip("#>-*| ").strip()
        if len(line) < 25:
            continue
        n = len(words & {w.lower() for w in WORD_RE.findall(line)})
        if n > best_n:
            best, best_n = line, n
    best = re.sub(r"\s+", " ", best.replace("**", "").replace("`", ""))
    return best[:EXCERPT_CHARS].rstrip() + ("…" if len(best) > EXCERPT_CHARS else "")


def section(text):
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def pick(result, threshold, which=None):
    """Pointers (max MAX_POINTERS, distinct pages) whose feature clears the bar."""
    picks, seen = [], set()
    for c in result.get("candidates", []):
        f = feature(c, which)
        if f is None or f < threshold or c.get("page") in seen:
            continue
        seen.add(c.get("page"))
        picks.append(c)
        if len(picks) >= MAX_POINTERS:
            break
    return picks


def decide(prompt, threshold=THRESHOLD):
    if is_trivial(prompt):
        return {"action": "skip", "reason": "trivial", "pointers": []}
    result = retrieve(query_for(prompt))
    if "cosine:" not in result.get("strategy", ""):
        # BM25-only has no absolute relevance signal: stay silent.
        return {"action": "skip", "reason": "no-embedder", "pointers": []}
    cands = result.get("candidates", [])
    top = max((feature(c) or 0 for c in cands), default=0)
    picks = pick(result, threshold)
    return {"action": "inject" if picks else "none", "top": round(top, 4),
            "pointers": [{"page": c["page"], "feature": round(feature(c), 4),
                          "section": section(c.get("text", "")),
                          "excerpt": excerpt(c.get("text", ""), prompt)}
                         for c in picks]}


def render(decision):
    lines = ["Vault recall (automatic, local — may be off-target; ignore it if irrelevant):"]
    for p in decision["pointers"]:
        slug = Path(p["page"]).stem
        sec = f" §{p['section']}" if p["section"] else ""
        lines.append(f"- [[{slug}]] (`{p['page']}`){sec}: \"{p['excerpt']}\"")
    lines.append("Open the page, or run retrieve.py, only if you need more.")
    return "\n".join(lines)


def log_event(prompt, decision, ms):
    try:
        if LOG_PATH.is_file() and LOG_PATH.stat().st_size > 262144:
            tail = LOG_PATH.read_text(encoding="utf-8").splitlines()[-500:]
            LOG_PATH.write_text("\n".join(tail) + "\n", encoding="utf-8")
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
               "chars": len(prompt), "action": decision["action"],
               "reason": decision.get("reason"), "top": decision.get("top"),
               "pages": [p["page"] for p in decision.get("pointers", [])],
               "ms": ms}
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def hook():
    if os.environ.get("WIKI_RECALL") == "0":
        return 0
    try:
        data = json.load(sys.stdin)
        prompt = data.get("prompt") or ""
        t0 = time.time()
        decision = decide(prompt)
        log_event(prompt, decision, int((time.time() - t0) * 1000))
        if decision["action"] == "inject":
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": render(decision)}}, ensure_ascii=False))
    except Exception:
        pass  # never block or pollute a prompt
    return 0


def evaluate(gold_path, cache_path=None):
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    cache = {}
    if cache_path and Path(cache_path).is_file():
        cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    rows = []
    for item in gold:
        prompt = item["prompt"]
        if is_trivial(prompt):
            rows.append((item, None))
            continue
        query = query_for(prompt)
        key = hashlib.sha256(query.encode("utf-8")).hexdigest()
        if key not in cache:
            cache[key] = retrieve(query)
        rows.append((item, cache[key]))
    if cache_path:
        Path(cache_path).write_text(json.dumps(cache), encoding="utf-8")

    kinds = {k: sum(1 for i, _ in rows if i["kind"] == k) for k in ("positive", "negative", "trivial")}
    trivial_skipped = sum(1 for i, r in rows if i["kind"] == "trivial" and r is None)
    nontrivial_skipped = [i["id"] for i, r in rows if i["kind"] != "trivial" and r is None]
    print(f"gold: {kinds}; trivial filtered {trivial_skipped}/{kinds['trivial']}; "
          f"non-trivial filtered as trivial: {nontrivial_skipped}")
    print("feature  thr   | pos: hit  wrong  silent | neg+trivial: injected | precision  recall  | avg ctx chars")
    best = None
    for which in ("qwen", "nomic", "mean"):
        for thr in [round(0.40 + 0.02 * k, 2) for k in range(21)]:
            hit = wrong = silent = neg_inj = n_inj = ctx = 0
            for item, res in rows:
                if res is None:
                    continue
                picks = pick(res, thr, which)
                pages = {c["page"] for c in picks}
                if picks:
                    n_inj += 1
                    ctx += len(render({"pointers": [{"page": c["page"], "section": section(c.get("text", "")),
                                                     "excerpt": excerpt(c.get("text", ""), item["prompt"])}
                                                    for c in picks]}))
                if item["kind"] == "positive":
                    if not picks:
                        silent += 1
                    elif pages & set(item["expected_pages"]):
                        hit += 1
                    else:
                        wrong += 1
                elif picks:
                    neg_inj += 1
            precision = hit / n_inj if n_inj else 0.0
            recall = hit / kinds["positive"] if kinds["positive"] else 0.0
            avg_ctx = ctx / n_inj if n_inj else 0
            print(f"{which:7}  {thr:.2f}  | {hit:9} {wrong:6} {silent:7} | {neg_inj:13} | "
                  f"{precision:9.2f} {recall:7.2f}  | {avg_ctx:8.0f}")
            score = precision * recall
            if best is None or score > best[0]:
                best = (score, which, thr, precision, recall, neg_inj)
    print(f"best precision*recall: feature={best[1]} threshold={best[2]} "
          f"precision={best[3]:.2f} recall={best[4]:.2f} negatives injected={best[5]}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--eval", metavar="GOLD")
    ap.add_argument("--cache", metavar="FILE", help="retrieval cache for --eval")
    args = ap.parse_args()
    if args.hook:
        return hook()
    if args.eval:
        return evaluate(args.eval, args.cache)
    if not args.prompt:
        ap.error("give a prompt, --hook or --eval")
    t0 = time.time()
    d = decide(args.prompt)
    d["ms"] = int((time.time() - t0) * 1000)
    print(json.dumps(d, indent=2, ensure_ascii=False))
    if d["action"] == "inject":
        print("\n" + render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
