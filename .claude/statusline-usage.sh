#!/usr/bin/env bash
# Status line that records Claude Code's status JSON (including plan `rate_limits`)
# so /orchestrate-plan can check usage between stories. Writes .claude/runs/usage.json.
in=$(cat)
dir="$(dirname "$0")/runs"
mkdir -p "$dir"
printf '%s' "$in" > "$dir/usage.json.tmp" && mv "$dir/usage.json.tmp" "$dir/usage.json"
python3 -c '
import json, sys
r = json.loads(sys.argv[1]).get("rate_limits") or {}
p = lambda k: (r.get(k) or {}).get("used_percentage")
print("5h: %s%%  7d: %s%%" % (p("five_hour"), p("seven_day")))' "$in" 2>/dev/null || echo "usage: n/a"
