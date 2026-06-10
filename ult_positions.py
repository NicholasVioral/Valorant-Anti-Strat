"""
ult_positions.py
----------------
Auto-generate an ult-cast position map from the positions table.

Ult cast detection: per player per round, the frame where ult_charges
drops from ult_max to < ult_max is the cast moment. Uses x/y at that frame.

Coordinate transform (Valorant API, rotation=-90 / Lotus-style maps):
    nx = locationY * x_mult + x_scalar
    ny = locationX * y_mult + y_scalar
    pct_x = nx * 100  (left %)
    pct_y = ny * 100  (top %)

All transform values come from map_meta (populated by scraper). Lotus fallback:
    x_mult=0.000072  x_scalar=0.454789
    y_mult=-0.000072 y_scalar=0.917752

Usage:
    python ult_positions.py --series 239296
    python ult_positions.py --series 239296 --map-num 2
    python ult_positions.py --series 239296 --out my_map.html
    python ult_positions.py --series 239296 --side atk
    python ult_positions.py --series 239296 --team "G2 Esports"
"""

import argparse
import base64
import json
import math
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent / "valorant.db"
IMG_CACHE = Path(__file__).parent / "map_cache"

LOTUS_FALLBACK = dict(x_mult=0.000072, x_scalar=0.454789,
                      y_mult=-0.000072, y_scalar=0.917752, rotation=-90)

# 10 perceptually distinct colors — assigned one per player IGN
PLAYER_PALETTE = [
    "#e8383d",  # red
    "#4da6ff",  # blue
    "#f0c040",  # gold
    "#3ecf8e",  # mint
    "#9b59b6",  # purple
    "#ff7f50",  # coral
    "#1fc6e8",  # cyan
    "#e91e96",  # pink
    "#a3e635",  # lime
    "#f97316",  # orange
]


def assign_player_colors(casts: list[dict]) -> dict[str, str]:
    """Return {ign: hex_color} sorted by team then IGN for consistency."""
    players = sorted(
        {(c["team_number"] or 1, c["player_ign"] or f"Player {c['player_id']}")
         for c in casts}
    )
    return {ign: PLAYER_PALETTE[i % len(PLAYER_PALETTE)]
            for i, (_, ign) in enumerate(players)}


# ── DB ────────────────────────────────────────────────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_match(conn, series_id: int, map_num: int):
    return conn.execute("""
        SELECT m.match_id, m.map_name, m.match_number,
               s.team1_name, s.team2_name, s.event_name
        FROM matches m
        JOIN series s ON s.series_id = m.series_id
        WHERE s.series_id = ? AND m.match_number = ?
    """, (series_id, map_num)).fetchone()


def get_map_meta(conn, map_name: str):
    return conn.execute("""
        SELECT x_mult, y_mult, x_scalar, y_scalar, rotation, minimap_url
        FROM map_meta WHERE map_name = ?
    """, (map_name,)).fetchone()


def get_rounds(conn, match_id: int) -> dict:
    rows = conn.execute("""
        SELECT round_number, attacking_team FROM rounds WHERE match_id = ?
    """, (match_id,)).fetchall()
    return {r["round_number"]: r["attacking_team"] for r in rows}


# ── Ult cast detection ────────────────────────────────────────────────────────

def detect_ult_casts(conn, match_id: int) -> list[dict]:
    """
    Return one row per ult cast: the frame where ult_charges first drops
    below ult_max for a given (player, round) pair.
    """
    rows = conn.execute("""
        SELECT player_id, player_ign, team_number, round_number,
               game_time_ms, x, y, ult_charges, ult_max
        FROM positions
        WHERE match_id = ? AND alive = 1 AND ult_max > 0
        ORDER BY player_id, round_number, game_time_ms
    """, (match_id,)).fetchall()

    if not rows:
        return []

    casts = []
    prev: dict[tuple, int] = {}  # (player_id, round_number) -> last ult_charges

    for r in rows:
        key          = (r["player_id"], r["round_number"])
        prev_charges = prev.get(key)
        curr_charges = r["ult_charges"]
        max_charges  = r["ult_max"]

        if (prev_charges is not None
                and prev_charges >= max_charges
                and curr_charges < max_charges
                and r["x"] is not None
                and r["y"] is not None):
            casts.append(dict(r))

        prev[key] = curr_charges

    return casts


# ── Callout lookup ────────────────────────────────────────────────────────────

def load_callouts(map_name: str) -> list[dict]:
    """Load callout list from {map_name}_callouts.json, e.g. lotus_callouts.json."""
    fname = Path(__file__).parent / f"{map_name.lower()}_callouts.json"
    if not fname.exists():
        return []
    with open(fname, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("callouts", [])


def nearest_callout(nx: float, ny: float, callouts: list[dict]) -> str:
    """Return the name of the nearest callout to normalized coords (nx, ny)."""
    if not callouts:
        return ""
    best = min(callouts, key=lambda c: math.hypot(c["normalizedX"] - nx,
                                                   c["normalizedY"] - ny))
    return best["name"]


# ── Coordinate transform ──────────────────────────────────────────────────────

def to_pct(wx: float, wy: float, rotation: int,
           x_mult: float, y_mult: float,
           x_scalar: float, y_scalar: float) -> tuple[float, float, float, float]:
    """Return (left_pct, top_pct, norm_x, norm_y)."""
    if rotation in (-90, 270):
        nx = wy * x_mult + x_scalar
        ny = wx * y_mult + y_scalar
    else:
        nx = wx * x_mult + x_scalar
        ny = wy * y_mult + y_scalar
    nx = max(0.0, min(1.0, nx))
    ny = max(0.0, min(1.0, ny))
    return round(nx * 100, 2), round(ny * 100, 2), nx, ny


# ── Map image ─────────────────────────────────────────────────────────────────

def fetch_map_image_b64(minimap_url: str, map_name: str) -> tuple[str, str]:
    IMG_CACHE.mkdir(exist_ok=True)
    safe  = map_name.lower().replace(" ", "_").replace("/", "_")
    ext   = "webp" if ".webp" in minimap_url else "png"
    cache = IMG_CACHE / f"{safe}.{ext}"

    if not cache.exists():
        print(f"  Downloading minimap: {minimap_url}")
        urllib.request.urlretrieve(minimap_url, cache)
    else:
        print(f"  Using cached minimap: {cache.name}")

    mime = "image/webp" if ext == "webp" else "image/png"
    return base64.b64encode(cache.read_bytes()).decode(), mime


# ── HTML rendering ────────────────────────────────────────────────────────────

def _markers_html(casts: list[dict], tx: dict, callouts: list[dict],
                  player_colors: dict) -> str:
    parts = []
    for c in casts:
        px, py, nx, ny = to_pct(c["x"], c["y"], tx["rotation"],
                                 tx["x_mult"], tx["y_mult"],
                                 tx["x_scalar"], tx["y_scalar"])
        ign     = c["player_ign"] or f"Player {c['player_id']}"
        color   = player_colors.get(ign, "#ffffff")
        rnd     = c["round_number"]
        secs    = (c["game_time_ms"] or 0) // 1000
        callout = nearest_callout(nx, ny, callouts)

        tip_x = "left:-148px;text-align:right;" if px > 65 else "left:20px;"
        tip_y = "top:-42px;" if py > 88 else "top:-6px;"
        loc   = f"{secs}s · {callout}" if callout else f"{secs}s into round"

        parts.append(
            f'<div class="dot" style="left:{px}%;top:{py}%;'
            f'background:{color};box-shadow:0 0 0 2px #000,0 0 8px {color}88;">'
            f'<div class="tip" style="{tip_x}{tip_y}">'
            f'<b style="color:{color}">{ign} · R{rnd}</b>'
            f'<span class="tip-sub">{loc}</span>'
            f'</div></div>'
        )
    return "\n".join(parts)


def _legend_html(casts: list[dict], player_colors: dict,
                 series_info: dict) -> str:
    by_team: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for c in casts:
        team = c["team_number"] or 1
        ign  = c["player_ign"] or f"Player {c['player_id']}"
        by_team[team][ign].append(c["round_number"])

    team_names = {1: series_info.get("team1_name", "Team 1"),
                  2: series_info.get("team2_name", "Team 2")}
    parts = []
    for team in sorted(by_team):
        parts.append(f'<div class="leg-team">{team_names.get(team, f"Team {team}")}</div>')
        for ign, rounds in sorted(by_team[team].items()):
            color   = player_colors.get(ign, "#ffffff")
            rnd_str = ", ".join(f"R{r}" for r in sorted(rounds))
            parts.append(
                f'<div class="leg-row">'
                f'<span class="leg-dot" style="background:{color}"></span>'
                f'<span style="color:{color};font-weight:600">{ign}</span>'
                f'<span class="leg-rounds">{rnd_str}</span>'
                f'</div>'
            )
    return "\n".join(parts)


def _table_html(casts: list[dict], tx: dict, callouts: list[dict],
                player_colors: dict) -> str:
    rows = []
    for c in sorted(casts, key=lambda x: (x["round_number"], x["game_time_ms"] or 0)):
        ign     = c["player_ign"] or f"Player {c['player_id']}"
        color   = player_colors.get(ign, "#ffffff")
        secs    = (c["game_time_ms"] or 0) // 1000
        _, _, nx, ny = to_pct(c["x"], c["y"], tx["rotation"],
                               tx["x_mult"], tx["y_mult"],
                               tx["x_scalar"], tx["y_scalar"])
        callout = nearest_callout(nx, ny, callouts) or "—"
        rows.append(
            f'<tr>'
            f'<td><span class="ign-dot" style="background:{color}"></span>'
            f'<span style="color:{color}">{ign}</span></td>'
            f'<td class="num">R{c["round_number"]}</td>'
            f'<td class="num">{secs}s</td>'
            f'<td class="dim">{callout}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def build_html(match_id: int, map_name: str, series_info: dict,
               casts: list[dict], tx: dict, callouts: list[dict],
               player_colors: dict, img_b64: str, img_mime: str) -> str:
    t1 = series_info.get("team1_name", "Team 1")
    t2 = series_info.get("team2_name", "Team 2")
    ev = series_info.get("event_name", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Ult Map — {map_name}</title>
<style>
:root{{--bg:#08090d;--surf:#10131a;--bdr:#1c2030;--gold:#f0c040;--dim:#50606e;--txt:#ccd6e8}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:"Segoe UI",system-ui,sans-serif;padding:32px}}
h1{{font-size:21px;font-weight:700;letter-spacing:.06em;color:#fff;margin-bottom:3px}}
.sub{{color:var(--dim);font-size:12px;margin-bottom:30px}}
.card{{background:var(--surf);border:1px solid var(--bdr);border-radius:8px;padding:22px;margin-bottom:22px}}
.card-title{{font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);margin-bottom:18px}}
.map-wrap{{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}}
.map-box{{position:relative;width:580px;height:580px;flex-shrink:0;border:1px solid var(--bdr);border-radius:4px;overflow:hidden}}
.map-box img{{width:100%;height:100%;display:block;object-fit:fill;filter:brightness(.82) contrast(1.06)}}
.map-over{{position:absolute;inset:0}}
.dot{{position:absolute;width:14px;height:14px;border-radius:50%;transform:translate(-50%,-50%);cursor:pointer;z-index:10;transition:transform .12s}}
.dot:hover{{transform:translate(-50%,-50%) scale(1.9);z-index:30}}
.tip{{display:none;position:absolute;white-space:nowrap;background:rgba(4,6,12,.96);border:1px solid rgba(255,255,255,.15);border-radius:5px;padding:6px 11px;font-size:11px;z-index:40;line-height:1.5}}
.dot:hover .tip{{display:block}}
.tip b{{display:block;font-size:12px;letter-spacing:.04em}}
.tip-sub{{display:block;color:#6a7e92;font-size:11px}}
.legend{{display:flex;flex-direction:column;gap:8px;min-width:200px}}
.leg-team{{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;margin-top:10px}}
.leg-team:first-child{{margin-top:0}}
.leg-row{{display:flex;align-items:center;gap:7px;font-size:13px}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.leg-rounds{{margin-left:auto;font-size:11px;color:#566070}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;font-size:10px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);padding:5px 12px;border-bottom:1px solid var(--bdr)}}
td{{padding:9px 12px;border-bottom:1px solid #0f1118;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#0d1016}}
.num{{font-variant-numeric:tabular-nums;color:#7a8898}}
.dim{{color:var(--dim)}}
.ign-dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle}}
</style>
</head>
<body>

<h1>{t1} vs {t2} — Ult Cast Locations</h1>
<div class="sub">{map_name} &nbsp;·&nbsp; Match {match_id} &nbsp;·&nbsp; {ev} &nbsp;·&nbsp; {len(casts)} ult casts &nbsp;·&nbsp; Hover markers for details</div>

<div class="card">
  <div class="card-title">Ult Cast Positions</div>
  <div class="map-wrap">
    <div class="map-box">
      <img src="data:{img_mime};base64,{img_b64}" alt="{map_name} minimap">
      <div class="map-over">
{_markers_html(casts, tx, callouts, player_colors)}
      </div>
    </div>
    <div class="legend">
{_legend_html(casts, player_colors, series_info)}
    </div>
  </div>
</div>

<div class="card">
  <div class="card-title">Ult Casts — Detail</div>
  <table>
    <thead><tr><th>Player</th><th>Round</th><th>Time</th><th>Location</th></tr></thead>
    <tbody>
{_table_html(casts, tx, callouts, player_colors)}
    </tbody>
  </table>
</div>

</body>
</html>"""


# ── Filtering ─────────────────────────────────────────────────────────────────

def filter_side(casts: list[dict], side: str, rounds_atk: dict) -> list[dict]:
    result = []
    for c in casts:
        atk_team = rounds_atk.get(c["round_number"])
        is_atk   = (c["team_number"] == atk_team)
        if (side == "atk" and is_atk) or (side == "def" and not is_atk):
            result.append(c)
    return result


def filter_team(casts: list[dict], team_name: str, series_info: dict) -> list[dict]:
    t1 = (series_info.get("team1_name") or "").lower()
    t2 = (series_info.get("team2_name") or "").lower()
    name_lower = team_name.lower()

    if name_lower in t1 or t1 in name_lower:
        target_team = 1
    elif name_lower in t2 or t2 in name_lower:
        target_team = 2
    else:
        print(f"  WARNING: '{team_name}' not matched. Teams: {t1!r}, {t2!r}")
        return casts

    return [c for c in casts if c["team_number"] == target_team]


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Generate ult-cast position map from DB")
    ap.add_argument("--series",  type=int, required=True, help="rib.gg series ID")
    ap.add_argument("--map-num", type=int, default=1,     help="Map number in series (default: 1)")
    ap.add_argument("--side",    choices=["atk", "def"],  help="Filter by attacking/defending side")
    ap.add_argument("--team",    type=str,                help="Filter by team name")
    ap.add_argument("--out",     type=str,                help="Output HTML filename")
    args = ap.parse_args()

    conn = db()

    match_row = get_match(conn, args.series, args.map_num)
    if not match_row:
        print(f"No match found for series {args.series} map {args.map_num}.")
        for r in conn.execute(
            "SELECT match_number, map_name, match_id FROM matches "
            "WHERE series_id=? ORDER BY match_number", (args.series,)
        ).fetchall():
            print(f"  Map {r['match_number']}: {r['map_name']} (match_id={r['match_id']})")
        sys.exit(1)

    match_id    = match_row["match_id"]
    map_name    = match_row["map_name"]
    series_info = dict(match_row)
    print(f"Match {match_id}: {map_name}  ({match_row['team1_name']} vs {match_row['team2_name']})")

    meta = get_map_meta(conn, map_name)
    if meta and None not in (meta["x_mult"], meta["y_mult"], meta["x_scalar"], meta["y_scalar"]):
        tx = {
            "x_mult":   meta["x_mult"],
            "y_mult":   meta["y_mult"],
            "x_scalar": meta["x_scalar"],
            "y_scalar": meta["y_scalar"],
            "rotation": meta["rotation"] if meta["rotation"] is not None else -90,
        }
        minimap_url = meta["minimap_url"]
        print(f"  Transform: x_mult={tx['x_mult']}  x_scalar={tx['x_scalar']}")
    else:
        print(f"  No map_meta for '{map_name}' — using Lotus fallback transform")
        tx = dict(LOTUS_FALLBACK)
        minimap_url = (meta["minimap_url"] if meta else None)

    if not minimap_url:
        print("ERROR: No minimap URL in map_meta. Run the scraper to populate it.")
        sys.exit(1)

    casts = detect_ult_casts(conn, match_id)
    print(f"  Detected {len(casts)} ult casts")

    if not casts:
        print("\nNo ult casts found. Check positions data:")
        pos_count = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE match_id=?", (match_id,)
        ).fetchone()[0]
        ult_rows = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE match_id=? AND ult_max > 0", (match_id,)
        ).fetchone()[0]
        print(f"  positions rows: {pos_count}  (ult_max > 0: {ult_rows})")
        if pos_count == 0:
            print(f"  -> python scraper.py --reparse-2d {args.series}")
        elif ult_rows == 0:
            print(f"  -> python scraper.py --force-2d {args.series}")
        sys.exit(1)

    if args.side:
        rounds_atk = get_rounds(conn, match_id)
        casts = filter_side(casts, args.side, rounds_atk)
        print(f"  After --side {args.side}: {len(casts)} casts")

    if args.team:
        casts = filter_team(casts, args.team, series_info)
        print(f"  After --team filter: {len(casts)} casts")

    if not casts:
        print("No casts remaining after filters.")
        sys.exit(0)

    img_b64, img_mime = fetch_map_image_b64(minimap_url, map_name)

    callouts = load_callouts(map_name)
    if callouts:
        print(f"  Loaded {len(callouts)} callouts for {map_name}")
    else:
        print(f"  No callout file found for {map_name} — tooltips will show time only")

    player_colors = assign_player_colors(casts)
    print(f"  Players: {', '.join(f'{ign}={c}' for ign, c in player_colors.items())}")

    html = build_html(match_id, map_name, series_info, casts, tx, callouts,
                      player_colors, img_b64, img_mime)

    if args.out:
        out_path = Path(args.out)
    else:
        safe_map = map_name.lower().replace(" ", "_")
        out_path = Path(__file__).parent / f"ult_map_{safe_map}_{match_id}.html"

    out_path.write_text(html, encoding="utf-8")
    print(f"  Saved: {out_path}")
    conn.close()


if __name__ == "__main__":
    main()
