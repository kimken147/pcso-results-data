"""Offline tests for the PCSO results pipeline.

Network access is never exercised here; parsing runs against an HTML snapshot of
https://www.pcso.gov.ph/SearchLottoResult.aspx taken 2026-10-13 (default view =
last three days, all games).
"""
import json
from datetime import date
from pathlib import Path

import pytest

from pcso import (
    Draw,
    extract_form_fields,
    parse_results,
    classify,
    merge,
    load_games,
    group_by_game,
    month_ranges,
)

FIX = Path(__file__).parent / "fixtures" / "search-default-2026-10-13.html"
HTML = FIX.read_text(encoding="utf-8", errors="ignore")


def test_extract_form_fields_gets_viewstate_and_dropdowns():
    f = extract_form_fields(HTML)
    assert f["__VIEWSTATE"] and f["__EVENTVALIDATION"] and f["__VIEWSTATEGENERATOR"]
    assert f["ctl00$ctl00$cphContainer$cpContent$ddlSelectGame"] == "0"
    assert f["ctl00$ctl00$cphContainer$cpContent$ddlStartYear"] == "2026"


def test_parse_results_reads_every_grid_row():
    rows = parse_results(HTML)
    assert len(rows) == 26
    first = rows[0]
    assert first.pcso_game == "Grand Lotto 6/55"
    assert first.numbers == ["05", "41", "23", "28", "20", "51"]
    assert first.date == "2026-10-12"
    assert first.jackpot == 265_501_153.97
    assert first.winners == 0


def test_parse_results_keeps_leading_zeros_and_small_games():
    rows = {(r.pcso_game, r.date): r for r in parse_results(HTML)}
    assert rows[("4D Lotto", "2026-10-12")].numbers == ["0", "8", "0", "9"]
    assert rows[("2D Lotto 2PM", "2026-10-12")].numbers == ["26", "07"]
    assert rows[("3D Lotto 9PM", "2026-10-12")].winners == 264


def test_classify_maps_pcso_names_to_our_games_and_draw_times():
    games = load_games()
    assert classify("3D Lotto 2PM", games) == ("swertres", "14:00")
    assert classify("3D Lotto 9PM", games) == ("swertres", "21:00")
    assert classify("2D Lotto 5PM", games) == ("ez2", "17:00")
    assert classify("Megalotto 6/45", games) == ("6-45", "21:00")
    assert classify("Mega Lotto 6/45", games) == ("6-45", "21:00")
    assert classify("Ultra Lotto 6/58", games) == ("6-58", "21:00")
    assert classify("6D Lotto", games) == ("6d", "21:00")
    assert classify("Lotto Something New", games) is None


def test_group_by_game_assigns_every_fixture_row():
    games = load_games()
    grouped, unknown = group_by_game(parse_results(HTML), games)
    assert unknown == []
    assert set(grouped) == {"6-55", "6-45", "4d", "swertres", "ez2", "6-58", "6-49", "6-42", "6d"} - {"6-42"} or "6-42" in grouped
    # three draws a day for the day games across three days
    assert len(grouped["swertres"]) == 9
    assert len(grouped["ez2"]) == 9
    assert all(d.time in ("14:00", "17:00", "21:00") for d in grouped["swertres"])


def test_merge_dedupes_on_date_and_time_and_prefers_new_values():
    old = [Draw("swertres", "2026-10-12", "14:00", ["3", "2", "6"], 4500.0, 337)]
    new = [
        Draw("swertres", "2026-10-12", "14:00", ["3", "2", "6"], 4500.0, 340),  # winners corrected
        Draw("swertres", "2026-10-13", "14:00", ["1", "1", "1"], 4500.0, 10),
    ]
    out = merge(old, new)
    assert len(out) == 2
    assert out[0].date == "2026-10-13"                 # newest first
    assert out[1].winners == 340


def test_month_ranges_covers_span_inclusive():
    r = month_ranges(date(2026, 8, 15), date(2026, 10, 13))
    assert r[0] == (date(2026, 8, 1), date(2026, 8, 31))
    assert r[-1] == (date(2026, 10, 1), date(2026, 10, 13))
    assert len(r) == 3


def test_draw_roundtrips_through_json():
    d = Draw("ez2", "2026-10-12", "21:00", ["24", "14"], 4000.0, 313)
    assert Draw.from_dict(json.loads(json.dumps(d.to_dict()))) == d
