export const meta = {
  name: 'leapp-notes-audit',
  description: 'Read-only notes audit of LEAPP modules: one auditor per module, then a blind check of the flagged statements mixed with unflagged controls',
  whenToUse: 'Auditing the examiner-facing text of several LEAPP modules at once. Prepare the input with notes-audit/prepare.py and run_corpus.sh first, then pass the args.json it writes.',
  phases: [
    { title: 'Audit', detail: 'one leapp-notes-auditor per module' },
    { title: 'Blind check', detail: 'one blind checker per module, statements only' },
  ],
}

// args (written by prepare.py): { root, head, core, src, modules: [{ module, keys, candidates: [{artifact, field, statement}] }] }
// The graph stops at findings. Fixes go through leapp-notes-auditor and the merge gate as usual.

const FINDINGS = {
  type: 'object',
  properties: {
    claims_checked: { type: 'integer' },
    keep: { type: 'integer' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          artifact: { type: 'string' },
          field: { type: 'string' },
          quote: { type: 'string' },
          kind: { type: 'string', enum: ['statement', 'omission'] },
          tier: { type: 'string', enum: ['data-proven', 'source-verified', 'unsupported'] },
          problem: { type: 'string' },
          support: { type: 'string' },
          verdict: { type: 'string', enum: ['soften', 'remove', 'add'] },
          replacement: { type: 'string' },
        },
        required: ['id', 'artifact', 'field', 'quote', 'kind', 'tier', 'problem', 'support', 'verdict', 'replacement'],
      },
    },
    could_not_check: { type: 'array', items: { type: 'string' } },
  },
  required: ['claims_checked', 'keep', 'findings', 'could_not_check'],
}

const CHECKS = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          outcome: { type: 'string', enum: ['supported', 'not_supported', 'could_not_check'] },
          reason: { type: 'string' },
          evidence: { type: 'string' },
        },
        required: ['id', 'outcome', 'reason', 'evidence'],
      },
    },
  },
  required: ['results'],
}

if (!args || !args.root || !args.head || !Array.isArray(args.modules)) {
  throw new Error('Pass the args.json written by notes-audit/prepare.py (root, head, core, src, modules).')
}
const { root, head, modules } = args
const core = args.core || 'ALEAPP'
const src = args.src || 'src'
const CONTROLS = 3
const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
const hash = (s) => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) } return h >>> 0 }
const inputs = (m) => `Root: ${root}\nModule: ${root}/${src}/scripts/artifacts/${m.module}.py, plus any shared helpers it calls under ${root}/${src} (pinned export of main at ${head})\nRun outputs, one per corpus key, each holding one report folder with _lava_artifacts.db and staged files under data/:\n${m.keys.map((k) => `- ${root}/run_output/${k}`).join('\n')}\nAn artifact with zero rows on a key has no LAVA table.`
const rules = `Read-only. Do not edit, commit, push or change anything under the root. Open LAVA databases only with a mode=ro URI built from Path.as_uri(). To open a staged SQLite file, first copy it with its -wal and -shm to a new folder you make with mktemp -d under the system temp directory, and delete that folder when done. Put any helper script in that folder too. Do not write to any scratchpad folder. Do not read anything else: no folder outside the root, no session transcripts, no memory files about this work, no local ${core} checkout, and no ${core} GitHub history, PRs or issues. Public primary sources (AOSP, Apple or vendor docs) may be fetched, with no evidence value in any request. Never quote evidence content (names, numbers, message text, identifiers, or paths holding them): describe its shape and give counts. No em-dashes. Say "known data", never the other phrase for it.`

const results = await pipeline(
  modules,
  (m) => agent(
    `Audit the examiner-facing text of this ${core} module: every description, notes sentence and column header of every artifact in it.\n\n${inputs(m)}\n\n${rules}\n\nReturn only problems in findings, with ids F1, F2 and so on, and count the statements you would keep in keep. Set kind to "statement" when existing text is wrong, overstated or unsupported, and put in quote the exact text copied from the module (a sentence or a column header), with no paraphrase and none of your own framing. Set kind to "omission" when no existing sentence is wrong but the report leaves out something an examiner needs, such as a value parsed and dropped; quote the nearest related text.`,
    { label: `audit:${m.module}`, phase: 'Audit', agentType: 'leapp-notes-auditor', schema: FINDINGS },
  ),
  async (audit, m) => {
    if (!audit) return { module: m.module, audit: null, packet: [], results: null }
    const flagged = audit.findings.filter((f) => f.kind === 'statement')
    const quotes = flagged.map((f) => norm(f.quote))
    const controls = (m.candidates || []).filter((c) => { const n = norm(c.statement); return !quotes.some((q) => q && (q.includes(n) || n.includes(q))) }).slice(0, CONTROLS)
    if (controls.length < CONTROLS) log(`${m.module}: only ${controls.length} controls left after removing flagged sentences`)
    const rows = [
      ...flagged.map((f) => ({ artifact: f.artifact, field: f.field, statement: f.quote, kind: 'flagged', finding: f.id })),
      ...controls.map((c) => ({ artifact: c.artifact, field: c.field, statement: c.statement, kind: 'control', finding: null })),
    ].sort((a, b) => hash(m.module + a.statement + a.field) - hash(m.module + b.statement + b.field))
      .map((r, i) => ({ ...r, id: `${m.module}-${String(i + 1).padStart(2, '0')}` }))
    const packet = rows.map(({ id, artifact, field, statement }) => ({ id, artifact, field, statement }))
    const checked = await agent(
      `Check examiner-facing statements from one ${core} module against its code and real run outputs. Judge each statement yourself from the inputs.\n\n${inputs(m)}\n\n${rules}\n\nStatements:\n${JSON.stringify(packet, null, 1)}\n\nFor each statement, decide what it tells an examiner. A statement can be a sentence, an excerpt or a column header. Give one outcome: "supported" means code or data you read shows it is accurate. "not_supported" means code or data you read shows it is wrong, overstated, or states a meaning, unit or cause the inputs do not establish. "could_not_check" means these inputs cannot settle it; do not pick the other two on plausibility. Give the file:line or the query and its count in evidence. Return one result per id.`,
      { label: `blind:${m.module}`, phase: 'Blind check', schema: CHECKS },
    )
    return { module: m.module, audit, packet: rows, results: checked ? checked.results : null }
  },
)
const clean = results.filter(Boolean)
const missing = modules.filter((m) => !clean.some((r) => r.module === m.module && r.audit && r.results)).map((m) => m.module)
if (missing.length) log(`Incomplete: ${missing.join(', ')}`)
return { root, head, core, results: clean, incomplete: missing }
