"""
vod_sync.py — Sync a YouTube broadcast VOD to rounds in valorant.db.

How it works:
  1. Downloads the VOD (720p, video only) via yt-dlp into vod_cache/.
  2. Decodes 1 frame every SCAN_STEP_S seconds with ffmpeg (raw pipe).
  3. OCRs two HUD regions with easyocr:
       - top-center : "ROUND N" text + the two score numbers (buy phase HUD)
       - top-left   : "CURRENT: <MAP>" broadcast map bar
  4. The first frame where "ROUND N" appears is taken as the start of round N.
     A score check (left + right == N - 1) validates the sighting so replays /
     highlight wipes don't create false round starts.
  5. Round numbers resetting to 1 start a new map segment; each segment is
     matched to a match in the chosen series by its OCR'd map name.
  6. Results land in two tables:
       vods        (youtube_id, url, title, series_id, status, message, ...)
       vod_rounds  (match_id, round_number, youtube_id, start_s, end_s)

Usage:
  python vod_sync.py <youtube_url> --series 104004
  python vod_sync.py --list-series
"""

import argparse
import difflib
import json
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DB_PATH      = Path(__file__).parent / "valorant.db"
CACHE_DIR    = Path(__file__).parent / "vod_cache"
SCAN_STEP_S  = 4          # seconds between sampled frames (buy phase lasts ~30s)
SCAN_WIDTH   = 1280       # frames normalised to this width before OCR
ROUND_END_PAD_S = 100     # assumed length of the last round of a map
CLIP_LEAD_S  = 2          # start clips a touch before the detected buy phase

# Fractions of the frame for the two HUD crops (measured on examplegame.png)
CENTER_CROP = (0.33, 0.00, 0.67, 0.10)   # x0, y0, x1, y1 — round text + scores
MAP_CROP    = (0.00, 0.00, 0.30, 0.05)   # "CURRENT: LOTUS" bar

KNOWN_MAPS = ["Ascent", "Bind", "Breeze", "Corrode", "Fracture", "Haven",
              "Icebox", "Lotus", "Pearl", "Split", "Sunset", "Abyss"]

ROUND_RE = re.compile(r"R[O0Q]U[NM][DO]?\s*(\d{1,2})", re.IGNORECASE)
SCORE_RE = re.compile(r"^\d{1,2}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vods (
    youtube_id  TEXT PRIMARY KEY,
    url         TEXT,
    title       TEXT,
    series_id   INTEGER,
    scanned_at  TEXT,
    status      TEXT,
    message     TEXT
);
CREATE TABLE IF NOT EXISTS vod_rounds (
    match_id     INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    youtube_id   TEXT NOT NULL,
    start_s      REAL NOT NULL,
    end_s        REAL,
    validated    INTEGER DEFAULT 0,
    PRIMARY KEY (match_id, round_number)
);
"""

_reader = None


def _ocr():
    """Lazy-load the easyocr reader (model load takes a few seconds)."""
    global _reader
    if _reader is None:
        import easyocr
        _reader = easyocr.Reader(["en"], gpu=True, verbose=False)
    return _reader


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ── Download ──────────────────────────────────────────────────────────────────

def download_vod(url: str, progress_cb=None) -> tuple[str, str, Path]:
    """Return (youtube_id, title, local_path). Reuses cached file if present."""
    import yt_dlp

    CACHE_DIR.mkdir(exist_ok=True)

    def hook(d):
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done  = d.get("downloaded_bytes") or 0
            pct   = (done / total * 100) if total else 0
            progress_cb("download", pct, f"Downloading VOD {pct:.0f}%")

    opts = {
        "format": "bestvideo[height<=720][ext=mp4]/bestvideo[height<=720]/best[height<=720]",
        "outtmpl": str(CACHE_DIR / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        vid, title = info["id"], info.get("title", "")
        existing = list(CACHE_DIR.glob(f"{vid}.*"))
        if existing:
            return vid, title, existing[0]
        ydl.download([url])
    path = next(CACHE_DIR.glob(f"{vid}.*"))
    return vid, title, path


# ── Frame iteration ───────────────────────────────────────────────────────────

def _video_dims(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,duration", "-of", "json", str(path)],
        capture_output=True, text=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"]), float(s.get("duration") or 0)


def iter_frames(path: Path, step_s: float = SCAN_STEP_S):
    """Yield (timestamp_s, HxWx3 BGR ndarray) one frame per step_s."""
    w0, h0, _dur = _video_dims(path)
    w = SCAN_WIDTH
    h = int(h0 * w / w0) // 2 * 2
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "quiet", "-i", str(path),
         "-vf", f"fps=1/{step_s},scale={w}:{h}",
         "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE)
    frame_bytes = w * h * 3
    i = 0
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        yield i * step_s, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        i += 1
    proc.stdout.close()
    proc.wait()


def _crop(frame, frac, upscale: int = 4):
    """Crop a fractional region and upscale it — HUD text is too small for
    reliable OCR at native resolution."""
    import cv2
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = frac
    region = frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
    return cv2.resize(region, None, fx=upscale, fy=upscale,
                      interpolation=cv2.INTER_LANCZOS4)


# ── HUD parsing ───────────────────────────────────────────────────────────────

def parse_center(frame) -> tuple[int | None, bool]:
    """Return (round_number, score_validated) from the top-center HUD crop."""
    tokens = _ocr().readtext(np.ascontiguousarray(_crop(frame, CENTER_CROP)),
                             detail=0)
    text = " ".join(tokens)
    m = ROUND_RE.search(text)
    if not m:
        return None, False
    rnum = int(m.group(1))
    if not 1 <= rnum <= 50:
        return None, False
    scores = [int(t) for t in tokens if SCORE_RE.match(t.strip()) and int(t) <= 30]
    # Buy-phase HUD shows both team scores; they must sum to round - 1.
    validated = any(a + b == rnum - 1
                    for i, a in enumerate(scores) for b in scores[i + 1:])
    return rnum, validated


def parse_map(frame) -> str | None:
    """Return the map name from the 'CURRENT: <MAP>' bar, or None."""
    tokens = _ocr().readtext(np.ascontiguousarray(_crop(frame, MAP_CROP)),
                             detail=0)
    text = " ".join(tokens).upper()
    m = re.search(r"CURRENT[:;\s]+([A-Z]+)", text)
    if not m:
        return None
    cand = m.group(1).title()
    hit = difflib.get_close_matches(cand, KNOWN_MAPS, n=1, cutoff=0.6)
    return hit[0] if hit else None


# ── Scan ──────────────────────────────────────────────────────────────────────

def scan_video(path: Path, progress_cb=None) -> list[dict]:
    """
    Scan the file and return map segments:
      [{"map": "Lotus", "rounds": {1: {"start_s": 123.0, "validated": True}, ...}}]
    """
    _, _, duration = _video_dims(path)
    segments: list[dict] = []
    seg = None
    last_round = 0

    for ts, frame in iter_frames(path):
        if progress_cb and duration:
            progress_cb("scan", ts / duration * 100,
                        f"Scanning {ts/60:.0f}/{duration/60:.0f} min")

        rnum, validated = parse_center(frame)
        if rnum is None:
            continue

        # Round number dropping back signals a new map in the same VOD.
        # Require score validation so a single OCR misread can't split a map.
        is_reset = validated and ((rnum == 1 and last_round >= 13)
                                  or rnum < last_round - 8)
        if seg is None or is_reset:
            seg = {"map": None, "map_votes": {}, "rounds": {}}
            segments.append(seg)
            last_round = 0
        last_round = max(last_round, rnum)

        cur = seg["rounds"].get(rnum)
        if cur is None:
            seg["rounds"][rnum] = {"start_s": ts, "validated": validated}
        elif validated and not cur["validated"]:
            # Prefer the first score-validated sighting over an unvalidated one
            # only if it is plausibly the same buy phase (within 60 s).
            if ts - cur["start_s"] <= 60:
                cur.update(start_s=min(cur["start_s"], ts), validated=True)

        if seg["map"] is None:
            name = parse_map(frame)
            if name:
                seg["map_votes"][name] = seg["map_votes"].get(name, 0) + 1
                if seg["map_votes"][name] >= 2:
                    seg["map"] = name

    # Settle maps that never reached 2 votes.
    for seg in segments:
        if seg["map"] is None and seg["map_votes"]:
            seg["map"] = max(seg["map_votes"], key=seg["map_votes"].get)
        seg.pop("map_votes", None)

    # Drop noise segments (a real map has at least 5 detected rounds).
    return [s for s in segments if len(s["rounds"]) >= 5]


# ── DB write ──────────────────────────────────────────────────────────────────

def save_segments(conn, segments: list[dict], series_id: int,
                  youtube_id: str) -> list[str]:
    """Match segments to the series' matches by map name and store rounds."""
    matches = conn.execute(
        "SELECT match_id, map_name FROM matches "
        "WHERE series_id = ? AND map_name IS NOT NULL", (series_id,)).fetchall()
    by_map = {m["map_name"]: m["match_id"] for m in matches}
    notes = []

    for seg in segments:
        if not seg["map"]:
            notes.append(f"segment with {len(seg['rounds'])} rounds: map unreadable, skipped")
            continue
        mid = by_map.get(seg["map"])
        if mid is None:
            notes.append(f"{seg['map']}: not a map of this series, skipped")
            continue
        rounds = dict(sorted(seg["rounds"].items()))
        nums = list(rounds)
        for i, rnum in enumerate(nums):
            start = max(0.0, rounds[rnum]["start_s"] - CLIP_LEAD_S)
            end = (rounds[nums[i + 1]]["start_s"] if i + 1 < len(nums)
                   else rounds[rnum]["start_s"] + ROUND_END_PAD_S)
            conn.execute(
                "INSERT OR REPLACE INTO vod_rounds "
                "(match_id, round_number, youtube_id, start_s, end_s, validated) "
                "VALUES (?,?,?,?,?,?)",
                (mid, rnum, youtube_id, start, end,
                 int(rounds[rnum]["validated"])))
        notes.append(f"{seg['map']}: {len(nums)} rounds synced (match {mid})")
    conn.commit()
    return notes


def scan_vod(url: str, series_id: int, progress_cb=None) -> dict:
    """Full pipeline: download → scan → save. Returns a summary dict."""
    t0 = time.time()
    vid, title, path = download_vod(url, progress_cb)
    if progress_cb:
        progress_cb("scan", 0, "Loading OCR model…")
    _ocr()
    segments = scan_video(path, progress_cb)
    conn = _db()
    notes = save_segments(conn, segments, series_id, vid)
    n_rounds = sum(len(s["rounds"]) for s in segments)
    status = "done" if notes else "no_rounds_found"
    msg = "; ".join(notes) if notes else \
        "No round HUD detected — is this a full match broadcast VOD?"
    conn.execute(
        "INSERT OR REPLACE INTO vods VALUES (?,?,?,?,?,?,?)",
        (vid, url, title, series_id,
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         status, msg))
    conn.commit()
    conn.close()
    summary = {"youtube_id": vid, "title": title, "series_id": series_id,
               "segments": [{"map": s["map"], "rounds": len(s["rounds"])}
                            for s in segments],
               "rounds_synced": n_rounds, "status": status, "message": msg,
               "elapsed_s": round(time.time() - t0)}
    if progress_cb:
        progress_cb("done", 100, msg)
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Sync a YouTube VOD to DB rounds")
    ap.add_argument("url", nargs="?", help="YouTube URL of the match broadcast")
    ap.add_argument("--series", type=int, help="series_id in valorant.db")
    ap.add_argument("--list-series", action="store_true")
    args = ap.parse_args()

    if args.list_series:
        conn = _db()
        for r in conn.execute(
                "SELECT series_id, team1_name, team2_name, event_name, start_date "
                "FROM series ORDER BY start_date DESC"):
            print(f"  {r['series_id']}  {r['team1_name']} vs {r['team2_name']}"
                  f"  ({r['event_name']}, {(r['start_date'] or '')[:10]})")
        return

    if not args.url or not args.series:
        ap.error("need <url> and --series (use --list-series to find the id)")

    def cb(phase, pct, msg):
        print(f"\r[{phase:8s}] {pct:5.1f}%  {msg:<60s}", end="", flush=True)

    summary = scan_vod(args.url, args.series, cb)
    print("\n" + json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
