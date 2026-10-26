"""merge_gate.py <run_dir> <issue> <round> [--no-merge]: hard checks + Jev ready_to_merge; merges when ready.

Reads implement.json (pr), review-<K>.json (tests), retained-<K>.json and status.json from <run_dir>; polls
GitHub mergeability; writes merge-<K>.json; on ready (>= 0.7) squash-merges, syncs main, drops the `ongoing`
label and closes the issue if still open. Prints one JSON line.
"""
import json, os, subprocess, sys, time
R, N, K = sys.argv[1], sys.argv[2], sys.argv[3]
merge = "--no-merge" not in sys.argv
sh = lambda *a: subprocess.run(a, capture_output=True, text=True)
ld = lambda f, d=None: json.load(open(f"{R}/{f}")) if os.path.exists(f"{R}/{f}") else d
pr = str(ld("implement.json")["pr"])
rev, st, kept = ld(f"review-{K}.json"), ld("status.json", {}), ld(f"retained-{K}.json", [])
open_ids = [k for k, v in st.items() if v != "resolved"]
m = "UNKNOWN"
for _ in range(12):
    m = json.loads(sh("gh", "pr", "view", pr, "--json", "mergeable").stdout or "{}").get("mergeable", "UNKNOWN")
    if m != "UNKNOWN": break
    time.sleep(5)
fails = [x for x, bad in (("tests " + rev["tests"], rev["tests"] != "passed"), ("open " + ",".join(open_ids), bool(open_ids)),
                          ("mergeable " + m, m != "MERGEABLE")) if bad]
res = {"issue": int(N), "pr": int(pr), "round": int(K), "hard_fail": fails, "score": None, "ready": False, "merged": False}
if not fails:
    info = json.loads(sh("gh", "pr", "view", pr, "--json", "title,additions,deletions,changedFiles").stdout)
    body = sh("gh", "issue", "view", N, "--json", "body", "-q", ".body").stdout
    ac = [l.strip() for l in body.splitlines() if l.strip().startswith("- [")]
    state = (f"PR #{pr} for issue #{N} ({info['title']}). Tests: {rev['tests']} ({rev['test_summary']}). "
             f"New retained findings in round {K}: " + (", ".join(f"{x['id']} [{x['severity']}]" for x in kept) or "none") + ". "
             "Earlier retained: " + (", ".join(f"{k} {v}" for k, v in st.items()) or "none") + ". "
             f"Acceptance criteria ({len(ac)}): " + " ".join(ac) + " Reviewer found no uncovered criterion. "
             f"Diff: {info['changedFiles']} files, +{info['additions']} -{info['deletions']}. Mergeable: {m}.")
    out = sh("python3", "-I", os.path.expanduser("~/.claude/skills/jev-classify/scripts/jev_decide.py"),
             "--spec", ".claude/skills/orchestrate-plan/merge-spec.json", "--state", state)
    try:
        res["score"] = json.loads(out.stdout.splitlines()[-1])["answers"]["ready_to_merge"]["value"]
    except Exception:
        res["hard_fail"].append("jev error: " + (out.stderr.strip()[-200:] or "no output"))
    res["ready"] = res["score"] is not None and res["score"] >= 0.7
    if res["ready"] and merge:
        r = sh("gh", "pr", "merge", pr, "--squash", "--delete-branch")
        sh("git", "checkout", "-q", "main"); sh("git", "pull", "-q", "--ff-only")
        sh("gh", "issue", "edit", N, "--remove-label", "ongoing")
        if sh("gh", "issue", "view", N, "--json", "state", "-q", ".state").stdout.strip() != "CLOSED":
            sh("gh", "issue", "close", N)
        res["merged"] = sh("gh", "pr", "view", pr, "--json", "state", "-q", ".state").stdout.strip() == "MERGED"
        res["commit"] = sh("git", "log", "--oneline", "-1").stdout.strip()
        if not res["merged"]: res["hard_fail"].append("merge failed: " + r.stderr.strip()[-200:])
json.dump(res, open(f"{R}/merge-{K}.json", "w"), indent=1)
print(json.dumps(res))
