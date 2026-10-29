import json
from pathlib import Path

from recap import parse_feed, pick, reconcile, validate_numbers, vote

FEED = """<feed>
<entry><yt:videoId>AAA</yt:videoId><title>5pm Lotto Result Today October 22 2026 Swertres 3D Lotto Ez2 2D Lotto PCSO</title><published>2026-10-22T09:11:20+00:00</published></entry>
<entry><yt:videoId>BBB</yt:videoId><title>2pm Lotto Result Today October 22 2026 Swertres 3D Lotto Ez2 2D Lotto PCSO</title><published>2026-10-22T06:11:42+00:00</published></entry>
<entry><yt:videoId>CCC</yt:videoId><title>9pm Lotto Result Today October 21 2026 PCSO</title><published>2026-10-21T13:19:17+00:00</published></entry>
<entry><yt:videoId>DDD</yt:videoId><title>Some unrelated video</title><published>2026-10-21T13:19:17+00:00</published></entry>
</feed>"""


def test_parse_feed_reads_slot_and_date():
    vids = parse_feed(FEED)
    assert [(v.id, v.slot, v.date) for v in vids] == [("AAA", "5pm", "2026-10-22"), ("BBB", "2pm", "2026-10-22"), ("CCC", "9pm", "2026-10-21")]
    assert pick(vids, "2026-10-22", "2pm").id == "BBB"
    assert pick(vids, "2026-10-22", "9pm") is None


def test_validate_numbers():
    assert validate_numbers(["28", "30"], 2, (1, 31)) == ["28", "30"]
    assert validate_numbers(["7", "10"], 2, (1, 31)) == ["07", "10"]
    assert validate_numbers(["6", "7", "8"], 3, (0, 9)) == ["6", "7", "8"]
    assert validate_numbers(["6", "7"], 3, (0, 9)) is None          # wrong count
    assert validate_numbers(["32", "1"], 2, (1, 31)) is None        # out of range
    assert validate_numbers(["0", "1"], 2, (1, 31)) is None         # 2D has no 0
    assert validate_numbers(["6", "7", "x"], 3, (0, 9)) is None     # OCR junk


def test_reconcile_drops_official_and_stale_and_reports_mismatch():
    doc = {"updated_at": None, "draws": [
        {"game": "ez2", "date": "2026-10-22", "time": "14:00", "numbers": ["28", "30"]},
        {"game": "swertres", "date": "2026-10-22", "time": "14:00", "numbers": ["6", "7", "9"]},
        {"game": "ez2", "date": "2026-10-22", "time": "17:00", "numbers": ["10", "07"]},
        {"game": "ez2", "date": "2026-10-10", "time": "17:00", "numbers": ["01", "02"]},
    ]}
    official = {("ez2", "2026-10-22", "14:00"): ["28", "30"], ("swertres", "2026-10-22", "14:00"): ["6", "7", "8"]}
    doc, mismatches = reconcile(doc, official, "2026-10-22")
    assert [(d["game"], d["time"]) for d in doc["draws"]] == [("ez2", "17:00")]   # official ones dropped, stale dropped
    assert len(mismatches) == 1 and mismatches[0]["official"] == ["6", "7", "8"]


def test_vote_requires_clear_agreement():
    assert vote(["13", "13", "13", "18"]) == ""            # 3/8 confusion seen on 2026-10-20: 75 % agreement is rejected
    assert vote(["8"] * 9 + ["3"]) == "8"                  # 90 % agreement over enough reads
    assert vote(["5", "5", "5"]) == ""                      # too few reads
    assert vote(["28"] * 12) == "28"


def test_parse_channel_page_reads_video_ids_and_titles():
    import json as _json
    import recap
    data = {"contents": [{"richItemRenderer": {"content": {"videoRenderer": {"videoId": "4gEzTvcMZq8", "title": {"runs": [{"text": "2pm Lotto Result Today October 25 2026 Swertres 3D Lotto Ez2 2D Lotto PCSO"}]}}}}},
                         {"lockupViewModel": {"contentId": "49D69UpVMi8", "metadata": {"lockupMetadataViewModel": {"title": {"content": "9pm Lotto Result Today October 24 2026 PCSO"}}}}},
                         {"videoRenderer": {"videoId": "xxxxxxxxxxx", "title": {"runs": [{"text": "Some other upload"}]}}}]}
    html = "<html><script>var ytInitialData = " + _json.dumps(data) + ";</script></html>"
    vids = recap.parse_channel_page(html)
    assert [(v.id, v.slot, v.date) for v in vids] == [("4gEzTvcMZq8", "2pm", "2026-10-25"), ("49D69UpVMi8", "9pm", "2026-10-24")]
    assert recap.parse_channel_page("<html>no data</html>") == []
