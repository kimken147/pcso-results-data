"""Fallback source for the day's draws: GMA News Online's lotto page
(https://www.gmanetwork.com/news/lotto/), which republishes PCSO's results for the last ~6 days
with jackpots but without winner counts. Used by `pcso.py update` only when pcso.gov.ph refuses
us (Akamai started answering 403 on 2026-10-28). Rows from here never overwrite a PCSO row.
"""
import re
import urllib.request
from datetime import datetime
from typing import Optional

from pcso import RawRow

URL = "https://www.gmanetwork.com/news/lotto/"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
      "Accept-Language": "en-PH,en;q=0.9"}
# GMA's labels → the names PCSO uses (games.json pcso_names); unlisted names pass through unchanged.
GAME_PREFIX = {"2D": "2D Lotto", "Swertres": "3D Lotto"}
_RESULT = re.compile(r'<div class="lotto-result[^"]*">\s*<a [^>]*?data-date="(?P<date>[^"]+)"[^>]*?data-type="(?P<game>[^"]+)"[^>]*>.*?</a>\s*'
                     r'<p>(?P<nums>[^<]*)</p>\s*<p class="--jackpot">(?P<amount>[^<]*)</p>', re.S)


def fetch(timeout: int = 60) -> str:
    with urllib.request.urlopen(urllib.request.Request(URL, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _amount(s: str) -> Optional[float]:
    """'P 312,102,304.83' → 312102304.83; '' → None."""
    t = re.sub(r"[^0-9.]", "", s)
    return float(t) if t and t != "." else None


def pcso_name(gma_label: str) -> str:
    """'2D 2PM' → '2D Lotto 2PM', 'Swertres 9PM' → '3D Lotto 9PM', 'Mega Lotto 6/45' unchanged."""
    head, _, tail = gma_label.partition(" ")
    if head in GAME_PREFIX and tail:
        return f"{GAME_PREFIX[head]} {tail}"
    return gma_label


def parse(html: str) -> list[RawRow]:
    out: list[RawRow] = []
    for m in _RESULT.finditer(html):
        try:
            iso = datetime.strptime(m["date"].strip(), "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        nums = m["nums"].split()
        if not nums or not all(n.isdigit() for n in nums):
            continue
        out.append(RawRow(pcso_name(m["game"].strip()), nums, iso, _amount(m["amount"]), None))
    return out
