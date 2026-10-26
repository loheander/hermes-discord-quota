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

For issue N (skip closed ones; start at `--from` if given, else at an issue labelled `ongoing` if one exists).
Run the usage check below before each new issue.

### Default: one Workflow run per story (keeps the orchestrator's context flat)

Run the saved workflow `implement-story` (`.claude/workflows/implement-story.js`) with `args: {"issue": N}`. It runs
implement → review → Jev triage (+ PR comment) → fix → Jev merge gate → merge → carry-over notes, up to 3 review
rounds, inside its own agents, and returns one small object (`status` merged|escalate, `pr`, `score`, `rounds`).
The orchestrator keeps only that result: report one progress line (issue, rounds, kept/dropped, merge score), then
do the usage check and start the next issue. On `escalate`, stop and show the user the reason and open comments.
To continue a story that is mid-way (after a pause or crash), pass `{"issue": N, "pr": P, "fixRound": K, "open": [...]}`
with the open comments from `.claude/runs/issue-N/status.json` and `retained-*.json`; it fixes round K, then reviews K+1.
Do not read agent transcripts or PR diffs into the orchestrator's context; the run files hold the details.

### Manual steps (fallback, or when the user asks to drive a story by hand)

1. **Implement**: follow `/implement-issue N` (marks it `ongoing`, spawns the implementer, keeps its agent id).
2. **Review**: follow `/review-issue N` (reviewer agent + Jev triage; yields the open comment list).
3. **Fix** if open comments exist: SendMessage to the same implementer agent id:
   `Fix review comments for #N (mode B), review round K:` followed by each open comment (id, file:line, text).
   It writes `.claude/runs/issue-N/fix-<K>.json`; disputed comments go to `status.json` as `disputed` and you decide
   them (accept the dispute, or send back once with your reasoning). Then go back to step 2.
   If the review had no open comments, go straight to step 4.
4. **Merge gate and merge**: `python3 .claude/skills/orchestrate-plan/merge_gate.py .claude/runs/issue-N N K`. It runs
   the hard checks (latest review `tests == "passed"`, no non-resolved ids in `status.json`, `mergeable` is
   `MERGEABLE`), then Jev `ready_to_merge` (>= 0.7, spec `merge-spec.json`), writes `merge-<K>.json`, and when ready
   squash-merges, syncs `main`, removes `ongoing` and closes the issue. It prints one JSON line. Not ready: back to
   step 2. Merged: append notes later stories need to `.claude/runs/carryover.md`, then proceed to the next issue.

Keep the orchestrator's context small: give agents paths (issue number, `carryover.md`, run dir) instead of pasting
issue bodies or notes, use quiet flags (`-q`) and one-line script output, and never echo agent JSON back.

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
