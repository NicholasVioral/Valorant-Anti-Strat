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

    # ── Map pool ──────────────────────────────────────────────────────────────
    lines.append("MAP POOL:")
    for m in map_pool:
        n  = m["maps_played"]
        wr = round(m["wins"] / n * 100) if n else 0
        lines.append(f"  {m['map_name']}: {n} played | {wr}% win rate")
    lines.append("")

    # ── Tactical pre-analysis ─────────────────────────────────────────────────
    insights = _derive_tactical_insights(
        team_name, map_pool, site_rows, timing_rows, player_rows, series_count
    )
    lines.append("═" * 60)
    lines.append("PRE-ANALYZED TACTICAL OBSERVATIONS")
    lines.append("═" * 60)
    lines.append(insights)

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

    # ── Player opening route frequencies ─────────────────────────────────────
    if route_data:
        for line in _format_route_analysis(route_data):
            lines.append(line)
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
- ALWAYS cite frequency from PLAYER OPENING ROUTES (e.g. "8/12 rounds" or "67% of ATK rounds").
- Every WATCH OUT must reference a specific round pattern or route frequency from the data.
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

def print_document(context: str, report: str, team: str, vs: str,
                   series_meta: dict, map_filter=None):
    W = 70
    def rule(char="="):   print(char * W)
    def thin():           print("-" * W)
    def blank():          print()
    def hdr(txt):
        print(); rule(); print(f"  {txt}"); rule()
    def sub(txt):
        print(); print(f"  {txt}"); thin()

    rule("=")
    title = f"MATCH ANALYSIS  —  {team.upper()}"
    pad   = (W - len(title)) // 2
    print(" " * pad + title)
    if series_meta:
        t1   = series_meta.get("team1_name", "")
        t2   = series_meta.get("team2_name", "")
        evt  = series_meta.get("event_name", "")
        date = (series_meta.get("start_date") or "")[:10]
        sub_title = f"{t1}  vs  {t2}   |   {evt}   |   {date}"
        pad2 = (W - len(sub_title)) // 2
        print(" " * max(pad2, 0) + sub_title)
    if map_filter:
        print(f"  Map: {', '.join(map_filter)}")
    rule("=")
    blank()

    # ── Parse context sections ────────────────────────────────────────────────
    lines = context.splitlines()
    in_routes = False; in_patterns = False; in_rounds = False
    pool_lines = []; player_lines = []; site_lines = []
    timing_lines = []; weapon_lines = []; route_lines = []
    pattern_lines = []; round_lines = []

    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("MAP POOL:"):
            i += 1
            while i < len(lines) and lines[i].strip():
                pool_lines.append(lines[i].strip())
                i += 1
        elif l.startswith("ATTACK TEMPO:"):
            timing_lines.append(l.strip())
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("SITE", "PLAYER", "ATK ", "DEF ")):
                timing_lines.append(lines[i].strip())
                i += 1
        elif l.startswith("SITE TENDENCIES:"):
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith("PLAYER"):
                site_lines.append(lines[i].strip())
                i += 1
        elif l.startswith("PLAYER ROLES"):
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("ATK WEAPON", "DEF WEAPON", chr(9552))):
                player_lines.append(lines[i].strip())
                i += 1
        elif l.startswith("ATK WEAPONS:") or l.startswith("DEF WEAPONS:"):
            weapon_lines.append(l.strip())
            i += 1
        elif "PLAYER OPENING ROUTES" in l:
            in_routes = True; i += 1
        elif "OPENING PATTERN RECOGNITION" in l:
            in_routes = False; in_patterns = True; i += 1
        elif "2D REPLAY" in l:
            in_patterns = False; in_rounds = True; i += 1
        elif in_routes and l.strip() and not l.strip().startswith(chr(9552)) and not l.strip().startswith("Shows"):
            route_lines.append(l)
            i += 1
        elif in_patterns and l.strip() and not l.strip().startswith(chr(9552)) and "position predicts" not in l:
            pattern_lines.append(l)
            i += 1
        elif in_rounds and l.strip() and not l.strip().startswith(chr(9552)):
            round_lines.append(l)
            i += 1
        else:
            i += 1

    # ── Map Pool ──────────────────────────────────────────────────────────────
    sub("MAP POOL")
    for pl in pool_lines:
        print(f"    {pl}")

    # ── Player Roster ─────────────────────────────────────────────────────────
    sub("PLAYER ROSTER")
    hdr_row = f"  {'PLAYER':<14}  {'DMG/RD':>6}  {'FK%':>4}  {'FD%':>4}  {'PLANTS':>6}  ROLE"
    print(hdr_row)
    print(f"  {'-'*14}  {'-'*6}  {'-'*4}  {'-'*4}  {'-'*6}  ----")
    import re as _re
    for pl in player_lines:
        if "dmg/rd" in pl:
            m = _re.match(r"\s*(\S+):\s*(\d+) dmg/rd \| FK (\d+)% \| FD (\d+)% \| (\d+) plants\s*(?:->|→)\s*(.+)", pl)
            if m:
                ign, dmg, fk, fd, plants, role = m.groups()
                print(f"  {ign:<14}  {dmg:>6}  {fk:>3}%  {fd:>3}%  {plants:>6}  {role}")
    blank()

    # ── Weapon Usage ──────────────────────────────────────────────────────────
    sub("WEAPON USAGE")
    for wl in weapon_lines:
        print(f"  {wl}")

    # ── Attack Timing ─────────────────────────────────────────────────────────
    sub("ATTACK TIMING")
    for tl in timing_lines:
        print(f"  {tl}")

    # ── Site Execution ────────────────────────────────────────────────────────
    sub("SITE EXECUTION TENDENCIES")
    for sl in site_lines:
        print(f"  {sl}")

    # ── Player Opening Routes ─────────────────────────────────────────────────
    sub("PLAYER OPENING ROUTES  (position at ~10s into ATK / DEF rounds)")
    cur_map = ""
    for rl in route_lines:
        stripped = rl.strip()
        if stripped.startswith("[") and "]" in stripped:
            cur_map = stripped
            print(f"\n  {cur_map}")
        elif stripped.startswith("ATK routes") or stripped.startswith("DEF routes"):
            print(f"\n    {stripped}")
        elif stripped:
            print(f"    {stripped}")

    # ── Opening Pattern Recognition ───────────────────────────────────────────
    sub("OPENING PATTERN RECOGNITION")
    cur_map = ""
    for pl in pattern_lines:
        stripped = pl.strip()
        if stripped.startswith("[") and "]" in stripped:
            cur_map = stripped
            print(f"\n  {cur_map}")
        elif stripped.startswith("PLAYER OPENING") or stripped.startswith("TEAM SIDE"):
            print(f"\n    {stripped}")
        elif stripped:
            print(f"    {stripped}")

    # ── Round-by-Round Setups ─────────────────────────────────────────────────
    sub("ROUND-BY-ROUND ATK SETUPS")
    cur_map = ""
    for rl in round_lines:
        stripped = rl.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if cur_map:
                blank()
            cur_map = stripped
            print(f"\n  Map: {cur_map}")
            thin()
        elif stripped.startswith("R") and "[" in stripped and "]" in stripped:
            print(f"\n  {stripped}")
        elif stripped.startswith("OPEN:"):
            # wrap each player on its own indented line
            players = stripped.replace("OPEN:", "").split(" | ")
            print(f"    Opening setup:")
            for p in players:
                print(f"      {p.strip()}")
        elif stripped.startswith("LATE:"):
            print(f"    Late-round: {stripped.replace('LATE:', '').strip()}")

    # ── AI Brief ─────────────────────────────────────────────────────────────
    hdr(f"COUNTER-STRAT BRIEF  |  Prepared for {vs.upper()}")
    blank()
    # Clean up and print the report with proper indentation
    for line in report.splitlines():
        if line.strip().startswith("###"):
            blank(); thin()
            print(f"  {line.strip().replace('###', '').strip().upper()}")
            thin()
        elif line.strip().startswith("WATCH OUT:"):
            print(f"    [!] {line.strip()[len('WATCH OUT:'):].strip()}")
        elif line.strip().startswith("PRIORITY:"):
            blank()
            print(f"  >> PRIORITY: {line.strip()[len('PRIORITY:'):].strip()}")
        elif line.strip().startswith("Good call:"):
            print(f"  >> GOOD CALL: {line.strip()[len('Good call:'):].strip()}")
        elif line.strip():
            print(f"  {line.strip()}")
    blank(); rule()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import sys, io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Generate anti-strat report from DB using local Ollama LLM"
    )
    ap.add_argument("--team",    required=True,          help="Team to analyze")
    ap.add_argument("--vs",      default="FURIA",        help="Your team name (default: FURIA)")
    ap.add_argument("--map",     nargs="+",              help="Filter to specific map(s)")
    ap.add_argument("--maps",    nargs="+",              help="Alias for --map")
    ap.add_argument("--series",  type=int,               help="Filter to one specific series ID")
    ap.add_argument("--model",   default="qwen2.5:14b",  help="Ollama model")
    ap.add_argument("--no-ai",   action="store_true",    help="Print data only, no AI report")
    ap.add_argument("--no-llm",  action="store_true",    help="Structured report without Ollama")
    ap.add_argument("--raw",     action="store_true",    help="Print raw context dump instead of formatted doc")
    ap.add_argument("--save",    action="store_true",    help="Save report to a .txt file")
    args = ap.parse_args()

    map_filter = args.map or args.maps
    sid        = args.series

    print(f"  Building context for {args.team}...", flush=True)
    context     = build_context(args.team, map_filter=map_filter, series_id=sid)
    series_meta = _get_series_meta(db(), sid) if sid else {}

    if args.no_ai or args.raw:
        print(context)
        return

    if args.no_llm:
        report = generate_report_no_llm(args.team, context, for_team=args.vs, map_filter=map_filter)
    else:
        print(f"  Asking {args.model} to generate brief... (30-90s)", flush=True)
        report = generate_report(args.team, context, model=args.model, for_team=args.vs)

    print_document(context, report, args.team, args.vs, series_meta, map_filter)

    if args.save:
        slug = args.team.lower().replace(" ", "_")
        maps = "_".join(map_filter) if map_filter else "all"
        out  = Path(__file__).parent / f"antistrat_{slug}_{maps}.txt"
        out.write_text(context + "\n\n" + report, encoding="utf-8")
        print(f"\n  Report saved to: {out}")

    if args.save:
        slug = args.team.lower().replace(" ", "_")
        out  = Path(__file__).parent / f"antistrat_{slug}.txt"
        out.write_text(
            f"CONTEXT\n{divider}\n{context}\n\n"
            f"ANTI-STRAT BRIEF\n{divider}\n{report}",
            encoding="utf-8"
        )
        print(f"\nReport saved to: {out}")


if __name__ == "__main__":
    main()
