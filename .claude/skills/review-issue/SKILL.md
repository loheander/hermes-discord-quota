---
name: review-issue
description: Review the PR of one hermes-discord-quota issue with the issue-reviewer agent, triage every comment with Jev (keep only actionable comments with impact on quality, architecture or reliability), post the retained ones on the PR, and list what is still unresolved. Use when asked to review an issue or PR, or as the review step of /orchestrate-plan.
argument-hint: <issue-number>
---

# /review-issue <N>

Run from `/home/lo/repo/hermes-discord-quota`. Requires `.claude/runs/issue-<N>/implement.json` (from
/implement-issue) for the PR number; otherwise find it with `gh pr list --search "Closes #<N>"`.

1. **Round number.** `K` = 1 + number of existing `.claude/runs/issue-<N>/review-*.json` files.
2. **Open retained comments.** Collect from `retained-*.json` of earlier rounds every comment not yet marked
   resolved in `status.json` (a map `id -> resolved|unresolved|disputed`).
3. **Spawn the reviewer** (fresh each round so it is not anchored on earlier rounds). Agent tool,
   `subagent_type: "issue-reviewer"` (fallback: `general-purpose`, `model: "sonnet"`, prepend
   "Read and follow .claude/agents/issue-reviewer.md as your role; do not edit files."). Give it issue N,
   the PR number, round K, and the open retained comments verbatim. Save its JSON to `review-<K>.json`.
4. **Update status.** For each entry in `previous`, write its status into `status.json`.
5. **Triage with Jev** (skill `jev-classify`). Write the findings as JSONL items to `triage-<K>-items.jsonl`:
   `{"id": "R2-1", "issue": "<issue title>", "file": "...:<line>", "severity": "...", "comment": "..."}`. Run
   `python3 -I ~/.claude/skills/jev-classify/scripts/jev_decide.py --spec .claude/skills/review-issue/triage-spec.json --items <items> > triage-<K>-out.jsonl`.
   **Retain** a finding when `actionable.value >= 0.6` and `impact.value != "none"`.
   Flag for your own judgement (do not silently drop) any finding with actionable in 0.4–0.6, impact
   confidence < 0.5, or severity `high` that Jev would drop; decide those yourself and note why.
   Treat Jev errors as "retain", never as "drop".
6. **Persist.** Write `retained-<K>.json` (the retained findings with Jev scores) and mark each as
   `unresolved` in `status.json`. Post one PR comment summarising the round:
   `gh pr comment <pr> --body-file <file>` with retained comments (id, file:line, impact, text), dropped
   comments in a collapsed `<details>` with their scores, and earlier comments' status.
7. **Report** the list of open comments to fix (retained this round + earlier unresolved), the test result
   from the review, and the counts kept/dropped.
