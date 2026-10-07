#!/usr/bin/env bash
# Wrapper for DragonScale tiling-check.py (on demand; maintenance/launchd/README.md).
# Code lives in vault-engine, the data in the vault (WIKI_VAULT, split 2026-10-06).
set -euo pipefail

ENGINE="$(cd "$(dirname "$0")/.." && pwd)"   # <vault-engine>/maintenance/..
export WIKI_VAULT="${WIKI_VAULT:-$HOME/projects/knowledge-vault}"
VAULT="$WIKI_VAULT"
LOG="$HOME/Library/Logs/dragonscale-tiling.log"
SCRIPT="$ENGINE/scripts/tiling-check.py"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds) start ==="

on_error() {
  local exit_code=$?
  local line=${1:-?}
  echo "ERROR: line $line exit=$exit_code"
  osascript -e "display notification \"DragonScale tiling failed at line $line — see $LOG\" with title \"DragonScale Tiling FAILED\" sound name \"Basso\"" 2>/dev/null || true
  exit "$exit_code"
}
trap 'on_error $LINENO' ERR

if [ ! -x "$SCRIPT" ]; then
  echo "tiling-check.py not present at $SCRIPT (DragonScale not bootstrapped on this machine), skipping"
  exit 0
fi

# ensure the embedding backend is reachable (LM Studio: `lms server start`)
EMBED_URL="${WIKI_EMBED_URL:-http://127.0.0.1:1234}"
case "$EMBED_URL" in http*) ;; *) EMBED_URL="http://$EMBED_URL" ;; esac
EMBED_MODEL="${WIKI_TILING_MODEL:-text-embedding-nomic-embed-text-v1.5@f16}"
if ! curl -fsS --max-time 2 "$EMBED_URL/v1/models" >/dev/null 2>&1; then
  echo "embedding backend not reachable at $EMBED_URL, skipping (run: lms server start)"
  exit 0
fi

# the embedding model must be loadable
if ! curl -fsS "$EMBED_URL/v1/models" | jq -e --arg m "$EMBED_MODEL" \
     '.data[] | select(.id == $m)' >/dev/null; then
  echo "model $EMBED_MODEL not present in LM Studio, skipping"
  echo "  (fix: lms get https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF)"
  exit 0
fi

# embed smoke test: server up + model listed does not prove the runtime works
# (2026-06-07→07-05: a brew bottle shipped without llama-server — the health
# endpoint stayed green while every embed failed, 4 weeks of silent skips).
# Same hazard on LM Studio, plus a new one: naming a model it cannot serve as
# an embedder returns 200 with a *substituted* model's vector rather than an
# error, so assert the response's own model field too.
SMOKE=$(curl -fsS --max-time 60 \
     -X POST "$EMBED_URL/v1/embeddings" \
     -H "Content-Type: application/json" \
     -d "{\"model\":\"$EMBED_MODEL\",\"input\":\"smoke\"}") || SMOKE=""
if ! printf '%s' "$SMOKE" | jq -e '.data[0].embedding | length > 0' >/dev/null 2>&1; then
  echo "ERROR: $EMBED_MODEL listed but the embed call failed — backend runtime broken"
  false  # not in a conditional → fires the ERR trap → notification + exit
fi
if ! printf '%s' "$SMOKE" | jq -e --arg m "$EMBED_MODEL" '.model == $m' >/dev/null 2>&1; then
  echo "ERROR: asked $EMBED_MODEL but the backend answered as $(printf '%s' "$SMOKE" | jq -r .model)"
  echo "  LM Studio silently substitutes a loaded model for one it cannot serve."
  false
fi

cd "$VAULT"
WIKI_EMBED_URL="$EMBED_URL" python3 "$SCRIPT"

echo "=== done ==="
