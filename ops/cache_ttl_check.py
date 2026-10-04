"""Which prompt-cache TTL the agent pool's Claude runs really use, and how much of each run's prefix
was read from the cache (stdlib only; runs inside the pool container, reads its session transcripts):

    docker exec -i personalos-agent-pool-1 python3 - --hours 6 < ops/cache_ttl_check.py

Per session: the first call's cache read (the prefix found cached: the stable prompt when it hit) and
its cache write, then the totals of 5-minute vs 1-hour cache writes. After pos.cache_policy is deployed,
agents with "5m" write ephemeral_5m tokens, those with "1h" ephemeral_1h (pos.cache_policy, the /me
cache_ttl and the worker's CLAUDE_CODE_PROMPT_CACHE_TTL). Prints no prompt text, only token counts.
"""

import argparse
import glob
import json
import os
import time

p = argparse.ArgumentParser()
p.add_argument("--hours", type=float, default=24)
p.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
a = p.parse_args()

since = time.time() - a.hours * 3600
tot = {"5m": 0, "1h": 0, "read": 0, "sessions": 0, "first_hit": 0}
for f in sorted(glob.glob(os.path.join(a.root, "*", "*.jsonl")), key=os.path.getmtime):
    if os.path.getmtime(f) < since:
        continue
    seen, first = set(), None
    s = {"5m": 0, "1h": 0, "read": 0}
    for line in open(f, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = e.get("message")
        if not isinstance(m, dict) or not m.get("usage") or m.get("id") in seen:
            continue
        seen.add(m.get("id"))
        u = m["usage"]
        cc = u.get("cache_creation") or {}
        s["5m"] += cc.get("ephemeral_5m_input_tokens", 0)
        s["1h"] += cc.get("ephemeral_1h_input_tokens", 0)
        s["read"] += u.get("cache_read_input_tokens", 0)
        if first is None:
            first = (e.get("timestamp", "")[:19], u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))
    if first is None:
        continue
    tot["sessions"] += 1
    for k in ("5m", "1h", "read"):
        tot[k] += s[k]
    print(f"{os.path.basename(os.path.dirname(f))[:24]:24} {first[0]}  first call read {first[1]:>6} write {first[2]:>6}"
          f"  | writes 5m {s['5m']:>7} 1h {s['1h']:>7} reads {s['read']:>8}")
w = tot["5m"] + tot["1h"]
print(f"\n{tot['sessions']} sessions: cache writes 5m {tot['5m']} / 1h {tot['1h']}, reads {tot['read']}, "
      f"read per write {tot['read'] / max(w, 1):.1f}")
