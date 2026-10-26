---
name: orchestrate-plan
description: Turn PLAN.md of hermes-discord-quota into dependency-ordered GitHub issues and drive them to merged code one at a time - implement with the issue-implementer agent, review with the issue-reviewer agent, triage comments and gate merges with Jev, fix until ready, merge to main. Use when asked to execute, orchestrate or implement the whole plan.
argument-hint: "[--from <issue-number>] [--max-rounds 3]"
---

# /orchestrate-plan

The main session is the orchestrator. Subagents cannot spawn subagents, so never delegate the loop itself.
Repo: `/home/lo/repo/hermes-discord-quota`, base branch `main`.

## Phase 1: stories (skip if issues labelled `story` already exist)

1. Read `PLAN.md` and split it into small user stories, each independently mergeable, testable without a
   running Hermes, and touching one concern (e.g. config parsing, window math, ledger, one hook, one command).
2. Each issue body uses this template:
   ```
   **Story:** As a <bot owner|Discord user|admin>, I want <capability> so that <outcome>.
   **Plan reference:** PLAN.md §x.y
   **Scope:** <what to build; modules; what is explicitly out of scope>
   **Acceptance criteria:**
   - [ ] <observable, testable criterion>
   **Depends on:** #a, #b   (or "none")
   **Order:** <k>/<total>
   ```
3. Create labels `story` and `ongoing` if missing. Create issues in dependency order (topological; ties by
   plan order) with `gh issue create --label story`, so issue numbers ascend in implementation order. Fill
   `Depends on` with real numbers.

## Phase 2: loop, one issue at a time, lowest open `story` number first

For issue N (skip closed ones; start at `--from` if given, else at an issue labelled `ongoing` if one exists). Run the usage check below before each new issue.
On start or resume, read `.claude/runs/HANDOFF.md` and `.claude/runs/carryover.md` if present: they replace the
conversation history of earlier stories. Pass the carryover notes relevant to issue N into the implementer prompt.

1. **Implement**: follow `/implement-issue N` (marks it `ongoing`, spawns the implementer, keeps its agent id).
2. **Review**: follow `/review-issue N` (reviewer agent + Jev triage; yields the open comment list).
3. **Fix** if open comments exist: SendMessage to the same implementer agent id:
   `Fix review comments for #N (mode B):` followed by each open comment (id, file:line, text). Save its JSON to
   `.claude/runs/issue-N/fix-<K>.json`; disputed comments go to `status.json` as `disputed` and you decide them
   (accept the dispute, or send back once with your reasoning). Then go back to step 2.
   If the review had no open comments, go straight to step 4.
4. **Merge gate.**
   - Hard checks first: latest review `tests == "passed"`, no `unresolved` ids in `status.json`,
     `gh pr view <pr> --json mergeable` is `MERGEABLE`. If any fails, it is not ready.
   - Then Jev: write one item summarising the PR (acceptance criteria, test summary, new retained findings,
     earlier retained ids with status, `gh pr diff <pr> --stat`-style size) and run
     `python3 -I ~/.claude/skills/jev-classify/scripts/jev_decide.py --spec .claude/skills/orchestrate-plan/merge-spec.json --state "<summary>"`.
     Ready when `ready_to_merge.value >= 0.7`. Save the output to `merge-<K>.json`.
   - Not ready: back to step 2 (review + fix again).
5. **Merge.** `gh pr merge <pr> --squash --delete-branch`, then `git checkout main && git pull --ff-only`,
   `gh issue edit N --remove-label ongoing` (the PR's `Closes #N` closes the issue; close it explicitly if not).
   Then do the context reset below instead of starting the next issue in the same context.

## Context reset after each merge

The orchestrator's context grows with every PR, so each story starts in a cleared context. After a successful
merge (and after updating later issue bodies and `carryover.md` with anything learned):

1. Write `.claude/runs/HANDOFF.md` (overwrite): the time, the last merged issue and PR, the next issue number,
   merged/remaining counts, open decisions, and anything the next story needs that is not already in the issue
   body or `carryover.md`. Everything needed to continue must be on disk (GitHub issues, labels, `.claude/runs/`).
2. Run the usage check. If it pauses, stop there (the `PAUSED` flow below).
3. Otherwise do not start the next issue. Tell the user in one line: merged #N, next is #M, run `/clear` and then
   `/orchestrate-plan` to continue. End the turn. The agent cannot clear its own context; the user runs `/clear`.
4. In the fresh context, `/orchestrate-plan` resumes from the lowest open `story` issue, reading `HANDOFF.md`
   and `carryover.md` first.

Mid-story (between implement, review and fix rounds) keep the context: the implementer is resumed by agent id,
which a cleared context still has in `.claude/runs/issue-<N>/agent_id`, but finishing the story first is simpler.

## Usage check between stories

Before starting each new issue (after a merge, and when the skill starts or resumes), check Claude plan usage:

1. Read `.claude/runs/usage.json`. It is written by `.claude/statusline-usage.sh` when that script is the
   status line (`"statusLine": {"type": "command", "command": "<repo>/.claude/statusline-usage.sh"}` in
   `.claude/settings.local.json`; the user enables this, agents must not edit settings). Use
   `rate_limits.five_hour.used_percentage` and `rate_limits.seven_day.used_percentage`. Ignore the file if it
   is older than 10 minutes.
2. If the file is missing or stale, ask the user to run `/usage` and report the numbers; do not guess.
3. If either value is above **95%**, pause: do not start the next issue. Leave merged work merged and an
   in-progress issue as is (label `ongoing`, branch pushed). Write `.claude/runs/PAUSED` with the time, the
   usage numbers and the next issue number, tell the user, and stop. Resume at the next session start by
   running `/orchestrate-plan`. It continues from the lowest open `story` issue, or from an issue labelled
   `ongoing` if one exists, reusing `.claude/runs/issue-<N>/`. Delete `PAUSED` when resuming.

## Limits and escalation

- At most `--max-rounds` (default 3) review rounds per issue. After that, stop and ask the user, showing the
  open comments and the last Jev scores.
- Stop and ask the user if a dependency is missing, the plan conflicts with the Hermes Agent code in a way
  that changes scope, or tests cannot run.
- Keep a short progress line per issue for the user: issue, rounds, kept/dropped comments, merge score.
