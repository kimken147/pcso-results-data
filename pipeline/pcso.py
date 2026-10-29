"""PCSO lotto results pipeline.

Source: https://www.pcso.gov.ph/SearchLottoResult.aspx — an ASP.NET WebForms page.
A GET returns the last three days for all games plus the hidden form state; a
POST with that state and a date range returns any window (the year dropdown goes
back ~10 years). PCSO sits behind Akamai, which rejects requests that don't look
like a browser, so we send a full browser header set. Headless browsers are
rejected and so is HTTP/1.1; HTTP/2 with browser headers is accepted, so requests go
through `curl --http2` (standard library otherwise).

Output: one JSON file per game in --out (default sites/lotto/src/data/results):

    {"game": "swertres", "updated_at": "...Z", "source": "...", "draws": [
       {"date": "2026-10-12", "time": "14:00", "numbers": ["3","2","6"], "jackpot": 4500.0, "winners": 337}, ...]}

Draws are newest-first and deduped on (date, time); re-running merges.

CLI:
    python pcso.py update   [--days 7]  [--out DIR]        # routine: last N days, all games
    python pcso.py backfill --from 2024-01 [--to 2026-10] [--out DIR]
    python pcso.py parse FILE.html                         # debug: print rows from a saved page
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

URL = "https://www.pcso.gov.ph/SearchLottoResult.aspx"
HERE = Path(__file__).resolve().parent
GAMES_FILE = HERE / "games.json"
DEFAULT_OUT = HERE.parent / "data" / "results"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="129", "Not=A?Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
PREFIX = "ctl00$ctl00$cphContainer$cpContent$"
GRID_ID = "cphContainer_cpContent_GridView1"
TIME_SUFFIX = {"2PM": "14:00", "5PM": "17:00", "9PM": "21:00"}


@dataclass(frozen=True)
class Draw:
    game: str            # our slug
    date: str            # YYYY-MM-DD
    time: str            # HH:MM, Philippine time
    numbers: list[str]   # as printed, leading zeros kept
    jackpot: Optional[float]
    winners: Optional[int]

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Draw":
        return Draw(d["game"], d["date"], d["time"], list(d["numbers"]), d.get("jackpot"), d.get("winners"))

    def __eq__(self, o: object) -> bool:  # list field breaks the default frozen eq/hash
        return isinstance(o, Draw) and self.to_dict() == o.to_dict()

    def __hash__(self) -> int:
        return hash((self.game, self.date, self.time, tuple(self.numbers)))


@dataclass(frozen=True)
class RawRow:
    pcso_game: str
    numbers: list[str]
    date: str
    jackpot: Optional[float]
    winners: Optional[int]


# --- parsing ------------------------------------------------------------------------

class _Grid(HTMLParser):
    """Collect rows of the results GridView as lists of cell text."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_grid = False
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id") == GRID_ID:
            self._in_grid = True
            self._depth = 0
        if not self._in_grid:
            return
        if tag == "table":
            self._depth += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if not self._in_grid:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._depth -= 1
            if self._depth <= 0:
                self._in_grid = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


class _Form(HTMLParser):
    """Hidden inputs + selected dropdown values of the first <form>."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self._select: Optional[str] = None
        self._opt: Optional[tuple[str, bool]] = None
        self._first_opt: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name") and a.get("type") in (None, "hidden", "text", "submit"):
            if a.get("type") != "submit":
                self.fields[a["name"]] = a.get("value", "")
        elif tag == "select" and a.get("name"):
            self._select = a["name"]
        elif tag == "option" and self._select:
            val = a.get("value", "")
            self._opt = (val, "selected" in a)
            self._first_opt.setdefault(self._select, val)
            if "selected" in a:
                self.fields[self._select] = val

    def handle_endtag(self, tag):
        if tag == "select" and self._select:
            self.fields.setdefault(self._select, self._first_opt.get(self._select, ""))
            self._select = None


def extract_form_fields(html: str) -> dict[str, str]:
    p = _Form()
    p.feed(html)
    return p.fields


def _num(s: str) -> Optional[float]:
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_results(html: str) -> list[RawRow]:
    g = _Grid()
    g.feed(html)
    out: list[RawRow] = []
    for cells in g.rows:
        if len(cells) < 5 or cells[0].upper() == "LOTTO GAME":
            continue
        game, combo, d, jackpot, winners = cells[:5]
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", d)
        if not m:
            continue
        iso = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        nums = [n for n in re.split(r"[-\s]+", combo.strip()) if n]
        w = _num(winners)
        out.append(RawRow(game.strip(), nums, iso, _num(jackpot), int(w) if w is not None else None))
    return out


# --- mapping ------------------------------------------------------------------------

def load_games(path: Path = GAMES_FILE) -> list[dict]:
    return json.loads(path.read_text())


def classify(pcso_game: str, games: list[dict]) -> Optional[tuple[str, str]]:
    """PCSO label -> (our slug, draw time). Day games carry the time in the label."""
    name = " ".join(pcso_game.split())
    for g in games:
        for alias in g.get("pcso_names", []):
            if alias.lower() == name.lower():
                m = re.search(r"\b(2PM|5PM|9PM)$", name.upper())
                t = TIME_SUFFIX[m.group(1)] if m else g["draws"][0]
                return g["slug"], t
    return None


def group_by_game(rows: Iterable[RawRow], games: list[dict]) -> tuple[dict[str, list[Draw]], list[str]]:
    grouped: dict[str, list[Draw]] = {}
    unknown: list[str] = []
    for r in rows:
        c = classify(r.pcso_game, games)
        if c is None:
            if r.pcso_game not in unknown:
                unknown.append(r.pcso_game)
            continue
        slug, t = c
        grouped.setdefault(slug, []).append(Draw(slug, r.date, t, r.numbers, r.jackpot, r.winners))
    return grouped, unknown


def merge(existing: Iterable[Draw], incoming: Iterable[Draw], fill_only: bool = False) -> list[Draw]:
    """Newest first, unique on (date, time); incoming wins (PCSO corrects winner counts).
    fill_only=True (fallback sources) adds missing draws but never replaces an existing one."""
    by_key: dict[tuple[str, str], Draw] = {(d.date, d.time): d for d in existing}
    for d in incoming:
        if fill_only and (d.date, d.time) in by_key:
            continue
        by_key[(d.date, d.time)] = d
    return sorted(by_key.values(), key=lambda d: (d.date, d.time), reverse=True)


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        last = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        out.append((cur, min(last, end)))   # whole months from the first; only the tail is clamped
        cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return out


# --- network ------------------------------------------------------------------------

def _request(data: Optional[bytes] = None, retries: int = 3) -> str:
    """GET (data=None) or POST the search page.

    Akamai in front of pcso.gov.ph returns 403 to any HTTP/1.1 client regardless of
    headers and accepts the same request over HTTP/2 (verified 2026-10-13). Python's
    urllib is HTTP/1.1-only, so we shell out to curl, which GitHub runners ship with.
    """
    headers = dict(HEADERS)
    if data is not None:
        headers.update({"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://www.pcso.gov.ph", "Referer": URL, "Sec-Fetch-Site": "same-origin"})
    cmd = ["curl", "--silent", "--show-error", "--http2", "--max-time", "60", "--write-out", "\n%{http_code}", URL]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    last = ""
    for i in range(retries):
        r = subprocess.run(cmd, input=data, capture_output=True)
        body, _, code = r.stdout.rpartition(b"\n")
        if r.returncode == 0 and code.strip() == b"200":
            return body.decode("utf-8", "ignore")
        last = f"curl rc={r.returncode} http={code.decode(errors='ignore').strip()} {r.stderr.decode(errors='ignore').strip()[:200]}"
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"PCSO request failed after {retries} attempts: {last}")


def fetch_range(start: date, end: date, session_html: Optional[str] = None) -> tuple[list[RawRow], str]:
    """POST the search form for [start, end], all games. Returns rows and the new page HTML
    (whose form state can seed the next request)."""
    html = session_html or _request()
    fields = extract_form_fields(html)
    fields.update({
        PREFIX + "ddlStartMonth": calendar.month_name[start.month],
        PREFIX + "ddlStartDate": str(start.day),
        PREFIX + "ddlStartYear": str(start.year),
        PREFIX + "ddlEndMonth": calendar.month_name[end.month],
        PREFIX + "ddlEndDay": str(end.day),
        PREFIX + "ddlEndYear": str(end.year),
        PREFIX + "ddlSelectGame": "0",
        PREFIX + "btnSearch": "Search Lotto",
    })
    fields["__EVENTTARGET"] = ""
    fields["__EVENTARGUMENT"] = ""
    body = urllib.parse.urlencode(fields).encode()
    page = _request(body)
    return parse_results(page), page


# --- storage ------------------------------------------------------------------------

def read_game(out: Path, slug: str) -> list[Draw]:
    f = out / f"{slug}.json"
    if not f.exists():
        return []
    return [Draw.from_dict(d) for d in json.loads(f.read_text())["draws"]]


def write_game(out: Path, slug: str, draws: list[Draw]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    doc = {
        "game": slug,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": URL,
        "draws": [d.to_dict() for d in draws],
    }
    (out / f"{slug}.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")


def apply(out: Path, rows: list[RawRow], games: list[dict], fill_only: bool = False) -> dict[str, int]:
    grouped, unknown = group_by_game(rows, games)
    if unknown:
        print(f"WARN unknown PCSO games (add to games.json): {unknown}", file=sys.stderr)
    added: dict[str, int] = {}
    for slug, draws in grouped.items():
        before = read_game(out, slug)
        after = merge(before, draws, fill_only=fill_only)
        if [d.to_dict() for d in after] != [d.to_dict() for d in before]:
            write_game(out, slug, after)
        added[slug] = len(after) - len(before)
    return added


# --- CLI ----------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="PCSO lotto results pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update"); u.add_argument("--days", type=int, default=7); u.add_argument("--out", type=Path, default=DEFAULT_OUT)
    b = sub.add_parser("backfill"); b.add_argument("--from", dest="start", required=True, help="YYYY-MM"); b.add_argument("--to", dest="end", help="YYYY-MM"); b.add_argument("--out", type=Path, default=DEFAULT_OUT); b.add_argument("--sleep", type=float, default=3.0)
    p = sub.add_parser("parse"); p.add_argument("file", type=Path)
    a = ap.parse_args(argv)
    games = load_games()

    if a.cmd == "parse":
        for r in parse_results(a.file.read_text(encoding="utf-8", errors="ignore")):
            print(r)
        return 0

    today = datetime.now(timezone(timedelta(hours=8))).date()   # Philippine time
    if a.cmd == "update":
        try:
            rows, _ = fetch_range(today - timedelta(days=a.days), today)
        except RuntimeError as e:
            # pcso.gov.ph (Akamai) has refused us since 2026-10-28; GMA News republishes the same
            # draws (no winner counts). Fill the gaps from there; PCSO rows win again once it answers.
            print(f"PCSO unavailable ({e}); falling back to GMA News", file=sys.stderr)
            import gma
            rows = gma.parse(gma.fetch())
            added = apply(a.out, rows, games, fill_only=True)
            print(f"update {today} via GMA fallback: rows={len(rows)} new={added}")
            return 0
        added = apply(a.out, rows, games)
        print(f"update {today}: rows={len(rows)} new={added}")
        return 0

    start = datetime.strptime(a.start, "%Y-%m").date()
    end = datetime.strptime(a.end, "%Y-%m").date() if a.end else today
    end = min(date(end.year, end.month, calendar.monthrange(end.year, end.month)[1]), today)
    session: Optional[str] = None
    total = 0
    for s, e in month_ranges(start, end):
        rows, session = fetch_range(s, e, session)
        added = apply(a.out, rows, games)
        total += len(rows)
        print(f"backfill {s}..{e}: rows={len(rows)} new={sum(added.values())}")
        time.sleep(a.sleep)
    print(f"backfill done rows={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
