"""
Housekeeping for data/pending.json, the early (unofficial) 2PM/5PM digit-game draws.

    python pending.py reconcile    # drop pending draws that now have official rows (or are older than 2 days)

Pending rows are written by an external job; this script only removes them once the official PCSO rows
exist in data/results/, printing MISMATCH when the numbers differ. Run after every official refresh.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PENDING_FILE = HERE.parent / "data" / "pending.json"
RESULTS_DIR = HERE.parent / "data" / "results"
PHT = timezone(timedelta(hours=8))


def load_pending(path: Path = PENDING_FILE) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"updated_at": None, "draws": []}


def save_pending(doc: dict, path: Path = PENDING_FILE) -> None:
    doc["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc["draws"].sort(key=lambda d: (d["date"], d["time"], d["game"]))
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")


def official_lookup(results_dir: Path = RESULTS_DIR) -> dict[tuple[str, str, str], list[str]]:
    out = {}
    for f in results_dir.glob("*.json"):
        doc = json.loads(f.read_text())
        for d in doc.get("draws", []):
            out[(doc["game"], d["date"], d["time"])] = d["numbers"]
    return out


def reconcile(doc: dict, official: dict, today: str) -> tuple[dict, list[dict]]:
    """Drop pending draws that now have official data (or are older than 2 days); report mismatches."""
    keep, mismatches = [], []
    for d in doc["draws"]:
        key = (d["game"], d["date"], d["time"])
        if key in official:
            if official[key] != d["numbers"]:
                mismatches.append({**d, "official": official[key]})
            continue
        if d["date"] < (datetime.fromisoformat(today) - timedelta(days=2)).strftime("%Y-%m-%d"):
            continue
        keep.append(d)
    doc["draws"] = keep
    return doc, mismatches


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["reconcile"]:
        print(__doc__, file=sys.stderr)
        return 2
    doc = load_pending()
    before = len(doc["draws"])
    doc, mismatches = reconcile(doc, official_lookup(), datetime.now(PHT).strftime("%Y-%m-%d"))
    for m in mismatches:
        print(f"MISMATCH {m['game']} {m['date']} {m['time']}: pending {m['numbers']} vs official {m['official']}", file=sys.stderr)
    if len(doc["draws"]) != before:
        save_pending(doc)
        print(f"pending: {before} -> {len(doc['draws'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
