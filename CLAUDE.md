# vault-engine

The code that runs Claude's long-term memory: retrieval, maintenance, ideation, consolidation and evaluation over the knowledge vault. Split out of `knowledge-vault` on 2026-10-06 (history kept with `git filter-repo`): the vault holds the data, this repo the engine. Conventions for the data itself — page schema, writes, the typed graph, the acceptance-gate figures — stay in the vault's `CLAUDE.md` and `WIKI.md`.

## Contract

- Every script finds its own siblings relative to itself and the vault through **`WIKI_VAULT`** (default `~/projects/knowledge-vault`): `wiki/`, `.vault-meta/` (index, address counter, locks), `eval/goldset*.json`, `maintenance/dream-ledger.jsonl`, `wiki/.lint/` and the `.git/wiki-*` markers all live in the vault.
- Callers: the dotfiles hooks (`wiki-autocommit.sh` refreshes the index and writes the consolidation-due list, `wiki-hot-cache.sh` probes the embedder, the `UserPromptSubmit` recall hook), the `save` and `wiki-apply` skills (symlinked into `~/.claude/skills/` by chezmoi), `vault-explorer`'s Recall lens, and Claude's read protocol in `~/.claude/CLAUDE.md`.

## Layout

```
scripts/       retrieval: contextual-prefix (chunks), bm25-index, rerank (local llama-embedding), retrieve, recall hook; address/lock helpers
maintenance/   wiki-maintenance.sh (lint → autofix → LLM proposals → judge → PENDING), semantic-scan, dream + dream-claude.workflow.js, consolidation-due; tests/; launchd/ (on demand)
eval/          run-eval.py — the gold sets it reads are vault data
skills/        save, wiki-apply (live), wiki-fold (retired record)
```

## Public repo (since 2026-10-07)

- **Nothing from the private vault goes in** — no page names, hosts, machines, services, people or quotes in code, comments, tests, commit messages or issues. Machine-specific values come from the environment (`WIKI_VAULT`, `WIKI_LINT_EVO_URL`, `WIKI_DREAM_EVO_URL`) or from the vault itself (`maintenance/dream-context.md`, the dream workflow's `args.repos`). Synthetic data only in tests.
- **This clone is the one in use**: the hooks, the skills and Claude's read protocol run `~/projects/vault-engine/...` directly. Change the code here, never in a copy, and never add code back to the vault.
- Commits: `area: short imperative subject`, signed (a ruleset requires it; another blocks force-push and deletion of `main`). Direct pushes to `main`. CI: `ci.yml` (byte-compile + tests), `ci-security.yml` (gitleaks, actionlint, zizmor).

## Working here

- Tests: `bash maintenance/run-tests.sh` (pytest via `uv`).
- Any retrieval change is measured before and after with the gate in the vault's `CLAUDE.md` §Acceptance gate: `python3 eval/run-eval.py --gold <set>` for the four gold sets, local hybrid and BM25-only (`WIKI_EMBED_URL=http://127.0.0.1:9`). `run-eval.py` prints the gold hash, the vault HEAD and this repo's HEAD.
- Changing the embedding input needs an `EMBED_SCHEME` bump in `scripts/rerank.py`.
- Nothing commits this repo automatically (the vault's SessionEnd hook commits only the vault's `wiki/` and `.vault-meta/`): commit your own work with its rationale, and push.
