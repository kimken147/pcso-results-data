"""
Unofficial early results for the 2PM / 5PM day draws (2D + 3D).

PCSO's own website publishes a whole day's draws in one batch at ~21:45 PHT, so for
"2PM result today" the official source is 7–8 hours late. The YouTube channel
"Labas ng Lotto" uploads a recap of every draw about 10 minutes after it ends; the
recap is a fixed slideshow and YouTube's public auto-thumbnails (maxres1.jpg = 25 %
mark, maxres2.jpg = 50 % mark) land exactly on the 2D and 3D result cards. Those
thumbnails live on i.ytimg.com (plain CDN, no sign-in wall), so a GitHub runner can
fetch them; we OCR the balls and publish the numbers flagged `pending` until the
official PCSO data arrives and `reconcile` replaces them.

    python recap.py update [--slot auto|2pm|5pm] [--date YYYY-MM-DD]   # fetch + OCR + write pending.json
    python recap.py reconcile                                           # drop pending draws that are now official
    python recap.py ocr <image.jpg>                                     # debug one frame

OCR backends: Anthropic vision when ANTHROPIC_API_KEY is set (also verifies the card's
own date / time heading), else tesseract with ball segmentation. Either way a result is
only written when it passes validate_numbers(); nothing is ever guessed.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
PENDING_FILE = HERE.parent / "data" / "pending.json"
RESULTS_DIR = HERE.parent / "data" / "results"
CHANNEL_ID = "UCSI8KYdAqCYO4vdqkZ23oPA"  # Labas ng Lotto
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
# The uploads playlist is the same list behind a different endpoint: YouTube answered the channel feed
# with 404 for 36 minutes on 2026-10-23 (seven 2PM runs in a row) while this one kept working.
FEED_URLS = [FEED_URL, f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{CHANNEL_ID[2:]}"]
# GitHub-hosted runners get 404 from both YouTube feeds every day 14:09–14:45 PHT (2026-10-23/24); the
# Cloudflare Worker in infra/cron-worker re-serves the feed from its edge at RECAP_FEED_PROXY (repo secret).
if os.environ.get("RECAP_FEED_PROXY"):
    FEED_URLS.insert(0, os.environ["RECAP_FEED_PROXY"])
PAGE_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}/videos"
PAGE_URLS = [PAGE_URL] + ([os.environ["RECAP_FEED_PROXY"] + "&src=page"] if os.environ.get("RECAP_FEED_PROXY") else [])
SOURCE = "youtube:labas-ng-lotto"
PHT = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
SLOT_TIME = {"2pm": "14:00", "5pm": "17:00", "9pm": "21:00"}
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
# frame index (1 = 25 %, 2 = 50 %) → game slug, picks, allowed range
FRAMES = {1: ("ez2", 2, (1, 31)), 2: ("swertres", 3, (0, 9))}


@dataclass
class Video:
    id: str
    title: str
    published: datetime
    slot: str      # 2pm / 5pm / 9pm
    date: str      # YYYY-MM-DD from the title


def get(url: str, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


# --- feed ---------------------------------------------------------------------------

def fetch_feed(attempts: int = 3, pause: int = 10) -> str:
    """Channel feed first, uploads-playlist feed as fallback, a few rounds apart; raises only if all fail."""
    import time
    last: Optional[Exception] = None
    for i in range(attempts):
        for url in FEED_URLS:
            try:
                return get(url).decode("utf-8", "ignore")
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                last = e
                print(f"feed {url.split('?', 1)[1].split('key=')[0] or url.split('/')[2]}: {e}", file=sys.stderr)
        if i < attempts - 1:
            time.sleep(pause)
    raise SystemExit(f"recap feed unavailable after {attempts} rounds: {last}")


def parse_title(title: str) -> Optional[tuple[str, str]]:
    """'2pm Lotto Result Today October 25 2026 …' → ('2pm', '2026-10-25'); None for other videos."""
    m = re.match(r"\s*(\d{1,2})pm Lotto Result Today ([A-Z][a-z]+) (\d{1,2}),? (\d{4})", title, re.I)
    if not m:
        return None
    slot = f"{int(m.group(1))}pm"
    if slot not in SLOT_TIME or m.group(2).title() not in MONTHS:
        return None
    return slot, f"{m.group(4)}-{MONTHS[m.group(2).title()]:02d}-{int(m.group(3)):02d}"


def parse_feed(xml: str) -> list[Video]:
    out: list[Video] = []
    for e in re.findall(r"(?s)<entry>(.*?)</entry>", xml):
        vid = re.search(r"<yt:videoId>([^<]*)", e)
        title = re.search(r"<title>([^<]*)", e)
        pub = re.search(r"<published>([^<]*)", e)
        if not (vid and title and pub):
            continue
        parsed = parse_title(title.group(1))
        if parsed:
            out.append(Video(vid.group(1), title.group(1), datetime.fromisoformat(pub.group(1).replace("Z", "+00:00")), *parsed))
    return out


def parse_channel_page(html: str) -> list[Video]:
    """The channel's /videos page (ytInitialData JSON). No timestamps there, so `published` is now."""
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return []
    found: list[tuple[str, str]] = []

    def walk(o) -> None:
        if isinstance(o, dict):
            if "videoRenderer" in o:
                v = o["videoRenderer"]
                found.append((v.get("videoId", ""), "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))))
            if "lockupViewModel" in o:
                v = o["lockupViewModel"]
                found.append((v.get("contentId", ""), v.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title", {}).get("content") or ""))
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    try:
        walk(json.loads(m.group(1)))
    except json.JSONDecodeError:
        return []
    out: list[Video] = []
    seen: set[str] = set()
    for vid, title in found:
        parsed = parse_title(title)
        if vid and parsed and vid not in seen:
            seen.add(vid)
            out.append(Video(vid, title, datetime.now(timezone.utc), *parsed))
    return out


def fetch_videos() -> list[Video]:
    """Recap videos from the RSS feeds, else from the channel page (YouTube's feed answered 404 to
    everyone, including Cloudflare, 14:09–14:45 PHT on 2026-10-23/24/25 while the page kept working)."""
    try:
        return parse_feed(fetch_feed(attempts=2, pause=5))
    except SystemExit as e:
        print(f"{e}; trying the channel page", file=sys.stderr)
    for url in PAGE_URLS:
        try:
            videos = parse_channel_page(get(url).decode("utf-8", "ignore"))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            print(f"page {url.split('/')[2]}: {e}", file=sys.stderr)
            continue
        if videos:
            print(f"found {len(videos)} recap videos on the channel page", file=sys.stderr)
            return videos
        print(f"page {url.split('/')[2]}: no recap videos parsed", file=sys.stderr)
    raise SystemExit("no video source available (feeds and channel page all failed)")


def pick(videos: list[Video], date: str, slot: str) -> Optional[Video]:
    for v in videos:
        if v.date == date and v.slot == slot:
            return v
    return None


def thumbnail(video_id: str, n: int) -> bytes:
    for q in ("maxres", "sd", "hq"):
        try:
            return get(f"https://i.ytimg.com/vi/{video_id}/{q}{n}.jpg")
        except Exception:
            continue
    raise RuntimeError(f"no thumbnail {n} for {video_id}")


# --- validation ---------------------------------------------------------------------

def validate_numbers(nums: list[str], picks: int, rng: tuple[int, int]) -> Optional[list[str]]:
    """Return normalised numbers (2-digit for 2D, 1-digit for 3D) or None."""
    if len(nums) != picks:
        return None
    out = []
    for n in nums:
        if not re.fullmatch(r"\d{1,2}", n):
            return None
        v = int(n)
        if not rng[0] <= v <= rng[1]:
            return None
        out.append(f"{v:02d}" if rng[1] > 9 else str(v))
    return out


# --- OCR: tesseract with ball segmentation ------------------------------------------

def segment_balls(img, band=(0.36, 0.74), min_width=40, gap=12):
    """Columns holding the white ball interiors, grouped into [x0, x1) spans."""
    from PIL import Image  # noqa: F401 (import guard)
    g = img.convert("L")
    w, h = g.size
    y0, y1 = int(h * band[0]), int(h * band[1])
    px = g.load()
    cols = []
    for x in range(w):
        white = sum(1 for y in range(y0, y1) if px[x, y] > 235)
        cols.append(white > (y1 - y0) * 0.25)
    spans, start = [], None
    for x, on in enumerate(cols + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            if x - start >= min_width:
                if spans and start - spans[-1][1] < gap:
                    spans[-1] = (spans[-1][0], x)
                else:
                    spans.append((start, x))
            start = None
    return (y0, y1), spans


def _ball_reads(img, span, y0, y1) -> list[str]:
    """Every (threshold × psm) read of one ball, digits only, empties dropped."""
    from PIL import ImageOps
    import pytesseract
    x0, x1 = span
    reads = []
    for thr in (95, 120, 145):
        ball = img.crop((x0, y0, x1, y1)).convert("L").point(lambda v, t=thr: 255 if v > t else 0)
        ball = ImageOps.expand(ball.resize((ball.width * 3, ball.height * 3)), border=40, fill=255)
        for psm in (10, 7, 8, 6):
            txt = re.sub(r"\D", "", pytesseract.image_to_string(ball, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789"))
            if txt:
                reads.append(txt)
    return reads


def vote(reads: list[str], min_reads: int = 4, min_share: float = 0.85) -> str:
    """The value most reads agree on, if it clearly dominates; '' otherwise (→ rejected).
    Tesseract confuses 3/8 and 1/4 on these balls, so a plain majority is not enough."""
    if len(reads) < min_reads:
        return ""
    best = max(set(reads), key=reads.count)
    return best if reads.count(best) / len(reads) >= min_share else ""


def ocr_tesseract(img_bytes: bytes) -> list[str]:
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    (y0, y1), spans = segment_balls(img)
    return [vote(_ball_reads(img, span, y0, y1)) for span in spans]


def ocr_debug(img_bytes: bytes) -> list[dict]:
    """Per-ball raw reads, to see why a frame was rejected."""
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    (y0, y1), spans = segment_balls(img)
    out = []
    for span in spans:
        reads = _ball_reads(img, span, y0, y1)
        out.append({"span": span, "vote": vote(reads), "reads": {v: reads.count(v) for v in sorted(set(reads))}})
    return out


# --- OCR: Anthropic vision ---------------------------------------------------------

def ocr_anthropic(img_bytes: bytes) -> dict:
    """Ask Claude for the card's heading and balls; returns {"game","time","date","numbers"}.
    RECAP_MODEL defaults to claude-opus-5-5 (reads these cards flawlessly; ~US$0.006 per frame,
    a few dollars a year); set RECAP_MODEL=claude-haiku-5-5 to run it for cents."""
    import base64
    import anthropic
    # ANTHROPIC_API_KEY from the environment. A key that is not scoped to a workspace must name
    # one per request (ANTHROPIC_WORKSPACE_ID → anthropic-workspace-id header).
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws} if ws else None)
    msg = client.messages.create(
        model=os.environ.get("RECAP_MODEL", "claude-opus-5-5"),
        max_tokens=300,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(img_bytes).decode()}},
            {"type": "text", "text": (
                "This is a PCSO lotto result card from a YouTube recap. Read it carefully and return ONLY a JSON object:\n"
                '{"game": "2D"|"3D"|"other", "time": "2PM"|"5PM"|"9PM"|null, "date": "YYYY-MM-DD"|null, "numbers": ["..", ".."]}\n'
                "- game/time/date come from the heading text (e.g. '2D LOTTO 5PM RESULT OCTOBER 22, 2026, THURSDAY').\n"
                "- numbers are the digits printed inside the balls, left to right, exactly as printed (2D balls hold 1-31, 3D balls hold a single digit 0-9).\n"
                "- If any ball is blank or unreadable, or the card is not a 2D/3D result card, use game \"other\" and numbers []."
            )},
        ]}],
    )
    if msg.stop_reason == "refusal":
        return {}
    text = "".join(getattr(b, "text", "") for b in msg.content)
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


# --- pending file -------------------------------------------------------------------

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


# --- commands -----------------------------------------------------------------------

def read_frame(img_bytes: bytes, n: int, expect_date: str, expect_slot: str) -> Optional[list[str]]:
    slug, picks, rng = FRAMES[n]
    if os.environ.get("ANTHROPIC_API_KEY"):
        r = ocr_anthropic(img_bytes)
        game = {"ez2": "2D", "swertres": "3D"}[slug]
        if r.get("game") != game or (r.get("time") or "").upper() != expect_slot.upper() or r.get("date") != expect_date:
            print(f"  frame{n}: card does not match expectation ({r})", file=sys.stderr)
            return None
        return validate_numbers([str(x) for x in r.get("numbers", [])], picks, rng)
    return validate_numbers(ocr_tesseract(img_bytes), picks, rng)


def cmd_update(args) -> int:
    now = datetime.now(PHT)
    date = args.date or now.strftime("%Y-%m-%d")
    slots = [args.slot] if args.slot != "auto" else [s for s, t in SLOT_TIME.items() if s != "9pm" and now.strftime("%H:%M") >= t]
    videos = fetch_videos()
    doc = load_pending()
    official = official_lookup()
    have = {(d["game"], d["date"], d["time"]) for d in doc["draws"]}
    written = 0
    for slot in slots:
        v = pick(videos, date, slot)
        if not v:
            print(f"{date} {slot}: no recap video yet")
            continue
        for n, (slug, picks, rng) in FRAMES.items():
            key = (slug, date, SLOT_TIME[slot])
            if key in official or key in have:
                continue
            nums = read_frame(thumbnail(v.id, n), n, date, slot)
            if not nums:
                print(f"{date} {slot} {slug}: OCR rejected", file=sys.stderr)
                continue
            doc["draws"].append({"game": slug, "date": date, "time": SLOT_TIME[slot], "numbers": nums, "source": SOURCE,
                                 "video": v.id, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            have.add(key)
            written += 1
            print(f"{date} {slot} {slug}: {' '.join(nums)}  (video {v.id})")
    doc, mismatches = reconcile(doc, official, date)
    for m in mismatches:
        print(f"MISMATCH {m['game']} {m['date']} {m['time']}: pending {m['numbers']} vs official {m['official']}", file=sys.stderr)
    if written or mismatches or args.force_write:
        save_pending(doc)
    return 0


def cmd_reconcile(args) -> int:
    doc = load_pending()
    before = len(doc["draws"])
    doc, mismatches = reconcile(doc, official_lookup(), datetime.now(PHT).strftime("%Y-%m-%d"))
    for m in mismatches:
        print(f"MISMATCH {m['game']} {m['date']} {m['time']}: pending {m['numbers']} vs official {m['official']}", file=sys.stderr)
    if len(doc["draws"]) != before:
        save_pending(doc)
        print(f"pending: {before} -> {len(doc['draws'])}")
    return 0


def cmd_ocr(args) -> int:
    if args.image:
        frames = [(args.image, Path(args.image).read_bytes())]
    else:
        videos = fetch_videos()
        chosen = [v for v in videos if v.slot != "9pm"][: args.last]
        frames = [(f"{v.date} {v.slot} frame{n}", thumbnail(v.id, n)) for v in chosen for n in (1, 2)]
    for label, b in frames:
        print(label, "->", ocr_tesseract(b), "| raw:", ocr_debug(b))
        if os.environ.get("ANTHROPIC_API_KEY"):
            print("   anthropic:", ocr_anthropic(b))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update"); u.add_argument("--slot", default="auto", choices=["auto", "2pm", "5pm"]); u.add_argument("--date"); u.add_argument("--force-write", action="store_true")
    sub.add_parser("reconcile")
    o = sub.add_parser("ocr"); o.add_argument("image", nargs="?"); o.add_argument("--last", type=int, default=4, help="without an image: OCR the last N 2pm/5pm recap videos from the feed")
    a = p.parse_args(argv)
    return {"update": cmd_update, "reconcile": cmd_reconcile, "ocr": cmd_ocr}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
