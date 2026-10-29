#!/usr/bin/env python3
"""Exit 0 when the day's data is complete, 1 otherwise — the loop guard for the workflows.
  python pipeline/wait_for.py official   # every game drawn today (Manila) has its 21:00 row in data/results
  python pipeline/wait_for.py 2pm|5pm    # data/pending.json or data/results holds today's 2PM/5PM EZ2 + Swertres
"""
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
PHT = timezone(timedelta(hours=8))
SLOT = {"2pm": "14:00", "5pm": "17:00"}

def main() -> int:
    what = sys.argv[1]
    now = datetime.now(PHT); today = now.strftime("%Y-%m-%d"); wd = now.strftime("%a")
    games = json.loads((ROOT / "pipeline" / "games.json").read_text())
    def has(slug: str, time: str) -> bool:
        f = ROOT / "data" / "results" / f"{slug}.json"
        return f.exists() and any(d["date"] == today and d["time"] == time for d in json.loads(f.read_text())["draws"])
    if what == "official":
        due = [g for g in games if not g.get("days") or wd in g["days"]]
        missing = [g["slug"] for g in due if not has(g["slug"], "21:00")]
        print("missing today 9PM:", missing or "none"); return 0 if not missing else 1
    t = SLOT[what]
    pend = json.loads((ROOT / "data" / "pending.json").read_text())["draws"]
    ok = all(has(s, t) or any(p["game"] == s and p["date"] == today and p["time"] == t for p in pend) for s in ("ez2", "swertres"))
    print(f"{what} for {today}:", "present" if ok else "not yet"); return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
