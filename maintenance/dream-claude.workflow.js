// dream-claude.workflow.js — stage 2 of the vault dream: Claude proposes when the
// local model (maintenance/dream.py) yields nothing that survives review.
// Run from Claude Code (multi-agent: needs the user's explicit go):
//   Workflow({scriptPath: "<vault>/maintenance/dream-claude.workflow.js",
//             args: {date: "YYYY-MM-DD", ledger: [...titles + reasons], pending: [...],
//                    maxCandidates: 8, pairs: [...] /* optional; default: dream.py --dry */}})
// Every agent is read-only; Claude alone writes the ledger and files only what
// the user approves. First run, numbers and review: wiki/meta/2026-10-03-dream-pilot.md.
// Resume note: the cache replays the longest unchanged prefix of agent() calls in
// CALL order, and verify gates start in completion order — a resume can re-run a
// few finished gates (2 novelty gates on 2026-10-04).
export const meta = {
  name: 'dream-claude',
  description: 'Claude-side vault dream: original ideas from the wiki, triaged, then verified through three skeptical gates',
  whenToUse: 'When maintenance/dream.py (local model) yields no idea that survives review, or the user asks Claude for original proposals from the vault',
  phases: [
    { title: 'Seeds', detail: 'page pairs from dream.py --dry when none are passed' },
    { title: 'Generate', detail: 'bridges over page pairs + open threads + recurring failures + simplification + reuse' },
    { title: 'Triage', detail: 'merge duplicates, drop ledger repeats and pending items, cap' },
    { title: 'Verify', detail: 'premises -> value -> novelty/decisions, each defaulting to refute' },
    { title: 'Synthesize', detail: 'rank the survivors' },
  ],
}

const A = args || {}
const VAULT = A.vault    // the vault (data) — required
const ENGINE = A.engine  // this repo's checkout (code, split from the vault 2026-10-06) — required
if (!VAULT || !ENGINE) throw new Error('pass args.vault and args.engine (absolute paths)')
const REPOS = (A.repos || []).join(', ') || 'the code repositories the vault pages point to'  // list them in args.repos
const DATE = A.date || 'unknown'
const MAX = A.maxCandidates || 8
const PAIRS_PER_AGENT = A.pairsPerAgent || 4
const LEDGER = (A.ledger || []).map(l => '- ' + l).join('\n') || '(read maintenance/dream-ledger.jsonl)'
const PENDING = (A.pending || []).map(l => '- ' + l).join('\n') || '(none)'

const COMMON = `You are working on the user's personal knowledge vault at ${VAULT} (plain markdown in git; Claude's long-term memory for one person's homelab and tooling). Today is ${DATE}.

ADVISOR: do not call the advisor tool unless you are stuck after two or more serious attempts or face a security-critical decision (the user's model-routing rule: never for routine) — not as an end-of-task check. On the first run 45 of 56 agents called it routinely, about 29% of the run's cost.

READ-ONLY. Do not Edit or Write any file, do not run any command that changes a file or git state, and do not run the model stages of the dream scripts. Other sessions may be writing to this repo. Read files with Read; search the vault with \`cd ${VAULT} && python3 scripts/retrieve.py "<English query>" --top 3 --chunks --compact\` (its index may lag today's edits, so open the live page with Read before relying on a chunk). Code repos you may read, read-only: ${REPOS}, and ${ENGINE} (the vault's own code).`

const CLASSES = `A local-model pilot (wiki/meta/2026-10-03-dream-pilot.md) produced 29 ideas and every one died in review. The ways they died — check each one BEFORE you propose anything:
1. Reverses a recorded decision (a page says "deliberately", "user decision", "decided", "rejected", "accepted risk", "NOT", "do not build it"). Example: moving a job back to a local model when a page records that it was moved to a hosted API on purpose.
2. Already in place or already offered (a script, alert, rule or URL already does it; Claude already offered it). Example: a one-liner to fetch a credential that a repository's bootstrap script already fetches.
3. Stale or false premise (struck-through text, "until <date>", retired tools and machines — the vault marks them superseded, decommissioned or struck through —, the frozen wiki/log.md). Example: building on a model runtime the vault records as removed.
4. Broken mechanism or wrong picture of a flow (the steps do not work, or happen in a different order). Example: reading an "exp" claim from a classic GitHub PAT, which is opaque.
5. Not worth it for one person (saves cents, needs recurring manual upkeep, or degrades silently when forgotten).
6. Unobserved generalization or unmeasured caveat (the user's rule: verify before warning).
7. Enterprise pattern for a hobby single-user setup (HA, service mesh, on-call, extra environments, a policy engine for one person).

Ideas already judged — do not re-propose them or near-variants:
${LEDGER}

Pending with the user already — do not re-propose:
${PENDING}`

const IDEA_ITEM = {
  type: 'object',
  properties: {
    title: { type: 'string' },
    kind: { type: 'string', enum: ['automation', 'simplification', 'synergy', 'experiment', 'question', 'fix'] },
    pages: { type: 'array', items: { type: 'string' } },
    idea: { type: 'string' },
    first_step: { type: 'string' },
    value: { type: 'string' },
    premises: {
      type: 'array',
      items: {
        type: 'object',
        properties: { claim: { type: 'string' }, path: { type: 'string' }, quote: { type: 'string' } },
        required: ['claim', 'path', 'quote'],
      },
    },
    checked: {
      type: 'object',
      properties: {
        decision: { type: 'string' }, in_place: { type: 'string' }, stale: { type: 'string' },
        mechanism: { type: 'string' }, worth_it: { type: 'string' }, observed: { type: 'string' },
        enterprise: { type: 'string' },
      },
      required: ['decision', 'in_place', 'stale', 'mechanism', 'worth_it', 'observed', 'enterprise'],
    },
  },
  required: ['title', 'kind', 'pages', 'idea', 'first_step', 'value', 'premises', 'checked'],
}
const IDEA_SCHEMA = { type: 'object', properties: { ideas: { type: 'array', items: IDEA_ITEM } }, required: ['ideas'] }

const OUTPUT_RULES = `Return at most 3 ideas — fewer is better than weak ones, and an empty list is a valid answer. For each idea: title (max 90 chars), kind, the wiki pages it touches, the idea in 2-4 sentences, ONE concrete first step, why it is worth it for this one person (value), the premises it rests on — each with the file path and a verbatim quote you read in the live file — and, under "checked", one line per failure class saying what you checked and where (e.g. "decision: dotfiles.md, no decision against it"). Write in English.`

function chunk(arr, n) { const out = []; for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n)); return out }

phase('Seeds')
let pairs = A.pairs
if (!pairs || !pairs.length) {
  const got = await agent(`${COMMON}\n\nRun \`cd ${VAULT} && WIKI_VAULT=${VAULT} python3 ${ENGINE}/maintenance/dream.py --dry --pairs ${A.pairCount || 8}\` (it only prints, no model is called) and return the page pairs it lists: the two wiki/ paths on each line after the cosine.`, {
    label: 'seeds', phase: 'Seeds',
    schema: { type: 'object', properties: { pairs: { type: 'array', items: { type: 'object', properties: { page_a: { type: 'string' }, page_b: { type: 'string' } }, required: ['page_a', 'page_b'] } } }, required: ['pairs'] },
  })
  // A dead agent (quota, API error) is an unfinished run, not "no pairs" —
  // same rule as the Verify gates (cloud review of 2026-10-04).
  if (!got) {
    log('seeds agent died (quota or API error): nothing generated — resume the run')
    return { error: 'seeds', generated: [], deadGenerators: [], candidates: [], dropped: [], verified: [], survivors: [], refuted: [], errored: [], shortlist: null }
  }
  pairs = got.pairs || []
}
log(`${pairs.length} page pairs for the bridge generators`)

const generators = chunk(pairs, PAIRS_PER_AGENT).map((group, gi) => ({
  label: `bridge-${gi + 1}`,
  prompt: `${COMMON}\n\n${CLASSES}\n\nMODE: bridges. For each page pair below, read BOTH pages in full with Read, and any third page you need (retrieve.py, Read, repos). Look for an idea that exists only because the two pages meet: something neither page says, which follows from a fact on one meeting a fact on the other. Most pairs have no such idea — skip them. Pairs:\n${group.map(p => `- ${p.page_a} x ${p.page_b}`).join('\n')}\n\n${OUTPUT_RULES}`,
})).concat([
  { label: 'open-threads', prompt: `${COMMON}\n\n${CLASSES}\n\nMODE: open threads. Read ${VAULT}/wiki/hot.md §Open threads and the pages each thread links. For each thread ask what it LACKS — a missing step, a cheaper path, a test that would settle a pending decision, an item that can already be closed — never the pending action restated.\n\n${OUTPUT_RULES}` },
  { label: 'recurring-failures', prompt: `${COMMON}\n\n${CLASSES}\n\nMODE: recurring failures. Read wiki/entities/platform-ops-gotchas.md, wiki/entities/workstation-gotchas.md and wiki/entities/vault-gotchas.md. Find a failure CLASS that has struck at least twice and is not yet closed, and propose the change that removes the class, not another check for it. Banned by the user's rules: generic monitoring or alerting, nag-style reminders (feedback-token-rotation-reminders: only expiry-driven rotations get dated reminders), unmeasured caveats and hardening that needs recurring manual upkeep (feedback-verify-before-warning).\n\n${OUTPUT_RULES}` },
  { label: 'simplification', prompt: `${COMMON}\n\n${CLASSES}\n\nMODE: simplification. Read wiki/entities/_index.md, wiki/sources/_index.md, wiki/runbooks/_index.md and wiki/hot.md, then any page or repo you need. Find something that can be removed, merged or retired — a service, agent, script, config, page, tool, duplicate path — with nothing lost. The user's recorded taste runs this way — the vault records its past simplifications (hot.md, the dated meta pages): fewer resident services, on-demand jobs instead of always-on timers, tools retired rather than kept alongside their replacements. Read as widely as you need.\n\n${OUTPUT_RULES}` },
  { label: 'reuse', prompt: `${COMMON}\n\n${CLASSES}\n\nMODE: reuse across machines. The estate: read ${VAULT}/maintenance/dream-context.md and hot.md §Topology. Find a need or open item on one of them that something ALREADY present elsewhere can serve, with no new component. Read entity pages, hot.md and repos as widely as you need.\n\n${OUTPUT_RULES}` },
])

phase('Generate')
const gen = await parallel(generators.map(g => () =>
  agent(g.prompt, { label: g.label, phase: 'Generate', schema: IDEA_SCHEMA })
    .then(r => (r ? { mode: g.label, ideas: r.ideas || [] } : null))))
const deadGenerators = gen.map((g, i) => (g ? null : generators[i].label)).filter(Boolean)
const generated = gen.filter(Boolean).flatMap(g => g.ideas.map(i => Object.assign({}, i, { mode: g.mode })))
log(`generated ${generated.length} ideas from ${generators.length - deadGenerators.length}/${generators.length} generators`)
if (deadGenerators.length) log(`dead generators, coverage is partial (resume to re-run them): ${deadGenerators.join(', ')}`)

phase('Triage')
let candidates = []
let dropped = []
if (generated.length) {
  const tri = await agent(`${COMMON}\n\n${CLASSES}\n\nTRIAGE. Below are ${generated.length} ideas from independent generators (JSON). Merge duplicates and near-duplicates (keep the best-evidenced version and list what was merged), drop any that repeats a judged idea or a pending item listed above, and keep at most ${MAX}: the ones most likely to survive a skeptical review of premises, novelty and value. Do not verify deeply here; judge from the text plus a quick look at a page if needed. Return each kept idea as its index in the list below plus an id c1, c2, ...; give a full merged idea ONLY when you merged several (with their indexes in merged_from) — do not copy unmerged ideas. Return every dropped idea with a one-line reason.\n\nIDEAS (index: idea):\n${generated.map((g, i) => i + ': ' + JSON.stringify(g)).join('\n')}`, {
    label: 'triage', phase: 'Triage',
    schema: {
      type: 'object',
      properties: {
        kept: { type: 'array', maxItems: MAX, items: { type: 'object', properties: { id: { type: 'string' }, index: { type: 'integer' }, merged_from: { type: 'array', items: { type: 'integer' } }, merged_idea: IDEA_ITEM }, required: ['id', 'index'] } },
        dropped: { type: 'array', items: { type: 'object', properties: { title: { type: 'string' }, reason: { type: 'string' } }, required: ['title', 'reason'] } },
      },
      required: ['kept', 'dropped'],
    },
  })
  // E3 (review 2026-10-04): the first run's triage re-emitted every kept idea in
  // full (97k chars, ~4 min on the barrier); indexes are enough unless merged.
  if (!tri) {
    log('triage agent died (quota or API error): nothing verified — resume the run')
    return { error: 'triage', generated, deadGenerators, candidates: [], dropped: [], verified: [], survivors: [], refuted: [], errored: [], shortlist: null }
  }
  const kept = (tri.kept || [])
    .map(k => ({ id: k.id, idea: k.merged_idea || generated[k.index], merged_from: k.merged_from || [] }))
    .filter(k => k.idea)
  // maxCandidates is the cost lever (each candidate costs up to 3 gate agents):
  // enforced here and by the schema's maxItems, not only asked for in the prompt.
  if (kept.length > MAX) log(`triage kept ${kept.length}, over the cap: verifying the first ${MAX}`)
  candidates = kept.slice(0, MAX)
  dropped = tri.dropped || []
}
log(`triage: ${candidates.length} kept, ${dropped.length} dropped (cap ${MAX})`)
if (!candidates.length) {
  log('no candidates survived triage: nothing to verify')
  return { generated, deadGenerators, candidates: [], dropped, verified: [], survivors: [], refuted: [], errored: [], shortlist: null }
}

const VERDICT = {
  type: 'object',
  properties: {
    pass: { type: 'boolean' },
    reason: { type: 'string' },
    evidence: { type: 'array', items: { type: 'object', properties: { path: { type: 'string' }, quote: { type: 'string' } }, required: ['path', 'quote'] } },
  },
  required: ['pass', 'reason', 'evidence'],
}
const show = c => JSON.stringify(c.idea, null, 1)
// Order (review 2026-10-04): premises, then value — cheapest gate that refuted
// (4 of 10 on the first run) — then novelty, the costliest (0 of 10 refuted).
const gates = [
  { key: 'premises', prompt: c => `${COMMON}\n\nGATE 1 of 3 — PREMISES. You are a skeptic; default to refuting. Re-derive every premise of the idea below from the LIVE files: the generator's quotes are claims, not evidence. For each premise open the file with Read and check the text exists, is current (not struck through, not "until <date>", not marked superseded/closed/removed/void, not contradicted by a later dated line on that or another page — use retrieve.py on the topic) and says what the idea needs. For claims about code or config, read the repo. Then check the mechanism: would the first step and the idea actually work, in the order the real flow runs? Uncertain -> pass=false. Every refutation cites path + verbatim quote.\n\nIDEA:\n${show(c)}` },
  { key: 'value', prompt: c => `${COMMON}\n\nGATE 2 of 3 — VALUE. You are a skeptic; default to refuting. Judge the idea for THIS user: one person, a hobby homelab, careful with both money and attention. Their rules (read wiki/meta/profile/ if unsure): no enterprise patterns for a hobby setup (communication-preferences); no unmeasured caveats and no hardening whose price is recurring manual upkeep with silent degradation if forgotten (feedback-verify-before-warning); CI/bots/linters only for repos with something to manage (feedback-tooling-scope); only expiry-driven rotations get dated reminders (feedback-token-rotation-reminders). Pass only if the benefit is concrete for this person, the setup is small, upkeep is near zero and nothing degrades silently. Say what it saves or prevents, measured where the vault has a number. Uncertain -> pass=false. Cite path + quote.\n\nIDEA:\n${show(c)}` },
  { key: 'novelty', prompt: c => `${COMMON}\n\n${CLASSES}\n\nGATE 3 of 3 — NOVELTY AND DECISIONS. You are a skeptic; default to refuting. Is this idea already done, already planned, already rejected or declined, contrary to a recorded decision or standing rule (~/.claude/CLAUDE.md §Standing rules and wiki/meta/profile/feedback-*.md), already offered by Claude, or a variant of a judged or pending idea listed above? Search with retrieve.py (several English queries) and Read the hits, check ${VAULT}/maintenance/dream-ledger.jsonl, \`git -C ${VAULT} log --oneline -300\` for related work, and the code repos. Uncertain -> pass=false. Cite path + quote.\n\nIDEA:\n${show(c)}` },
]

phase('Verify')
const verified = await pipeline(candidates, ...gates.map(g => async (prev, c) => {
  const state = prev && prev.gates ? prev : { c, gates: [] }
  if (state.error || state.gates.some(x => !x.pass)) return state
  const v = await agent(g.prompt(c), { label: `${g.key}:${c.id}`, phase: 'Verify', schema: VERDICT })
  // A verifier that died (quota, API error) is not a refutation: keep the item
  // out of both lists so a resume re-runs it (first run, 2026-10-03: 10 gates
  // lost to a session limit had been counted as refuted).
  if (!v) { state.error = g.key; return state }
  state.gates.push(Object.assign({ gate: g.key }, v))
  return state
}))
const done = verified.filter(Boolean)
const errored = done.filter(s => s.error).map(s => ({ id: s.c.id, title: s.c.idea.title, gate: s.error }))
const survivors = done.filter(s => !s.error && s.gates.length === gates.length && s.gates.every(g => g.pass))
const refuted = done.filter(s => !s.error && s.gates.some(g => !g.pass)).map(s => {
  const g = s.gates.find(x => !x.pass)
  return { id: s.c.id, title: s.c.idea.title, gate: g.gate, reason: g.reason, evidence: g.evidence }
})
log(`verify: ${survivors.length} survived all three gates, ${refuted.length} refuted, ${errored.length} unfinished (verifier died; resume the run)`)

phase('Synthesize')
let shortlist = null
if (survivors.length) {
  shortlist = await agent(`${COMMON}\n\nSYNTHESIS. These ideas survived three skeptical gates (premises, value, novelty/decisions). Rank them by value for the user, best first. For each: a one-line pitch, the first step, the evidence (path + quote) that matters most, the expected benefit, the cost, and anything the user must decide. English. Do not add ideas.\n\nSURVIVORS:\n${JSON.stringify(survivors.map(s => ({ id: s.c.id, idea: s.c.idea, gates: s.gates })))}`, {
    label: 'synthesis', phase: 'Synthesize',
    schema: {
      type: 'object',
      properties: {
        ranked: { type: 'array', items: { type: 'object', properties: {
          id: { type: 'string' }, title: { type: 'string' }, pitch: { type: 'string' }, first_step: { type: 'string' },
          evidence: { type: 'string' }, benefit: { type: 'string' }, cost: { type: 'string' }, user_decides: { type: 'string' },
        }, required: ['id', 'title', 'pitch', 'first_step', 'evidence', 'benefit', 'cost'] } },
      },
      required: ['ranked'],
    },
  })
}
return { generated, deadGenerators, candidates, dropped, verified: done, survivors, refuted, errored, shortlist }
