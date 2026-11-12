#!/usr/bin/env bash
# On-demand wiki maintenance (run by hand: a laptop is asleep at any fixed
# hour; maintenance/README.md). Deterministic, no model: step 1's whitelisted
# frontmatter autofix, committed by name; step 2's full lint report in the
# ignored wiki/.lint/report.json and the run date in
# .vault-meta/last-maintenance, left uncommitted. The findings are judged and
# fixed in a session, by Claude; the write errors among them already reach
# every session through the hooks' `wiki-lint.py --brief`.
#
# Until 2026-11-12 a model drafted fix proposals from the report (a local
# model through LM Studio, or a remote llama-server), a judge split them into
# auto/defer buckets and a PENDING flag queued them for a skill. That pass was
# removed: none of its proposals was ever applied, and it was the only part of
# the job that needed a model, a backend probe or a network call.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"   # <vault-engine>/maintenance (vault repo 2026-09-27, vault-engine 2026-10-06)
ENGINE_ROOT="$(cd "$SCRIPTS/.." && pwd)"
export WIKI_VAULT="${WIKI_VAULT:-$HOME/projects/knowledge-vault}"
VAULT_ROOT="$WIKI_VAULT"
VAULT="$VAULT_ROOT/wiki"
LINT_DIR="$VAULT/.lint"
LOG="$HOME/Library/Logs/wiki-maintenance.log"

mkdir -p "$LINT_DIR" "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds) start ==="

# Failure notification: any non-zero exit fires a macOS notification.
# All output goes to the log, so without it a failed run (script crash, jq
# syntax error in a future edit) would only show up if you happened to read
# the log file. The ERR trap covers failing commands; an explicit abort must
# go through die(), since `exit 1` does not fire ERR. Off macOS, osascript is
# missing and the notifications are skipped.
on_error() {
  local exit_code=$?
  local line=${1:-?}
  echo "ERROR: line $line exit=$exit_code"
  osascript -e "display notification \"Failed at line $line (exit $exit_code) — see ~/Library/Logs/wiki-maintenance.log\" with title \"Wiki Maintenance FAILED\" sound name \"Basso\"" 2>/dev/null || true
  exit "$exit_code"
}
trap 'on_error $LINENO' ERR
die() {
  echo "ERROR: $1"
  osascript -e "display notification \"$1 — see ~/Library/Logs/wiki-maintenance.log\" with title \"Wiki Maintenance FAILED\" sound name \"Basso\"" 2>/dev/null || true
  exit 1
}

# 0. local-patch sentinel. Vault retrieval scripts are a vendored fork of
#    the claude-obsidian plugin scripts (local patches v1.7.3/v1.7.4/v1.7.5 —
#    recorded in the vault). The plugin is uninstalled
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
  echo "WARNING: local patches missing: $SENTINEL_MISSING — re-port from the vault's record of the local patches (v1.7.3/4/5)"
  osascript -e "display notification \"Local patches missing: $SENTINEL_MISSING — re-port from the vault's record of the local patches (v1.7.3/4/5)\" with title \"Wiki Maintenance: patch regression\" sound name \"Basso\"" 2>/dev/null || true
fi

# 1. deterministic safe autofix (whitelist: name/type/tags inferred
#    from path + existing frontmatter; never touches description or
#    content). Committed right here, limited to the fixed files, with the
#    summary in the message: git log is the operation log since 2026-09-26
#    (log.md is frozen).
AUTOFIX_REPORT=$(mktemp)
trap 'rm -f "$AUTOFIX_REPORT"' EXIT
"$SCRIPTS/wiki-autofix.py" > "$AUTOFIX_REPORT"
AUTOFIX_COUNT=$(jq '.fixed_count' "$AUTOFIX_REPORT")
echo "autofix_count=$AUTOFIX_COUNT"
if [ "$AUTOFIX_COUNT" -gt 0 ]; then
  SUMMARY=$(jq -r '.fixed | map((.path | sub("\\.md$"; "")) + " (" + (.fields | join(",")) + ")") | join("; ")' "$AUTOFIX_REPORT")
  # Paths are relative to wiki/. The commit names them, so it never takes
  # what another session has staged. Push is left to step 4 or to the
  # caller's session-end hook, which pushes any commit ahead of origin.
  FIXED_PATHS=()
  while IFS= read -r p; do FIXED_PATHS+=("wiki/$p"); done < <(jq -r '.fixed[].path' "$AUTOFIX_REPORT")
  if git -C "$VAULT_ROOT" add -- "${FIXED_PATHS[@]}" \
     && git -C "$VAULT_ROOT" commit -q -m "wiki-autofix: $AUTOFIX_COUNT frontmatter fix(es)" -m "$SUMMARY" -- "${FIXED_PATHS[@]}"; then
    echo "git: autofix committed"
  else
    echo "git: autofix commit failed (non-fatal; the session-end auto-commit will sweep it)"
  fi
fi
rm -f "$AUTOFIX_REPORT"

# 2. deterministic lint (post-autofix), every finding type. `--brief` in the
#    hooks already shows the write errors (frontmatter, links, relations) to
#    each session; the age and size findings (stale pages, oversize hot.md or
#    log.md) appear only in this report.
REPORT="$LINT_DIR/report.json"
"$SCRIPTS/wiki-lint.py" > "$REPORT"
COUNT=$(jq '.findings | length' "$REPORT")
SHA=$(jq -r '.vault_sha' "$REPORT")
BY_TYPE=$(jq -r '[.findings[].type] | group_by(.) | map("\(.[0]) \(length)") | join(", ")' "$REPORT")
echo "findings=$COUNT vault_sha=$SHA${BY_TYPE:+ ($BY_TYPE)}"

# Completion marker for the staleness tripwire in the caller's SessionStart
# hook. It goes right after the lint produced a report, so that a clean run
# writes it too: a well-maintained vault must not be nagged every 21 days.
# The marker is tracked (.vault-meta/, not the ignored .lint/), so a run on
# one clone of the vault dates the round on every clone once it is committed.
# Until 2026-11-11 it was .lint/.last-run, which the tripwire still reads.
mkdir -p "$VAULT_ROOT/.vault-meta"
date -Iseconds > "$VAULT_ROOT/.vault-meta/last-maintenance"

# 3. notify
if [ "$COUNT" -eq 0 ]; then
  echo "clean"
  osascript -e "display notification \"clean (autofix $AUTOFIX_COUNT)\" with title \"Wiki Maintenance\"" 2>/dev/null || true
else
  osascript -e "display notification \"$COUNT finding(s): $BY_TYPE — review wiki/.lint/report.json in a session\" with title \"Wiki Maintenance\"" 2>/dev/null || true
fi

echo "=== done ==="

# 4. push. The run's tracked writes are step 1's autofix pages, committed
#    there by name, and step 2's .vault-meta/last-maintenance, left
#    uncommitted like any leftover; everything else lands in the ignored .lint/.
#    Until 2026-10-28 this step also staged `git add -A -- wiki .vault-meta`
#    and committed it, but by then a dirty tree could only hold another live
#    session's edits, possibly half-written, which went out under this run's
#    message. Leftovers are the caller's session-end hook's job.
(
  cd "$VAULT_ROOT"
  if [ "$(git rev-list --count "@{u}..HEAD" 2>/dev/null || echo 0)" -gt 0 ]; then
    if git push --quiet origin HEAD; then
      echo "git: pushed"
    else
      echo "git: push failed (non-fatal)"
    fi
  fi
) || echo "git: step failed (non-fatal)"

# Test harness available at: $SCRIPTS/run-tests.sh (pytest via uv)
