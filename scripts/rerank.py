#!/usr/bin/env python3
"""rerank.py — query-time reranker for chunk candidates.

Takes a query string + a list of candidate chunks (from BM25, vector, or any
upstream stage) and reorders them using semantic similarity.

v1.7 strategy (in preference order, automatically chosen at runtime):
  1. If the embedding backend is reachable AND the models are loadable
       → embed the query, embed each candidate's contextualized_text,
         rank by cosine. Caches per-chunk embeddings in
         .vault-meta/embed-cache.json keyed by body_hash.
  2. Otherwise
       → no-op rerank: return candidates in input order with a synthesized
         note. Caller (retrieve.py) still gets a useful result; downstream
         drill-into-page logic is unchanged.

Future v1.7.x upgrade paths:
  - Cross-encoder reranker (sentence-transformers BGE-base) if installed
  - Cohere Rerank API if COHERE_API_KEY set
  - Voyage Rerank API if VOYAGE_API_KEY set

Mirrors the localhost-only WIKI_EMBED_URL guard from scripts/tiling-check.py:
remote backends require --allow-remote-backend because page bodies
are POSTed as embedding input.

Usage:
  rerank.py "query string" --candidates candidates.json [--top 5]
  rerank.py "query string" --candidates - --top 5    # stdin
  rerank.py --peek "query string"                     # show strategy chosen

Candidates JSON shape:
  [{"chunk_id": "c-000042:3", "path": ".vault-meta/chunks/.../chunk-003.json", "score": 7.1}, ...]

Output: ranked candidates with `rerank_score` added.

Exit codes:
  0 — success
  2 — usage error
  3 — candidate input malformed
  10 — backend unreachable (no-op rerank performed, exit 0 with note)
  11 — model not pulled (no-op rerank performed, exit 0 with note)
"""

import argparse
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT_ROOT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
META_DIR = VAULT_ROOT / ".vault-meta"
EMBED_CACHE_PATH = META_DIR / "embed-cache.json"
CACHE_LOCK = META_DIR / ".embed-cache.lock"

DEFAULT_EMBED_URL = "http://127.0.0.1:1234"
# WIKI_EMBED_MODEL swaps the embedder without editing this file. The cache key
# carries the model name, so vectors from different models coexist safely and
# an A/B does not thrash the cache.
DEFAULT_MODEL = os.environ.get(
    "WIKI_EMBED_MODEL",
    "text-embedding-nomic-embed-text-v1.5@f16,"
    "text-embedding-qwen3-embedding-0.6b")

# nomic-embed-text is task-prefix-conditioned: it was trained with an
# instruction prefix on every input, and embedding raw text runs the model
# out of distribution. Symptom (measured 2026-07-29): cosine scores compress
# into a narrow band (~0.43-0.51 across both relevant and irrelevant chunks),
# so the rerank barely reorders BM25 and sometimes inverts it.
#
# Measured on a 40-query gold set, prefixes vs none: R@1 0.82 -> 0.88,
# MRR 0.879 -> 0.913, at identical cost (they are literally string prefixes).
# Only nomic-* models take these; anything else is embedded verbatim.
# Per-family prefix schemes. Getting this wrong is silent: the 2026-07-29 round
# found nomic running prefix-free and compressing every cosine into 0.43-0.51.
#   nomic-embed-text : symmetric search_query/search_document prefixes.
#   qwen3-embedding  : instruction on the QUERY side only; documents go in raw
#                      (per the Qwen3-Embedding model card). Prefixing documents
#                      too would be the mirror of the 07-29 defect.
PREFIX_SCHEMES = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "qwen3-embedding": (
        "Instruct: Given a search query, retrieve relevant passages that "
        "answer the query\nQuery: ", ""),
}

# Bumped whenever the embedding *input* changes shape. The cache is keyed by
# chunk body_hash, which does not change when we start prefixing — without
# this tag every cached vector would be served as if it were prefixed and the
# fix would silently no-op on all 356 already-embedded chunks.
EMBED_SCHEME = "v6-struck-out"  # 2026-10-06: ~~struck~~ text left the indexed text (v5 2026-09-04: status/replaced_by prefix)


# Comma-separated WIKI_EMBED_MODEL fuses several rerankers (see rerank()).
MODELS = [m.strip() for m in DEFAULT_MODEL.split(",") if m.strip()]


def model_family(model):
    """Family key for a model id, or None if unrecognised.

    Matched by substring rather than by splitting on ":". Ollama named these
    `nomic-embed-text` and `qwen3-embedding:0.6b`; LM Studio names the same
    weights `text-embedding-nomic-embed-text-v1.5@f16` and
    `text-embedding-qwen3-embedding-0.6b`. The old split-on-colon lookup
    matched the Ollama forms and silently missed every LM Studio one.
    """
    for family in PREFIX_SCHEMES:
        if family in model:
            return family
    return None


def prefix_scheme(model):
    """(query_prefix, doc_prefix) for a model. An unknown model is fatal.

    Deliberately no ("", "") fallback: embedding prefix-free is not a degraded
    mode, it is the 2026-07-29 defect documented above, and it fails silently
    with plausible-looking scores. Better to refuse than to quietly regress.
    """
    family = model_family(model)
    if family is None:
        log(f"ERR: no prefix scheme for embedding model {model!r}.")
        log(f"  Known families: {', '.join(sorted(PREFIX_SCHEMES))}")
        log("  Embedding it unprefixed compresses cosines into ~0.43-0.51 and")
        log("  barely reorders BM25. Add the family to PREFIX_SCHEMES instead.")
        sys.exit(EXIT_USAGE)
    return PREFIX_SCHEMES[family]


MODEL_FAMILY = model_family(MODELS[0])

# --- Local backend (2026-09-26) ---------------------------------------------
# LM Studio stays closed by default (user decision 2026-08-29: no resident
# daemon, it costs RAM), which left daily retrieval BM25-only: 6 hybrid vs 27
# BM25-only calls between 2026-09-04 and 09-25. The local backend runs
# Homebrew's `llama-embedding` on the SAME GGUF files LM Studio serves, once
# per query, then exits — nothing stays resident. Measured on the M1 Max:
# nomic f16 0.17 s / 377 MB, qwen3-0.6b Q8 0.60 s / 1.3 GB per query, both
# models in parallel. It is the default whenever the binary and the GGUFs are
# present; WIKI_EMBED_URL forces the HTTP backend (LM Studio, a remote llama-server).
LLAMA_EMBEDDING = (os.environ.get("WIKI_LLAMA_EMBEDDING")
                   or shutil.which("llama-embedding")
                   or "/opt/homebrew/bin/llama-embedding")
GGUF_DIR = Path(os.environ.get("WIKI_GGUF_DIR",
                               str(Path.home() / ".lmstudio" / "models")))
LOCAL_GGUF = {
    "text-embedding-nomic-embed-text-v1.5@f16":
        "nomic-ai/nomic-embed-text-v1.5-GGUF/nomic-embed-text-v1.5.f16.gguf",
    "text-embedding-qwen3-embedding-0.6b":
        "Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf",
}
# Longest chunk is ~5.7k chars (~2k tokens); 4096 keeps every chunk in one
# ubatch (non-causal embedders need the whole sequence in one) while leaving
# RAM small — the model's own n_ctx (32k for qwen3) made RSS balloon.
LOCAL_CTX = 4096
LOCAL_SEP = "<#wiki-embd-sep#>"
LOCAL_TIMEOUT_SEC = 600
# Texts longer than the context make llama-embedding exit 1 instead of
# truncating; real chunks peak at ~1.8k tokens, so this cap only matters for a
# pathological single line (hashes, base64) and keeps it embeddable.
LOCAL_MAX_CHARS = 8000
# Vectors from different runtimes are not guaranteed bit-comparable, so the
# local backend files its vectors under a tagged model name. The key keeps the
# `<model>:<EMBED_SCHEME>:<body_hash>` shape bm25-index.py's GC parses.
LOCAL_TAG = "llamacpp2/"  # v2 (2026-09-26): --no-escape, so text is embedded verbatim like over HTTP


def local_models():
    """MODELS the local backend can serve now ([] when binary/GGUFs missing)."""
    if not (os.path.isfile(LLAMA_EMBEDDING) and os.access(LLAMA_EMBEDDING, os.X_OK)):
        return []
    return [m for m in MODELS
            if m in LOCAL_GGUF and (GGUF_DIR / LOCAL_GGUF[m]).is_file()]


def _run_llama_embedding(model, texts):
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(LOCAL_SEP.join(texts))
        proc = subprocess.run(
            [LLAMA_EMBEDDING, "-m", str(GGUF_DIR / LOCAL_GGUF[model]), "-f", path,
             "--no-escape", "--embd-separator", LOCAL_SEP,
             "--embd-output-format", "array", "-ngl", "99",
             "-c", str(LOCAL_CTX), "-b", str(LOCAL_CTX), "-ub", str(LOCAL_CTX)],
            capture_output=True, timeout=LOCAL_TIMEOUT_SEC)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RuntimeError(f"llama-embedding exit {proc.returncode}: {' | '.join(tail)}")
    vecs = json.loads(proc.stdout)
    if len(vecs) != len(texts):
        raise RuntimeError(f"llama-embedding returned {len(vecs)} vectors for {len(texts)} texts")
    return vecs


def local_embed_batch(model, texts):
    """Embed texts with one short-lived llama-embedding process.

    If the batch fails, retry text by text so one bad chunk costs only its own
    vector (None) instead of dropping the whole query to BM25 order. Callers
    treat the first text (the query) as mandatory.
    """
    if not texts:
        return []
    # An empty prompt can be dropped by the splitter and shift every vector
    # after it onto the wrong text; never send one.
    clean = [((t.replace(LOCAL_SEP, " ")[:LOCAL_MAX_CHARS]) or " ") for t in texts]
    try:
        return _run_llama_embedding(model, clean)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
        if len(clean) == 1:
            raise
        log(f"batch embed failed ({model}): {e} — retrying text by text")
    out = []
    for t in clean:
        try:
            out.append(_run_llama_embedding(model, [t])[0])
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            log(f"embed failed for one text ({model}): {e}")
            out.append(None)
    return out


def pick_backend(allow_remote):
    """(kind, active_models, url): kind is 'local', 'http' or None."""
    if not os.environ.get("WIKI_EMBED_URL"):
        local = local_models()
        if local:
            return "local", local, None
    url = embed_url(allow_remote)
    alive, models = backend_alive(url)
    if not alive:
        return None, [], url
    return "http", [m for m in MODELS if m.split(":")[0] in models], url
BACKEND_TIMEOUT_SEC = 3
EMBED_TIMEOUT_SEC = 30
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CANDIDATES = 3
EXIT_NO_BACKEND = 10
EXIT_NO_MODEL = 11


def log(msg):
    print(msg, file=sys.stderr)


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def embed_url(allow_remote):
    url = os.environ.get("WIKI_EMBED_URL", DEFAULT_EMBED_URL).rstrip("/")
    if not allow_remote:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        if host not in ("127.0.0.1", "localhost", "::1"):
            log(f"ERR: WIKI_EMBED_URL={url} points off-localhost (host={host!r}).")
            log("  Page bodies are POSTed as embedding inputs; keep them on this machine.")
            log("  Either: (a) start the local server — `lms server start`")
            log("  Or:     (b) pass --allow-remote-backend through retrieve.py, which forwards it here.")
            log(f"  Or:     (c) unset WIKI_EMBED_URL to fall back to {DEFAULT_EMBED_URL}.")
            sys.exit(EXIT_USAGE)
    return url


def backend_alive(url):
    """(reachable, [model ids]) from the OpenAI-shaped /v1/models."""
    try:
        req = urllib.request.Request(f"{url}/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=BACKEND_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read(MAX_RESPONSE_BYTES))
            return True, [m.get("id", "") for m in data.get("data", [])]
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return False, []


def embed_one(url, model, text):
    payload = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/v1/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT_SEC) as resp:
        data = json.loads(resp.read(MAX_RESPONSE_BYTES))
        items = data.get("data") or []
        return items[0].get("embedding", []) if items else []


def _live_key(k):
    """True for a key of the current embed scheme and, for local-backend
    vectors, the current LOCAL_TAG (vectors under a superseded tag, e.g.
    llamacpp/ before --no-escape, are never hit again)."""
    return (f":{EMBED_SCHEME}:" in k
            and not (k.startswith("llamacpp") and not k.startswith(LOCAL_TAG)))


def load_cache():
    """Load the embed cache, dropping vectors from a superseded embed scheme.

    Keys are `{model}:{EMBED_SCHEME}:{body_hash}`. Anything else is from an
    older input shape (e.g. the pre-2026-07-29 unprefixed vectors) and is not
    comparable to freshly-embedded ones, so it is pruned rather than kept as
    dead weight — the cache was 2.3MB of vectors that could never be hit again.
    Returns (cache, pruned_count).
    """
    if not EMBED_CACHE_PATH.is_file():
        return {}, 0
    try:
        raw = json.loads(EMBED_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, 0
    kept = {k: v for k, v in raw.items() if _live_key(k)}
    return kept, len(raw) - len(kept)


def save_cache(cache):
    """Persist the embed cache atomically.

    v1.7.2 / closes audit M7: previously used blocking fcntl.LOCK_EX with no
    timeout, which could hang indefinitely on a non-flock-capable filesystem
    (some NFS mounts, network shares, FUSE backends without lock support).
    Now uses LOCK_NB with a 3-attempt retry loop, then falls back to writing
    without the lock (with a WARN) so the rerank pipeline never hangs the
    user's session. The temp + os.replace pattern provides write atomicity
    even without the lock; the lock only serializes concurrent writers.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CACHE_LOCK), os.O_CREAT | os.O_WRONLY, 0o644)
    locked = False
    try:
        for attempt in range(3):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                time.sleep(0.1)
        if not locked:
            msg = ("WARN: rerank embed-cache lock unavailable after 3 tries; "
                   "writing unlocked (atomic via temp+rename). Concurrent writers "
                   "may overwrite each other's last update.")
            log(msg)
            # v1.9.1 / closes audit Data M1: also route to .vault-meta/hook.log so
            # the user sees the event via wiki-lint (stderr alone is invisible to
            # most callers; this matches the hook's logging shape).
            try:
                META_DIR.mkdir(parents=True, exist_ok=True)
                hook_log = META_DIR / "hook.log"
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                with hook_log.open("a", encoding="utf-8") as fh:
                    fh.write(f"{ts} rerank embed-cache lock unavailable; wrote unlocked\n")
            except OSError:
                pass  # never block on a logging failure
        # Merge with what is on disk now: a concurrent retrieve may have added
        # vectors since we loaded, and last-writer-wins would drop them.
        merged = dict(cache)
        if EMBED_CACHE_PATH.is_file():
            try:
                on_disk = json.loads(EMBED_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(on_disk, dict):
                    merged = {**{k: v for k, v in on_disk.items()
                                 if _live_key(k)}, **cache}
            except (json.JSONDecodeError, OSError):
                pass
        tmp = EMBED_CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, EMBED_CACHE_PATH)
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def load_chunk(chunk_rel_path):
    p = VAULT_ROOT / chunk_rel_path
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def rerank(query, candidates, top_k=5, allow_remote=False):
    """Returns candidates list, possibly truncated to top_k, with rerank_score added.
    Falls back to input-order if the backend is unavailable (still adds rerank_source: 'noop').
    """
    if not candidates:
        return []
    kind, active, url = pick_backend(allow_remote)
    if kind is None:
        log("embedding backend unreachable — no-op rerank")
        for c in candidates:
            c["rerank_score"] = float(c.get("score", 0.0))
            c["rerank_source"] = "noop-no-backend"
        return candidates[:top_k]
    # Degrade to the models that ARE available rather than to no rerank at all:
    # a fused config must not turn a missing second model into BM25-only order,
    # which would be a far worse ranking than the surviving model alone.
    if not active:
        log(f"model(s) {', '.join(MODELS)} not available — no-op rerank")
        for c in candidates:
            c["rerank_score"] = float(c.get("score", 0.0))
            c["rerank_source"] = "noop-no-model"
        return candidates[:top_k]
    if len(active) < len(MODELS):
        absent = [m for m in MODELS if m not in active]
        log(f"model(s) {', '.join(absent)} not available — reranking with {', '.join(active)}")
    tag = LOCAL_TAG if kind == "local" else ""

    cache, pruned = load_cache()
    cache_dirty = pruned > 0
    if pruned:
        log(f"embed-cache: pruned {pruned} vectors from a superseded embed scheme")

    # One embedding job per model: the query plus every candidate chunk whose
    # vector is not cached yet. The local backend runs the models in parallel,
    # one short-lived process each.
    chunk_of = {}
    for c in candidates:
        chunk = load_chunk(c.get("path", ""))
        if chunk:
            chunk_of[id(c)] = chunk
    jobs = {}
    for model in active:
        q_prefix, doc_prefix = prefix_scheme(model)
        texts, keys = [q_prefix + query], [None]
        for cid, chunk in chunk_of.items():
            key = f"{tag}{model}:{EMBED_SCHEME}:{chunk.get('body_hash', '')}"
            if key not in cache and key not in keys:
                texts.append(doc_prefix + (chunk.get("contextualized_text")
                                           or chunk.get("raw_text", "")))
                keys.append(key)
        jobs[model] = (texts, keys)

    def run_job(model):
        texts, _ = jobs[model]
        if kind == "local":
            vecs = local_embed_batch(model, texts)
        else:
            vecs = [embed_one(url, model, texts[0])]
            for t in texts[1:]:
                try:
                    vecs.append(embed_one(url, model, t))
                except Exception as e:
                    log(f"embed failed for one chunk ({model}): {e}")
                    vecs.append(None)
        if not vecs or vecs[0] is None:
            raise RuntimeError("query embedding failed")
        return vecs

    try:
        with ThreadPoolExecutor(max_workers=len(active)) as pool:
            results = dict(zip(active, pool.map(run_job, active)))
    except Exception as e:
        log(f"embedding failed ({kind}): {e}")
        for c in candidates:
            c["rerank_score"] = float(c.get("score", 0.0))
            c["rerank_source"] = "noop-embed-error"
        return candidates[:top_k]

    per_model = {}
    for model in active:
        vecs = results[model]
        _, keys = jobs[model]
        q_emb = vecs[0]
        for key, vec in zip(keys[1:], vecs[1:]):
            if vec:
                cache[key] = vec
                cache_dirty = True
        scores = {}
        for cid, chunk in chunk_of.items():
            emb = cache.get(f"{tag}{model}:{EMBED_SCHEME}:{chunk.get('body_hash', '')}")
            if emb:
                scores[cid] = cosine(q_emb, emb)
        per_model[model] = scores

    # Fusing >1 model: cosine scales are not comparable across models (nomic
    # sits in a narrow band, qwen3 spreads much wider), so a raw average lets
    # whichever model has the wider spread decide the ranking outright.
    # Standardize within the candidate set per model, then average. This fuses
    # *scores*; the RRF rank-fusion tried on 2026-07-29 scored worse and is a
    # different thing — don't conflate the two when reading the eval history.
    # Single-model stays raw cosine so scores remain byte-identical to v1.7.5.
    single = len(active) == 1
    stats = {}
    for model, scores in per_model.items():
        vals = list(scores.values())
        mean = sum(vals) / len(vals) if vals else 0.0
        var = sum((v - mean) ** 2 for v in vals) / len(vals) if vals else 0.0
        stats[model] = (mean, math.sqrt(var) or 1.0)

    for c in candidates:
        parts = []
        for model in active:
            sc = per_model[model].get(id(c))
            if sc is None:
                continue
            if single:
                parts.append(sc)
            else:
                mean, sd = stats[model]
                parts.append((sc - mean) / sd)
        if not parts:
            c["rerank_score"] = 0.0
            c["rerank_source"] = "missing-chunk"
            continue
        c["rerank_score"] = sum(parts) / len(parts)
        c["rerank_source"] = f"cosine:{tag}{'+'.join(active)}"
        # Raw per-model cosines, kept next to the fused (per-query z-scored)
        # score: the z-score only ranks candidates against each other, while
        # an absolute "is anything relevant at all" decision (recall.py) needs
        # the raw similarity.
        c["cosine"] = {m: round(per_model[m][id(c)], 4)
                       for m in active if id(c) in per_model[m]}

    if cache_dirty:
        save_cache(cache)

    ranked = sorted(candidates, key=lambda x: x.get("rerank_score", 0.0), reverse=True)
    return ranked[:top_k]


def embed_all(allow_remote, batch=64):
    """Pre-embed every chunk that has no cached vector yet (one-shot).

    Query time then only embeds the query plus chunks written since. Also run
    by the SessionEnd auto-commit worker after an index refresh (detached, so
    it never delays exit), so the per-prompt recall hook never pays for lazy
    embedding inside its time budget.
    """
    kind, active, url = pick_backend(allow_remote)
    if kind is None or not active:
        log("embed-all: no embedding backend available")
        return EXIT_NO_BACKEND
    tag = LOCAL_TAG if kind == "local" else ""
    cache, _ = load_cache()
    chunks = []
    for p in sorted((META_DIR / "chunks").glob("*/*.json")):
        chunk = load_chunk(p.relative_to(VAULT_ROOT))
        if chunk:
            chunks.append(chunk)
    added = 0
    for model in active:
        _, doc_prefix = prefix_scheme(model)
        todo, seen = [], set()
        for chunk in chunks:
            key = f"{tag}{model}:{EMBED_SCHEME}:{chunk.get('body_hash', '')}"
            if key in cache or key in seen:
                continue
            seen.add(key)
            todo.append((key, doc_prefix + (chunk.get("contextualized_text")
                                            or chunk.get("raw_text", ""))))
        t0 = time.time()
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            texts = [t for _, t in part]
            try:
                vecs = (local_embed_batch(model, texts) if kind == "local"
                        else [embed_one(url, model, t) for t in texts])
            except Exception as e:
                log(f"embed-all: batch {i // batch} failed ({model}): {e}")
                continue
            for (key, _), vec in zip(part, vecs):
                if vec:
                    cache[key] = vec
                    added += 1
        log(f"embed-all: {tag}{model}: {len(todo)} to embed of {len(chunks)} chunks "
            f"in {time.time() - t0:.1f}s")
        if added:
            save_cache(cache)
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="Rerank chunk candidates by semantic similarity.")
    parser.add_argument("query", nargs="?", help="Query text")
    parser.add_argument("--candidates", help="Path to candidates JSON or `-` for stdin",
                        default=None)
    parser.add_argument("--top", type=int, default=5, help="Top-K to return")
    parser.add_argument("--peek", action="store_true",
                        help="Print rerank strategy chosen and exit")
    parser.add_argument("--embed-all", action="store_true",
                        help="Pre-embed every chunk missing a cached vector, then exit")
    parser.add_argument("--allow-remote-backend", "--allow-remote-ollama",
                        dest="allow_remote_backend", action="store_true",
                        help="Accept a non-localhost WIKI_EMBED_URL (potential data exfil). "
                             "--allow-remote-ollama is a deprecated alias.")
    args = parser.parse_args()

    if args.embed_all:
        return embed_all(args.allow_remote_backend)

    if args.peek:
        if not args.query:
            log("--peek needs a query string")
            sys.exit(EXIT_USAGE)
        kind, active, url = pick_backend(args.allow_remote_backend)
        tag = LOCAL_TAG if kind == "local" else ""
        strategy = ("noop-no-backend" if kind is None else
                    f"cosine:{tag}{'+'.join(active)}" if active else "noop-no-model")
        print(json.dumps({
            "query": args.query,
            "strategy": strategy,
            "backend": kind,
            "backend_url": url,
            "llama_embedding": LLAMA_EMBEDDING if kind == "local" else None,
            "models": active,
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, indent=2))
        return EXIT_OK

    if not args.query or args.candidates is None:
        log("usage: rerank.py <query> --candidates <path|-> [--top N]")
        return EXIT_USAGE

    if args.candidates == "-":
        cand_text = sys.stdin.read()
    else:
        cand_text = Path(args.candidates).read_text(encoding="utf-8")
    try:
        candidates = json.loads(cand_text)
        if not isinstance(candidates, list):
            raise ValueError("candidates must be a JSON list")
    except (json.JSONDecodeError, ValueError) as e:
        log(f"ERR: bad candidates JSON: {e}")
        return EXIT_CANDIDATES

    result = rerank(args.query, candidates, top_k=args.top,
                    allow_remote=args.allow_remote_backend)
    print(json.dumps(result, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
