"""
Anti-strat report generator using a local Ollama LLM.

Pulls structured stats + 2D position data from the DB, derives tactical
observations in Python, then asks the LLM to write a match-prep brief
in the format used by professional coaching staffs.

Usage:
  python antistrat.py --team "NRG Esports" --vs "FURIA"
  python antistrat.py --team "NRG Esports" --no-ai          # print stats only
  python antistrat.py --team "NRG Esports" --save           # save to file
  python antistrat.py --team "NRG Esports" --model llama3.1

Make sure Ollama is running:  ollama serve
And the model is pulled:      ollama pull qwen2.5:14b
"""

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

import insights  as _insight_engine
import questions as _questions

DB_PATH       = Path(__file__).parent / "valorant.db"
EXAMPLES_DIR  = Path(__file__).parent / "coaching_examples"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── DB queries ────────────────────────────────────────────────────────────────

def _series_clause(series_id):
    """Return extra SQL AND clause and param dict for optional series filtering."""
    if series_id:
        return "AND s.series_id = :sid", {"sid": series_id}
    return "", {}


def _get_map_pool(conn, team_name, series_id=None):
    sp = {"n": team_name, "sid": series_id} if series_id else {"n": team_name}
    sc = "AND m.series_id = :sid" if series_id else ""
    rows = conn.execute(f"""
        SELECT m.map_name,
               COUNT(*)  AS maps_played,
               SUM(CASE WHEN m.winning_team = t.tnum THEN 1 ELSE 0 END) AS wins
        FROM matches m
        JOIN series s ON s.series_id = m.series_id
        JOIN (
            SELECT m2.match_id,
                   CASE WHEN LOWER(s2.team1_name)=LOWER(:n) THEN 1 ELSE 2 END AS tnum
            FROM matches m2 JOIN series s2 ON s2.series_id = m2.series_id
            WHERE (LOWER(s2.team1_name)=LOWER(:n) OR LOWER(s2.team2_name)=LOWER(:n))
        ) t ON t.match_id = m.match_id
        WHERE m.map_name IS NOT NULL {sc}
        GROUP BY m.map_name ORDER BY maps_played DESC
    """, sp).fetchall()
    return [dict(r) for r in rows]


def _get_series_meta(conn, series_id):
    """Return series metadata (teams, event, date) for a given series_id."""
    row = conn.execute("""
        SELECT team1_name, team2_name, event_name, start_date
        FROM series WHERE series_id=?
    """, (series_id,)).fetchone()
    return dict(row) if row else {}


def _get_site_rows(conn, team_name, series_id=None):
    sp = {"n": team_name, "sid": series_id} if series_id else {"n": team_name}
    sc = "AND rs.series_id = :sid" if series_id else ""
    rows = conn.execute(f"""
        SELECT rs.map_name, rs.economy_tier, rs.site_executed,
               rs.won_round, rs.score_self, rs.score_opp, rs.won_prev_round
        FROM round_states rs
        JOIN series s ON s.series_id = rs.series_id
        WHERE rs.side = 'atk'
          AND rs.site_executed IS NOT NULL
          AND (
            (LOWER(s.team1_name)=LOWER(:n) AND rs.team_number=1)
            OR
            (LOWER(s.team2_name)=LOWER(:n) AND rs.team_number=2)
          )
          {sc}
    """, sp).fetchall()
    return [dict(r) for r in rows]


def _get_timing_rows(conn, team_name, series_id=None):
    sp = {"n": team_name, "sid": series_id} if series_id else {"n": team_name}
    sc = "AND k.series_id = :sid" if series_id else ""
    rows = conn.execute(f"""
        SELECT k.round_time_ms, m.map_name
        FROM kills k
        JOIN matches m ON m.match_id = k.match_id
        JOIN series s ON s.series_id = m.series_id
        JOIN (
            SELECT m2.match_id,
                   CASE WHEN LOWER(s2.team1_name)=LOWER(:n) THEN 1 ELSE 2 END AS tnum
            FROM matches m2 JOIN series s2 ON s2.series_id = m2.series_id
            WHERE (LOWER(s2.team1_name)=LOWER(:n) OR LOWER(s2.team2_name)=LOWER(:n))
        ) t ON t.match_id = k.match_id
        WHERE k.killer_team = t.tnum
          AND k.is_first_kill = 1
          AND k.side = 'atk'
          AND k.round_time_ms IS NOT NULL
          {sc}
    """, sp).fetchall()
    return [dict(r) for r in rows]


def _get_player_rows(conn, team_name, series_id=None):
    sp = {"n": team_name, "sid": series_id} if series_id else {"n": team_name}
    sc = "AND ps.series_id = :sid" if series_id else ""
    rows = conn.execute(f"""
        SELECT p.ign, ps.side,
               COUNT(*)              AS rounds,
               AVG(ps.kills)         AS avg_kills,
               AVG(ps.deaths)        AS avg_deaths,
               SUM(ps.first_kills)   AS total_fk,
               SUM(ps.first_deaths)  AS total_fd,
               SUM(ps.plants)        AS total_plants,
               AVG(ps.damage)        AS avg_damage
        FROM player_stats ps
        JOIN players p ON p.player_id = ps.player_id
        JOIN series s ON s.series_id = ps.series_id
        WHERE (
            (LOWER(s.team1_name)=LOWER(:n) AND ps.team_number=1)
            OR
            (LOWER(s.team2_name)=LOWER(:n) AND ps.team_number=2)
          )
          {sc}
        GROUP BY p.ign, ps.side ORDER BY avg_damage DESC
    """, sp).fetchall()
    return [dict(r) for r in rows]


def _get_weapon_rows(conn, team_name, series_id=None):
    sp = {"n": team_name, "sid": series_id} if series_id else {"n": team_name}
    sc = "AND k.series_id = :sid" if series_id else ""
    rows = conn.execute(f"""
        SELECT k.weapon, k.side, COUNT(*) AS uses
        FROM kills k
        JOIN series s ON s.series_id = k.series_id
        JOIN (
            SELECT m2.match_id,
                   CASE WHEN LOWER(s2.team1_name)=LOWER(:n) THEN 1 ELSE 2 END AS tnum
            FROM matches m2 JOIN series s2 ON s2.series_id = m2.series_id
            WHERE (LOWER(s2.team1_name)=LOWER(:n) OR LOWER(s2.team2_name)=LOWER(:n))
        ) t ON t.match_id = k.match_id
        WHERE k.killer_team = t.tnum AND k.weapon IS NOT NULL {sc}
        GROUP BY k.weapon, k.side ORDER BY uses DESC
    """, sp).fetchall()
    return [dict(r) for r in rows]


def _get_series_count(conn, team_name, series_id=None):
    if series_id:
        return 1
    row = conn.execute("""
        SELECT COUNT(DISTINCT s.series_id) FROM series s
        WHERE LOWER(s.team1_name)=LOWER(:n) OR LOWER(s.team2_name)=LOWER(:n)
    """, {"n": team_name}).fetchone()
    return row[0] if row else 0


# ── 2D position analysis ──────────────────────────────────────────────────────

# Map-specific Y-axis site thresholds (game-engine units).
_MAP_SITE_THRESHOLDS = {
    "breeze": {"b_max": -1800, "a_min": 1800, "b_label": "B", "a_label": "A"},
    "haven":  None,
    "split":  {"b_max": -1500, "a_min": 1500, "b_label": "A", "a_label": "B"},
    "pearl":  {"b_max": -2000, "a_min": 2000, "b_label": "B", "a_label": "A"},
    "bind":   {"b_max": -2000, "a_min": 2000, "b_label": "A", "a_label": "B"},
}

# Pearl callout zones: (name, wx_min, wx_max, wy_min, wy_max)
# Verified against 2D replay positions — wx/wy are raw game-engine coords
# from the positions table (large float values like 6692, -5558).
_PEARL_ZONES = [
    # Calibrated from official Pearl callout map image + match 239376 confirmed positions.
    # Coordinate axes: positive wy = A side (left on map), negative wy = B side (right on map)
    #                  low wx = attacker spawn (top), high wx = defender spawn (bottom)
    # Verified: B site (6692,-5558), A site (6430,4319), A Secret (8384,4978),
    #   B Club (1601,-1958), Mid Top (2480,2192), A Restaurant (3652,3593),
    #   B Ramp (2350,-3531), B Main (3761,-4442), B Hall (7542,-3065)
    #
    # A SIDE — specific zones first, then broader
    ("A Secret",        8000, 10500,  4000,  5500),
    ("A Flowers",       7500,  9500,  3800,  5300),
    ("A Dugout",        7000,  9000,  4500,  6500),
    ("A site",          5400,  7800,  4000,  6300),
    ("A Main",          4500,  6700,  4600,  6700),
    ("A Link",          5500,  7300,  1700,  3700),
    ("A Art",           4000,  6300,  1500,  3800),
    ("A Restaurant",    2200,  4000,  2800,  4800),
    ("Mid Top",         1700,  3400,  1000,  3500),
    # MID
    ("Mid Plaza",       3500,  5700,  -900,  1300),
    ("Mid Doors",       4400,  6200,  -400,  1600),
    ("Mid Shops",       1600,  3400, -1400,   300),
    ("Mid Connector",   5700,  7800,  -700,  1300),
    ("Sewers",          6900,  8900,  -600,  1800),
    # B SIDE
    ("B Link",          4400,  6600, -2700,  -500),
    ("B Tower",         5300,  7300, -3300, -1300),
    ("B Tunnel",        5800,  8000, -3300, -1300),
    ("B Hall",          6700,  8900, -4100, -1900),
    ("B site",          5100,  7900, -6400, -2700),
    ("B Screen",        5800,  7900, -4600, -2200),
    ("B Main",          3400,  6700, -6200, -4000),
    ("B Ramp",          1700,  5100, -5800, -3000),
    ("B Club",           700,  2700, -4200, -1600),
    # Spawns — catch-all
    ("Defender Spawn",  9200, 12000, -2700,  3200),
    ("Attacker Spawn",  -500,  1800, -3300,  3300),
]

# Breeze callout zones
_BREEZE_ZONES = [
    ("A site",          3500,  8000,  3500,  7000),
    ("A Lobby",         1500,  5000,  2000,  5000),
    ("A Hall",          4000,  8000,  1500,  4000),
    ("A Ramp",          2000,  5500,   500,  3000),
    ("Mid",             2000,  7000,  -500,  2000),
    ("B site",          1500,  7000, -6000, -2000),
    ("B Main",           500,  5000, -8000, -4000),
    ("B Tunnel",        2000,  6000, -4000, -1500),
    ("B Elbow",         4000,  8000, -3500, -1000),
    ("Attacker Spawn",   500,  5000, -1000,  2500),
    ("Defender Spawn",  5000, 12000, -2000,  3000),
]

_MAP_ZONES = {
    "pearl":  _PEARL_ZONES,
    "breeze": _BREEZE_ZONES,
}

# Agent ID → name mapping (sourced from rib.gg API dump)
_AGENT_NAMES: dict[int, str] = {
    1: "Breach", 2: "Raze", 3: "Cypher", 4: "Sova", 5: "Killjoy",
    6: "Viper", 7: "Phoenix", 8: "Brimstone", 9: "Sage", 10: "Reyna",
    11: "Omen", 12: "Jett", 13: "Skye", 14: "Yoru", 15: "Astra",
    16: "KAY/O", 17: "Chamber", 18: "Neon", 19: "Fade", 20: "Harbor",
    21: "Gekko", 22: "Deadlock", 23: "Iso", 25: "Clove", 26: "Vyse",
    27: "Tejo", 28: "Waylay", 29: "Veto", 33: "Miks",
}

# Which site a callout belongs to (for pattern lean classification)
_A_SIDE_CALLOUTS = {
    "A Secret", "A Flowers", "A Dugout", "A site", "A Main",
    "A Link", "A Art", "A Restaurant", "Mid Top",
    "A Lobby", "A Hall", "A Ramp",  # Breeze
}
_B_SIDE_CALLOUTS = {
    "B Club", "B Ramp", "B Main", "B Tower", "B Tunnel",
    "B Hall", "B site", "B Screen", "B Link",
    "B Elbow",  # Breeze
}

def _callout_side(callout: str) -> str:
    if callout in _A_SIDE_CALLOUTS:
        return "A"
    if callout in _B_SIDE_CALLOUTS:
        return "B"
    return "Mid"


def _pos_to_callout(map_name: str, wx: float, wy: float) -> str:
    """Translate raw game-engine (wx, wy) to a named map callout."""
    zones = _MAP_ZONES.get((map_name or "").lower())
    if not zones:
        return f"({wx:.0f},{wy:.0f})"
    for name, wx_min, wx_max, wy_min, wy_max in zones:
        if wx_min <= wx <= wx_max and wy_min <= wy <= wy_max:
            return name
    return f"({wx:.0f},{wy:.0f})"


def _analyze_player_routes(conn, team_name: str, map_filter=None, series_id=None) -> dict:
    """
    Sample each player's callout at t=8-12s in every ATK round (and t=2-4s in DEF rounds).
    Returns: {
        map_name: {
            'atk_total': int,          # total ATK rounds played (denominator)
            'def_total': int,          # total DEF rounds played
            'atk': {ign: Counter},     # callout counts across rounds WITH position data
            'def': {ign: Counter},
        }
    }
    """
    from collections import Counter
    sp = {"n": team_name, "sid": series_id} if series_id else {"n": team_name}
    sc = "AND m.series_id = :sid" if series_id else ""
    matches = conn.execute(f"""
        SELECT m.match_id, m.map_name,
               CASE WHEN LOWER(s.team1_name)=LOWER(:n) THEN 1 ELSE 2 END AS tnum
        FROM matches m JOIN series s ON s.series_id=m.series_id
        WHERE (LOWER(s.team1_name)=LOWER(:n) OR LOWER(s.team2_name)=LOWER(:n)) {sc}
    """, sp).fetchall()

    if map_filter:
        lf = [m.lower() for m in map_filter]
        matches = [m for m in matches if (m["map_name"] or "").lower() in lf]

    result = {}

    for match in matches:
        mid      = match["match_id"]
        map_name = (match["map_name"] or "").strip()
        tnum     = match["tnum"]

        if map_name.lower() not in _MAP_ZONES:
            continue

        if map_name not in result:
            result[map_name] = {
                'atk_total': 0, 'def_total': 0,
                'atk': defaultdict(Counter), 'def': defaultdict(Counter),
            }
        r = result[map_name]

        for side, key, t_lo, t_hi in [('atk', 'atk', 8000, 12000),
                                       ('def', 'def', 2000, 4000)]:
            rounds = conn.execute("""
                SELECT round_number FROM round_states
                WHERE match_id=? AND team_number=? AND side=?
            """, (mid, tnum, side)).fetchall()

            r[f'{side}_total'] += len(rounds)

            for rnd in rounds:
                rnum = rnd[0]
                frames = conn.execute("""
                    SELECT player_ign, x, y FROM positions
                    WHERE match_id=? AND round_number=? AND team_number=?
                      AND game_time_ms BETWEEN ? AND ?
                    ORDER BY game_time_ms
                """, (mid, rnum, tnum, t_lo, t_hi)).fetchall()

                seen = {}
                for f in frames:
                    ign = f["player_ign"] or "?"
                    if ign not in seen:
                        seen[ign] = (f["x"] or 0, f["y"] or 0)

                for ign, (wx, wy) in seen.items():
                    short_ign = ign.split()[-1] if " " in ign else ign
                    callout = _pos_to_callout(map_name, wx, wy)
                    r[key][short_ign][callout] += 1

    # convert inner defaultdicts to plain dicts
    for map_name in result:
        result[map_name]['atk'] = dict(result[map_name]['atk'])
        result[map_name]['def'] = dict(result[map_name]['def'])
    return result


def _format_route_analysis(route_data: dict) -> list[str]:
    """Format player route frequencies into human-readable lines."""
    lines = []
    lines.append("═" * 60)
    lines.append("PLAYER OPENING ROUTES  (ATK: ~10s in | DEF: ~3s in = initial deployment)")
    lines.append("Shows how often each player takes each path — use this to predict setups")
    lines.append("═" * 60)
    for map_name, data in sorted(route_data.items()):
        atk_total = data.get('atk_total', 0)
        def_total = data.get('def_total', 0)
        atk_routes = data.get('atk', {})
        def_routes = data.get('def', {})

        lines.append(f"\n  [{map_name}]  ATK: {atk_total} rounds  |  DEF: {def_total} rounds")

        lines.append("  ATK routes (where they go when attacking):")
        for ign, counter in sorted(atk_routes.items()):
            if not counter or atk_total == 0:
                continue
            top = counter.most_common(4)
            route_str = "  |  ".join(
                f"{zone}: {cnt}/{atk_total} ({cnt*100//atk_total}%)" for zone, cnt in top
            )
            lines.append(f"    {ign:<12} → {route_str}")

        lines.append("  DEF routes (where they hold when defending):")
        for ign, counter in sorted(def_routes.items()):
            if not counter or def_total == 0:
                continue
            top = counter.most_common(4)
            route_str = "  |  ".join(
                f"{zone}: {cnt}/{def_total} ({cnt*100//def_total}%)" for zone, cnt in top
            )
            lines.append(f"    {ign:<12} → {route_str}")

    return lines


def _analyze_opening_patterns(conn, team_name: str, map_filter=None, series_id=None) -> dict:
    """
    Team-level pattern recognition:
    1. Setup shape (A/B/Mid count at t=8-12s) → site executed
    2. Player pairs that co-locate on same side → site executed (only pairs seen 2+ times)
    3. Entry frag leaders: who gets first blood and where (victim callout)
    """
    from collections import Counter

    sp = {"n": team_name, "sid": series_id} if series_id else {"n": team_name}
    sc = "AND m.series_id = :sid" if series_id else ""
    matches = conn.execute(f"""
        SELECT m.match_id, m.map_name,
               CASE WHEN LOWER(s.team1_name)=LOWER(:n) THEN 1 ELSE 2 END AS tnum
        FROM matches m JOIN series s ON s.series_id=m.series_id
        WHERE (LOWER(s.team1_name)=LOWER(:n) OR LOWER(s.team2_name)=LOWER(:n)) {sc}
    """, sp).fetchall()

    if map_filter:
        lf = [m.lower() for m in map_filter]
        matches = [m for m in matches if (m["map_name"] or "").lower() in lf]

    result = {}

    for match in matches:
        mid      = match["match_id"]
        map_name = (match["map_name"] or "").strip()
        tnum     = match["tnum"]

        if map_name.lower() not in _MAP_ZONES:
            continue

        if map_name not in result:
            result[map_name] = {
                'total_atk':       0,
                'rounds_with_pos': 0,
                'shape_site':      defaultdict(Counter),
                'pair_site':       defaultdict(Counter),
                'entry_frags':     defaultdict(lambda: {'total': 0, 'zones': Counter()}),
                'entry_rounds':    0,
                'player_agents':   {},   # {short_ign: agent_name}
            }
        r = result[map_name]

        # Build ign → agent name for this match
        agent_rows = conn.execute("""
            SELECT p.ign, mp.agent_id FROM match_players mp
            JOIN players p ON p.player_id = mp.player_id
            WHERE mp.match_id=? AND mp.team_number=?
        """, (mid, tnum)).fetchall()
        for ar in agent_rows:
            raw   = ar["ign"].split()[-1] if " " in ar["ign"] else ar["ign"]
            agent = _AGENT_NAMES.get(ar["agent_id"], f"Agent{ar['agent_id']}")
            # Store under both original case and uppercase so positions-table igns match
            r['player_agents'][raw]            = agent
            r['player_agents'][raw.upper()]    = agent
            r['player_agents'][raw.lower()]    = agent

        atk_rounds = conn.execute("""
            SELECT round_number, site_executed FROM round_states
            WHERE match_id=? AND team_number=? AND side='atk'
        """, (mid, tnum)).fetchall()
        r['total_atk'] += len(atk_rounds)
        site_by_round = {rnd["round_number"]: rnd["site_executed"] for rnd in atk_rounds}

        # ── Setup shape + player pairs ──────────────────────────────────────
        for rnum, site in site_by_round.items():
            frames = conn.execute("""
                SELECT player_ign, x, y FROM positions
                WHERE match_id=? AND round_number=? AND team_number=?
                  AND game_time_ms BETWEEN 8000 AND 12000
                ORDER BY game_time_ms
            """, (mid, rnum, tnum)).fetchall()

            if not frames:
                continue

            r['rounds_with_pos'] += 1

            seen = {}
            for f in frames:
                ign = f["player_ign"] or "?"
                if ign not in seen:
                    seen[ign] = (f["x"] or 0, f["y"] or 0)

            a_players, b_players = [], []
            for ign, (wx, wy) in seen.items():
                short = ign.split()[-1] if " " in ign else ign
                side  = _callout_side(_pos_to_callout(map_name, wx, wy))
                if side == "A":
                    a_players.append(short)
                elif side == "B":
                    b_players.append(short)

            na, nb = len(a_players), len(b_players)

            if na >= 4:
                shape = "A stack (4-5 A-side)"
            elif nb >= 4:
                shape = "B stack (4-5 B-side)"
            elif na >= 3 and nb >= 2:
                shape = "3A / 2B split"
            elif nb >= 3 and na >= 2:
                shape = "3B / 2A split"
            elif na == 3:
                shape = "3A + Mid/spawn"
            elif nb == 3:
                shape = "3B + Mid/spawn"
            else:
                shape = "Mixed / Mid-heavy"

            if site:
                r['shape_site'][shape][site] += 1

            # All pairs co-located on same side
            for side_label, group in [("A", sorted(a_players)), ("B", sorted(b_players))]:
                if len(group) < 2:
                    continue
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        key = (group[i], group[j], side_label)
                        if site:
                            r['pair_site'][key][site] += 1

        # ── Entry frags ─────────────────────────────────────────────────────
        team_pids = {row[0] for row in conn.execute(
            "SELECT player_id FROM match_players WHERE match_id=? AND team_number=?",
            (mid, tnum)
        ).fetchall()}

        if team_pids:
            fk_rows = conn.execute("""
                SELECT k.round_number, p.ign, k.victim_x, k.victim_y
                FROM kills k JOIN players p ON p.player_id = k.killer_id
                WHERE k.match_id=? AND k.side='atk' AND k.is_first_kill=1
                  AND k.killer_id IN ({})
            """.format(",".join("?" * len(team_pids))),
            [mid] + list(team_pids)).fetchall()

            atk_rnums = set(site_by_round)
            r['entry_rounds'] += len({row["round_number"] for row in fk_rows
                                       if row["round_number"] in atk_rnums})
            for row in fk_rows:
                if row["round_number"] not in atk_rnums:
                    continue
                short = (row["ign"].split()[-1] if " " in row["ign"] else row["ign"])
                zone  = _pos_to_callout(map_name, row["victim_x"] or 0, row["victim_y"] or 0)
                r['entry_frags'][short]['total'] += 1
                r['entry_frags'][short]['zones'][zone] += 1

    for map_name, r in result.items():
        r['shape_site']  = {k: dict(v) for k, v in r['shape_site'].items()}
        r['pair_site']   = {k: dict(v) for k, v in r['pair_site'].items()}
        r['entry_frags'] = {k: {'total': v['total'], 'zones': dict(v['zones'])}
                            for k, v in r['entry_frags'].items()}
    return result


def _format_pattern_analysis(pattern_data: dict) -> list[str]:
    lines = []
    lines.append("═" * 60)
    lines.append("OPENING PATTERN RECOGNITION")
    lines.append("Team setup → site executed  |  who leads entry duels")
    lines.append("═" * 60)

    for map_name, data in sorted(pattern_data.items()):
        total     = data['total_atk']
        with_pos  = data['rounds_with_pos']
        entry_rds = data['entry_rounds']
        agents    = data.get('player_agents', {})

        def tag(ign):
            """Return 'ign (Agent)' if agent is known, else just 'ign'."""
            a = agents.get(ign)
            return f"{ign} ({a})" if a else ign

        lines.append(f"\n  [{map_name}] — {with_pos} of {total} ATK rounds have 2D data\n")
        if with_pos == 0:
            lines.append("  No position data available for this map.")
            continue

        # 1. Team setup shapes
        shape_site = data['shape_site']
        if shape_site:
            lines.append("  HOW THEIR SETUP PREDICTS THE SITE:")
            for shape, sc in sorted(shape_site.items(), key=lambda x: -sum(x[1].values())):
                n    = sum(sc.values())
                top  = max(sc, key=lambda s: sc[s])
                topc = sc[top]
                rest = {s: c for s, c in sc.items() if s != top}
                times = "once" if n == 1 else f"{n} times"
                if topc == n:
                    lines.append(f"    Every time they ran a {shape} ({times}), they executed {top}.")
                elif topc > n // 2:
                    fake_site  = next(iter(rest))
                    fake_count = rest[fake_site]
                    fake_word  = "once" if fake_count == 1 else f"{fake_count} times"
                    lines.append(
                        f"    When they ran a {shape}, they went to {top} site "
                        f"{topc} out of {n} times — but they faked over to {fake_site} {fake_word}."
                    )
                else:
                    spread = " and ".join(f"{s} site ({sc[s]}x)" for s in sorted(sc, key=lambda s: -sc[s]))
                    lines.append(f"    A {shape} setup split between {spread} — hard to read from this alone.")
            lines.append("")

        # 2. Player pairs, top 4, min 2 appearances
        notable = sorted(
            [(k, v) for k, v in data['pair_site'].items() if sum(v.values()) >= 2],
            key=lambda x: -sum(x[1].values())
        )[:4]
        if notable:
            lines.append("  PLAYER COMBINATIONS TO WATCH:")
            for (p1, p2, side), sc in notable:
                n    = sum(sc.values())
                top  = max(sc, key=lambda s: sc[s])
                topc = sc[top]
                rest = {s: c for s, c in sc.items() if s != top}
                p1t, p2t = tag(p1), tag(p2)
                if topc == n:
                    lines.append(
                        f"    When {p1t} and {p2t} are both on {side}-side, "
                        f"they always execute {top} — this happened all {n} times we saw it."
                    )
                elif topc > n // 2:
                    fake_site  = next(iter(rest))
                    fake_count = rest[fake_site]
                    fake_word  = "once" if fake_count == 1 else f"{fake_count} times"
                    lines.append(
                        f"    If {p1t} and {p2t} are on {side}-side together, "
                        f"they usually go to {top} — that happened {topc} out of the {n} times we saw this. "
                        f"They faked to {fake_site} {fake_word}."
                    )
                else:
                    lines.append(
                        f"    {p1t} and {p2t} open {side}-side together regularly "
                        f"({n} times), but the execution splits evenly — not a reliable tell."
                    )
            lines.append("")

        # 3. Entry frags
        ef = data['entry_frags']
        if ef and entry_rds > 0:
            lines.append(f"  WHO MAKES FIRST CONTACT ({entry_rds} rounds tracked):")
            for ign, ed in sorted(ef.items(), key=lambda x: -x[1]['total']):
                n         = ed['total']
                pct       = n * 100 // entry_rds
                top_zones = [z for z, _ in sorted(ed['zones'].items(), key=lambda x: -x[1])[:2]]
                zone_str  = " and ".join(top_zones) if top_zones else "various locations"
                ign_t     = tag(ign)
                if pct >= 50:
                    lines.append(
                        f"    {ign_t} is their primary entry fragger — he found first blood "
                        f"in {n} out of {entry_rds} rounds. He almost always makes that contact "
                        f"at {zone_str}, so watch for him pushing there."
                    )
                elif pct >= 25:
                    lines.append(
                        f"    {ign_t} leads entry in about 1 out of every 3 rounds "
                        f"({n} of {entry_rds}), usually at {zone_str}."
                    )
                else:
                    lines.append(f"    {ign_t} found first blood once, at {zone_str}.")

    return lines


def _infer_site_from_centroid(map_name, cx, cy):
    """Return 'A', 'B', 'mid', or None based on map + centroid Y coordinate."""
    thresh = _MAP_SITE_THRESHOLDS.get((map_name or "").lower())
    if not thresh:
        return None
    if cy < thresh["b_max"]:
        return thresh["b_label"]
    if cy > thresh["a_min"]:
        return thresh["a_label"]
    return "mid"


def _analyze_2d_positions(conn, team_name, map_filter=None, series_id=None):
    """
    For each ATK round with position data, describe:
      - Opening setup (first 3s): which players go where
      - Late-round convergence (last 15s of round): where alive attackers cluster
    Returns a per-map dict of round descriptions.
    """
    sp2 = {"n": team_name, "sid": series_id} if series_id else {"n": team_name}
    sc2 = "AND m.series_id = :sid" if series_id else ""
    matches = conn.execute(f"""
        SELECT m.match_id, m.map_name,
               CASE WHEN LOWER(s.team1_name)=LOWER(:n) THEN 1 ELSE 2 END AS tnum
        FROM matches m JOIN series s ON s.series_id=m.series_id
        WHERE (LOWER(s.team1_name)=LOWER(:n) OR LOWER(s.team2_name)=LOWER(:n)) {sc2}
    """, sp2).fetchall()

    if map_filter:
        lf = [m.lower() for m in map_filter]
        matches = [m for m in matches if (m["map_name"] or "").lower() in lf]

    result = defaultdict(list)  # map_name → list of round description strings

    for match in matches:
        mid      = match["match_id"]
        map_name = match["map_name"] or "Unknown"
        tnum     = match["tnum"]

        atk_rounds = conn.execute("""
            SELECT rs.round_number, rs.site_executed, rs.economy_tier,
                   rs.won_round, rs.score_self, rs.score_opp
            FROM round_states rs
            WHERE rs.match_id=? AND rs.team_number=? AND rs.side='atk'
            ORDER BY rs.round_number
        """, (mid, tnum)).fetchall()

        for rnd in atk_rounds:
            rnum = rnd["round_number"]

            frames = conn.execute("""
                SELECT game_time_ms, player_ign, x, y, alive, has_spike
                FROM positions
                WHERE match_id=? AND round_number=? AND team_number=?
                ORDER BY game_time_ms
            """, (mid, rnum, tnum)).fetchall()

            if not frames:
                continue

            times = [f["game_time_ms"] for f in frames]
            max_t = max(times)

            def get_snapshot(target_ms, window=2500):
                """Best frame per player closest to target_ms."""
                by_player = {}
                for f in frames:
                    if abs(f["game_time_ms"] - target_ms) > window:
                        continue
                    ign = f["player_ign"] or "?"
                    if ign not in by_player or abs(f["game_time_ms"] - target_ms) < abs(by_player[ign]["game_time_ms"] - target_ms):
                        by_player[ign] = dict(f)
                return by_player

            # Opening positions (0-3s)
            opening = get_snapshot(1500)
            # Late round positions (last 15s, or mid-round if short)
            late_target = max(0, max_t - 8000)
            late        = get_snapshot(late_target, window=5000)

            econ   = (rnd["economy_tier"] or "?")[:4]
            result_str = "WIN" if rnd["won_round"] else "LOSS"
            score  = f"{rnd['score_self']}-{rnd['score_opp']}"
            site   = rnd["site_executed"] or "?"

            # Describe opening positions with callout names
            open_desc = ""
            if opening:
                positions = sorted(opening.values(), key=lambda p: p["y"] or 0)
                parts = []
                for p in positions:
                    ign  = (p["player_ign"] or "?").replace(f"{team_name.split()[0]} ", "")
                    x, y = p["x"] or 0, p["y"] or 0
                    callout = _pos_to_callout(map_name, x, y)
                    parts.append(f"{ign}@{callout}")
                open_desc = " | ".join(parts)

            # Describe late round (alive players only)
            late_desc = ""
            inferred_site = None
            if late:
                alive_late = {k: v for k, v in late.items() if v["alive"]}
                if alive_late:
                    xs = [v["x"] or 0 for v in alive_late.values()]
                    ys = [v["y"] or 0 for v in alive_late.values()]
                    cx, cy = sum(xs)/len(xs), sum(ys)/len(ys)
                    names = [k.replace(f"{team_name.split()[0]} ", "") for k in alive_late]
                    late_callout = _pos_to_callout(map_name, cx, cy)
                    late_desc = f"{len(alive_late)} alive [{','.join(names)}] converging → {late_callout}"
                    inferred_site = _infer_site_from_centroid(map_name, cx, cy)

            # Use inferred site from 2D centroid when kill-based site is unknown
            display_site = site if site and site != "?" else (inferred_site or "?")
            desc = f"R{rnum} [{econ}] {score} → {display_site} [{result_str}]"
            if open_desc:
                desc += f"\n      OPEN: {open_desc}"
            if late_desc:
                desc += f"\n      LATE: {late_desc}"

            result[map_name].append(desc)

    return dict(result)


# ── Tactical pre-analysis ────────────────────────────────────────────────────

def _derive_tactical_insights(team_name, map_pool, site_rows, timing_rows,
                               player_rows, series_count):
    """
    Derive plain-language tactical observations from the computed stats.
    These go into the LLM prompt as pre-analyzed facts so the LLM reasons
    about counter-strategies rather than just reading raw numbers.
    """
    lines = []
    total_maps = sum(m["maps_played"] for m in map_pool)

    lines.append(f"Sample size: {series_count} series | {total_maps} maps")
    lines.append("(All percentages below are computed from the DB — treat small samples as tendencies, not certainties)")
    lines.append("")

    # ── Attack timing ──────────────────────────────────────────────────────────
    times = [r["round_time_ms"] for r in timing_rows if r["round_time_ms"]]
    if times:
        n      = len(times)
        avg_s  = sum(times) / n / 1000
        rush   = sum(1 for t in times if t < 20_000)
        slow   = sum(1 for t in times if t >= 40_000)
        lines.append("ATTACK TEMPO:")
        if rush / n >= 0.55:
            lines.append(f"  → RUSH TEAM: {round(rush/n*100)}% of ATK rounds have first contact before 20s (avg {avg_s:.1f}s)")
            lines.append("    IMPLICATION: Don't play passive setups — be ready for contact at 10-15s")
        elif slow / n >= 0.4:
            lines.append(f"  → SLOW DEFAULT TEAM: {round(slow/n*100)}% of ATK rounds wait past 40s (avg {avg_s:.1f}s)")
            lines.append("    IMPLICATION: Hold your util, they will be patient — punish with proactive info")
        else:
            lines.append(f"  → MIXED PACE: avg first contact at {avg_s:.1f}s — varies by round state")

        # Per-map timing
        by_map: dict = defaultdict(list)
        for r in timing_rows:
            if r["round_time_ms"]:
                by_map[r["map_name"]].append(r["round_time_ms"])
        for map_name, mtimes in by_map.items():
            if len(mtimes) >= 3:
                mr = sum(1 for t in mtimes if t < 20_000)
                lines.append(f"    {map_name}: {round(mr/len(mtimes)*100)}% rush (<20s)")
        lines.append("")

    # ── Site execution ─────────────────────────────────────────────────────────
    if site_rows:
        by_map: dict = defaultdict(list)
        for r in site_rows:
            by_map[r["map_name"]].append(r)

        lines.append("SITE TENDENCIES:")
        for map_name in sorted(by_map.keys()):
            rows  = by_map[map_name]
            total = len(rows)
            site_counts: dict = defaultdict(int)
            site_wins:   dict = defaultdict(int)
            for r in rows:
                site_counts[r["site_executed"]] += 1
                if r["won_round"]:
                    site_wins[r["site_executed"]] += 1

            sorted_sites = sorted(site_counts, key=lambda s: -site_counts[s])
            lines.append(f"  {map_name} ({total} ATK rounds with site data):")
            for site in sorted_sites:
                cnt = site_counts[site]
                pct = round(cnt / total * 100)
                wr  = round(site_wins[site] / cnt * 100) if cnt else 0
                lines.append(f"    {site}-site: {pct}% ({cnt} rds) | {wr}% win rate")

            # Economy tells
            by_econ: dict = defaultdict(list)
            for r in rows:
                by_econ[r["economy_tier"]].append(r)
            for tier in ("eco", "half", "full"):
                er = by_econ.get(tier, [])
                if len(er) >= 2:
                    sc: dict = defaultdict(int)
                    for r in er:
                        sc[r["site_executed"]] += 1
                    dist = " / ".join(f"{s}: {round(c/len(er)*100)}%" for s, c in sorted(sc.items(), key=lambda x: -x[1]))
                    lines.append(f"    {tier.upper()} ({len(er)} rds): {dist}")

            # After-loss tendency
            after_loss = [r for r in rows if r["won_prev_round"] == 0]
            if len(after_loss) >= 3:
                sc2: dict = defaultdict(int)
                for r in after_loss:
                    sc2[r["site_executed"]] += 1
                primary = max(sc2, key=lambda s: sc2[s])
                pct2 = round(sc2[primary] / len(after_loss) * 100)
                if pct2 >= 60:
                    lines.append(f"    AFTER LOSS: they lean {primary}-site ({pct2}%) — predictable")
                else:
                    lines.append(f"    AFTER LOSS: site distribution balanced — no tilt tendency")
        lines.append("")

    # ── Player roles ───────────────────────────────────────────────────────────
    if player_rows:
        agg: dict = defaultdict(lambda: {"rounds": 0, "kills": 0.0, "deaths": 0.0,
                                          "fk": 0, "fd": 0, "plants": 0, "damage": 0.0})
        for r in player_rows:
            ign = r["ign"] or "Unknown"
            rds = r["rounds"] or 0
            agg[ign]["rounds"]  += rds
            agg[ign]["kills"]   += (r["avg_kills"]  or 0) * rds
            agg[ign]["deaths"]  += (r["avg_deaths"] or 0) * rds
            agg[ign]["fk"]      += r["total_fk"]    or 0
            agg[ign]["fd"]      += r["total_fd"]    or 0
            agg[ign]["plants"]  += r["total_plants"] or 0
            agg[ign]["damage"]  += (r["avg_damage"] or 0) * rds

        ranked = sorted(agg.items(), key=lambda x: -x[1]["damage"])
        lines.append("PLAYER ROLES (derived from stats):")

        for ign, s in ranked:
            rds = s["rounds"]
            if rds == 0:
                continue
            fk_pct  = round(s["fk"]     / rds * 100)
            fd_pct  = round(s["fd"]     / rds * 100)
            avg_dmg = round(s["damage"] / rds)
            avg_k   = s["kills"]  / rds
            avg_d   = s["deaths"] / rds

            # Role inference
            if fk_pct >= 20 and fd_pct >= 20:
                role = "ENTRY FRAGGER — opens site, dies often but generates first blood"
            elif fk_pct >= 20 and fd_pct < 15:
                role = "LURK / STAR — high impact, avoids first contact"
            elif s["plants"] >= 4 and fd_pct >= 18:
                role = "IGL / BOMB CARRIER — plants often, plays for the team"
            elif fd_pct >= 22:
                role = "SACRIFICIAL ENTRY — trades their life for space"
            elif avg_dmg >= 150:
                role = "CARRY — high damage, likely star player"
            else:
                role = "SUPPORT / ANCHOR"

            lines.append(
                f"  {ign}: {avg_dmg} dmg/rd | FK {fk_pct}% | FD {fd_pct}% | {s['plants']} plants"
                f" → {role}"
            )

        # Who to prioritize shutting down
        top_carry = max(ranked, key=lambda x: x[1]["damage"] / max(x[1]["rounds"], 1))
        top_entry = max(ranked, key=lambda x: x[1]["fk"] / max(x[1]["rounds"], 1))
        lines.append("")
        lines.append(f"  SHUT DOWN PRIORITY: {top_carry[0]} (highest damage output)")
        if top_entry[0] != top_carry[0]:
            lines.append(f"  ENTRY TO TRADE: {top_entry[0]} (highest first-kill rate) — "
                         "if FURIA wins this duel, the execute stalls")
        lines.append("")

    return "\n".join(lines)


# ── Context builder ───────────────────────────────────────────────────────────

def build_context(team_name, map_filter=None, series_id=None):
    conn = db()

    sid = series_id
    map_pool     = _get_map_pool(conn, team_name, sid)
    site_rows    = _get_site_rows(conn, team_name, sid)
    timing_rows  = _get_timing_rows(conn, team_name, sid)
    player_rows  = _get_player_rows(conn, team_name, sid)
    weapon_rows  = _get_weapon_rows(conn, team_name, sid)
    series_count = _get_series_count(conn, team_name, sid)
    series_meta  = _get_series_meta(conn, sid) if sid else {}
    pos_analysis  = _analyze_2d_positions(conn, team_name, map_filter, sid)
    route_data    = _analyze_player_routes(conn, team_name, map_filter, sid)
    pattern_data  = _analyze_opening_patterns(conn, team_name, map_filter, sid)

    conn.close()

    if not map_pool:
        return (f"No data found for '{team_name}'.\n"
                f"Run: python collect.py --team \"{team_name}\" --last 7")

    if map_filter:
        lf = [m.lower() for m in map_filter]
        map_pool   = [m for m in map_pool   if (m["map_name"] or "").lower() in lf]
        site_rows  = [r for r in site_rows  if (r["map_name"] or "").lower() in lf]
        timing_rows= [r for r in timing_rows if (r["map_name"] or "").lower() in lf]

    total_maps = sum(m["maps_played"] for m in map_pool)
    total_wins = sum(m["wins"]        for m in map_pool)

    lines = []
    lines.append(f"TEAM: {team_name.upper()}")
    lines.append(f"DATA: {series_count} series | {total_maps} maps | "
                 f"{total_wins}W-{total_maps - total_wins}L map record")
    lines.append("")

    # ── Conditional tendencies (ranked by significance) — LEAD with this ─────
    lines.append("═" * 60)
    lines.append("CONDITIONAL TENDENCIES  (strongest decision patterns, ranked by z-score)")
    lines.append("Format: Given [condition] -> executes [site] X%  (baseline Y%, n=rounds)")
    lines.append("New conditions: first-contact location, per-player FB, contact timing, kill lead.")
    lines.append("Only statistically significant patterns shown (n>=4, delta>=10pp, z>=1.2).")
    lines.append("═" * 60)
    _single_map = map_filter[0] if map_filter and len(map_filter) == 1 else None
    _cond_data  = _insight_engine.find_insights(team_name, _single_map)
    if _cond_data and _cond_data.get("insights"):
        _ins_list = _cond_data["insights"]
        lines.append(
            f"  {_cond_data['total']} ATK rounds  |  "
            f"Baseline A: {_cond_data['baseline_a']}%  /  B: {_cond_data['baseline_b']}%  |  "
            f"Plant rate: {_cond_data['baseline_plant_rate']}%"
        )
        for ins in _ins_list:
            stars = ins["stars"].replace("★", "*")
            plant_note = (
                f"  plant {ins['plant_rate']}% (base {ins['baseline_plant_rate']}%)"
                if abs(ins['plant_rate'] - ins['baseline_plant_rate']) >= 10 else ""
            )
            lines.append(
                f"  #{ins['rank']:2d} [{ins['category']}]  "
                f"Given {ins['label']}"
            )
            lines.append(
                f"      -> {ins['site']} site {ins['conditional_pct']}%  "
                f"(baseline {ins['baseline_pct']}%, n={ins['n']}, "
                f"{'+' if ins['delta'] > 0 else ''}{ins['delta']}pp, z={ins['z']})"
                f"{plant_note}"
            )
    else:
        lines.append("  No significant conditional tendencies found in current dataset.")
    lines.append("")

    # ── Map pool ──────────────────────────────────────────────────────────────
    lines.append("MAP POOL:")
    for m in map_pool:
        n  = m["maps_played"]
        wr = round(m["wins"] / n * 100) if n else 0
        lines.append(f"  {m['map_name']}: {n} played | {wr}% win rate")
    lines.append("")

    # ── Tactical pre-analysis ─────────────────────────────────────────────────
    tactical = _derive_tactical_insights(
        team_name, map_pool, site_rows, timing_rows, player_rows, series_count
    )
    lines.append("═" * 60)
    lines.append("PRE-ANALYZED TACTICAL OBSERVATIONS")
    lines.append("═" * 60)
    lines.append(tactical)

    # ── Weapon tendencies ─────────────────────────────────────────────────────
    atk_weapons: dict = defaultdict(int)
    def_weapons: dict = defaultdict(int)
    for r in weapon_rows:
        if r["side"] == "atk":
            atk_weapons[r["weapon"]] += r["uses"]
        else:
            def_weapons[r["weapon"]] += r["uses"]

    if atk_weapons:
        top_atk = sorted(atk_weapons.items(), key=lambda x: -x[1])[:5]
        lines.append("ATK WEAPONS: " + " | ".join(f"{w}: {c}" for w, c in top_atk))
    if def_weapons:
        top_def = sorted(def_weapons.items(), key=lambda x: -x[1])[:5]
        lines.append("DEF WEAPONS: " + " | ".join(f"{w}: {c}" for w, c in top_def))
    lines.append("")

    # ── Opening pattern → site execute correlation ────────────────────────────
    if pattern_data:
        for line in _format_pattern_analysis(pattern_data):
            lines.append(line)
        lines.append("")

    # ── 2D position analysis (per-round detail) ───────────────────────────────
    if pos_analysis:
        lines.append("═" * 60)
        lines.append("2D REPLAY — PER-ROUND SETUPS (ATK rounds, named callouts)")
        lines.append("═" * 60)
        for map_name, round_descs in sorted(pos_analysis.items()):
            lines.append(f"  [{map_name}]")
            for desc in round_descs:
                lines.append(f"    {desc}")
            lines.append("")
    else:
        lines.append("(No 2D position data — run collection to populate)")

    # ── Q1: Ult availability ──────────────────────────────────────────────────
    ult_res = _questions.ult_behavior(team_name, _single_map)
    lines.append("═" * 60)
    lines.append("ULT PATTERNS  [status: " + ult_res["status"] + "]")
    lines.append("═" * 60)
    if ult_res["findings"]:
        lines.append(f"  Baseline: A {ult_res['baseline_a']}%  B {ult_res['baseline_b']}%"
                     f"  |  ATK rounds: {ult_res['atk_rounds']}")
        for f in ult_res["findings"]:
            lines.append(f"  {f['label']}  —  {f['ult_n']} rounds with ult")
            lines.append(f"    Site: A {f['a_pct']}%  B {f['b_pct']}%"
                         f"  (baseline A {f['baseline_a']}%  B {f['baseline_b']}%,"
                         f" Δ {'+' if f['delta_a'] >= 0 else ''}{f['delta_a']}pp A)")
            if f["fake_rate"] is not None and f["baseline_fake"] is not None:
                lines.append(f"    Fake rate: {f['fake_rate']}%"
                             f"  (baseline {f['baseline_fake']}%, n={f['fake_n']})")
    else:
        lines.append(f"  {ult_res['reason']}")
    lines.append("")

    # ── Q2: One orb off ult ───────────────────────────────────────────────────
    orb_res = _questions.one_orb_off_ult(team_name, _single_map)
    lines.append("═" * 60)
    lines.append("ONE ORB OFF ULT PATTERNS  [status: " + orb_res["status"] + "]")
    lines.append("═" * 60)
    lines.append(f"  {orb_res['reason']}")
    lines.append("")

    # ── Q4/Q5: Defensive weapon patterns ─────────────────────────────────────
    for wpn in ("Judge", "Operator"):
        wpn_res = _questions.weapon_def_patterns(team_name, wpn, _single_map)
        lines.append("═" * 60)
        lines.append(f"DEFENSIVE {wpn.upper()} PATTERNS  [status: {wpn_res['status']}]")
        lines.append("═" * 60)
        if wpn_res["findings"]:
            lines.append(f"  Total DEF kills with {wpn}: {wpn_res['total_kills']}")
            for f in wpn_res["findings"]:
                zones_str = "  ".join(
                    f"{z} {n}×" for z, n in
                    sorted(f["zones"].items(), key=lambda x: -x[1])
                )
                lines.append(f"  {f['label']}  on {f['map']}:  {f['total']} kills")
                lines.append(f"    Zone: {zones_str}")
                lines.append(f"    Avg timing: {f['avg_timing']}s into round")
        else:
            lines.append(f"  {wpn_res['reason']}")
        lines.append("")

    # ── Q3: Defensive stacking ────────────────────────────────────────────────
    stack_res = _questions.def_stacking(team_name, _single_map)
    lines.append("═" * 60)
    lines.append("LOW-MONEY DEFENSIVE STACKS  [status: " + stack_res["status"] + "]")
    lines.append("═" * 60)
    if stack_res["findings"]:
        for map_n, counts in stack_res["findings"].items():
            total = counts["total"]
            lines.append(f"  {map_n}  (n={total} eco/half DEF rounds with positions)")
            for cls in ("A-heavy", "B-heavy", "Mid-heavy", "Default"):
                n = counts[cls]
                if n:
                    pct = round(n / total * 100)
                    lines.append(f"    {cls:<12}  {n}/{total}  ({pct}%)")
    else:
        lines.append(f"  {stack_res['reason']}")
    lines.append("")

    return "\n".join(lines)


# ── Ollama report ─────────────────────────────────────────────────────────────

def _load_format_example():
    """Load the coaching format template to use as a few-shot example."""
    path = EXAMPLES_DIR / "format_template.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


SYSTEM_PROMPT = (
    "You are an elite Valorant esports analyst preparing match-prep anti-strat briefs "
    "for a VCT-level professional team. "
    "You write in the direct, tactical style used by professional coaching staffs: "
    "specific setups, named positions, WATCH OUT calls, and actionable counter-strategies. "
    "You NEVER invent statistics — every claim is grounded in the provided data. "
    "You DO use your knowledge of Valorant maps to translate X/Y coordinates into "
    "named positions (A site, B main, mid link, short, etc.)."
)


def generate_report(team_name, context, model="qwen2.5:14b", for_team="FURIA"):
    try:
        import ollama
    except ImportError:
        return "ERROR: ollama not installed. Run: pip install ollama"

    # Extract which maps are in this brief so we can name them explicitly
    map_lines = [l.strip() for l in context.splitlines() if l.strip().startswith("MAP POOL:")]
    maps_in_context = []
    in_map_pool = False
    for line in context.splitlines():
        if "MAP POOL:" in line:
            in_map_pool = True
            continue
        if in_map_pool:
            stripped = line.strip()
            if stripped and ":" in stripped:
                maps_in_context.append(stripped.split(":")[0].strip())
            elif not stripped:
                break
    maps_str = ", ".join(maps_in_context) if maps_in_context else "each map in the data"

    prompt = f"""You are writing an anti-strat brief on {team_name} for {for_team}'s coaching staff. Maps: {maps_str}.

The brief answers two questions:
1. "[Map] CT" = {team_name} is ATTACKING. How should {for_team} DEFEND against them?
2. "[Map] ATK" = {team_name} is DEFENDING (CT side). How should {for_team} ATTACK into them?

DATA ON {team_name.upper()}:
{context}

---

Write the brief now. One section per map. Use exactly this structure:

[MAP NAME] CT  (= {team_name} attacking, {for_team} defending)
WATCH OUT: [specific player name + exact callout position + what they do there and how often, e.g. "brawk opens B Main in 8/12 rounds (67%) — sets up a split with skuba at B Link"]
WATCH OUT: [another specific pattern with player name, callout, and frequency]
WATCH OUT: [another]
WATCH OUT: [another]
PRIORITY: [the single most critical pattern {for_team} must have an answer for — name the player, position, and timing]
Good call: [one specific counter with named positions, e.g. "stack B Hall early before brawk gets set up — send two to B Hall at 10s and deny the lane"]

[MAP NAME] ATK  (= {team_name} defending, {for_team} attacking)
WATCH OUT: [specific player name + where they hold on CT side + what angle/weapon — pulled from the data]
WATCH OUT: [another CT pattern]
WATCH OUT: [another]
WATCH OUT: [another]
PRIORITY: [the single most important CT adjustment — which player is most dangerous and where]
Good call: [specific way {for_team} can exploit NRG's CT setup — name the callout and the approach]

Rules:
- Only write about {maps_str}. Do NOT write about other maps.
- Use EXACT Pearl callout names from the data: B Hall, B Screen, B Main, B Ramp, B site, B Tower, B Tunnel, B Link, Mid Connector, Mid Doors, Mid Plaza, Mid Shops, A Link, A Art, A site, A Flowers, A Secret, A Dugout, A Main, A Restaurant.
- Name players by short IGN (e.g. JAWGEMO, trent, leaf, BABYBAY, valyn).
- ALWAYS cite statistics from CONDITIONAL TENDENCIES, OPENING PATTERN RECOGNITION, ULT AVAILABILITY, DEFENSIVE WEAPON PATTERNS, or DEFENSIVE STACKING sections.
- For CT sections: reference DEFENSIVE WEAPON PATTERNS (Judge/Operator zones and timing) and DEFENSIVE STACKING (low-economy stack tendencies).
- For ATK sections: reference ULT AVAILABILITY (which player's ult shifts site execution) alongside CONDITIONAL TENDENCIES.
- Every WATCH OUT must reference a specific pattern or frequency from the data.
- Do NOT invent numbers. If data is limited, say "small sample" but still use what's there.
- No generic advice — every sentence must be actionable and specific to THIS team's patterns."""

    print(f"\nAsking {model} to generate the report... (may take 30-90s)\n")

    try:
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system",  "content": SYSTEM_PROMPT},
                {"role": "user",    "content": prompt},
            ],
            options={"temperature": 0.4, "num_predict": 2000},
        )
        return response["message"]["content"]
    except Exception as e:
        return f"ERROR calling Ollama: {e}\nMake sure Ollama is running: ollama serve"


# ── No-LLM structured report ─────────────────────────────────────────────────

def generate_report_no_llm(team_name, context_str, for_team="FURIA", map_filter=None):
    """
    Produce a coaching-format anti-strat brief in pure Python from DB data.
    Used when Ollama is unavailable or OOM.
    """
    import re

    conn = db()
    map_pool     = _get_map_pool(conn, team_name)
    timing_rows  = _get_timing_rows(conn, team_name)
    player_rows  = _get_player_rows(conn, team_name)
    weapon_rows  = _get_weapon_rows(conn, team_name)
    pos_analysis = _analyze_2d_positions(conn, team_name, map_filter)
    conn.close()

    if map_filter:
        lf = [m.lower() for m in map_filter]
        map_pool    = [m for m in map_pool    if (m["map_name"] or "").lower() in lf]
        timing_rows = [r for r in timing_rows if (r["map_name"] or "").lower() in lf]

    # ── Aggregate player stats across both sides ──────────────────────────────
    agg: dict = defaultdict(lambda: {"rounds": 0, "fk": 0, "fd": 0,
                                      "plants": 0, "damage": 0.0})
    for r in player_rows:
        ign = r["ign"] or "Unknown"
        rds = r["rounds"] or 0
        agg[ign]["rounds"]  += rds
        agg[ign]["fk"]      += r["total_fk"]     or 0
        agg[ign]["fd"]      += r["total_fd"]     or 0
        agg[ign]["plants"]  += r["total_plants"] or 0
        agg[ign]["damage"]  += (r["avg_damage"]  or 0) * rds

    ranked = sorted(agg.items(), key=lambda x: -(x[1]["damage"] / max(x[1]["rounds"], 1)))
    carry_ign   = ranked[0][0]   if ranked else ""
    entry_ign   = max(agg, key=lambda k: agg[k]["fk"] / max(agg[k]["rounds"], 1)) if agg else ""
    planter_ign = max(agg, key=lambda k: agg[k]["plants"]) if agg else ""

    # ── Attack tempo ──────────────────────────────────────────────────────────
    times    = [r["round_time_ms"] for r in timing_rows if r["round_time_ms"]]
    rush_pct = round(sum(1 for t in times if t < 20_000) / len(times) * 100) if times else 0

    # ── Operator usage on CT ──────────────────────────────────────────────────
    def_ops = sum(r["uses"] for r in weapon_rows
                  if r["side"] == "def" and r["weapon"] == "Operator")

    out = []
    out.append(f"NRG ESPORTS — {for_team.upper()} MATCH PREP (Breeze)")
    out.append("=" * 60)
    out.append("")

    # ── Player breakdown ──────────────────────────────────────────────────────
    out.append("PLAYERS")
    out.append("-" * 40)
    for ign, s in ranked:
        rds = max(s["rounds"], 1)
        dmg = round(s["damage"] / rds)
        fk  = round(s["fk"]    / rds * 100)
        fd  = round(s["fd"]    / rds * 100)
        tag = ("CARRY"  if ign == carry_ign  else
               "ENTRY"  if ign == entry_ign  else
               "ANCHOR" if ign == planter_ign else "SUPPORT")
        out.append(f"  {ign:12s}  {dmg} dmg/rd  FK {fk}%  FD {fd}%  "
                   f"plants {s['plants']}  [{tag}]")
    out.append("")

    # ── Per-map brief ─────────────────────────────────────────────────────────
    for m in map_pool:
        map_name = m["map_name"]
        out.append(f"MAP: {map_name.upper()}")
        out.append("=" * 60)

        # Count site visits from 2D inferred labels
        site_counts: dict = defaultdict(int)
        site_wins:   dict = defaultdict(int)
        for desc in pos_analysis.get(map_name, []):
            first_line = desc.split("\n")[0]
            hit = re.search(r"→ ([A-Za-z?]+) \[(WIN|LOSS)\]", first_line)
            if hit and hit.group(1) not in ("?", "mid"):
                site_counts[hit.group(1)] += 1
                if hit.group(2) == "WIN":
                    site_wins[hit.group(1)] += 1

        total_2d = sum(site_counts.values())

        # --- CT SECTION ---
        out.append("")
        out.append(f"CT — Defending {map_name}")
        out.append("-" * 40)
        out.append(f"WATCH OUT: {rush_pct}% rush rate — first contact hits before 20s. "
                   "Don't over-extend; take default setups and react off early info.")

        if total_2d:
            b_cnt = site_counts.get("B", 0)
            a_cnt = site_counts.get("A", 0)
            b_pct = round(b_cnt / total_2d * 100)
            a_pct = round(a_cnt / total_2d * 100)
            b_wr  = round(site_wins.get("B", 0) / b_cnt * 100) if b_cnt else 0
            a_wr  = round(site_wins.get("A", 0) / a_cnt * 100) if a_cnt else 0

            if b_pct >= 55:
                out.append(f"WATCH OUT: B-heavy execution — {b_pct}% of rounds converge B-site "
                           f"({b_cnt} rounds, {b_wr}% win rate). Stack 3 on B, keep one player "
                           "cutting caves/mid and one soft-anchor A. Don't let them walk in free.")
            elif a_pct >= 55:
                out.append(f"WATCH OUT: A-side execution — {a_pct}% of rounds push A "
                           f"({a_cnt} rounds, {a_wr}% win rate). Stack A main; hold B with passive 2-man.")
            else:
                out.append(f"WATCH OUT: Split execution — B {b_pct}% / A {a_pct}%. "
                           "Don't commit hard to either site early. Play for info and rotate.")

        out.append(f"WATCH OUT: {entry_ign} is their opener ({round(agg[entry_ign]['fk'] / max(agg[entry_ign]['rounds'],1)*100)}% FK). "
                   f"If he wins the first duel, the execute is live. Win this trade and the round stalls.")

        if def_ops >= 5:
            out.append(f"WATCH OUT: {def_ops} Op kills on CT across these maps. "
                       "They play long hold angles — don't dry-peek; smoke first.")

        out.append(f"PRIORITY: Neutralize {carry_ign} early. "
                   f"He outputs {round(agg[carry_ign]['damage']/max(agg[carry_ign]['rounds'],1))} dmg/rd "
                   f"and wins rounds alone. Prioritize him in post-plant and peak-clear situations.")
        out.append("")

        # --- ATK SECTION ---
        out.append(f"ATK — Attacking {map_name}")
        out.append("-" * 40)

        if def_ops >= 5:
            out.append(f"WATCH OUT: {def_ops} Op kills on their CT. Long-range angles — B main catwalk, "
                       "A main ramp. Pop-flash and smoke before committing. Don't trade Op for rifles.")

        if total_2d and len(site_counts) > 1:
            dominant  = max(site_counts, key=lambda s: site_counts[s])
            neglected = min(site_counts, key=lambda s: site_counts[s])
            dom_pct   = round(site_counts[dominant]  / total_2d * 100)
            neg_pct   = round(site_counts[neglected] / total_2d * 100)
            if dom_pct - neg_pct >= 20:  # only flag if meaningful gap
                out.append(f"Good call: NRG neglects {neglected}-site ({neg_pct}%, {site_counts[neglected]} rounds). "
                           f"FURIA can fake {neglected} to pull their rotation, "
                           f"then execute {dominant}-side where NRG has committed their firepower.")

        out.append(f"Good call: {planter_ign} plants in {agg[planter_ign]['plants']} rounds — "
                   "give him time and space post-entry. Don't rush the plant.")
        out.append(f"PRIORITY: Slow NRG's CT pace. They thrive in 3v5 scrambles after an early pick. "
                   f"Win mid control first (deny caves), then execute clean with utility.")
        out.append("")

    return "\n".join(out)


# ── Document printer ─────────────────────────────────────────────────────────

def _wrap(text: str, width: int = 64, indent: str = "  ") -> list[str]:
    """Word-wrap text to width, returning lines with indent applied."""
    words = text.split()
    lines, line = [], ""
    for w in words:
        if len(line) + len(w) + 1 > width and line:
            lines.append(indent + line)
            line = w
        else:
            line = (line + " " + w).lstrip()
    if line:
        lines.append(indent + line)
    return lines


def print_document(data: dict, team: str, vs: str,
                   series_meta: dict, map_filter=None):
    """
    Three-section report:  STRONGEST TENDENCIES  /  RECURRING PATTERNS  /  SUPPORTING EVIDENCE
    Takes the dict returned by insights.find_insights() directly — no context string needed.
    """
    W = 70
    def rule(c="="): print(c * W)
    def thin():      print("-" * W)
    def blank():     print()
    def section(txt):
        blank(); rule(); print(f"  {txt}"); thin(); blank()

    insights_list = data.get("insights", [])
    patterns      = data.get("patterns", [])
    breakdowns    = data.get("breakdowns", {})
    total         = data["total"]
    n_a, n_b      = data["n_a"], data["n_b"]
    ba, bb        = data["baseline_a"], data["baseline_b"]
    plant_rate    = data["baseline_plant_rate"]
    map_label     = ", ".join(map_filter) if map_filter else data.get("map", "")

    # ── Header ────────────────────────────────────────────────────────────────
    rule()
    title = f"MATCH ANALYSIS  —  {team.upper()}"
    print(" " * ((W - len(title)) // 2) + title)
    if series_meta:
        t1, t2 = series_meta.get("team1_name",""), series_meta.get("team2_name","")
        evt    = series_meta.get("event_name","")
        date   = (series_meta.get("start_date") or "")[:10]
        sub    = f"{t1}  vs  {t2}   |   {evt}   |   {date}"
        print(" " * max((W - len(sub)) // 2, 0) + sub)
    print(f"  {map_label}  ·  {total} ATK rounds  ·  "
          f"Baseline A {ba}%  B {bb}%")
    rule()

    # ── Section 1: STRONGEST TENDENCIES ──────────────────────────────────────
    section("STRONGEST TENDENCIES")

    if not insights_list:
        print("  No significant tendencies found.")
        print(f"  (Requires n≥5 per condition, ≥10pp swing from baseline, z≥1.2)")
        blank()
    else:
        for ins in insights_list:
            conf    = ins["confidence"]
            outcome = f"→ {ins['site']} Finish  {ins['conditional_pct']}%"
            meta    = (f"Baseline {ins['baseline_pct']}%"
                       f"  ·  {ins['n']} rounds"
                       f"  ·  Confidence: {conf}")
            label   = ins["label"][0].upper() + ins["label"][1:]
            print(f"  {ins['rank']}.  {label}")
            print(f"     {outcome}")
            print(f"     {meta}")
            blank()

    # ── Section 2: RECURRING PATTERNS ────────────────────────────────────────
    section("RECURRING PATTERNS")

    if not patterns:
        print("  Insufficient data to derive patterns.")
        blank()
    else:
        for sent in patterns:
            for i, line in enumerate(_wrap(sent, width=64)):
                prefix = "  *" if i == 0 else "   "
                print(prefix + line)
            blank()

    # ── Section 3: SUPPORTING EVIDENCE ───────────────────────────────────────
    section("SUPPORTING EVIDENCE")

    print(f"  Baseline: A {ba}% ({n_a}/{total} rounds)"
          f"  |  B {bb}% ({n_b}/{total} rounds)"
          f"  |  Plant rate {plant_rate}%")
    blank()

    for bd_name, bd in breakdowns.items():
        label  = bd.get("label", bd_name)
        values = bd.get("data", {})
        if not values:
            continue
        print(f"  {label}")
        for key, counts in values.items():
            n_sub = counts["n"]
            a_cnt = counts["a"]
            b_cnt = counts["b"]
            a_pct = round(a_cnt / n_sub * 100)
            b_pct = 100 - a_pct
            if a_cnt > b_cnt:
                lead = f"{a_pct}% A"
            elif b_cnt > a_cnt:
                lead = f"{b_pct}% B"
            else:
                lead = "50/50"
            note = "  ⚠ small sample" if n_sub < 5 else ""
            print(f"    {key:<14}  n={n_sub:2d}:  "
                  f"A {a_cnt:2d}  B {b_cnt:2d}   →  {lead}{note}")
        blank()

    rule()
    blank()

    _single_map = map_filter[0] if map_filter and len(map_filter) == 1 else None

    def _status_line(status: str) -> str:
        labels = {
            "found":                "Found",
            "no_results":           "No Results",
            "insufficient_sample":  "Insufficient Sample",
            "data_missing":         "Data Missing",
            "not_implemented":      "Not Implemented",
        }
        return f"  Status: {labels.get(status, status)}"

    # ── Section 4: ULT-BASED TENDENCIES ──────────────────────────────────────
    ult_res = _questions.ult_behavior(team, _single_map)
    section("ULT PATTERNS")
    print(_status_line(ult_res["status"]))
    blank()
    if ult_res["status"] != "found":
        print(f"  Reason:")
        for line in _wrap(ult_res["reason"], width=62):
            print(line)
        if ult_res.get("ult_counts"):
            nonzero = {p: n for p, n in ult_res["ult_counts"].items() if n > 0}
            if nonzero:
                blank()
                print(f"  Ult rounds detected (below threshold of {ult_res['threshold']}):")
                for p, n in sorted(nonzero.items(), key=lambda x: -x[1]):
                    print(f"    {p}: {n} round(s)")
    else:
        print(f"  Baseline: A {ult_res['baseline_a']}%  B {ult_res['baseline_b']}%"
              f"  |  ATK rounds: {ult_res['atk_rounds']}")
        blank()
        for f in ult_res["findings"]:
            print(f"  {f['label']}  ({f['ult_n']} rounds with ult)")
            if f.get("round_log"):
                print(f"  Rounds fired: {', '.join(f['round_log'])}")
            print(f"  Ult available: A {f['a_pct']}%  B {f['b_pct']}%")
            print(f"  Baseline:      A {f['baseline_a']}%  B {f['baseline_b']}%"
                  f"  (Δ {'+' if f['delta_a'] >= 0 else ''}{f['delta_a']}pp"
                  f" toward {'A' if f['delta_a'] >= 0 else 'B'})")
            if f["fake_rate"] is not None and f["baseline_fake"] is not None:
                print(f"  Fake rate: {f['fake_rate']}%"
                      f"  (baseline {f['baseline_fake']}%, n={f['fake_n']})")
            blank()
    rule()
    blank()

    # ── Section 5: ONE ORB OFF ULT ────────────────────────────────────────────
    orb_res = _questions.one_orb_off_ult(team, _single_map)
    section("ONE ORB OFF ULT PATTERNS")
    print(_status_line(orb_res["status"]))
    blank()
    print(f"  Reason:")
    for line in _wrap(orb_res["reason"], width=62):
        print(line)
    rule()
    blank()

    # ── Section 6: DEFENSIVE WEAPON PATTERNS ─────────────────────────────────
    for wpn in ("Judge", "Operator"):
        wpn_res = _questions.weapon_def_patterns(team, wpn, _single_map)
        section(f"DEFENSIVE {wpn.upper()} PATTERNS")
        print(_status_line(wpn_res["status"]))
        blank()
        if wpn_res["status"] != "found":
            print(f"  Reason:")
            for line in _wrap(wpn_res["reason"], width=62):
                print(line)
        else:
            print(f"  Total DEF kills with {wpn}: {wpn_res['total_kills']}")
            blank()
            for f in wpn_res["findings"]:
                zones_str = "  ".join(
                    f"{z} {n}×" for z, n in
                    sorted(f["zones"].items(), key=lambda x: -x[1])
                )
                small = "  ⚠ small sample" if f["small"] else ""
                print(f"  {f['label']}  —  {f['map']}  ({f['total']} kills{small})")
                print(f"  Zone: {zones_str}")
                print(f"  Avg timing: {f['avg_timing']}s into round")
                blank()
        rule()
        blank()

    # ── Section 7: DEFENSIVE STACKING ────────────────────────────────────────
    stack_res = _questions.def_stacking(team, _single_map)
    section("LOW-MONEY DEFENSIVE STACKS")
    print(_status_line(stack_res["status"]))
    blank()
    if stack_res["status"] != "found":
        print(f"  Reason:")
        for line in _wrap(stack_res["reason"], width=62):
            print(line)
        if stack_res.get("eco_half_total", 0) > 0:
            blank()
            print(f"  Eco/half DEF rounds found: {stack_res['eco_half_total']}")
            print(f"  Rounds with position data: {stack_res.get('rounds_with_pos', 0)}")
            if stack_res.get("maps_no_pos"):
                print(f"  Maps without positions: {', '.join(stack_res['maps_no_pos'])}")
    else:
        for map_n, counts in stack_res["findings"].items():
            total = counts["total"]
            print(f"  {map_n}  (n={total} eco/half rounds with position data)")
            for cls in ("A-heavy", "B-heavy", "Mid-heavy", "Default"):
                n = counts[cls]
                if n:
                    pct = round(n / total * 100)
                    print(f"    {cls:<12}  {n}/{total}  ({pct}%)")
            blank()
    rule()
    blank()


# ── Legacy context-based document printer (used only by --brief mode) ─────────

def _print_brief(context: str, report: str, team: str, vs: str,
                 series_meta: dict, map_filter=None):
    """Append the LLM-generated counter-strat brief to stdout."""
    W = 70
    def rule(c="="): print(c * W)
    def thin():      print("-" * W)
    def blank():     print()

    rule()
    title = f"COUNTER-STRAT BRIEF  |  Prepared for {vs.upper()}"
    print(" " * max((W - len(title)) // 2, 0) + title)
    rule(); blank()

    for line in report.splitlines():
        s = line.strip()
        if not s:
            blank()
        elif s.startswith("###"):
            blank(); thin()
            print(f"  {s.replace('###','').strip().upper()}")
            thin()
        elif s.startswith("WATCH OUT:"):
            print(f"    [!] {s[len('WATCH OUT:'):].strip()}")
        elif s.startswith("PRIORITY:"):
            blank(); print(f"  >> PRIORITY: {s[len('PRIORITY:'):].strip()}")
        elif s.startswith("Good call:"):
            print(f"  >> GOOD CALL: {s[len('Good call:'):].strip()}")
        else:
            print(f"  {s}")
    blank(); rule()


# ── HTML report ──────────────────────────────────────────────────────────────

_HTML_CSS = """
:root {
  --bg: #0d0f14;
  --surface: #161a24;
  --surface2: #1e2433;
  --border: #2a3045;
  --accent: #e8383d;
  --accent2: #ff6b35;
  --gold: #f0c040;
  --green: #3ecf8e;
  --blue: #4da6ff;
  --muted: #6b7a99;
  --text: #dce3f0;
  --text2: #a8b4cc;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Segoe UI', system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.6;
  padding: 24px 16px 60px;
}
.report-wrap { max-width: 960px; margin: 0 auto; }

/* Header */
.report-header {
  background: linear-gradient(135deg, #1a0a0b 0%, #0d1a2e 100%);
  border: 1px solid var(--accent);
  border-radius: 10px;
  padding: 28px 32px;
  margin-bottom: 24px;
}
.report-header h1 {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 2px;
  color: #fff;
  text-transform: uppercase;
}
.report-header .sub {
  color: var(--text2);
  font-size: 13px;
  margin-top: 6px;
}
.report-header .meta-pills {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
}
.pill {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 4px 12px;
  font-size: 12px;
  color: var(--text2);
}
.pill strong { color: var(--text); }

/* Sections */
.section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 18px;
  overflow: hidden;
}
.section-header {
  background: var(--surface2);
  border-bottom: 1px solid var(--border);
  padding: 12px 20px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.section-header h2 {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--text);
}
.section-number {
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.section-body { padding: 16px 20px; }

/* Tendency cards */
.tendency-card {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-left: 3px solid var(--gold);
  border-radius: 6px;
  padding: 14px 16px;
  margin-bottom: 12px;
}
.tendency-card:last-child { margin-bottom: 0; }
.tendency-rank { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; margin-bottom: 4px; }
.tendency-label { font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 10px; }
.tendency-stats {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  align-items: center;
}
.tendency-outcome {
  background: #1a2e1a;
  border: 1px solid #2a5a2a;
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 13px;
  font-weight: 700;
  color: var(--green);
}
.tendency-meta { color: var(--text2); font-size: 12px; }
.conf-badge {
  font-size: 11px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 3px;
  text-transform: uppercase;
}
.conf-high   { background: #2a1a00; color: var(--gold); border: 1px solid #5a3800; }
.conf-medium { background: #1a1a2e; color: var(--blue); border: 1px solid #2a2a5a; }
.conf-low    { background: var(--surface2); color: var(--muted); border: 1px solid var(--border); }

/* Patterns */
.pattern-item {
  display: flex;
  gap: 10px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}
.pattern-item:last-child { border-bottom: none; }
.pattern-bullet {
  color: var(--accent2);
  font-weight: 700;
  flex-shrink: 0;
  margin-top: 1px;
}
.pattern-text { color: var(--text2); font-size: 13px; }

/* Evidence table */
.evidence-baseline {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 16px;
  margin-bottom: 16px;
  font-size: 13px;
  color: var(--text2);
}
.evidence-baseline strong { color: var(--text); }
.evidence-group { margin-bottom: 18px; }
.evidence-group-label {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 8px;
}
table { width: 100%; border-collapse: collapse; }
th {
  background: var(--surface2);
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  padding: 7px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  color: var(--text2);
  font-size: 13px;
}
tr:last-child td { border-bottom: none; }
.n-small { color: var(--accent); font-size: 11px; }
.site-a { color: var(--blue); font-weight: 700; }
.site-b { color: var(--accent2); font-weight: 700; }
.pct-bar-wrap { display: flex; align-items: center; gap: 8px; min-width: 130px; }
.pct-bar-bg { flex: 1; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
.pct-bar-fill { height: 100%; border-radius: 3px; }

/* Ult / Questions sections */
.status-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  padding: 2px 9px;
  border-radius: 3px;
  text-transform: uppercase;
  margin-bottom: 10px;
}
.status-found    { background: #1a2e1a; color: var(--green); border: 1px solid #2a5a2a; }
.status-missing  { background: var(--surface2); color: var(--muted); border: 1px solid var(--border); }
.ult-finding {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-left: 3px solid var(--blue);
  border-radius: 6px;
  padding: 12px 16px;
  margin-bottom: 10px;
  font-size: 13px;
}
.ult-finding:last-child { margin-bottom: 0; }
.ult-label { font-weight: 700; color: var(--text); margin-bottom: 8px; }
.ult-row { color: var(--text2); margin-bottom: 4px; }
.ult-delta-pos { color: var(--green); }
.ult-delta-neg { color: var(--accent); }

/* Weapon findings */
.wpn-finding {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent2);
  border-radius: 6px;
  padding: 12px 16px;
  margin-bottom: 10px;
  font-size: 13px;
}
.wpn-label { font-weight: 700; color: var(--text); margin-bottom: 6px; }
.wpn-zones { color: var(--text2); margin-bottom: 4px; }
.zone-tag {
  display: inline-block;
  background: #1a1a2e;
  border: 1px solid #2a2a5a;
  border-radius: 3px;
  padding: 1px 7px;
  margin: 2px 3px 2px 0;
  font-size: 12px;
  color: var(--blue);
}

/* Stack findings */
.stack-finding {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px 16px;
  margin-bottom: 10px;
}
.stack-map { font-weight: 700; color: var(--text); margin-bottom: 10px; font-size: 13px; }
.stack-bars { display: flex; flex-direction: column; gap: 8px; }
.stack-row { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.stack-key { color: var(--text2); width: 100px; flex-shrink: 0; }
.stack-val { color: var(--text); width: 60px; flex-shrink: 0; }
.no-data { color: var(--muted); font-style: italic; font-size: 13px; padding: 8px 0; }
"""

def _h(text: str) -> str:
    """HTML-escape text."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _conf_class(conf: str) -> str:
    if "High" in conf or "Very" in conf:
        return "conf-high"
    if "Medium" in conf:
        return "conf-medium"
    return "conf-low"


def _pct_bar(pct: int, color: str) -> str:
    return (f'<div class="pct-bar-wrap">'
            f'<div class="pct-bar-bg"><div class="pct-bar-fill" '
            f'style="width:{pct}%;background:{color};"></div></div>'
            f'<span style="font-size:12px;color:var(--text2)">{pct}%</span>'
            f'</div>')


def _status_badge(status: str) -> str:
    labels = {
        "found":               "Found",
        "no_results":          "No Results",
        "insufficient_sample": "Insufficient Sample",
        "data_missing":        "Data Missing",
        "not_implemented":     "Not Implemented",
    }
    css = "status-found" if status == "found" else "status-missing"
    return f'<span class="status-badge {css}">{labels.get(status, status)}</span>'


def generate_html(data: dict, team: str, vs: str,
                  series_meta: dict, map_filter=None) -> str:
    """Render the full anti-strat report as a self-contained HTML string."""
    insights_list = data.get("insights", [])
    patterns      = data.get("patterns", [])
    breakdowns    = data.get("breakdowns", {})
    total         = data["total"]
    n_a, n_b      = data["n_a"], data["n_b"]
    ba, bb        = data["baseline_a"], data["baseline_b"]
    plant_rate    = data["baseline_plant_rate"]
    map_label     = ", ".join(map_filter) if map_filter else data.get("map", "All Maps")
    _single_map   = map_filter[0] if map_filter and len(map_filter) == 1 else None

    parts = []

    # ── Header ────────────────────────────────────────────────────────────────
    sub_html = ""
    if series_meta:
        t1  = _h(series_meta.get("team1_name", ""))
        t2  = _h(series_meta.get("team2_name", ""))
        evt = _h(series_meta.get("event_name", ""))
        dt  = _h((series_meta.get("start_date") or "")[:10])
        sub_html = f'<div class="sub">{t1} vs {t2} &nbsp;·&nbsp; {_h(evt)} &nbsp;·&nbsp; {dt}</div>'

    parts.append(f"""
<div class="report-header">
  <h1>Match Analysis — {_h(team)}</h1>
  {sub_html}
  <div class="meta-pills">
    <span class="pill">Map: <strong>{_h(map_label)}</strong></span>
    <span class="pill">ATK Rounds: <strong>{total}</strong></span>
    <span class="pill">Baseline A: <strong>{ba}%</strong></span>
    <span class="pill">Baseline B: <strong>{bb}%</strong></span>
    <span class="pill">Plant Rate: <strong>{plant_rate}%</strong></span>
  </div>
</div>""")

    # ── Section 1: Strongest Tendencies ──────────────────────────────────────
    body = ""
    if not insights_list:
        body = '<p class="no-data">No significant tendencies found (requires n≥5 per condition, ≥10pp swing, z≥1.2).</p>'
    else:
        for ins in insights_list:
            conf  = ins["confidence"]
            delta = ins["delta"]
            delta_str = f"{'+'if delta>0 else ''}{delta}pp"
            body += f"""
<div class="tendency-card">
  <div class="tendency-rank">#{ins['rank']}  [{_h(ins['category'])}]</div>
  <div class="tendency-label">{_h(ins['label'])}</div>
  <div class="tendency-stats">
    <span class="tendency-outcome">→ {_h(ins['site'])} Finish &nbsp; {ins['conditional_pct']}%</span>
    <span class="tendency-meta">Baseline {ins['baseline_pct']}% &nbsp;·&nbsp; {delta_str} &nbsp;·&nbsp; n={ins['n']} &nbsp;·&nbsp; z={ins['z']}</span>
    <span class="conf-badge {_conf_class(conf)}">{_h(conf)}</span>
  </div>
</div>"""

    parts.append(f"""
<div class="section">
  <div class="section-header"><div class="section-number">1</div><h2>Strongest Tendencies</h2></div>
  <div class="section-body">{body}</div>
</div>""")

    # ── Section 2: Recurring Patterns ────────────────────────────────────────
    body = ""
    if not patterns:
        body = '<p class="no-data">Insufficient data to derive patterns.</p>'
    else:
        for sent in patterns:
            body += f'<div class="pattern-item"><span class="pattern-bullet">▸</span><span class="pattern-text">{_h(sent)}</span></div>'

    parts.append(f"""
<div class="section">
  <div class="section-header"><div class="section-number">2</div><h2>Recurring Patterns</h2></div>
  <div class="section-body">{body}</div>
</div>""")

    # ── Section 3: Supporting Evidence ───────────────────────────────────────
    body = f"""
<div class="evidence-baseline">
  Baseline: <strong>A {ba}%</strong> ({n_a}/{total} rounds) &nbsp;|&nbsp;
  <strong>B {bb}%</strong> ({n_b}/{total} rounds) &nbsp;|&nbsp;
  Plant rate <strong>{plant_rate}%</strong>
</div>"""

    for bd_name, bd in breakdowns.items():
        label  = bd.get("label", bd_name)
        values = bd.get("data", {})
        if not values:
            continue
        rows_html = ""
        for key, counts in values.items():
            n_sub = counts["n"]
            a_cnt = counts["a"]
            b_cnt = counts["b"]
            a_pct = round(a_cnt / n_sub * 100) if n_sub else 0
            b_pct = 100 - a_pct
            small = '<span class="n-small"> ⚠ small</span>' if n_sub < 5 else ""
            rows_html += f"""
<tr>
  <td>{_h(str(key))}{small}</td>
  <td style="text-align:center">{n_sub}</td>
  <td><span class="site-a">{a_cnt}</span></td>
  <td>{_pct_bar(a_pct, "var(--blue)")}</td>
  <td><span class="site-b">{b_cnt}</span></td>
  <td>{_pct_bar(b_pct, "var(--accent2)")}</td>
</tr>"""

        body += f"""
<div class="evidence-group">
  <div class="evidence-group-label">{_h(label)}</div>
  <table>
    <thead><tr><th>Condition</th><th>n</th><th>A kills</th><th>A %</th><th>B kills</th><th>B %</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>"""

    parts.append(f"""
<div class="section">
  <div class="section-header"><div class="section-number">3</div><h2>Supporting Evidence</h2></div>
  <div class="section-body">{body}</div>
</div>""")

    # ── Section 4: Ult Patterns ───────────────────────────────────────────────
    ult_res = _questions.ult_behavior(team, _single_map)
    body = _status_badge(ult_res["status"])
    if ult_res["status"] != "found":
        body += f'<p class="pattern-text" style="margin-top:8px">{_h(ult_res["reason"])}</p>'
        ult_counts = ult_res.get("ult_counts", {})
        nonzero = {p: n for p, n in ult_counts.items() if n > 0}
        if nonzero:
            body += f'<p class="tendency-meta" style="margin-top:10px">Ult rounds detected (below threshold of {ult_res["threshold"]}):</p><ul style="margin:6px 0 0 20px">'
            for p, n in sorted(nonzero.items(), key=lambda x: -x[1]):
                body += f'<li style="color:var(--text2);font-size:13px">{_h(p)}: {n} round(s)</li>'
            body += "</ul>"
    else:
        body += f'<div class="evidence-baseline" style="margin:10px 0">Baseline: <strong>A {ult_res["baseline_a"]}%</strong> &nbsp;|&nbsp; <strong>B {ult_res["baseline_b"]}%</strong> &nbsp;|&nbsp; ATK rounds: <strong>{ult_res["atk_rounds"]}</strong></div>'
        for f in ult_res["findings"]:
            delta_a = f["delta_a"]
            dcls    = "ult-delta-pos" if delta_a >= 0 else "ult-delta-neg"
            fake_html = ""
            if f["fake_rate"] is not None and f["baseline_fake"] is not None:
                fake_html = f'<div class="ult-row">Fake rate: <strong>{f["fake_rate"]}%</strong> (baseline {f["baseline_fake"]}%, n={f["fake_n"]})</div>'
            rounds_html = ""
            if f.get("round_log"):
                rounds_html = (f'<div class="ult-row" style="color:var(--gold)">'
                               f'Rounds fired: {_h(", ".join(f["round_log"]))}</div>')
            body += f"""
<div class="ult-finding">
  <div class="ult-label">{_h(f['label'])} <span style="font-weight:400;color:var(--muted)">({f['ult_n']} rounds with ult)</span></div>
  {rounds_html}
  <div class="ult-row">Ult available: <span class="site-a">A {f['a_pct']}%</span> &nbsp; <span class="site-b">B {f['b_pct']}%</span></div>
  <div class="ult-row">Baseline: A {f['baseline_a']}%  B {f['baseline_b']}%
    &nbsp;·&nbsp; <span class="{dcls}">Δ {'+' if delta_a >= 0 else ''}{delta_a}pp toward {'A' if delta_a >= 0 else 'B'}</span>
  </div>
  {fake_html}
</div>"""

    parts.append(f"""
<div class="section">
  <div class="section-header"><div class="section-number">4</div><h2>Ult Patterns</h2></div>
  <div class="section-body">{body}</div>
</div>""")

    # ── Section 5: One Orb Off Ult ────────────────────────────────────────────
    orb_res = _questions.one_orb_off_ult(team, _single_map)
    body = _status_badge(orb_res["status"])
    body += f'<p class="pattern-text" style="margin-top:8px">{_h(orb_res["reason"])}</p>'
    parts.append(f"""
<div class="section">
  <div class="section-header"><div class="section-number">5</div><h2>One Orb Off Ult Patterns</h2></div>
  <div class="section-body">{body}</div>
</div>""")

    # ── Sections 6a/6b: Defensive Weapon Patterns ─────────────────────────────
    for sec_num, wpn in enumerate(("Judge", "Operator"), start=6):
        wpn_res = _questions.weapon_def_patterns(team, wpn, _single_map)
        body = _status_badge(wpn_res["status"])
        if wpn_res["status"] != "found":
            body += f'<p class="pattern-text" style="margin-top:8px">{_h(wpn_res["reason"])}</p>'
        else:
            body += f'<div class="evidence-baseline" style="margin:10px 0">Total DEF kills with {_h(wpn)}: <strong>{wpn_res["total_kills"]}</strong></div>'
            for f in wpn_res["findings"]:
                zones_html = "".join(f'<span class="zone-tag">{_h(z)} {n}×</span>'
                                     for z, n in sorted(f["zones"].items(), key=lambda x: -x[1]))
                small_note = ' <span class="n-small">⚠ small sample</span>' if f.get("small") else ""
                body += f"""
<div class="wpn-finding">
  <div class="wpn-label">{_h(f['label'])} — {_h(f['map'])} ({f['total']} kills{small_note})</div>
  <div class="wpn-zones">Zones: {zones_html}</div>
  <div style="color:var(--text2);font-size:12px;margin-top:6px">Avg timing: <strong style="color:var(--text)">{f['avg_timing']}s</strong> into round</div>
</div>"""

        label = f"Defensive {wpn} Patterns"
        parts.append(f"""
<div class="section">
  <div class="section-header"><div class="section-number">{sec_num}</div><h2>{label}</h2></div>
  <div class="section-body">{body}</div>
</div>""")

    # ── Section 8: Defensive Stacking ────────────────────────────────────────
    stack_res = _questions.def_stacking(team, _single_map)
    body = _status_badge(stack_res["status"])
    if stack_res["status"] != "found":
        body += f'<p class="pattern-text" style="margin-top:8px">{_h(stack_res["reason"])}</p>'
        if stack_res.get("eco_half_total", 0) > 0:
            body += (f'<div class="tendency-meta" style="margin-top:10px">'
                     f'Eco/half DEF rounds found: {stack_res["eco_half_total"]} &nbsp;·&nbsp; '
                     f'Rounds with position data: {stack_res.get("rounds_with_pos",0)}</div>')
    else:
        STACK_COLORS = {
            "A-heavy":   "var(--blue)",
            "B-heavy":   "var(--accent2)",
            "Mid-heavy": "var(--gold)",
            "Default":   "var(--muted)",
        }
        for map_n, counts in stack_res["findings"].items():
            total_st = counts["total"]
            bar_rows = ""
            for cls in ("A-heavy", "B-heavy", "Mid-heavy", "Default"):
                n_cls = counts[cls]
                if not n_cls:
                    continue
                pct_cls = round(n_cls / total_st * 100)
                color   = STACK_COLORS.get(cls, "var(--text2)")
                bar_rows += f"""
<div class="stack-row">
  <span class="stack-key">{_h(cls)}</span>
  <span class="stack-val">{n_cls}/{total_st}</span>
  {_pct_bar(pct_cls, color)}
</div>"""
            body += f"""
<div class="stack-finding">
  <div class="stack-map">{_h(map_n)} <span style="font-weight:400;color:var(--muted);font-size:12px">(n={total_st} eco/half rounds with position data)</span></div>
  <div class="stack-bars">{bar_rows}</div>
</div>"""

    parts.append(f"""
<div class="section">
  <div class="section-header"><div class="section-number">8</div><h2>Low-Money Defensive Stacks</h2></div>
  <div class="section-body">{body}</div>
</div>""")

    # ── Assemble ──────────────────────────────────────────────────────────────
    body_html = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anti-Strat: {_h(team)} — {_h(map_label)}</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<div class="report-wrap">
{body_html}
</div>
</body>
</html>"""


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import sys, io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Valorant tendency report — DATA → PATTERN → EVIDENCE"
    )
    ap.add_argument("--team",    required=True,         help="Team to analyze")
    ap.add_argument("--vs",      default="FURIA",       help="Your team (used in brief header)")
    ap.add_argument("--map",     nargs="+",             help="Filter to specific map(s)")
    ap.add_argument("--maps",    nargs="+",             help="Alias for --map")
    ap.add_argument("--series",  type=int,              help="Filter to one series ID")
    ap.add_argument("--model",   default="qwen2.5:14b", help="Ollama model (--brief only)")
    ap.add_argument("--brief",   action="store_true",   help="Append LLM counter-strat brief (requires Ollama)")
    ap.add_argument("--save",    action="store_true",   help="Save report to .txt file")
    ap.add_argument("--html",    action="store_true",   help="Save report as HTML file and open in browser")
    # Legacy aliases — kept for backward compatibility, map to default behaviour
    ap.add_argument("--no-ai",  action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-llm", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--raw",    action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    map_filter  = args.map or args.maps
    _single_map = map_filter[0] if map_filter and len(map_filter) == 1 else None
    sid         = args.series

    print(f"  Analyzing {args.team}...", flush=True)
    data        = _insight_engine.find_insights(args.team, _single_map)
    series_meta = _get_series_meta(db(), sid) if sid else {}

    if not data:
        print(f"  No data found for '{args.team}'.")
        if map_filter:
            print(f"  (Filtered to map: {', '.join(map_filter)})")
        return

    print_document(data, args.team, args.vs, series_meta, map_filter)

    if args.brief:
        print(f"\n  Asking {args.model} for brief... (30-90s)", flush=True)
        context = build_context(args.team, map_filter=map_filter, series_id=sid)
        report  = generate_report(args.team, context, model=args.model, for_team=args.vs)
        _print_brief(context, report, args.team, args.vs, series_meta, map_filter)

    if args.save:
        import io as _io
        buf = _io.StringIO()
        import sys as _sys
        old_out = _sys.stdout
        _sys.stdout = buf
        print_document(data, args.team, args.vs, series_meta, map_filter)
        _sys.stdout = old_out
        slug = args.team.lower().replace(" ", "_")
        maps = "_".join(map_filter) if map_filter else "all"
        out  = Path(__file__).parent / f"antistrat_{slug}_{maps}.txt"
        out.write_text(buf.getvalue(), encoding="utf-8")
        print(f"\n  Saved: {out}")

    if args.html:
        slug = args.team.lower().replace(" ", "_")
        maps = "_".join(map_filter) if map_filter else "all"
        out  = Path(__file__).parent / f"antistrat_{slug}_{maps}.html"
        html = generate_html(data, args.team, args.vs, series_meta, map_filter)
        out.write_text(html, encoding="utf-8")
        print(f"\n  HTML saved: {out}")
        import webbrowser
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()

