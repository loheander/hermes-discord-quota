export const meta = {
  name: 'implement-story',
  description: 'Drive one hermes-discord-quota story issue to a merged PR: implement, review, Jev triage, fix, Jev merge gate',
  whenToUse: 'From /orchestrate-plan, once per story issue. args: {issue, pr?, fixRound?, open?, maxRounds?}',
  phases: [
    { title: 'Implement', detail: 'issue-implementer mode A opens the PR' },
    { title: 'Review', detail: 'issue-reviewer, then Jev triage and PR comment' },
    { title: 'Fix', detail: 'issue-implementer mode B on retained comments' },
    { title: 'Merge', detail: 'hard checks + Jev ready_to_merge, squash merge' },
    { title: 'Learn', detail: 'carry-over notes for later stories' },
  ],
}

// args: {issue: 10} for a fresh story. To continue a story mid-way: {issue, pr, fixRound: K, open: [...]}
// fixes round K's open comments first, then reviews round K+1.
const REPO = '/home/lo/repo/hermes-discord-quota'
const N = args.issue
const RUN = `${REPO}/.claude/runs/issue-${N}`
const MAX = args.maxRounds || 3
const CARRY = `Read ${REPO}/.claude/runs/carryover.md for notes from earlier stories and apply the ones relevant to this issue.`

const COMMENTS = {
  type: 'array',
  items: {
    type: 'object',
    properties: { id: { type: 'string' }, status: { type: 'string', enum: ['resolved', 'disputed'] }, note: { type: 'string' } },
    required: ['id', 'status', 'note'],
  },
}
const IMPL = {
  type: 'object',
  properties: {
    issue: { type: 'integer' }, branch: { type: 'string' }, pr: { type: 'integer' },
    tests: { type: 'string', enum: ['passed', 'failed'] }, test_summary: { type: 'string' },
    deviations: { type: 'string', description: 'At most 5 lines: deviations from PLAN.md or the issue, decisions the orchestrator must know' },
    comments: COMMENTS,
  },
  required: ['issue', 'branch', 'pr', 'tests', 'test_summary', 'deviations', 'comments'],
}
const REVIEW = {
  type: 'object',
  properties: {
    pr: { type: 'integer' }, round: { type: 'integer' },
    tests: { type: 'string', enum: ['passed', 'failed'] }, test_summary: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' }, file: { type: 'string' }, line: { type: 'integer' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] }, comment: { type: 'string' },
        },
        required: ['id', 'file', 'line', 'severity', 'comment'],
      },
    },
    previous: {
      type: 'array',
      items: {
        type: 'object',
        properties: { id: { type: 'string' }, status: { type: 'string', enum: ['resolved', 'unresolved'] }, note: { type: 'string' } },
        required: ['id', 'status', 'note'],
      },
    },
  },
  required: ['pr', 'round', 'tests', 'test_summary', 'findings', 'previous'],
}
const OPEN = {
  type: 'array',
  items: {
    type: 'object',
    properties: { id: { type: 'string' }, file: { type: 'string' }, line: { type: 'integer' }, comment: { type: 'string' } },
    required: ['id', 'file', 'comment'],
  },
}
const TRIAGE = {
  type: 'object',
  properties: {
    kept: { type: 'array', items: { type: 'string' } },
    dropped: { type: 'array', items: { type: 'string' } },
    decisions: { type: 'string', description: 'How each flagged finding was decided, one line each; empty if none' },
    open: OPEN,
  },
  required: ['kept', 'dropped', 'decisions', 'open'],
}
const GATE = {
  type: 'object',
  properties: {
    ready: { type: 'boolean' }, merged: { type: 'boolean' }, score: { type: ['number', 'null'] },
    hard_fail: { type: 'array', items: { type: 'string' } }, commit: { type: 'string' },
  },
  required: ['ready', 'merged', 'score', 'hard_fail'],
}

const fmtOpen = open => open.map(c => `- ${c.id} (${c.file}${c.line ? ':' + c.line : ''}): ${c.comment}`).join('\n')
let pr = args.pr
let open = args.open || []
let startRound = 1
let impl = null

if (!pr) {
  phase('Implement')
  impl = await agent(
    `Implement issue #${N} (mode A). Repo ${REPO}. First check that every issue on the "Depends on:" line is closed; ` +
    `if one is open, stop and report tests "failed" with the open dependency in deviations. Otherwise mark the issue: ` +
    `\`gh issue edit ${N} --add-label ongoing\`. ${CARRY} Write your report JSON to ${RUN}/implement.json.`,
    { label: `implement #${N}`, phase: 'Implement', agentType: 'issue-implementer', schema: IMPL })
  if (!impl || !impl.pr || impl.tests !== 'passed') return { issue: N, status: 'escalate', reason: 'implementation failed', impl }
  pr = impl.pr
} else if (open.length) {
  startRound = (args.fixRound || 1) + 1
  phase('Fix')
  const fix = await agent(
    `Fix review comments for #${N} (mode B), PR #${pr}, review round ${args.fixRound || 1}. Repo ${REPO}. ${CARRY} ` +
    `Write your report JSON to ${RUN}/fix-${args.fixRound || 1}.json. Comments:\n${fmtOpen(open)}`,
    { label: `fix #${N} r${args.fixRound || 1}`, phase: 'Fix', agentType: 'issue-implementer', schema: IMPL })
  if (!fix) return { issue: N, pr, status: 'escalate', reason: 'fix agent failed', open }
  open = open.map(c => ({ ...c, fix: (fix.comments.find(x => x.id === c.id) || {}).note || '' }))
}

const rounds = []
for (let k = startRound; k <= MAX; k++) {
  phase('Review')
  const prev = open.length ? `\nPreviously retained comments to re-check (implementer's note after "fix:"):\n` +
    open.map(c => `- ${c.id} (${c.file}${c.line ? ':' + c.line : ''}): ${c.comment}${c.fix ? ' | fix: ' + c.fix : ''}`).join('\n') : ''
  const review = await agent(
    `Review issue #${N}, PR #${pr}, round ${k}. Repo ${REPO}. Number new findings R${k}-<i>. ` +
    `Write your report JSON to ${RUN}/review-${k}.json.${prev}`,
    { label: `review #${N} r${k}`, phase: 'Review', agentType: 'issue-reviewer', schema: REVIEW })
  if (!review) return { issue: N, pr, status: 'escalate', reason: `review round ${k} failed`, rounds }

  const triage = await agent(
    `You are the orchestrator's triage step for issue #${N}, PR #${pr}, review round ${k}, in ${REPO} (run commands from there).\n` +
    `1. Make sure ${RUN}/review-${k}.json holds exactly this reviewer report (write it if missing or different):\n` +
    `${JSON.stringify(review)}\n` +
    `2. Run: python3 .claude/skills/review-issue/triage.py ${RUN} ${k} "#${N} $(gh issue view ${N} --json title -q .title)"\n` +
    `   (no --post yet). It prints kept, dropped, flagged and unresolved ids.\n` +
    `3. Decide every flagged finding yourself: keep it if it is actionable and affects quality, architecture or reliability, ` +
    `otherwise drop it. Apply a changed decision to retained-${k}.json and status.json (dropped ids get no status entry), ` +
    `and append an "Orchestrator decisions" note to comment-${k}.md. If a retained comment is partly out of the issue's ` +
    `scope, append an "Orchestrator scoping" note there and narrow the comment text you return.\n` +
    `4. Post it: gh pr comment ${pr} --body-file ${RUN}/comment-${k}.md\n` +
    `5. Return the open comments: every id whose status.json value is not "resolved", with file, line and the comment text ` +
    `(from retained-*.json, narrowed if you scoped it).`,
    { label: `triage #${N} r${k}`, phase: 'Review', schema: TRIAGE })
  if (!triage) return { issue: N, pr, status: 'escalate', reason: `triage round ${k} failed`, rounds }
  open = triage.open
  if (review.tests !== 'passed') {
    open = open.concat([{ id: `R${k}-tests`, file: 'tests/', comment: `Tests fail on the branch: ${review.test_summary}. Make \`make test\` pass.` }])
  }
  rounds.push({ round: k, tests: review.tests, findings: review.findings.length, kept: triage.kept, dropped: triage.dropped, decisions: triage.decisions })
  log(`#${N} round ${k}: tests ${review.tests}, kept ${triage.kept.length}, dropped ${triage.dropped.length}, open ${open.length}`)

  if (!open.length) {
    phase('Merge')
    const gate = await agent(
      `In ${REPO}, run: python3 .claude/skills/orchestrate-plan/merge_gate.py ${RUN} ${N} ${k}\n` +
      `It prints one JSON line. Return its ready, merged, score, hard_fail and commit fields exactly as printed. Do nothing else.`,
      { label: `merge gate #${N} r${k}`, phase: 'Merge', schema: GATE, effort: 'low' })
    if (!gate) return { issue: N, pr, status: 'escalate', reason: 'merge gate agent failed', rounds }
    rounds[rounds.length - 1].gate = gate
    if (gate.merged) {
      phase('Learn')
      const learn = await agent(
        `Issue #${N} (PR #${pr}) of ${REPO} was just merged. Read ${RUN}/review-*.json, ${RUN}/retained-*.json, ` +
        `${RUN}/implement.json, the PR description (\`gh pr view ${pr}\`) and ${REPO}/.claude/runs/carryover.md. ` +
        `Append to carryover.md only new, concrete notes that later stories need (verified Hermes API facts, decided ` +
        `policies, shared objects to reuse), one line each, prefixed "(#${N})"; skip anything already there. If a ` +
        `deviation changes what a later open story issue says, append a short "Notes from #${N}" section to that ` +
        `issue body with \`gh issue edit\`. Return one line: how many notes you added and which issues you edited.`,
        { label: `learn #${N}`, phase: 'Learn', effort: 'low' })
      return { issue: N, pr, status: 'merged', score: gate.score, commit: gate.commit, rounds, deviations: impl ? impl.deviations : '', learn }
    }
    if (gate.hard_fail.some(f => f.startsWith('mergeable') || f.startsWith('jev error') || f.startsWith('merge failed'))) {
      return { issue: N, pr, status: 'escalate', reason: gate.hard_fail.join('; '), rounds }
    }
    log(`#${N} round ${k}: not ready (Jev ${gate.score}); reviewing again`)
    continue
  }

  if (k === MAX) break
  phase('Fix')
  const fix = await agent(
    `Fix review comments for #${N} (mode B), PR #${pr}, review round ${k}. Repo ${REPO}. ${CARRY} ` +
    `Write your report JSON to ${RUN}/fix-${k}.json. Comments:\n${fmtOpen(open)}`,
    { label: `fix #${N} r${k}`, phase: 'Fix', agentType: 'issue-implementer', schema: IMPL })
  if (!fix) return { issue: N, pr, status: 'escalate', reason: `fix round ${k} failed`, open, rounds }
  open = open.map(c => ({ ...c, fix: (fix.comments.find(x => x.id === c.id) || {}).note || '' }))
}

return { issue: N, pr, status: 'escalate', reason: `not merged after ${MAX} review rounds`, open, rounds }
