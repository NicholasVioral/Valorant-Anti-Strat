"""
Valorant Counter-Strat web app.
Run:  python app.py
Then open:  http://localhost:5000
"""

import json
import math
import sqlite3
import statistics
from pathlib import Path

from flask import Flask, render_template, jsonify, request
import insights as _insights

app = Flask(__name__)
DB_PATH = Path(__file__).parent / "valorant.db"


# ── DB helpers ────────────────────────────────────────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Coordinate conversion ─────────────────────────────────────────────────────

def world_to_minimap(wx, wy, x_mult, y_mult, x_scalar, y_scalar, rotation):
    """
    Convert world coordinates to minimap image fractions [0,1] using Riot's
    official per-map multipliers from valorant-api.com (xMultiplier etc.).

    Formula:  mapX = gameX * xMultiplier + xScalarToAdd
              mapY = gameY * yMultiplier + yScalarToAdd

    For maps rotated ±90°/270° the world X/Y axes are swapped:
      worldY drives mapX, worldX drives mapY.
    This is the only formula that places kill events on walkable pixels in the
    displayIcon images (verified against Pearl pixel colors).
    """
    if wx is None or wy is None:
        return None, None
    if None in (x_mult, y_mult, x_scalar, y_scalar):
        return None, None
    if rotation in (-90, 270):
        mx = wy * x_mult + x_scalar
        my = wx * y_mult + y_scalar
    else:
        mx = wx * x_mult + x_scalar
        my = wy * y_mult + y_scalar
    return round(max(0.0, min(1.0, mx)), 4), round(max(0.0, min(1.0, my)), 4)


# ── Data queries ──────────────────────────────────────────────────────────────

def get_all_teams():
    conn = db()
    rows = conn.execute("""
        SELECT
            name,
            COUNT(DISTINCT t.series_id) AS series,
            COUNT(DISTINCT m.match_id)  AS maps,
            SUM(CASE WHEN m.winning_team = t.team_num THEN 1 ELSE 0 END) AS wins
        FROM (
            SELECT team1_name AS name, series_id, 1 AS team_num FROM series
            UNION ALL
            SELECT team2_name, series_id, 2 FROM series
        ) t
        JOIN matches m ON m.series_id = t.series_id
        WHERE name IS NOT NULL
        GROUP BY name
        ORDER BY series DESC, name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_map_meta():
    conn = db()
    rows = conn.execute("SELECT * FROM map_meta").fetchall()
    conn.close()
    result = {}
    for r in rows:
        r = dict(r)
        r["sites"] = json.loads(r.get("sites_json") or "{}")
        result[r["map_name"]] = r
    return result


def get_team_kills(team_name):
    """
    Return all kill events involving this team as victim (= their death positions)
    and as killer (= positions they pushed to), with minimap coordinates.
    """
    conn = db()
    map_meta = get_map_meta()

    # Get team number per match
    matches_q = conn.execute("""
        SELECT m.match_id, m.map_name, m.x_origin, m.y_origin, m.map_id,
               CASE WHEN LOWER(s.team1_name)=LOWER(?) THEN 1 ELSE 2 END AS team_number,
               m.winning_team
        FROM matches m
        JOIN series s ON s.series_id = m.series_id
        WHERE LOWER(s.team1_name) = LOWER(?) OR LOWER(s.team2_name) = LOWER(?)
    """, (team_name, team_name, team_name)).fetchall()

    kills = []
    for m in matches_q:
        mid      = m["match_id"]
        tnum     = m["team_number"]
        map_name = m["map_name"]
        meta     = map_meta.get(map_name) or {}
        x_mult   = meta.get("x_mult")
        y_mult   = meta.get("y_mult")
        x_scalar = meta.get("x_scalar")
        y_scalar = meta.get("y_scalar")
        rot      = meta.get("rotation") or -90

        rows = conn.execute("""
            SELECT victim_x, victim_y, round_time_ms, round_number,
                   side, victim_team, killer_team, is_first_kill, weapon
            FROM kills
            WHERE match_id = ? AND victim_x IS NOT NULL
        """, (mid,)).fetchall()

        for k in rows:
            mx, my = world_to_minimap(
                k["victim_x"], k["victim_y"], x_mult, y_mult, x_scalar, y_scalar, rot
            )
            if mx is None:
                continue
            point = {
                "x":       mx,
                "y":       my,
                "map":     map_name,
                "side":    k["side"] or "unknown",
                "time_ms": k["round_time_ms"],
                "round":   k["round_number"],
                "weapon":  k["weapon"] or "",
                "first":   bool(k["is_first_kill"]),
            }
            if k["victim_team"] == tnum:
                point["type"] = "death"   # this team died
            else:
                point["type"] = "kill"    # this team killed
            kills.append(point)

    conn.close()
    return kills


def get_team_stats(team_name):
    conn = db()
    matches = conn.execute("""
        SELECT m.match_id, m.map_name, m.winning_team,
               CASE WHEN LOWER(s.team1_name)=LOWER(?) THEN 1 ELSE 2 END AS team_number
        FROM matches m
        JOIN series s ON s.series_id = m.series_id
        WHERE LOWER(s.team1_name)=LOWER(?) OR LOWER(s.team2_name)=LOWER(?)
        ORDER BY m.match_id
    """, (team_name, team_name, team_name)).fetchall()

    if not matches:
        conn.close()
        return {}

    total = len(matches)
    wins  = sum(1 for m in matches if m["winning_team"] == m["team_number"])
    maps  = sorted(set(m["map_name"] for m in matches if m["map_name"]))

    # Timing: first kills when attacking
    timing_rows = conn.execute("""
        SELECT k.round_time_ms, k.side
        FROM kills k
        JOIN (
            SELECT m.match_id,
                   CASE WHEN LOWER(s.team1_name)=LOWER(?) THEN 1 ELSE 2 END AS team_number
            FROM matches m JOIN series s ON s.series_id=m.series_id
            WHERE LOWER(s.team1_name)=LOWER(?) OR LOWER(s.team2_name)=LOWER(?)
        ) t ON t.match_id = k.match_id
        WHERE k.killer_team = t.team_number
          AND k.is_first_kill = 1
          AND k.side = 'atk'
    """, (team_name, team_name, team_name)).fetchall()

    times = [r["round_time_ms"] for r in timing_rows if r["round_time_ms"]]
    timing = {
        "avg_s":   round(statistics.mean(times) / 1000, 1) if times else None,
        "min_s":   round(min(times) / 1000, 1) if times else None,
        "max_s":   round(max(times) / 1000, 1) if times else None,
        "early":   sum(1 for t in times if t < 20_000),
        "mid":     sum(1 for t in times if 20_000 <= t < 50_000),
        "late":    sum(1 for t in times if t >= 50_000),
        "buckets": {},
    }
    # Build histogram buckets (5-second bins)
    for t in times:
        bucket = (t // 5_000) * 5
        timing["buckets"][bucket] = timing["buckets"].get(bucket, 0) + 1

    conn.close()
    return {
        "team":       team_name,
        "total_maps": total,
        "wins":       wins,
        "losses":     total - wins,
        "win_pct":    round(wins / total * 100) if total else 0,
        "maps":       maps,
        "timing":     timing,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    teams = get_all_teams()
    return render_template("index.html", teams=teams)


@app.route("/team/<path:name>")
def team_page(name):
    stats    = get_team_stats(name)
    map_meta = get_map_meta()
    if not stats:
        return f"Team '{name}' not found.", 404
    return render_template("team.html", team=name, stats=stats, map_meta=map_meta)


@app.route("/api/kills/<path:name>")
def api_kills(name):
    return jsonify(get_team_kills(name))


@app.route("/api/maps")
def api_maps():
    return jsonify(get_map_meta())


@app.route("/api/site-probability/<path:name>")
def api_site_probability(name):
    return jsonify(get_site_probability(name))


def get_site_probability(team_name: str) -> dict:
    conn = db()

    # Fetch all ATK-side round_states where site was inferred
    rows = conn.execute("""
        SELECT rs.round_number, rs.map_name, rs.economy_tier,
               rs.score_self, rs.score_opp, rs.won_prev_round,
               rs.ult_players_json, rs.site_executed, rs.won_round,
               s.team1_name, s.team2_name, rs.team_number
        FROM round_states rs
        JOIN series s ON s.series_id = rs.series_id
        WHERE rs.side = 'atk'
          AND (LOWER(s.team1_name) = LOWER(?) OR LOWER(s.team2_name) = LOWER(?))
          AND (
            (LOWER(s.team1_name) = LOWER(?) AND rs.team_number = 1)
            OR
            (LOWER(s.team2_name) = LOWER(?) AND rs.team_number = 2)
          )
        ORDER BY rs.match_id, rs.round_number
    """, (team_name, team_name, team_name, team_name)).fetchall()

    # All ATK rounds (for timeline)
    all_rounds = [dict(r) for r in rows]
    for r in all_rounds:
        r["ult_count"] = len(json.loads(r["ult_players_json"] or "[]"))
        del r["ult_players_json"]

    # Only rounds with a known site for probability calculations
    known = [r for r in all_rounds if r["site_executed"] in ("A", "B")]
    sites = sorted({r["site_executed"] for r in known})

    def prob_dist(subset):
        total = len(subset)
        if total == 0:
            return {}
        counts = {}
        for r in subset:
            s = r["site_executed"]
            counts[s] = counts.get(s, 0) + 1
        return {s: round(counts.get(s, 0) / total, 3) for s in sites}

    # Overall
    overall = prob_dist(known)

    # By economy tier
    by_econ = {}
    for tier in ("eco", "half", "full", "heavy", "unknown"):
        subset = [r for r in known if r["economy_tier"] == tier]
        if subset:
            by_econ[tier] = {"dist": prob_dist(subset), "n": len(subset)}

    # By previous round outcome
    by_prev = {}
    for prev, label in ((1, "won_prev"), (0, "lost_prev")):
        subset = [r for r in known if r["won_prev_round"] == prev]
        if subset:
            by_prev[label] = {"dist": prob_dist(subset), "n": len(subset)}

    # By score state (winning / tied / losing)
    by_score = {}
    for label, fn in [
        ("leading",  lambda r: r["score_self"] > r["score_opp"]),
        ("tied",     lambda r: r["score_self"] == r["score_opp"]),
        ("trailing", lambda r: r["score_self"] < r["score_opp"]),
    ]:
        subset = [r for r in known if fn(r)]
        if subset:
            by_score[label] = {"dist": prob_dist(subset), "n": len(subset)}

    # By map
    by_map = {}
    for map_name in sorted({r["map_name"] for r in known}):
        subset = [r for r in known if r["map_name"] == map_name]
        by_map[map_name] = {"dist": prob_dist(subset), "n": len(subset)}

    # Round-by-round ATK timeline (all rounds, site may be None)
    timeline = []
    for r in all_rounds:
        timeline.append({
            "round":    r["round_number"],
            "map":      r["map_name"],
            "econ":     r["economy_tier"],
            "site":     r["site_executed"],
            "won":      r["won_round"],
            "score":    f"{r['score_self']}-{r['score_opp']}",
            "prev_won": r["won_prev_round"],
            "ults":     r["ult_count"],
        })

    conn.close()
    return {
        "sites":     sites,
        "total_atk": len(all_rounds),
        "known":     len(known),
        "overall":   overall,
        "by_econ":   by_econ,
        "by_prev":   by_prev,
        "by_score":  by_score,
        "by_map":    by_map,
        "timeline":  timeline,
    }


@app.route("/api/predict/<path:name>")
def api_predict(name):
    map_filter   = request.args.get("map",         "all")
    econ_filter  = request.args.get("economy",     "all")
    score_filter = request.args.get("score_state", "all")

    conn = db()

    conditions = [
        "rs.side = 'atk'",
        "rs.site_executed IS NOT NULL",
        """(
            (LOWER(s.team1_name) = LOWER(:name) AND rs.team_number = 1)
            OR
            (LOWER(s.team2_name) = LOWER(:name) AND rs.team_number = 2)
        )""",
    ]
    params: dict = {"name": name}

    if map_filter != "all":
        conditions.append("rs.map_name = :map")
        params["map"] = map_filter

    if econ_filter != "all":
        conditions.append("rs.economy_tier = :econ")
        params["econ"] = econ_filter

    if score_filter == "leading":
        conditions.append("rs.score_self > rs.score_opp")
    elif score_filter == "tied":
        conditions.append("rs.score_self = rs.score_opp")
    elif score_filter == "trailing":
        conditions.append("rs.score_self < rs.score_opp")

    where = " AND ".join(conditions)

    rows = conn.execute(f"""
        SELECT rs.round_number, rs.map_name, rs.economy_tier,
               rs.score_self, rs.score_opp, rs.site_executed,
               rs.won_round, rs.won_prev_round, rs.match_id,
               rs.ult_players_json
        FROM round_states rs
        JOIN series s ON s.series_id = rs.series_id
        WHERE {where}
        ORDER BY rs.match_id DESC, rs.round_number DESC
    """, params).fetchall()

    rows = [dict(r) for r in rows]
    conn.close()

    total = len(rows)
    site_counts: dict[str, int] = {}
    for r in rows:
        s = r["site_executed"]
        site_counts[s] = site_counts.get(s, 0) + 1

    # Sorted list of (site, count, pct) for consistent display order
    distribution = [
        {
            "site": s,
            "count": c,
            "pct": round(c / total * 100) if total else 0,
        }
        for s, c in sorted(site_counts.items())
    ]

    recent = []
    for r in rows[:3]:
        recent.append({
            "round":     r["round_number"],
            "map":       r["map_name"],
            "economy":   r["economy_tier"],
            "score":     f"{r['score_self']}-{r['score_opp']}",
            "score_self": r["score_self"],
            "score_opp":  r["score_opp"],
            "site":      r["site_executed"],
            "won":       bool(r["won_round"]),
            "prev_won":  r["won_prev_round"],
            "ults":      len(json.loads(r["ult_players_json"] or "[]")),
        })

    return jsonify({
        "total":        total,
        "distribution": distribution,
        "insufficient": total < 5,
        "recent":       recent,
    })


# ── Replay routes ────────────────────────────────────────────────────────────

@app.route("/replay/g2/pearl")
def replay_g2_pearl():
    conn = db()
    rows = conn.execute("""
        SELECT rs.match_id, rs.round_number, rs.site_executed,
               rs.economy_tier, rs.score_self, rs.score_opp, rs.won_round,
               s.team1_name, s.team2_name, s.event_name, s.start_date,
               CASE WHEN LOWER(s.team1_name) LIKE '%g2%' THEN 1 ELSE 2 END AS g2_num
        FROM round_states rs
        JOIN series s ON s.series_id = rs.series_id
        JOIN matches m ON m.match_id = rs.match_id
        WHERE m.map_name = 'Pearl'
          AND rs.side = 'atk'
          AND (
            (LOWER(s.team1_name) LIKE '%g2%' AND rs.team_number = 1)
            OR
            (LOWER(s.team2_name) LIKE '%g2%' AND rs.team_number = 2)
          )
        ORDER BY s.start_date, rs.match_id, rs.round_number
    """).fetchall()
    conn.close()
    rounds = []
    for r in rows:
        opp = r["team2_name"] if "g2" in (r["team1_name"] or "").lower() else r["team1_name"]
        site = r["site_executed"] or "?"
        econ = (r["economy_tier"] or "?")[:4]
        result = "W" if r["won_round"] else "L"
        rounds.append({
            "match_id":     r["match_id"],
            "round_number": r["round_number"],
            "site":         r["site_executed"],
            "economy":      r["economy_tier"],
            "score":        f"{r['score_self']}-{r['score_opp']}",
            "won":          bool(r["won_round"]),
            "label":        f"vs {opp} · R{r['round_number']} · {site} · {econ} [{result}]",
            "event":        (r["event_name"] or "").replace("Champions Tour 2026: ", ""),
        })
    return render_template("replay.html", rounds=rounds)


@app.route("/api/replay/round/<int:match_id>/<int:round_num>")
def api_replay_round(match_id, round_num):
    conn = db()
    match_row = conn.execute("""
        SELECT m.map_name, mm.minimap_url, mm.rotation, mm.sites_json,
               mm.x_mult, mm.y_mult, mm.x_scalar, mm.y_scalar
        FROM matches m
        LEFT JOIN map_meta mm ON mm.map_name = m.map_name
        WHERE m.match_id = ?
    """, (match_id,)).fetchone()
    if not match_row:
        return jsonify({"error": "not found"}), 404

    g2_row = conn.execute("""
        SELECT CASE WHEN LOWER(s.team1_name) LIKE '%g2%' THEN 1 ELSE 2 END AS g2_num,
               s.team1_name, s.team2_name, s.event_name
        FROM matches m JOIN series s ON s.series_id=m.series_id
        WHERE m.match_id=?
    """, (match_id,)).fetchone()
    if not g2_row:
        return jsonify({"error": "series not found"}), 404

    g2_num   = g2_row["g2_num"]
    rot      = match_row["rotation"] or 0
    x_mult   = match_row["x_mult"]
    y_mult   = match_row["y_mult"]
    x_scalar = match_row["x_scalar"]
    y_scalar = match_row["y_scalar"]
    sites    = json.loads(match_row["sites_json"] or "{}")

    def to_mm(wx, wy):
        return world_to_minimap(wx, wy, x_mult, y_mult, x_scalar, y_scalar, rot)

    # G2 player roster for this match
    players = conn.execute("""
        SELECT mp.player_id, p.ign
        FROM match_players mp
        LEFT JOIN players p ON p.player_id = mp.player_id
        WHERE mp.match_id = ? AND mp.team_number = ?
        ORDER BY mp.player_id
    """, (match_id, g2_num)).fetchall()
    player_list = [{"id": p["player_id"], "ign": p["ign"] or f"P{i+1}"}
                   for i, p in enumerate(players)]

    # Kill events for this round
    kills = conn.execute("""
        SELECT victim_id, killer_id, victim_team, killer_team,
               victim_x, victim_y, round_time_ms, weapon, is_first_kill, ability_type
        FROM kills
        WHERE match_id = ? AND round_number = ? AND victim_x IS NOT NULL
        ORDER BY round_time_ms
    """, (match_id, round_num)).fetchall()

    events = []
    for k in kills:
        mx, my = to_mm(k["victim_x"], k["victim_y"])
        if mx is None:
            continue
        events.append({
            "t":          k["round_time_ms"],
            "victim_id":  k["victim_id"],
            "killer_id":  k["killer_id"],
            "vteam":      k["victim_team"],
            "kteam":      k["killer_team"],
            "x":          mx,
            "y":          my,
            "weapon":     k["weapon"] or "",
            "first":      bool(k["is_first_kill"]),
            "ability":    k["ability_type"] is not None,
            "g2_kill":    k["killer_team"] == g2_num,
            "g2_death":   k["victim_team"] == g2_num,
        })

    duration_ms = max((e["t"] for e in events), default=30_000) + 4_000

    rs = conn.execute("""
        SELECT site_executed, economy_tier, score_self, score_opp, won_round
        FROM round_states
        WHERE match_id=? AND round_number=? AND side='atk' AND team_number=?
    """, (match_id, round_num, g2_num)).fetchone()

    conn.close()
    opp = g2_row["team2_name"] if "g2" in (g2_row["team1_name"] or "").lower() \
          else g2_row["team1_name"]
    ev  = (g2_row["event_name"] or "").replace("Champions Tour 2026: ", "")

    return jsonify({
        "match_id":    match_id,
        "round":       round_num,
        "g2_team":     g2_num,
        "duration_ms": duration_ms,
        "opponent":    opp,
        "event":       ev,
        "map_name":    match_row["map_name"],
        "site":        rs["site_executed"] if rs else None,
        "economy":     rs["economy_tier"]  if rs else None,
        "score":       f"{rs['score_self']}-{rs['score_opp']}" if rs else "?",
        "won":         bool(rs["won_round"]) if rs else None,
        "players":     player_list,
        "map_meta":    {"minimap_url": match_row["minimap_url"], "sites": sites},
        "events":      events,
    })


# ── Conditional probability report ───────────────────────────────────────────

@app.route("/report/<path:team>/<path:map_name>")
def report_page(team, map_name):
    report = _insights.find_insights(team, map_name)
    if not report:
        return f"No data found for '{team}' on {map_name}.", 404
    return render_template("report.html", report=report)


@app.route("/report/<path:team>")
def report_page_all_maps(team):
    report = _insights.find_insights(team)
    if not report:
        return f"No data found for '{team}'.", 404
    return render_template("report.html", report=report)


if __name__ == "__main__":
    print("Starting server at http://localhost:5000")
    app.run(debug=True, port=5000)
