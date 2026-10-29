from pathlib import Path

import gma
import pcso

FIX = Path(__file__).parent / "fixtures" / "gma-lotto-2026-10-28.html"


def test_parse_gma_listing():
    rows = gma.parse(FIX.read_text(encoding="utf-8"))
    assert len(rows) == 54
    by = {(r.pcso_game, r.date): r for r in rows}
    ez2 = by[("2D Lotto 2PM", "2026-10-28")]
    assert ez2.numbers == ["30", "05"] and ez2.jackpot == 4000.0 and ez2.winners is None
    g655 = by[("Grand Lotto 6/55", "2026-10-28")]
    assert g655.numbers == ["36", "21", "16", "26", "49", "13"] and g655.jackpot == 312102304.83
    assert by[("3D Lotto 9PM", "2026-10-28")].numbers == ["0", "5", "1"]


def test_gma_names_map_onto_games_json():
    rows = gma.parse(FIX.read_text(encoding="utf-8"))
    grouped, unknown = pcso.group_by_game(rows, pcso.load_games())
    assert unknown == []
    assert set(grouped) >= {"ez2", "swertres", "6-55", "6-45", "4d"}


def test_fill_only_merge_never_overwrites_pcso():
    pcso_row = pcso.Draw("ez2", "2026-10-28", "14:00", ["30", "05"], 4000.0, 122)
    gma_row = pcso.Draw("ez2", "2026-10-28", "14:00", ["30", "05"], 4000.0, None)
    new_row = pcso.Draw("ez2", "2026-10-28", "21:00", ["15", "15"], 4000.0, None)
    out = pcso.merge([pcso_row], [gma_row, new_row], fill_only=True)
    assert [(d.time, d.winners) for d in out] == [("21:00", None), ("14:00", 122)]
    # default merge: incoming PCSO row replaces the GMA one (winners get filled in)
    out2 = pcso.merge([new_row], [pcso.Draw("ez2", "2026-10-28", "21:00", ["15", "15"], 4000.0, 98)])
    assert out2[0].winners == 98
