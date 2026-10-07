---
name: save
description: "Explicit /save: decompose the conversation into atomic facts and file them in the knowledge vault. Triggers on: /save, save this to the wiki, file this in the vault."
---

# /save — file the conversation into the vault

Vault: `~/projects/knowledge-vault`. Routes and the proactive-save triggers live in `~/.claude/CLAUDE.md` §Wiki Knowledge Base; this skill is the explicit, reviewable version of the same routing. **Not a session dump.**

1. Decompose the conversation into atomic facts. Route each one with the Routes table (profile / entities / sources / runbooks). Prefer updating an existing page over creating one — find it with `python3 ~/projects/vault-engine/scripts/retrieve.py "<english query>" --top 3 --chunks --compact`. New entity/source pages get a typed `relations:` block, declared on both ends (schema: vault `CLAUDE.md` §Typed graph).
2. Only if a synthesis remains that does not decompose (a reusable multi-step analysis) create `wiki/meta/<YYYY-MM-DD>-<topic>.md` with `type: meta`.
3. New pages: frontmatter `name`, `description`, `type`, `tags[]`, `title:` = filename stem; allocate `address:` with `bash ~/projects/vault-engine/scripts/allocate-address.sh` before the index refresh.
4. Touch `wiki/hot.md` only if the current state changed (pending decisions/actions only, under 7 KB). No log entry: `git log` is the operation log since 2026-09-26 (`wiki/log.md` is a frozen archive).
5. Refresh the index: `python3 ~/projects/vault-engine/scripts/contextual-prefix.py --all && python3 ~/projects/vault-engine/scripts/bm25-index.py build` (both; never `--allow-egress`).
6. Commit with the subject `<page>: what changed` (several pages: the main one, or `vault: …`) and push (standing grant for this repo). The SessionEnd hook commits anything left over, but a named commit is better history.

Everything written to the vault is English; verbatim user quotes stay in their original language.
