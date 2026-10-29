# PCSO lotto results — data

Machine-readable results of every PCSO lotto draw (Swertres/3D, EZ2/2D, 6/58, 6/55, 6/49, 6/45, 6/42, 6D, 4D)
from January 2024, one JSON file per game in `data/results/`, refreshed automatically after each day's draws.
Source: the Philippine Charity Sweepstakes Office's results search (pcso.gov.ph). `data/pending.json`
holds the day's 2PM/5PM digit draws read from the PCSO livestream recap before the official batch is
published (~21:45 PHT); they are replaced by the official rows the same evening.

```
data/results/<game>.json  → { "game", "updated_at", "source", "draws": [ { "date", "time", "numbers", "jackpot", "winners" }, … ] }  newest first
```

Pipeline: `pipeline/pcso.py` (official), `pipeline/recap.py` (early results), `pipeline/gma.py` (fallback source when pcso.gov.ph refuses the fetch).
Results are reproduced from PCSO; in case of discrepancy PCSO's record prevails.
