// Consolidation pass over journal-shaped vault pages (vault CLAUDE.md §Consolidate),
// first run 2026-10-06/07 (12 pages in two waves: 6 then 15 agents).
// Per unit: an Opus draft written ONLY to the scratchpad (read the vault through
// `git show <base>:<path>`), an independent Sonnet verifier (lost / drift /
// unsupported), an Opus revision that applies or rejects each finding. Nothing here
// writes the vault: the caller applies one unit at a time — check the live page's
// blob still equals the base blob, copy the draft, run the gate (same gold files
// before and after), commit with the ledger saved under
// <vault>/maintenance/consolidation-ledger/. Multi-agent: only on the user's go.
// Run: Workflow({scriptPath: "<vault-engine>/maintenance/consolidate.workflow.js",
//   args: {vault, base, out, date, units: [{id, pages: [{path, kb}], notes}]}})
export const meta = {
  name: 'vault-consolidate',
  description: 'Consolidate journal-shaped vault pages: draft rewrite, independent verify, revise (drafts in scratchpad only, no vault writes)',
  phases: [
    { title: 'Draft', detail: 'one agent per unit rewrites its pages to current state; drafts to scratchpad' },
    { title: 'Verify', detail: 'independent read-only verifier: lost / drift / unsupported', model: 'sonnet' },
    { title: 'Revise', detail: 'apply valid findings to the drafts, reject the rest with reasons' },
  ],
}

const V = args.vault
const BASE = args.base
const OUT = args.out
const DATE = args.date

const DRAFT_SCHEMA = {
  type: 'object',
  properties: {
    drafts: { type: 'array', items: { type: 'object', properties: {
      path: { type: 'string' }, draft_file: { type: 'string' },
      old_bytes: { type: 'integer' }, new_bytes: { type: 'integer' } },
      required: ['path', 'draft_file', 'old_bytes', 'new_bytes'] } },
    ledger_file: { type: 'string' },
    uncertain: { type: 'array', items: { type: 'string' } },
    cited_headings_kept: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
  required: ['drafts', 'ledger_file', 'uncertain', 'notes'],
}

const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object', properties: {
      kind: { type: 'string', enum: ['LOST', 'DRIFT', 'UNSUPPORTED'] },
      page: { type: 'string' }, old_quote: { type: 'string' }, new_quote: { type: 'string' },
      why: { type: 'string' }, fix: { type: 'string' } },
      required: ['kind', 'page', 'why', 'fix'] } },
    verdict: { type: 'string' },
  },
  required: ['findings', 'verdict'],
}

const REVISE_SCHEMA = {
  type: 'object',
  properties: {
    applied: { type: 'integer' },
    rejected: { type: 'array', items: { type: 'object', properties: {
      finding: { type: 'string' }, reason: { type: 'string' } }, required: ['finding', 'reason'] } },
    final_drafts: { type: 'array', items: { type: 'object', properties: {
      path: { type: 'string' }, draft_file: { type: 'string' }, new_bytes: { type: 'integer' } },
      required: ['path', 'draft_file', 'new_bytes'] } },
    notes: { type: 'string' },
  },
  required: ['applied', 'rejected', 'final_drafts'],
}

const pageList = (u) => u.pages.map(p => `- ${p.path} (${p.kb} KB)`).join('\n')

function draftPrompt(u) {
  return `You consolidate pages of a personal knowledge vault. Vault: ${V} (a git repo; plain markdown under wiki/, English only). Today is ${DATE}.

STRICT WRITE RULE: do not modify, create or delete anything under ${V} — no edits, no commits, no index refresh. Read the vault through git: \`git -C ${V} show ${BASE}:<path>\` is the exact version you consolidate (base commit ${BASE}). Write ONLY under ${OUT}/${u.id}/ (create the directory).

Pages in this unit:
${pageList(u)}

Unit notes: ${u.notes}

What consolidation means here (vault CLAUDE.md §Consolidate):
- A page keeps its CURRENT state, decisions with their reasons, user decisions (often carrying an Italian verbatim quote — keep quotes verbatim), rejected alternatives with their numbers, open items (unchecked boxes) and gotchas that still apply. Session narrative (what was tried in which order), resolved diagnostics, per-session snapshots and superseded or struck-through text leave the page: git keeps every version. End the page's intro with one sentence saying it was consolidated on ${DATE} and that the session record is in \`git log -- <path>\`, the last full version in \`git show ${BASE}:<path>\`.
- Resolve staleness inside the page: when a later dated section supersedes an earlier statement, keep only the current one and say since when. If neither the page nor other vault pages tell you whether a statement is still current, KEEP it and list it as uncertain.
- Do not invent facts, numbers, dates or commands. Do not touch live systems (no ssh, kubectl, tailscale, network calls). Reading other vault pages (\`git -C ${V} show ${BASE}:<path>\`, \`git -C ${V} grep ... ${BASE} -- wiki\`) is fine and encouraged, to see what is canonical where.
- The page's FIRST sentence after the H1 says what the thing is — it becomes the prefix of every retrieval chunk of the page.
- Frontmatter: keep every field, the \`relations:\` block byte-for-byte, and \`address:\`; set \`updated: ${DATE}\` and add \`consolidated: ${DATE}\` right after it. Keep \`description:\` unless it is now wrong.
- Keep, worded exactly as before, every heading another page cites with \`§\` (find them: \`git -C ${V} grep -n -E "\\\\[\\\\[([^]|]*/)?<slug>(\\\\|[^]]*)?\\\\]\\\\][^.]{0,6}§" ${BASE} -- wiki\`). Keep every [[wikilink]] target the old page linked, at least once. Open items stay as a checklist; closed items leave (their lasting result stays in the right section).
- Prefer present-tense statements with "since YYYY-MM-DD" over dated session headings; group by topic (state, configuration, decisions, gotchas, open items), not by session.
- A fact that belongs on another page (a procedure that has a runbook, or a block duplicated on a canonical page) is replaced by one line + [[link]]; never keep the same block on two pages.

Deliverables, all under ${OUT}/${u.id}/:
1. For each page, the full new file at ${OUT}/${u.id}/<basename of its path>.
2. ${OUT}/${u.id}/ledger.md: one line per old section or block — "old heading (bytes) → kept / rewritten / moved to X / left to git (why)" — plus every dropped statement that carried a number or a decision, with where it went.

Return the structured result: drafts (vault path, draft file, old and new byte sizes), the ledger path, the uncertain statements (quote + why), the cited headings you kept, short notes.`
}

function verifyPrompt(u, d) {
  return `You are an independent, skeptical verifier of a wiki page rewrite. READ-ONLY: do not edit, create or delete any file anywhere.

Vault: ${V} (plain markdown, English). Convention (vault CLAUDE.md §Consolidate): a page keeps the current state, decisions with reasons, user decisions, rejected alternatives with their numbers, open items and live gotchas; session narrative leaves because git keeps every version.

OLD versions — read each with \`git -C ${V} show ${BASE}:<path>\`:
${pageList(u)}
NEW versions (drafts):
${d.drafts.map(x => `- ${x.draft_file} (for ${x.path})`).join('\n')}
Ledger: ${d.ledger_file}
Pages the new version points to may hold moved material: read them (\`git -C ${V} show ${BASE}:<path>\`) before calling something lost.

Read each OLD version completely, then the NEW one and the ledger. Report:
1. LOST — a statement in OLD that is (a) still current as of ${DATE} (a later dated section can supersede an earlier one; superseded, deleted, retired, abandoned or struck-through items are NOT current), (b) absent from NEW and from the pages it points to, and (c) not deliberately left to git by the ledger as session narrative or snapshot. Only what a future session would need: configuration facts, gotchas, decisions and their reasons, user decisions, open to-dos, numbers that drive a decision.
2. DRIFT — a decision, user decision, number, date or causal claim that NEW states differently (weaker, stronger, wrong number or date, lost qualifier, wrong attribution).
3. UNSUPPORTED — a factual statement in NEW that neither OLD nor a page it points to supports (pointers excepted).
Also check: frontmatter preserved (relations block identical to OLD), every heading other pages cite with § kept (\`git -C ${V} grep ... ${BASE} -- wiki\`), every old wikilink target still linked.
Be strict and specific: for each finding give the page, the verbatim OLD quote (max 200 chars), the NEW quote if relevant, why it matters in one sentence, and the exact fix. At most 25 findings, most important first. End with a one-line verdict.`
}

function revisePrompt(u, d, v) {
  return `You revise draft rewrites of vault pages after an independent verification. Write ONLY the draft files and the ledger listed below (all under ${OUT}/${u.id}/); do not touch ${V}.

Drafts:
${d.drafts.map(x => `- ${x.draft_file} (for ${x.path})`).join('\n')}
Ledger: ${d.ledger_file}
OLD versions: \`git -C ${V} show ${BASE}:<path>\` for each of:
${pageList(u)}

Verifier findings (JSON):
${JSON.stringify(v.findings, null, 1)}

For each finding, check it against OLD (and any page it cites). If it is correct, apply the fix in the draft, keeping the consolidation rules: current state, decisions with reasons, user decisions verbatim, open items as a checklist, cited § headings worded as before, the relations block untouched, no invented facts. If it is wrong — OLD does not say that, the statement is superseded, or the ledger deliberately left it to git as narrative — reject it with a one-sentence reason. Append a "## Verifier findings" section to the ledger listing what you applied (one line each) and what you rejected (with the reason). Return the structured result.`
}

phase('Draft')
const results = await pipeline(
  args.units,
  (u) => agent(draftPrompt(u), { label: `draft:${u.id}`, phase: 'Draft', schema: DRAFT_SCHEMA }),
  (d, u) => agent(verifyPrompt(u, d), { label: `verify:${u.id}`, phase: 'Verify', schema: VERIFY_SCHEMA, model: 'sonnet', effort: 'high' })
    .then(v => ({ d, v })),
  ({ d, v }, u) => {
    if (!v.findings.length) {
      return { unit: u.id, draft: d, verify: v, revise: { applied: 0, rejected: [], final_drafts: d.drafts, notes: 'no findings' } }
    }
    return agent(revisePrompt(u, d, v), { label: `revise:${u.id}`, phase: 'Revise', schema: REVISE_SCHEMA, effort: 'high' })
      .then(r => ({ unit: u.id, draft: d, verify: v, revise: r }))
  },
)
const done = results.filter(Boolean)
log(`${done.length}/${args.units.length} units finished`)
return done.map(r => ({
  unit: r.unit,
  drafts: r.revise.final_drafts,
  ledger: r.draft.ledger_file,
  uncertain: r.draft.uncertain,
  findings: r.verify.findings.length,
  verdict: r.verify.verdict,
  applied: r.revise.applied,
  rejected: r.revise.rejected,
  notes: [r.draft.notes, r.revise.notes].filter(Boolean).join(' | '),
}))
