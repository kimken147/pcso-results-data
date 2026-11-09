"""Export data/results/*.json as flat CSV + Parquet tables for the public dataset mirrors
(Hugging Face `lottotayo/pcso-lotto-results`, Kaggle). Also writes the dataset card (README.md)
and Kaggle's dataset-metadata.json into the export folder, so one `hf upload` / `kaggle datasets
version` of that folder publishes everything.

    python export_datasets.py --out data/export [--kaggle-user <username>]

Only official draws are exported (data/pending.json is skipped): the mirrors are meant for analysis,
not for live results. Column names are stable; add columns, never rename.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAMES = json.loads((ROOT / "pipeline" / "games.json").read_text(encoding="utf-8"))
SITE = "https://lottotayo.ph"
MAX_NUMBERS = 6

COLUMNS = [
    "game", "game_name", "draw_date", "draw_time", "numbers",
    *[f"n{i}" for i in range(1, MAX_NUMBERS + 1)],
    "jackpot_php", "winners", "source",
]


def rows() -> list[dict]:
    out: list[dict] = []
    names = {g["slug"]: g["name"] for g in GAMES}
    for path in sorted((ROOT / "data" / "results").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        game = doc["game"]
        for d in doc["draws"]:
            if d.get("pending"):
                continue
            nums = [str(n) for n in d["numbers"]]
            row = {
                "game": game,
                "game_name": names.get(game, game),
                "draw_date": d["date"],
                "draw_time": d["time"],
                "numbers": " ".join(nums),
                "jackpot_php": d.get("jackpot"),
                "winners": d.get("winners"),
                "source": doc.get("source", ""),
            }
            for i in range(MAX_NUMBERS):
                row[f"n{i + 1}"] = nums[i] if i < len(nums) else None
            out.append(row)
    out.sort(key=lambda r: (r["draw_date"], r["draw_time"], r["game"]), reverse=True)
    return out


def write_csv(path: Path, data: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in data:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})


def write_parquet(path: Path, data: list[dict]) -> bool:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False
    cols = {
        "game": pa.array([r["game"] for r in data], pa.string()),
        "game_name": pa.array([r["game_name"] for r in data], pa.string()),
        "draw_date": pa.array([datetime.strptime(r["draw_date"], "%Y-%m-%d").date() for r in data], pa.date32()),
        "draw_time": pa.array([r["draw_time"] for r in data], pa.string()),
        "numbers": pa.array([r["numbers"] for r in data], pa.string()),
    }
    for i in range(1, MAX_NUMBERS + 1):
        cols[f"n{i}"] = pa.array([r.get(f"n{i}") for r in data], pa.string())
    cols["jackpot_php"] = pa.array([r["jackpot_php"] for r in data], pa.float64())
    cols["winners"] = pa.array([r["winners"] for r in data], pa.int64())
    cols["source"] = pa.array([r["source"] for r in data], pa.string())
    pq.write_table(pa.table(cols), path, compression="zstd")
    return True


def card(data: list[dict], generated: str) -> str:
    first = min(r["draw_date"] for r in data)
    last = max(r["draw_date"] for r in data)
    per_game = {}
    for r in data:
        per_game[r["game_name"]] = per_game.get(r["game_name"], 0) + 1
    table = "\n".join(f"| {g} | {n:,} |" for g, n in sorted(per_game.items(), key=lambda x: -x[1]))
    return f"""---
license: cc-by-4.0
language:
- en
pretty_name: PCSO Lotto Results (Philippines)
tags:
- lottery
- philippines
- pcso
- time-series
- tabular
size_categories:
- 1K<n<10K
task_categories:
- tabular-classification
- time-series-forecasting
configs:
- config_name: default
  data_files: draws.parquet
---

# PCSO Lotto Results (Philippines)

Every official draw of the Philippine Charity Sweepstakes Office (PCSO) lotto games since {first}:
Ultra Lotto 6/58, Grand Lotto 6/55, Super Lotto 6/49, Mega Lotto 6/45, Lotto 6/42, 6D, 4D, Swertres (3D) and EZ2 (2D),
including the 2PM / 5PM / 9PM digit-game draws, the jackpot amount and the number of jackpot winners.

**Updated daily** by the pipeline in [kimken147/pcso-results-data](https://github.com/kimken147/pcso-results-data),
minutes after PCSO publishes the day's batch (about 21:45 Manila time). Last refresh: {generated} UTC. Rows: {len(data):,} ({first} → {last}).

Maintained by [Lotto Tayo](https://lottotayo.ph), which also offers a [free embeddable results widget and live JSON]({SITE}/widget)
for websites that publish PCSO results.

## Files

| File | Description |
|---|---|
| `draws.parquet` | All draws, one row per draw, typed columns (recommended) |
| `draws.csv` | Same rows as CSV |
| `games.csv` | Game metadata: slug, name, draw days, draw times, numbers picked, number range, minimum jackpot |

## Columns

| Column | Type | Meaning |
|---|---|---|
| `game` | string | Game slug: `6-58`, `6-55`, `6-49`, `6-45`, `6-42`, `6d`, `4d`, `swertres`, `ez2` |
| `game_name` | string | Official game name |
| `draw_date` | date | Draw date (Asia/Manila) |
| `draw_time` | string | `21:00` for lotto games; `14:00`, `17:00`, `21:00` for Swertres and EZ2 |
| `numbers` | string | Winning numbers in the order PCSO published them, space-separated, zero-padded |
| `n1` … `n6` | string | The same numbers split into columns; empty for games with fewer numbers |
| `jackpot_php` | float | Jackpot prize in Philippine pesos; for the digit games (Swertres, EZ2, 4D, 6D) the fixed first prize |
| `winners` | int | Number of jackpot / first-prize winners (null when PCSO did not publish it) |
| `source` | string | PCSO page the row was taken from |

For 6/58, 6/55, 6/49, 6/45 and 6/42 the order of the numbers does not matter for prizes; for Swertres, EZ2, 4D and 6D it does
(standard play), with "rambolito" variants paying on any order.

## Rows per game

| Game | Draws |
|---|---|
{table}

## Source and licence

Results are reproduced from PCSO's public results search (pcso.gov.ph). PCSO's official record prevails in case of discrepancy.
Dataset compiled and maintained by Lotto Tayo, released under **CC BY 4.0**: free to use with attribution to
"Lotto Tayo (lottotayo.ph)". Lotto does not sell tickets or predictions; past draws do not influence future draws.

## Loading

```python
import pandas as pd
df = pd.read_parquet("hf://datasets/lottotayo/pcso-lotto-results/draws.parquet")
```
"""


def kaggle_metadata(user: str, generated: str) -> dict:
    return {
        "title": "PCSO Lotto Results (Philippines), updated daily",
        "id": f"{user}/pcso-lotto-results",
        "subtitle": "Every official PCSO draw since 2024: 9 games, jackpots, winners; updated daily",
        "description": (
            "Every official draw of the Philippine Charity Sweepstakes Office (PCSO) lotto games since January 2024, "
            "including the 2PM/5PM/9PM digit-game draws, jackpot amounts and jackpot winner counts. Updated daily by an "
            "automated pipeline (https://github.com/kimken147/pcso-results-data) minutes after PCSO publishes the day's results. "
            f"Maintained by Lotto Tayo ({SITE}), which also offers a free embeddable results widget and live JSON at {SITE}/widget. "
            f"Last refresh: {generated} UTC. Source: pcso.gov.ph; PCSO's official record prevails. Licence CC BY 4.0."
        ),
        "licenses": [{"name": "CC-BY-4.0"}],
        "keywords": ["lottery", "philippines", "pcso", "time series", "tabular"],
        "resources": [
            {"path": "draws.csv", "description": "All draws, one row per draw"},
            {"path": "draws.parquet", "description": "Same rows, typed columns"},
            {"path": "games.csv", "description": "Game metadata"},
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/export")
    ap.add_argument("--kaggle-user", default=None)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = rows()
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    write_csv(out / "draws.csv", data)
    has_parquet = write_parquet(out / "draws.parquet", data)
    with (out / "games.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["game", "game_name", "draw_days", "draw_times", "picks", "range", "min_jackpot_php"])
        for g in GAMES:
            w.writerow([g["slug"], g["name"], " ".join(g.get("days") or []), " ".join(g["draws"]), g["picks"], g["range"], g.get("min_jackpot") or ""])
    (out / "README.md").write_text(card(data, generated), encoding="utf-8")
    if args.kaggle_user:
        (out / "dataset-metadata.json").write_text(json.dumps(kaggle_metadata(args.kaggle_user, generated), indent=2), encoding="utf-8")
    print(f"exported {len(data)} draws → {out} (parquet={'yes' if has_parquet else 'no, pyarrow missing'})")


if __name__ == "__main__":
    main()
