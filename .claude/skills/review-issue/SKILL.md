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
   the PR number, round K, and the open retained comments verbatim. It writes `review-<K>.json` itself; check the
   file exists (write it from the final JSON block if not).
4. **Update status.** For each entry in `previous`, write its status into `status.json`.
5. **Triage with Jev** (skill `jev-classify`). If neither the file nor a JSON block exists, ask the reviewer once via
   SendMessage to resend only the block (no re-review). Then run
   `python3 .claude/skills/review-issue/triage.py .claude/runs/issue-<N> <K> "#<N> <issue title>" [--post <pr>]`
   (`--post` when there are no flagged findings to decide; it posts the comment and prints one JSON line). It updates
   `status.json` from `previous`, sends the findings to Jev with `triage-spec.json`, and keeps a finding when
   `actionable >= 0.6` and `impact != "none"`. A Jev error counts as "keep". It writes `retained-<K>.json` (new
   ones marked `unresolved`) and the PR comment `comment-<K>.md`, and prints kept, dropped, `flagged` and open ids.
   Decide every `flagged` finding yourself (actionable 0.4–0.6, impact confidence < 0.5, or a dropped `high`)
   and note why. If a retained comment is partly out of the issue's scope, append an "Orchestrator scoping"
   note to `comment-<K>.md` and narrow the fix request.
6. **Post** (if not done with `--post`): `gh pr comment <pr> --body-file .claude/runs/issue-<N>/comment-<K>.md`.
7. **Report** the list of open comments to fix (retained this round + earlier unresolved), the test result
   from the review, and the counts kept/dropped.
