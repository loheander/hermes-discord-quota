---
name: implement-issue
description: Implement one GitHub issue (user story) of hermes-discord-quota with the issue-implementer agent. Marks the issue as ongoing, runs the agent in implement mode, and records the branch and PR. Use when asked to implement an issue, or as step 1 of /orchestrate-plan.
argument-hint: <issue-number>
---

# /implement-issue <N>

Run from `/home/lo/repo/hermes-discord-quota`. State for the run lives in `.claude/runs/issue-<N>/`
(gitignored).

1. **Check order.** `gh issue view <N> --json title,body,state,labels`. Read the `Depends on:` line in the body;
   every listed issue must be closed. If not, stop and report which dependency is open.
2. **Mark ongoing.** `gh issue edit <N> --add-label ongoing` (create the label once with
   `gh label create ongoing --color FBCA04 --description "Being implemented by an agent"` if missing).
   Make sure the main tree is clean and on `main`: `git status --porcelain` empty, `git checkout main && git pull --ff-only`.
3. **Spawn the implementer.** Agent tool, `subagent_type: "issue-implementer"` (if that type is not
   available in this session, use `general-purpose` with `model: "sonnet"` and prepend
   "Read and follow .claude/agents/issue-implementer.md as your role."). Prompt:
   `Implement issue #<N> (mode A). Repo /home/lo/repo/hermes-discord-quota.` plus the issue title.
   Keep the returned agent id: the same agent fixes review comments later via SendMessage, so it keeps context.
4. **Record.** Parse the agent's final JSON block. Save it to `.claude/runs/issue-<N>/implement.json` and the
   agent id to `.claude/runs/issue-<N>/agent_id`. Verify the PR exists (`gh pr view <pr> --json url,headRefName`).
   If tests failed or there is no PR, send the agent one follow-up asking it to finish; if it still fails, stop and report.
5. **Report** branch, PR URL and test summary.
