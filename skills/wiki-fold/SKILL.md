---
name: wiki-fold
description: "RETIRED 2026-09-26 (log.md frozen). Extractive rollup of the last 2^k wiki/log.md entries into a fold page in wiki/folds/; dry-run by default. Triggers on: fold the log, run wiki-fold, log rollup."
---

> [!warning] Retired 2026-09-26
> `wiki/log.md` is frozen and `git log` is the operation log, so there is nothing new to fold. This skill is no longer symlinked into `~/.claude/skills/`; it stays here as the record of how the existing `wiki/folds/` pages (up to 2026-08-29) were made.

# wiki-fold: Extractive Log Rollup

Implements a bounded subset of Mechanism 1 from [[DragonScale Memory]]: flat fold over raw `wiki/log.md` entries. Fold-of-folds (hierarchical level-stacking) is **out of scope for this skill**; see "Scope boundary" below.

A fold is **additive**: child log entries and their referenced pages are never modified, moved, or deleted. A fold is **extractive**: every outcome and theme in the output must be traceable to a specific child log entry. No invented facts, no synthesis beyond what the child entries support.

---

> [!warning] Local divergence — log entry format (patched 2026-08-25)
> Upstream parses log entries as `## [date] op | title` headings with child
> bullets underneath. **This vault's `log.md` uses one-line bullets**:
> `- YYYY-MM-DD · category · **title** — body with [[wikilinks]]`, newest first.
> The upstream pattern matched **5** lines against **234** real entries, so every
> fold run found nothing to fold and M1 sat dormant from 2026-05-09 to
> 2026-08-25 — with `log.md` excluded from the retrieval index since v1.7.4,
> that left ~96% of the operation log unreachable. Sections 1, 2 and 6 below are
> patched for the real format. Since 2026-09-25 this copy is the only one: the
> claude-obsidian plugin is uninstalled and `~/.claude/skills/wiki-fold` is a
> symlink to this directory. See [[claude-obsidian-divergence]].

## Scope boundary (explicit)

This skill does **not** implement:
- Fold-of-folds / hierarchical level stacking (DragonScale spec calls for it; deferred to a future skill).
- Automatic triggering (folds are always human-invoked in Phase 1).
- Semantic-tiling dedup (Mechanism 3; separate skill).

It **does** implement:
- Flat fold over raw log.md entries at a chosen batch exponent `k`.
- Structural idempotency via a deterministic fold ID.
- Extractive summarization with count-checking.

When referring to level in frontmatter, use `batch_exponent: k` (not `level: k`), because this skill does not produce hierarchical levels.

---

## Modes

| Mode | Writes? | Invocation |
|---|---|---|
| **dry-run (default)** | **No Write tool calls.** Emit fold content via Bash `cat`/`heredoc` to stdout only. | `fold the log, dry-run k=3` |
| **commit** | Uses Write/Edit tools, then one git commit of the three writes. Compose full content first, then sequence writes. | `fold the log, commit k=3` (only after a clean dry-run) |

**Why stdout-only in dry-run**: dry-run must leave zero residue in the working tree — the SessionEnd hook (`~/.claude/hooks/wiki-autocommit.sh`) commits whatever is left in the vault when the session ends.

---

## Concurrency (v1.7+)

The fold-page write in commit mode MUST be preceded by `wiki-lock acquire`:

```bash
FOLD_PATH="wiki/folds/${FOLD_ID}.md"
bash ~/projects/vault-engine/scripts/wiki-lock.sh acquire "$FOLD_PATH" || {
  echo "FAIL: another writer holds $FOLD_PATH; aborting fold."; exit 75
}
# … write the fold via Write/Edit …
bash ~/projects/vault-engine/scripts/wiki-lock.sh release "$FOLD_PATH"
```

Fold pages are deterministically named (`fold-k{K}-from-{DATE}-to-{DATE}-n{COUNT}.md`), so two parallel folds with the same parameters target the same path. Without the lock, they could overwrite each other's outputs. The duplicate-detection check inside this skill (already documented below) handles the "fold already exists" case at the SKILL level; the lock handles the in-flight-write race at the OS level.

Dry-run mode does not acquire a lock (no writes happen).

See `skills/wiki-ingest/SKILL.md` §Concurrency for the full lock semantics.

---

## Deterministic fold ID

Every fold has an ID derived from its inputs:

```
fold-k{K}-from-{EARLIEST-DATE}-to-{LATEST-DATE}-n{COUNT}
```

Example: `fold-k3-from-2026-04-10-to-2026-04-23-n8`.

The filename in commit mode is `wiki/folds/{FOLD-ID}.md`. No date-of-creation in the filename. No timestamp in the title.

**Duplicate detection (required)**: before emitting any output, check if `wiki/folds/{FOLD-ID}.md` already exists. If so, report "Fold already exists at wiki/folds/{FOLD-ID}.md. Use --force to overwrite, or pick a different range." and stop. This is the no-op idempotency guarantee; byte-identical content is NOT guaranteed (LLM prose varies) but the filename and scope are.

---

## Parameters

- `k` (default 4): batch exponent. Batch size = `2^k`. Typical values: k=3 (8), k=4 (16), k=5 (32).
- `range` (optional): explicit entry range `entries 1-16`. Overrides k.
- `--force`: overwrite an existing fold with the same ID. Default no.
- `--commit`: write to wiki/. Without it, dry-run stdout-only.

If fewer than `2^k` log entries exist, report the shortfall and stop. Do not silently fold a partial batch.

---

## Procedure

### 1. Parse log entries

```
grep -n "^- 20" wiki/log.md | tail -{2^k}
```

**`tail`, not `head`.** This log is newest-first, so the oldest unfolded entries
are at the BOTTOM. Folds consume from the old end — the existing fold covers the
earliest range in the file. Using `head` would fold the newest entries and leave
a permanent hole in the middle.

Each match is one **complete entry on a single line**, shaped
`- YYYY-MM-DD · category · **title** — body`. There are no child bullets to
collect: the whole entry is that one line. Record line number, date, category
(the `·`-delimited field after the date; may be absent), the bolded title, and
the body text.

Skip any line already covered by an existing fold: read the `children:`
frontmatter of every page in `wiki/folds/` first and exclude those dates+titles.

### 2. Extract child page identifiers

From each entry line, extract:
- every inline `[[wikilink]]` (these are the entry's referenced pages — this
  vault carries no `Location:` / `Pages created:` / `Pages updated:` lines)
- the trailing wikilinks, which by convention name the primary pages the entry
  is about
- any `path/to/file` in backticks, recorded as a touched path rather than a page

Build a structured children list:
```yaml
children:
  - date: "2026-04-23"
    op: "save"
    title: "DragonScale Memory v0.2 — post-adversarial-review"
    page: "[[DragonScale Memory]]"
  - ...
```

One record per log entry. Do not dedupe by page: if two entries both point to `[[DragonScale Memory]]`, both records appear, distinguishable by date and title.

### 3. Read referenced pages (bounded)

Read only the pages that are not already captured fully in the log entry's bullets. Budget: 0-10 page reads. Hard ceiling: 15. If an entry's referenced page is missing, record `page_missing: true` and proceed.

### 4. Extractive summarization with count checks

Write the fold body per `references/fold-template.md`. **Rules**:

- **Extractive only.** Every outcome bullet and theme bullet must cite a specific child entry (e.g., `(from 2026-04-14 session)`) or a quoted line from that entry. Do not introduce events, counts, or interpretations not present in a child entry.
- **Log entry is the primary source.** If the log entry's bullets and the referenced meta-page disagree on a fact (e.g., a count), prefer the log-entry bullets and flag the mismatch as "source mismatch: log says X, meta says Y."
- **Count checks.** If you write "N concept pages" or "M repos updated," grep the source entries for the number and verify. Numeric mismatches are dry-run blockers.
- **No merging across entries without naming them.** A theme that spans multiple entries must name each contributing entry inline.
- **Uncertainty is a feature.** If an entry is ambiguous, say "ambiguous in source: [[Entry]]" rather than picking one interpretation.

### 5. Self-check before emitting

Before printing output, verify:
- Every child in `children:` frontmatter appears exactly once in the Child Entries table.
- Every entry in the table appears in the `children:` frontmatter.
- Every numeric claim in Key Outcomes is grep-verifiable against a child entry.
- The fold ID is deterministic and the file does not already exist (or `--force` is set).

If any check fails, abort and report the specific failure.

### 6. Emit

**Dry-run**: use Bash `cat <<'EOF' ... EOF` to stdout. Do not use Write. Print the fold ID and a one-line summary of what the commit step would do.

**Commit** (only after user says "commit the fold"):
1. `Write` the fold page to `wiki/folds/{FOLD-ID}.md`.
2. `Edit` `wiki/index.md` to add the fold link under a `## Folds` section (create section if missing).
3. `Edit` `wiki/log.md` to prepend one entry **in this vault's bullet format**
   — writing the upstream heading format here is what would re-break section 1:
   ```
   - YYYY-MM-DD · fold · **k{K} rollup of N entries, {EARLIEST-DATE} → {LATEST-DATE}** — extractive rollup of N log entries into [[folds/{FOLD-ID}]]. Children unmodified.
   ```
4. Refresh the index (`python3 ~/projects/vault-engine/scripts/contextual-prefix.py --all && python3 ~/projects/vault-engine/scripts/bm25-index.py build`), then one commit: `git add -A && git commit -m "fold: k{K} rollup {EARLIEST-DATE} → {LATEST-DATE}"`, and push.

---

## Output schema

See `references/fold-template.md` for the canonical frontmatter and body layout.

---

## Invariants

1. **Structural idempotency**: same range + same k → same fold ID → duplicate detection prevents double-writes. LLM prose may vary across runs; the *location and scope* are fixed.
2. **Additive**: children are never modified.
3. **Bounded reads**: 0-15 child-page reads per fold.
4. **Extractive**: zero invented facts. Count checks enforced.
5. **No chaining**: wiki-fold does not invoke wiki-lint, wiki-ingest, autoresearch, or save.

---

## What NOT to do

- Do not use Write/Edit during dry-run. Bash stdout only.
- Do not include the current date in the fold filename or title. Use the child entry range.
- Do not silently dedupe children by page title. One record per log entry.
- Do not write "emergent themes" that span entries without naming which entries contribute.
- Do not claim byte-identical idempotency. Structural idempotency is the actual guarantee.
- Do not update `wiki/hot.md`. Ownership stays with save/ingest skills.

---

## Reversal

Committed fold reversal (one commit, or in this order):
1. Remove the log.md fold entry.
2. Remove the index.md entry.
3. Delete the fold page file.

Or: `git revert` the fold commit. Child pages are untouched in either path.

---

## Example dry-run sequence

User: "fold the log, dry-run k=3"

1. Parse `wiki/log.md` top 8 entries.
2. Build structured children list (8 records).
3. Read 0-10 referenced pages as needed.
4. Produce fold ID: `fold-k3-from-2026-04-10-to-2026-04-23-n8`.
5. Check `wiki/folds/fold-k3-from-2026-04-10-to-2026-04-23-n8.md` does not exist.
6. Write fold body following the template.
7. Run self-check (frontmatter/table consistency, count verification).
8. Emit via `cat <<'EOF' ... EOF` to stdout.
9. Report: "Dry-run complete. Fold ID: {FOLD-ID}. To commit: 'commit the fold'."

---

## How to think (10-principle mapping)

When working on this skill, apply the 10-principle loop. See [`skills/think/SKILL.md`](../think/SKILL.md) for the canonical framework.

| # | Principle | Application here |
|---|-----------|-------------------|
| 1 | OBSERVE (ext) | Read the last 2^k log entries FULLY. Skimming defeats extractive summarization. |
| 2 | OBSERVE (int) | Am I tempted to synthesize beyond what the child entries support? Extractive-only is the binding rule. |
| 3 | LISTEN | Which themes emerge naturally from the child entries? Don't impose themes from outside the children. |
| 4 | THINK | Extractive only. Every outcome must be traceable to a specific child entry. Count check at the end. |
| 5 | CONNECT (lat) | Cross-entry patterns ARE the value-add. The single-entry view misses these. |
| 6 | CONNECT (sys) | DragonScale Mechanism 1 + wiki-lock + address allocator. Folds are part of the memory architecture. |
| 7 | FEEL | A good fold lets future-me skim a year of work in 5 minutes. Aim for that compression. |
| 8 | ACCEPT | Dry-run first. Commit only when the self-check passes. Honor the bounded-scope constraint (no fold-of-folds yet). |
| 9 | CREATE | Fold page at `wiki/folds/<fold-id>.md` linking to all child entries. |
| 10 | GROW | Fold-of-folds (hierarchical level-stacking) is v_next scope — note as you encounter it, don't sneak it in. |
