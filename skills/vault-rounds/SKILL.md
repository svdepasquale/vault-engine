---
name: vault-rounds
description: "Run the knowledge vault's on-demand rounds — maintenance, the learning round, the dream — when the user asks for one: /vault-rounds [maintenance|learning|dream|all], or in words (\"run the learning round\"). Never unprompted: the SessionStart reminder only offers them."
argument-hint: "[maintenance | learning | dream | all]"
---

# /vault-rounds — the vault's rounds, by name

The rounds stay on demand. This skill is how the user asks for one, on whichever machine holds the session; it is not a timer's entry point (an unattended `semantic-scan.py --dump` would date the learning round with no judging done). Vault: `${WIKI_VAULT:-$HOME/projects/knowledge-vault}`. Engine: `$HOME/projects/vault-engine`. Resolve both to absolute paths on this machine.

Argument: `maintenance`, `learning`, `dream`, or `all` (the three, in that order). The dream runs only when named, as `dream` or `all`. With no argument, decide what is due after step 3's sync, from the vault's own markers, not from the SessionStart section, which is read before the sync. The markers are `.vault-meta/last-maintenance` (due at 21 days), `.vault-meta/last-learning` (30) and the newest `date` in `maintenance/dream-ledger.jsonl` (30). Run the due maintenance and learning, and name the dream if it is due. If nothing is due, say so and ask which one.

## Before any round

1. Read the vault's `CLAUDE.md` §Learning vs dream and the procedures page it points to, whole. The steps, the private values (hosts, repos, URLs) and each round's last run live there, not here.
2. Check the machine is free for it. If the vault records a rule that this machine must not run agents or models while it is used for something else (a game, a stream), check that rule now and stop if it does not hold. Keep loud local compute (a local model judge) out of the vault's night window.
3. Sync. A fresh session pulled the other clone a few seconds in; an older or resumed one did not: run `git -C <vault> pull --ff-only`. If it refuses, say why and stop.

## maintenance

`bash <engine>/maintenance/wiki-maintenance.sh`: no model, a safe frontmatter autofix, the full lint into `wiki/.lint/report.json`, the run date. Then read the report in the session: the age and size findings, and `hub_lag`, whose pages the learning round reads.

## learning

The procedures page has the steps; in short:
1. `semantic-scan.py --dump` (no model; it stamps the round).
2. Claude judges each pair against the live pages. A read-only subagent for the 30 pairs is the usual shape.
3. Re-check every proposal at its source before applying it.
4. Apply the verified fixes and links: typed relations on both pages, one commit per page. A fact that replaces another is replaced on every living page that states it.
5. Read the `hub_lag` pages from a fresh full lint: `python3 <engine>/maintenance/wiki-lint.py > <scratchpad>/lint.json`, unless maintenance ran in this session. `wiki/.lint/report.json` belongs to one clone and can be weeks old.
6. Run `wiki-lint.py`, refresh the index, and run the acceptance gate (vault `CLAUDE.md` §Acceptance gate).

Every write happens in this session, where the user sees it.

## dream

Invoking `/vault-rounds dream` (or `all`) is the user's explicit go for the multi-agent run.
1. Seeds: `dream.py --dry` (no model) gives the pairs.
2. Run `Workflow({scriptPath: "<engine>/maintenance/dream-claude.workflow.js", args: {...}})`, with `vault` and `engine` as this machine's absolute paths and the other args as the procedures page gives them.
3. Review gate. Claude first checks every survivor's premises on the live pages. Then the user gets one message: the endorsed ideas, one line each, and the discarded ones with their reasons.
4. Every verdict, Claude's and the user's, is a line in the dream ledger. File only what the user approves, on the page it concerns.
5. Commit the ledger by name: it is outside the auto-commit's pathspec.

## Close

One line per round: done (what changed, with the commits), nothing to do, or not done (why). Then push; the vault has a standing push grant.
