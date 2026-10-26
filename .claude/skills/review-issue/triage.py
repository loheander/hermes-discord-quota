"""triage.py <run_dir> <round> <issue title> [--post <pr>]: Jev-triage review-<K>.json, write retained-<K>.json,
comment-<K>.md, update status.json; with --post, also post the comment on the PR. Prints one compact JSON line."""
import json, subprocess, sys, os
R, K, title = sys.argv[1], sys.argv[2], sys.argv[3]
post = sys.argv[sys.argv.index("--post") + 1] if "--post" in sys.argv else None
r = json.load(open(f"{R}/review-{K}.json"))
st = json.load(open(f"{R}/status.json")) if os.path.exists(f"{R}/status.json") else {}
for p in r.get("previous", []): st[p["id"]] = p["status"]
items = f"{R}/triage-{K}-items.jsonl"
with open(items, "w") as f:
    for x in r["findings"]:
        f.write(json.dumps({"id": x["id"], "issue": title, "file": f'{x["file"]}:{x.get("line","")}', "severity": x["severity"], "comment": x["comment"]}) + "\n")
jev = {}
if r["findings"]:
    out = subprocess.run(["python3", "-I", os.path.expanduser("~/.claude/skills/jev-classify/scripts/jev_decide.py"), "--spec", ".claude/skills/review-issue/triage-spec.json", "--items", items], capture_output=True, text=True)
    open(f"{R}/triage-{K}-out.jsonl", "w").write(out.stdout)
    for l in out.stdout.splitlines():
        d = json.loads(l); jev[d["id"]] = d
kept, dropped, flagged = [], [], []
for x in r["findings"]:
    d = jev.get(x["id"])
    if not d or "error" in d or "answers" not in d:
        x["jev"] = {"error": True}; kept.append(x); flagged.append((x["id"], "jev error -> retained")); continue
    a = d["answers"]; act = a["actionable"]["value"]; imp = a["impact"]["value"]; conf = a["impact"]["confidence"]
    x["jev"] = {"actionable": act, "impact": imp, "impact_conf": conf}
    keep = act >= 0.6 and imp != "none"
    if 0.4 <= act < 0.6 or conf < 0.5 or (x["severity"] == "high" and not keep):
        flagged.append((x["id"], f"borderline: actionable {act:.2f}, impact {imp} ({conf:.2f})"))
    (kept if keep else dropped).append(x)
json.dump(kept, open(f"{R}/retained-{K}.json", "w"), indent=1)
for x in kept: st[x["id"]] = "unresolved"
json.dump(st, open(f"{R}/status.json", "w"), indent=1)
fmt = lambda x: f"- **{x['id']}** `{x['file']}:{x.get('line','')}` [{x['severity']}" + (f", {x['jev']['impact']}, actionable {x['jev']['actionable']:.2f}" if "impact" in x["jev"] else ", jev error") + f"]: {x['comment']}"
b = f"### Review round {K}\n\nReviewer (issue-reviewer, sonnet): tests {r['tests']} ({r['test_summary']}). Comments triaged with Jev (`typesafe/jev-1.14`); retained when actionable ≥ 0.6 and impact ≠ none.\n\n"
b += "**Retained (to fix):**\n\n" + ("\n".join(map(fmt, kept)) or "none") + "\n\n"
b += ("<details><summary>Dropped (" + str(len(dropped)) + ")</summary>\n\n" + "\n".join(map(fmt, dropped)) + "\n\n</details>\n\n") if dropped else "**Dropped:** none.\n\n"
if r.get("previous"): b += "**Earlier retained:** " + ", ".join(f"{p['id']} {p['status']}" for p in r["previous"]) + "\n"
open(f"{R}/comment-{K}.md", "w").write(b)
if post:
    subprocess.run(["gh", "pr", "comment", post, "--body-file", f"{R}/comment-{K}.md"], capture_output=True, check=True)
print(json.dumps({"kept": [x["id"] for x in kept], "dropped": [x["id"] for x in dropped], "flagged": flagged,
                  "unresolved": [k for k, v in st.items() if v != "resolved"], "posted": bool(post)}))
