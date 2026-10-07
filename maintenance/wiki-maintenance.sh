#!/usr/bin/env bash
# On-demand wiki maintenance (run by hand: a laptop is asleep at any fixed
# hour; maintenance/launchd/README.md). Runs deterministic
# lint, then asks an LLM to draft conservative fix proposals: local LM
# Studio with qwen/qwen3.8-27b by default (since 2026-08-27), a remote
# llama-server (Qwen3-Coder-Next) only with WIKI_LINT_BACKEND=evo + WIKI_LINT_EVO_URL, and no fallback
# between them (step 0b). Never writes inside the vault; only deposits a
# proposal file gated by a PENDING flag.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"   # <vault-engine>/maintenance (vault repo 2026-09-27, vault-engine 2026-10-06)
ENGINE_ROOT="$(cd "$SCRIPTS/.." && pwd)"
export WIKI_VAULT="${WIKI_VAULT:-$HOME/projects/knowledge-vault}"
VAULT_ROOT="$WIKI_VAULT"
VAULT="$VAULT_ROOT/wiki"
LINT_DIR="$VAULT/.lint"
LOG="$HOME/Library/Logs/wiki-maintenance.log"
# qwen3.5:9b replaced gemma3:12b 2026-07-06 after a 3-round A/B on the
# real lint prompt: gemma fabricated a target path in 3/3 rounds
# (a misspelled entity path), qwen 0/2 with the few-shot template. qwen is a
# thinking model. Under Ollama this needed `think:false` on every
# /api/generate payload or all tokens went to `.thinking` and `.response`
# came back empty (verified: 703s runaway, ctx-killed). On the OpenAI
# shape used now the equivalent guard is chat_template_kwargs
# .enable_thinking=false in llm_generate; it is ignored by backends that
# do not implement it, so WIKI_LINT_MODEL overrides stay safe.
MODEL="${WIKI_LINT_MODEL:-qwen/qwen3.8-27b}"

mkdir -p "$LINT_DIR"
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds) start ==="

# Failure notification: any non-zero exit fires a macOS notification
# with the failing line. Without this, weekly launchd runs that fail
# silently (script crash, ollama down, jq syntax error in a future
# edit) would only show up if you happened to read the log file.
on_error() {
  local exit_code=$?
  local line=${1:-?}
  echo "ERROR: line $line exit=$exit_code"
  osascript -e "display notification \"Failed at line $line (exit $exit_code) — see ~/Library/Logs/wiki-maintenance.log\" with title \"Wiki Maintenance FAILED\" sound name \"Basso\"" 2>/dev/null || true
  exit "$exit_code"
}
trap 'on_error $LINENO' ERR

# 0. local-patch sentinel. Vault retrieval scripts are a vendored fork of
#    the claude-obsidian plugin scripts (local patches v1.7.3/v1.7.4/v1.7.5 —
#    see wiki/entities/dragonscale-memory.md). The plugin is uninstalled
#    since 2026-09-25, but a manual re-vendor would still silently revert them: retrieval keeps working,
#    just at ~12x the token cost. Fail loud here instead of failing silent.
#
#    v1.7.5 additions matter even more than the cost ones, because their
#    regression is INVISIBLE: losing the nomic task prefixes or the chunk GC
#    does not change latency or output shape, it just quietly makes the
#    ranking worse. Nothing would surface that except eval/run-eval.py.
SENTINEL_MISSING=""
grep -q -- '--chunks' "$ENGINE_ROOT/scripts/retrieve.py" 2>/dev/null \
  || SENTINEL_MISSING="retrieve.py(--chunks)"
grep -q 'EXCLUDE_PAGES' "$ENGINE_ROOT/scripts/contextual-prefix.py" 2>/dev/null \
  || SENTINEL_MISSING="$SENTINEL_MISSING contextual-prefix.py(EXCLUDE_PAGES)"
grep -q 'search_document: ' "$ENGINE_ROOT/scripts/rerank.py" 2>/dev/null \
  || SENTINEL_MISSING="$SENTINEL_MISSING rerank.py(nomic-task-prefix)"
grep -q 'EMBED_SCHEME' "$ENGINE_ROOT/scripts/rerank.py" 2>/dev/null \
  || SENTINEL_MISSING="$SENTINEL_MISSING rerank.py(EMBED_SCHEME)"
grep -q 'PREFIX_SCHEMES' "$ENGINE_ROOT/scripts/rerank.py" 2>/dev/null \
  || SENTINEL_MISSING="$SENTINEL_MISSING rerank.py(per-model-prefixes)"
grep -q 'qwen3-embedding' "$ENGINE_ROOT/scripts/rerank.py" 2>/dev/null \
  || SENTINEL_MISSING="$SENTINEL_MISSING rerank.py(fused-reranker)"
grep -q 'gc_chunk_dirs' "$ENGINE_ROOT/scripts/contextual-prefix.py" 2>/dev/null \
  || SENTINEL_MISSING="$SENTINEL_MISSING contextual-prefix.py(chunk-GC)"
grep -q 'clean_overlap_tail' "$ENGINE_ROOT/scripts/contextual-prefix.py" 2>/dev/null \
  || SENTINEL_MISSING="$SENTINEL_MISSING contextual-prefix.py(clean-overlap)"
grep -q 'per_page_cap' "$ENGINE_ROOT/scripts/retrieve.py" 2>/dev/null \
  || SENTINEL_MISSING="$SENTINEL_MISSING retrieve.py(per-page-cap)"
if [ -n "$SENTINEL_MISSING" ]; then
  echo "WARNING: local patches missing: $SENTINEL_MISSING — re-port from dragonscale-memory.md §v1.7.3/4/5"
  osascript -e "display notification \"Local patches missing: $SENTINEL_MISSING — re-port from dragonscale-memory §v1.7.3/4/5\" with title \"Wiki Maintenance: patch regression\" sound name \"Basso\"" 2>/dev/null || true
fi

LMS_URL="${WIKI_LINT_URL:-http://127.0.0.1:1234}"
case "$LMS_URL" in http*) ;; *) LMS_URL="http://$LMS_URL" ;; esac

# 0b. backend selection. Reworked 2026-08-27 for the laptop: LM Studio on
#     this machine is the default, because the remote box is powered off unless
#     someone deliberately turns it on. Opt in with WIKI_LINT_BACKEND=evo.
#
#     Deliberately NO automatic fallback from evo to local. The 2026-07-17
#     outage hid for weeks precisely because a degraded evo silently fell
#     through to a healthy-looking local path; if you asked for evo and evo
#     is not serving the model, that is an error worth seeing.
EVO_URL="${WIKI_LINT_EVO_URL:-}"   # e.g. http://<llm-box>:8080; required with WIKI_LINT_BACKEND=evo
EVO_MODEL="${WIKI_LINT_EVO_MODEL:-Qwen3-Coder-Next}"
BACKEND="${WIKI_LINT_BACKEND:-lmstudio}"
if [ "$BACKEND" = "evo" ] && [ -z "$EVO_URL" ]; then
  echo "ERROR: WIKI_LINT_BACKEND=evo needs WIKI_LINT_EVO_URL (e.g. http://<llm-box>:8080)."
  exit 1
fi
if [ "$BACKEND" = "evo" ]; then
  # Both checks required: /health goes green before/without a model loaded
  # (router mode — the exact state that hid the 07-17 outage).
  if curl -fsS --max-time 3 "$EVO_URL/health" 2>/dev/null | grep -q '"ok"' \
     && curl -fsS --max-time 5 "$EVO_URL/v1/models" 2>/dev/null | grep -q "\"$EVO_MODEL\""; then
    MODEL="$EVO_MODEL"; API_URL="$EVO_URL"
  else
    echo "ERROR: WIKI_LINT_BACKEND=evo but $EVO_URL is not serving $EVO_MODEL."
    echo "  Power on the box and set WIKI_LINT_EVO_URL, or unset WIKI_LINT_BACKEND to use local LM Studio."
    exit 1
  fi
else
  API_URL="$LMS_URL"
fi
echo "backend=$BACKEND model=$MODEL"

# llm_generate <prompt-file> <max-tokens> <out-file>
# Backend-agnostic single-shot generation: writes the raw model text to
# out-file, logs a snippet of the raw API response on failure. Callers
# retry; this does one attempt.
llm_generate() {
  local prompt_file=$1 max_tokens=$2 out_file=$3
  local payload resp rc=1
  payload=$(mktemp); resp=$(mktemp)
  # One shape for both backends: llama-server (evo) and LM Studio both speak
  # OpenAI /v1/chat/completions, so the old Ollama-native /api/generate branch
  # is gone along with Ollama itself.
  #
  # Two switches against thinking-runaway, one per backend: llama-server
  # (evo) honours chat_template_kwargs.enable_thinking (a verified no-op on
  # Qwen3-Coder-Next, A/B 2026-07-18); LM Studio IGNORES it for
  # qwen3.8-27b and honours reasoning_effort:"none" instead (measured
  # 2026-09-26: 0 reasoning tokens, "OK" in 0.6 s). Without it the model
  # spent the whole budget reasoning and answered nothing (2026-08-30, 09-15).
  jq -n --arg model "$MODEL" --rawfile prompt "$prompt_file" --argjson n "$max_tokens" \
    '{model:$model, stream:false, temperature:0.2, max_tokens:$n,
      chat_template_kwargs:{enable_thinking:false}, reasoning_effort:"none",
      messages:[{role:"user", content:$prompt}]}' > "$payload"
  if curl -fsS --max-time 300 -X POST "$API_URL/v1/chat/completions" \
       -H "Content-Type: application/json" --data-binary @"$payload" -o "$resp"; then
    jq -r '.choices[0].message.content // empty' "$resp" > "$out_file" && rc=0
  fi
  # `[ -s ]` is not enough: jq prints a newline for an empty string, so an
  # empty answer is a 1-byte file. That is exactly what qwen3.8-27b returned
  # on 2026-08-30 and 09-15 — every token spent on reasoning (LM Studio
  # ignores enable_thinking:false), finish_reason "length", content "" — and
  # the smoke test logged OK both times. Require real text, and treat a
  # length-truncated answer as a failure, not a result.
  local finish
  finish=$(jq -r '.choices[0].finish_reason // empty' "$resp" 2>/dev/null)
  if [ "$rc" -ne 0 ] || ! grep -q '[^[:space:]]' "$out_file" 2>/dev/null \
     || [ "$finish" = "length" ]; then
    echo "llm_generate: $BACKEND finish_reason=${finish:-?} raw response: $(head -c 300 "$resp" 2>/dev/null)"
    rc=1
  fi
  rm -f "$payload" "$resp"
  return $rc
}

# 1. ensure the local backend is serving. `lms server start` is idempotent
#    and returns quickly when it is already up, so unlike the old ollama
#    path there is no launchd agent to kickstart and no risk of racing a
#    second server onto the port.
backend_ready() { curl -fsS --max-time 2 "$API_URL/v1/models" >/dev/null 2>&1; }
if [ "$BACKEND" = "lmstudio" ] && ! backend_ready; then
  echo "LM Studio server not reachable at $API_URL, starting it"
  "$HOME/.lmstudio/bin/lms" server start >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    sleep 1
    backend_ready && break
  done
  backend_ready || { echo "LM Studio still unreachable after 30s, aborting"; exit 1; }
fi

# 1b. inference smoke test. Reachability alone proves nothing: the
#     2026-06-07→07-05 outage (brew 0.30.7 bottle shipped without
#     llama-server) left /api/version green while every GGUF generate
#     failed — tiling skipped for 4 weeks and this script never noticed
#     because lint kept returning 0 findings, so the LLM step was never
#     exercised. Exercise the real model once, even with nothing to lint.
SMOKE_FILE=$(mktemp)
SMOKE_PROMPT=$(mktemp)
trap 'rm -f "$SMOKE_FILE" "$SMOKE_PROMPT"' EXIT
printf 'Reply with exactly: OK' > "$SMOKE_PROMPT"
SMOKE_OK=0
for attempt in 1 2; do
  if llm_generate "$SMOKE_PROMPT" 8 "$SMOKE_FILE"; then
    SMOKE_OK=1
    break
  fi
  echo "inference smoke test failed (attempt $attempt/2)"
  [ "$attempt" -lt 2 ] && sleep 15
done
if [ "$SMOKE_OK" -ne 1 ]; then
  echo "inference smoke test FAILED for $MODEL ($BACKEND) — runtime broken, aborting"
  osascript -e "display notification \"$MODEL ($BACKEND) cannot generate — runtime broken, see ~/Library/Logs/wiki-maintenance.log\" with title \"Wiki Maintenance FAILED\" sound name \"Basso\"" 2>/dev/null || true
  exit 1
fi
echo "inference smoke test OK ($MODEL via $BACKEND)"
rm -f "$SMOKE_FILE" "$SMOKE_PROMPT"

# 2a. deterministic safe autofix (whitelist: name/type/tags inferred
#     from path + existing frontmatter; never touches description or
#     content). Committed right here, limited to the fixed files, with the
#     summary in the message: git log is the operation log since 2026-09-26
#     (log.md is frozen), and the clean-lint and error exits below return
#     before step 6 — a summary held for step 6 would be lost on those paths.
AUTOFIX_REPORT=$(mktemp)
trap 'rm -f "$AUTOFIX_REPORT"' EXIT
"$SCRIPTS/wiki-autofix.py" > "$AUTOFIX_REPORT"
AUTOFIX_COUNT=$(jq '.fixed_count' "$AUTOFIX_REPORT")
echo "autofix_count=$AUTOFIX_COUNT"
if [ "$AUTOFIX_COUNT" -gt 0 ]; then
  SUMMARY=$(jq -r '.fixed | map((.path | sub("\\.md$"; "")) + " (" + (.fields | join(",")) + ")") | join("; ")' "$AUTOFIX_REPORT")
  # Paths are relative to wiki/ and kebab-case (no spaces). Push is left to
  # step 6 or to wiki-autocommit.sh, which pushes any commit ahead of origin.
  if jq -r '.fixed[].path' "$AUTOFIX_REPORT" | sed 's|^|wiki/|' \
       | (cd "$VAULT_ROOT" && xargs git add --) \
     && git -C "$VAULT_ROOT" commit -q -m "wiki-autofix: $AUTOFIX_COUNT frontmatter fix(es)" -m "$SUMMARY"; then
    echo "git: autofix committed"
  else
    echo "git: autofix commit failed (non-fatal; SessionEnd auto-commit will sweep it)"
  fi
fi
rm -f "$AUTOFIX_REPORT"

# 2b. deterministic lint (post-autofix)
REPORT="$LINT_DIR/report.json"
"$SCRIPTS/wiki-lint.py" > "$REPORT"
COUNT=$(jq '.findings | length' "$REPORT")
SHA=$(jq -r '.vault_sha' "$REPORT")
echo "findings=$COUNT vault_sha=$SHA"

# Completion marker for the staleness tripwire in wiki-hot-cache.sh. It goes
# here, right after the lint produced a report, so that BOTH exits reach it.
# Putting it further down meant the zero-findings path — the healthiest
# possible outcome — returned early and never wrote it, so a well-maintained
# vault would be nagged every 21 days forever. A nag the user has learned to
# dismiss is worse than no nag at all.
#
# PENDING cannot serve as the marker either: it is removed once proposals are
# applied, so a fully-applied vault would read as stale for the same reason.
date -Iseconds > "$LINT_DIR/.last-run"

if [ "$COUNT" -eq 0 ]; then
  echo "clean, exit"
  rm -f "$LINT_DIR/PENDING"
  exit 0
fi

# 3. LLM proposal pass via REST API (avoids TTY cursor-control leakage
#    that polluted previous `ollama run` output: `[1D[K` sequences in
#    long lines). llm_generate handles payload/parse per backend.
DATE=$(date +%Y-%m-%d)
PROPOSALS="$LINT_DIR/proposals-$DATE.md"
PROMPT_FILE=$(mktemp)
RESPONSE_FILE=$(mktemp)
trap 'rm -f "$PROMPT_FILE" "$RESPONSE_FILE"' EXIT

{
  cat "$SCRIPTS/wiki-lint-prompt.tmpl"
  cat "$REPORT"
} > "$PROMPT_FILE"

# Retry with backoff: a cold/just-kickstarted server can 500 the first
# generate while the model loads; one failure is not a verdict.
GEN_OK=0
for attempt in 1 2 3; do
  if llm_generate "$PROMPT_FILE" 2048 "$RESPONSE_FILE"; then
    GEN_OK=1
    break
  fi
  echo "generate failed on $BACKEND (attempt $attempt/3)"
  [ "$attempt" -lt 3 ] && sleep $((attempt * 20))
done
if [ "$GEN_OK" -ne 1 ]; then
  echo "LLM generate failed after 3 attempts ($BACKEND), aborting"
  osascript -e "display notification \"$MODEL ($BACKEND) returned no usable proposals — see ~/Library/Logs/wiki-maintenance.log\" with title \"Wiki Maintenance FAILED\" sound name \"Basso\"" 2>/dev/null || true
  exit 1
fi

LLM_OUT=$(cat "$RESPONSE_FILE")

{
  echo "---"
  echo "generated: $(date -Iseconds)"
  echo "model: $MODEL"
  echo "vault_sha: $SHA"
  echo "findings_count: $COUNT"
  echo "report: .lint/report.json"
  echo "---"
  echo
  echo "# Wiki Maintenance Proposals — $DATE"
  echo
  echo "Apply with: \`/wiki-apply\`"
  echo
  printf '%s\n' "$LLM_OUT"
} > "$PROPOSALS"

echo "wrote $PROPOSALS"

# 4. judge: split proposals into auto/defer buckets via deterministic
#    rubric (in code, not in skill markdown). Only the auto bucket is
#    safe for SessionStart auto-apply; the defer bucket needs manual
#    /wiki-apply interactive review. Cap auto-apply at 10 entries by
#    truncating the auto bucket if the LLM produced more.
JUDGE_JSON=$(mktemp)
trap 'rm -f "$PROMPT_FILE" "$RESPONSE_FILE" "$JUDGE_JSON"' EXIT
"$SCRIPTS/wiki-judge.py" "$PROPOSALS" > "$JUDGE_JSON"
AUTO=$(jq -r .auto "$JUDGE_JSON")
DEFER=$(jq -r .defer "$JUDGE_JSON")
echo "judge: auto=$AUTO defer=$DEFER"

AUTO_FILE="$LINT_DIR/proposals-auto-$DATE.md"
if [ -f "$AUTO_FILE" ]; then
  AUTO_BLOCKS=$(grep -c '^### proposal-' "$AUTO_FILE" || echo 0)
  if [ "$AUTO_BLOCKS" -gt 10 ]; then
    echo "WARNING: auto bucket has $AUTO_BLOCKS proposals, cap=10 — keeping first 10"
    awk '/^### proposal-/{c++} c<=10' "$AUTO_FILE" > "$AUTO_FILE.cap"
    mv "$AUTO_FILE.cap" "$AUTO_FILE"
  fi
fi

# 4b. staleness flag. PENDING is written HERE, after judging and capping,
#     not right after the proposals file: schema 2 records one hash per
#     proposal TARGET, and the targets are only known once the buckets
#     exist and the cap has dropped anything past the tenth.
#
#     The old flag was the whole-vault $SHA, which made the gate fire on
#     any vault write at all — including the ones this pipeline and the
#     SessionStart hook make themselves. log.md was exempted from the
#     hash on 2026-06-10; hot.md never was, and it is rewritten at the end
#     of nearly every session, so /wiki-apply was refusing to run over
#     drift on pages no proposal referenced. Rationale: wiki-pending.py.
"$SCRIPTS/wiki-pending.py" write >/dev/null
echo "PENDING: $(jq -r '.targets | length' "$LINT_DIR/PENDING") target(s) hashed"

# 5. notify
osascript -e "display notification \"auto=$AUTO defer=$DEFER (of $COUNT findings)\" with title \"Wiki Maintenance\"" 2>/dev/null || true

echo "=== done ==="

# 6. commit + push. Pipeline-owned (Obsidian and obsidian-git are gone since
#    2026-09-25; wiki-autocommit.sh covers interactive sessions). Without this, multi-day
#    drift accumulates locally (verified 2026-05-09 → 2026-05-16, 7d
#    gap with Obsidian closed). Scoped to wiki/ and .vault-meta/ since
#    2026-10-04, like wiki-autocommit.sh: code is its author's to commit.
(
  cd "$VAULT_ROOT"
  if [ -n "$(git status --porcelain -- wiki .vault-meta)" ]; then
    git add -A -- wiki .vault-meta
    git commit -m "wiki-maintenance: auto-commit $(date -Iseconds)" -- wiki .vault-meta >/dev/null
    echo "git: committed"
  fi
  if [ "$(git rev-list --count @{u}..HEAD 2>/dev/null || echo 0)" -gt 0 ]; then
    if git push --quiet origin HEAD; then
      echo "git: pushed"
    else
      echo "git: push failed (non-fatal)"
    fi
  fi
) || echo "git: step failed (non-fatal)"

# Test harness available at: $SCRIPTS/run-tests.sh (pytest via uv)
