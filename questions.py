"""
Coach-question analysis: answers specific tactical questions from match data.

Every function returns a result dict with:
  status   — "found" | "no_results" | "insufficient_sample" | "data_missing" | "not_implemented"
  reason   — plain-English explanation of the status
  findings — the actual data (list or dict, empty when no results)
  + function-specific diagnostic fields for rendering

Q1. Agent has ult available → ATK behavior change
Q2. Agent one orb off ult  → ATK behavior change  [needs re-scrape for ult_charges data]
Q3. Low-money CT stack tendencies                 [requires position data]
Q4. Judge on defense: who, where, when
Q5. Operator on defense: who, where, when
"""

import json
import sqlite3
from collections import defaultdict
from pathlib import Path


def round_timer(elapsed_ms: int) -> str:
    """
    Convert elapsed-since-action-phase milliseconds to the in-game round timer
    string (counting down from 1:40).  Values past 100s are post-plant and
    shown as '+Xs plant' so they match the defuse clock on the VOD.
    """
    elapsed_s = elapsed_ms / 1000
    if elapsed_s <= 100:
        remaining = 100 - elapsed_s
        mins = int(remaining) // 60
        secs = int(remaining) % 60
        return f"{mins}:{secs:02d}"
    else:
        # Post-plant: timer has switched to the 45s defuse clock.
        # We don't know exact plant time, so just show elapsed-past-100.
        over = elapsed_s - 100
        return f"+{over:.0f}s plant"

DB_PATH = Path(__file__).parent / "valorant.db"

MIN_N_ULT    = 3
MIN_N_WEAPON = 2
MIN_N_STACK  = 3

_SITE_Y: dict = {
    "pearl":    {"b_max": -2000, "a_min": 2000},
    "breeze":   {"b_max": -1800, "a_min": 1800},
    "split":    {"b_max": -1500, "a_min": 1500},
    "bind":     {"b_max": -2000, "a_min": 2000},
    "ascent":   {"b_max": -1500, "a_min": 1500},
    "fracture": {"b_max": -1500, "a_min": 1500},
    "sunset":   {"b_max": -1500, "a_min": 1500},
    "abyss":    {"b_max": -1500, "a_min": 1500},
    "haven":    None,
    "lotus":    None,
}


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _classify_y(map_name: str, y) -> str | None:
    if y is None:
        return None
    t = _SITE_Y.get((map_name or "").lower())
    if not t:
        return None
    if y >= t["a_min"]:
        return "A"
    if y <= t["b_max"]:
        return "B"
    return "Mid"


def _team_matches(conn, team_name: str, map_name: str = None) -> list[dict]:
    sql = """
        SELECT m.match_id, m.map_name,
               CASE WHEN LOWER(s.team1_name)=LOWER(:name) THEN 1 ELSE 2 END AS team_num
        FROM matches m JOIN series s ON s.series_id = m.series_id
        WHERE LOWER(s.team1_name)=LOWER(:name) OR LOWER(s.team2_name)=LOWER(:name)
    """
    p: dict = {"name": team_name}
    if map_name:
        sql += " AND LOWER(m.map_name)=LOWER(:map)"
        p["map"] = map_name
    return [dict(r) for r in conn.execute(sql, p).fetchall()]


def _team_pids(conn, match_id: int, team_num: int) -> set[int]:
    rows = conn.execute(
        "SELECT player_id FROM match_players WHERE match_id=? AND team_number=?",
        (match_id, team_num)
    ).fetchall()
    return {r["player_id"] for r in rows}


def _fc_sides(conn, matches: list[dict]) -> dict:
    """First-contact zone per (match_id, round_number) from earliest ATK kill victim_y."""
    result: dict = {}
    for m in matches:
        map_n = (m.get("map_name") or "").lower()
        kills = conn.execute("""
            SELECT round_number, victim_y FROM kills
            WHERE match_id=? AND side='atk'
            ORDER BY round_number, round_time_ms
        """, (m["match_id"],)).fetchall()
        seen: set = set()
        for k in kills:
            key = (m["match_id"], k["round_number"])
            if key not in seen:
                result[key] = _classify_y(map_n, k["victim_y"])
                seen.add(key)
    return result


# -- Q1: Ult behavior (ult fired on ATK) -----------------------------------------

def ult_behavior(team_name: str, map_name: str = None) -> dict:
    """
    Per player: rounds where they started with full ult (ult_charges == ult_max)
    AND actually fired it (charges dropped to 0 mid-round).
    Each player evaluated independently from teammates.
    Requires ult_charges/ult_max data scraped from 2D replays.
    """
    conn = _db()
    matches = _team_matches(conn, team_name, map_name)
    if not matches:
        conn.close()
        return {
            "status":   "data_missing",
            "reason":   f"No matches found for '{team_name}'" +
                        (f" on {map_name}" if map_name else "") + ".",
            "findings": [],
        }

    sql = """
        SELECT rs.match_id, rs.round_number, rs.site_executed, rs.won_round
        FROM round_states rs
        JOIN matches m ON m.match_id = rs.match_id
        JOIN series s ON s.series_id = m.series_id
        WHERE rs.side='atk' AND rs.site_executed IN ('A','B')
          AND ((LOWER(s.team1_name)=LOWER(:name) AND rs.team_number=1)
               OR (LOWER(s.team2_name)=LOWER(:name) AND rs.team_number=2))
    """
    p: dict = {"name": team_name}
    if map_name:
        sql += " AND LOWER(m.map_name)=LOWER(:map)"
        p["map"] = map_name
    atk_rows = {(r["match_id"], r["round_number"]): dict(r)
                for r in conn.execute(sql, p).fetchall()}

    if not atk_rows:
        conn.close()
        return {
            "status":   "data_missing",
            "reason":   "No ATK rounds with site data found in round_states.",
            "findings": [],
        }

    n_total  = len(atk_rows)
    n_a_base = sum(1 for r in atk_rows.values() if r["site_executed"] == "A")
    base_a   = round(n_a_base / n_total * 100)
    base_b   = 100 - base_a

    pid_agent: dict[int, list[int]] = defaultdict(list)
    pid_ign:   dict[int, str]       = {}
    match_team_pids: dict[int, set] = {}
    for m in matches:
        pids_rows = conn.execute("""
            SELECT mp.player_id, mp.agent_id, pl.ign
            FROM match_players mp JOIN players pl ON pl.player_id=mp.player_id
            WHERE mp.match_id=? AND mp.team_number=?
        """, (m["match_id"], m["team_num"])).fetchall()
        own: set = set()
        for r in pids_rows:
            pid_agent[r["player_id"]].append(r["agent_id"])
            pid_ign[r["player_id"]] = r["ign"]
            own.add(r["player_id"])
        match_team_pids[m["match_id"]] = own

    # Require ult_charges data (new schema)
    match_ids    = [m["match_id"] for m in matches]
    placeholders = ",".join("?" * len(match_ids))
    has_data     = conn.execute(
        f"SELECT COUNT(*) FROM positions WHERE match_id IN ({placeholders}) AND ult_max > 0",
        match_ids,
    ).fetchone()[0]
    if not has_data:
        conn.close()
        return {
            "status":     "data_missing",
            "reason":     "No ult charge data. Re-scrape 2D replays to populate ult_charges.",
            "findings":   [],
            "atk_rounds": n_total,
        }

    # Per-round: for each own-team player independently check:
    #   1. Did they START the round with full ult? (ult_charges == ult_max)
    #   2. Did ult_charges drop to 0 LATER in the round? (ult was fired)
    ult_fired_rounds: dict[int, list] = defaultdict(list)
    rounds_with_pos = 0

    for (mid, rnum), round_row in atk_rows.items():
        own_pids = match_team_pids.get(mid, set())

        pos_rows = conn.execute("""
            SELECT player_id, game_time_ms, ult_charges, ult_max, alive
            FROM positions
            WHERE match_id=? AND round_number=? AND ult_max > 0
            ORDER BY player_id, game_time_ms
        """, (mid, rnum)).fetchall()

        if not pos_rows:
            continue

        by_pid: dict[int, list] = defaultdict(list)
        for row in pos_rows:
            if row["player_id"] in own_pids:
                by_pid[row["player_id"]].append(row)

        if not by_pid:
            continue
        rounds_with_pos += 1

        for pid, frames in by_pid.items():
            if len(frames) < 2:
                continue
            # Detect any transition: non-zero charges → 0 while alive.
            # Catches players who charge mid-round and immediately fire.
            prev = frames[0]["ult_charges"]
            fire_ms = None
            for f in frames[1:]:
                if prev > 0 and f["ult_charges"] == 0 and f["alive"] == 1:
                    fire_ms = f["game_time_ms"]
                    break
                prev = f["ult_charges"]
            if fire_ms is not None:
                ult_fired_rounds[pid].append({
                    **round_row,
                    "fire_timer": round_timer(fire_ms),
                })

    ult_counts = {
        pid_ign.get(pid, str(pid)): len(rounds)
        for pid, rounds in ult_fired_rounds.items()
    }

    findings: list[dict] = []
    for pid, rounds in ult_fired_rounds.items():
        if len(rounds) < MIN_N_ULT:
            continue
        ign      = pid_ign.get(pid, str(pid))
        agents   = pid_agent.get(pid, [])
        agent_id = max(set(agents), key=agents.count) if agents else None
        n        = len(rounds)
        pct_a    = round(sum(1 for r in rounds if r["site_executed"] == "A") / n * 100)
        round_log = [
            f"R{r['round_number']} @{r['fire_timer']}"
            for r in rounds
        ]
        findings.append({
            "player":     ign,
            "agent_id":   agent_id,
            "label":      f"{ign} (agent {agent_id})" if agent_id else ign,
            "ult_n":      n,
            "total_n":    n_total,
            "a_pct":      pct_a,
            "b_pct":      100 - pct_a,
            "baseline_a": base_a,
            "baseline_b": base_b,
            "delta_a":    pct_a - base_a,
            "round_log":  round_log,
        })

    conn.close()
    findings.sort(key=lambda x: -x["ult_n"])
    max_count = max(ult_counts.values(), default=0)

    if findings:
        status = "found"
        reason = (f"{len(findings)} player(s) with {MIN_N_ULT}+ rounds where they fired their ult "
                  f"({rounds_with_pos}/{n_total} ATK rounds had position data).")
    elif rounds_with_pos == 0:
        status = "data_missing"
        reason = "Position data exists but no rounds had ult_max > 0. Re-scrape 2D replays."
    elif max_count == 0:
        status = "no_results"
        reason = (f"Checked {rounds_with_pos} rounds with position data. "
                  f"No player both started with full ult and fired it on ATK.")
    else:
        status = "insufficient_sample"
        per = ", ".join(f"{p}: {n}" for p, n in
                        sorted(ult_counts.items(), key=lambda x: -x[1]) if n > 0)
        reason = f"Ult-fired rounds below threshold of {MIN_N_ULT} per player. Counts: {per}."

    return {
        "status":          status,
        "reason":          reason,
        "findings":        findings,
        "atk_rounds":      n_total,
        "rounds_with_pos": rounds_with_pos,
        "ult_counts":      ult_counts,
        "threshold":       MIN_N_ULT,
        "baseline_a":      base_a,
        "baseline_b":      base_b,
    }


# ── Q2: One orb off ult → ATK behavior ───────────────────────────────────────

def one_orb_off_ult(team_name: str, map_name: str = None) -> dict:
    """
    For each player: when they start a round one orb away from ult (ult_charges
    == ult_max - 1), how does site execution compare to the baseline?
    Requires positions data scraped with the new ult_charges column.
    """
    conn = _db()
    matches = _team_matches(conn, team_name, map_name)
    if not matches:
        conn.close()
        return {
            "status":   "data_missing",
            "reason":   f"No matches found for '{team_name}'" +
                        (f" on {map_name}" if map_name else "") + ".",
            "findings": [],
        }

    match_ids    = [m["match_id"] for m in matches]
    placeholders = ",".join("?" * len(match_ids))
    has_data     = conn.execute(
        f"SELECT COUNT(*) FROM positions WHERE match_id IN ({placeholders}) AND ult_max > 0",
        match_ids,
    ).fetchone()[0]

    if not has_data:
        conn.close()
        return {
            "status":   "data_missing",
            "reason":   ("Ult charge progress data not yet collected. "
                         "Re-scrape 2D replays to populate ult_charges. "
                         "Run: python scraper.py scrape-2d --series <id> --force"),
            "findings": [],
        }

    # ATK rounds baseline (same query as ult_behavior)
    sql = """
        SELECT rs.match_id, rs.round_number, rs.site_executed, rs.won_round
        FROM round_states rs
        JOIN matches m ON m.match_id = rs.match_id
        JOIN series s ON s.series_id = m.series_id
        WHERE rs.side='atk' AND rs.site_executed IN ('A','B')
          AND ((LOWER(s.team1_name)=LOWER(:name) AND rs.team_number=1)
               OR (LOWER(s.team2_name)=LOWER(:name) AND rs.team_number=2))
    """
    p: dict = {"name": team_name}
    if map_name:
        sql += " AND LOWER(m.map_name)=LOWER(:map)"
        p["map"] = map_name
    atk_rows  = {(r["match_id"], r["round_number"]): dict(r)
                 for r in conn.execute(sql, p).fetchall()}

    if not atk_rows:
        conn.close()
        return {
            "status":   "data_missing",
            "reason":   "No ATK rounds with site data found in round_states.",
            "findings": [],
        }

    n_total  = len(atk_rows)
    n_a_base = sum(1 for r in atk_rows.values() if r["site_executed"] == "A")
    base_a   = round(n_a_base / n_total * 100)
    base_b   = 100 - base_a

    # Per-player player IGN lookup
    pid_ign: dict[int, str] = {}
    pid_agent: dict[int, list] = defaultdict(list)
    match_team_pids = {m["match_id"]: _team_pids(conn, m["match_id"], m["team_num"])
                       for m in matches}
    for m in matches:
        rows = conn.execute("""
            SELECT mp.player_id, mp.agent_id, p.ign
            FROM match_players mp JOIN players p ON p.player_id=mp.player_id
            WHERE mp.match_id=? AND mp.team_number=?
        """, (m["match_id"], m["team_num"])).fetchall()
        for r in rows:
            pid_ign[r["player_id"]]   = r["ign"]
            pid_agent[r["player_id"]].append(r["agent_id"])

    # Find one-orb-off rounds per player using early-round positions (T ≤ 5s)
    player_buckets: dict[int, list] = defaultdict(list)
    for (mid, rnum), round_row in atk_rows.items():
        own_pids = match_team_pids.get(mid, set())
        pos_rows = conn.execute("""
            SELECT player_id, ult_charges, ult_max
            FROM positions
            WHERE match_id=? AND round_number=? AND game_time_ms <= 5000
              AND ult_max > 0
        """, (mid, rnum)).fetchall()

        seen: dict[int, tuple] = {}
        for p in pos_rows:
            pid = p["player_id"]
            if pid in own_pids and pid not in seen:
                seen[pid] = (p["ult_charges"], p["ult_max"])

        for pid, (charges, max_c) in seen.items():
            if charges == max_c - 1:
                player_buckets[pid].append(round_row)

    # Build findings
    orb_counts = {pid_ign.get(pid, str(pid)): len(rnds)
                  for pid, rnds in player_buckets.items()}

    findings: list[dict] = []
    for pid, rounds in player_buckets.items():
        if len(rounds) < MIN_N_ULT:
            continue
        ign      = pid_ign.get(pid, str(pid))
        agents   = pid_agent.get(pid, [])
        agent_id = max(set(agents), key=agents.count) if agents else None
        n        = len(rounds)
        pct_a    = round(sum(1 for r in rounds if r["site_executed"] == "A") / n * 100)
        findings.append({
            "player":     ign,
            "agent_id":   agent_id,
            "label":      f"{ign} (agent {agent_id})" if agent_id else ign,
            "orb_n":      n,
            "total_n":    n_total,
            "a_pct":      pct_a,
            "b_pct":      100 - pct_a,
            "baseline_a": base_a,
            "baseline_b": base_b,
            "delta_a":    pct_a - base_a,
        })

    conn.close()
    findings.sort(key=lambda x: -x["orb_n"])
    max_orb = max(orb_counts.values(), default=0)

    if findings:
        status = "found"
        reason = f"{len(findings)} player(s) had {MIN_N_ULT}+ one-orb-off rounds."
    elif max_orb == 0:
        status = "no_results"
        reason = (f"Ult charge data available but no player was found one orb off "
                  f"in any of the {n_total} ATK rounds.")
    else:
        status = "insufficient_sample"
        per = ", ".join(f"{p}: {n}" for p, n in sorted(orb_counts.items()) if n > 0)
        reason = (f"One-orb-off rounds found but below threshold of {MIN_N_ULT}. "
                  f"Counts: {per}.")

    return {
        "status":     status,
        "reason":     reason,
        "findings":   findings,
        "atk_rounds": n_total,
        "orb_counts": orb_counts,
        "threshold":  MIN_N_ULT,
        "baseline_a": base_a,
        "baseline_b": base_b,
    }


# ── Q3: Defensive stacking on low economy ────────────────────────────────────

def def_stacking(team_name: str, map_name: str = None) -> dict:
    """
    For eco/half DEF rounds: classify T=15–25s setup as A-heavy / B-heavy /
    Mid-heavy / Default using position snapshots.
    """
    conn = _db()
    matches = _team_matches(conn, team_name, map_name)
    if not matches:
        conn.close()
        return {
            "status":   "data_missing",
            "reason":   f"No matches found for '{team_name}'.",
            "findings": {},
        }

    eco_half_total   = 0
    rounds_with_pos  = 0
    maps_no_pos: list[str] = []
    classified: dict = {}

    for m in matches:
        map_n  = m["map_name"] or ""
        thresh = _SITE_Y.get(map_n.lower())
        if not thresh:
            continue

        def_rounds = conn.execute("""
            SELECT round_number FROM round_states
            WHERE match_id=? AND side='def' AND team_number=?
              AND economy_tier IN ('eco','half')
        """, (m["match_id"], m["team_num"])).fetchall()

        if not def_rounds:
            continue

        eco_half_total += len(def_rounds)
        map_had_pos = False

        for rrow in def_rounds:
            rn = rrow["round_number"]
            pos_rows = conn.execute("""
                SELECT player_id, y FROM positions
                WHERE match_id=? AND round_number=? AND team_number=? AND alive=1
                  AND game_time_ms BETWEEN 15000 AND 25000
                ORDER BY player_id, game_time_ms
            """, (m["match_id"], rn, m["team_num"])).fetchall()

            if not pos_rows:
                continue

            map_had_pos = True
            seen_pid: set = set()
            ys: list[float] = []
            for p in pos_rows:
                if p["player_id"] not in seen_pid:
                    ys.append(p["y"])
                    seen_pid.add(p["player_id"])

            if not ys:
                continue

            rounds_with_pos += 1
            n_a   = sum(1 for y in ys if y >= thresh["a_min"])
            n_b   = sum(1 for y in ys if y <= thresh["b_max"])
            total = len(ys)

            if n_a >= max(3, total * 0.6):
                cls = "A-heavy"
            elif n_b >= max(3, total * 0.6):
                cls = "B-heavy"
            elif (total - n_a - n_b) >= max(3, total * 0.6):
                cls = "Mid-heavy"
            else:
                cls = "Default"

            if map_n not in classified:
                classified[map_n] = {"A-heavy": 0, "B-heavy": 0,
                                     "Mid-heavy": 0, "Default": 0, "total": 0}
            classified[map_n][cls]     += 1
            classified[map_n]["total"] += 1

        if not map_had_pos and map_n not in maps_no_pos:
            maps_no_pos.append(map_n)

    conn.close()
    findings = {k: v for k, v in classified.items() if v["total"] >= MIN_N_STACK}

    if findings:
        status = "found"
        reason = (f"Found position data for {rounds_with_pos} eco/half DEF rounds "
                  f"across {len(findings)} map(s).")
    elif rounds_with_pos > 0:
        status = "insufficient_sample"
        reason = (f"{rounds_with_pos} eco/half DEF rounds had positions, "
                  f"but no map reached the minimum of {MIN_N_STACK}.")
    elif eco_half_total > 0:
        status = "data_missing"
        reason = (f"{eco_half_total} eco/half DEF rounds found, but none have "
                  f"position data. Maps without positions: "
                  f"{', '.join(maps_no_pos) if maps_no_pos else 'all maps'}.")
    else:
        status = "data_missing"
        reason = "No eco or half-buy DEF rounds found in round_states."

    return {
        "status":          status,
        "reason":          reason,
        "findings":        findings,
        "eco_half_total":  eco_half_total,
        "rounds_with_pos": rounds_with_pos,
        "maps_no_pos":     maps_no_pos,
        "threshold":       MIN_N_STACK,
    }


# ── Q4 / Q5: Defensive weapon patterns ───────────────────────────────────────

def weapon_def_patterns(team_name: str, weapon: str,
                        map_name: str = None) -> dict:
    """
    For a specific DEF weapon (Judge / Operator):
    who uses it, from which zone (via victim position), at what timing?
    """
    conn = _db()

    sql = """
        SELECT k.killer_id, k.victim_y, k.round_time_ms, m.map_name, k.match_id
        FROM kills k
        JOIN matches m ON m.match_id = k.match_id
        JOIN series s ON s.series_id = m.series_id
        WHERE k.side='def' AND k.weapon=:weapon
          AND ((LOWER(s.team1_name)=LOWER(:name) AND k.killer_team=1)
               OR (LOWER(s.team2_name)=LOWER(:name) AND k.killer_team=2))
    """
    p: dict = {"name": team_name, "weapon": weapon}
    if map_name:
        sql += " AND LOWER(m.map_name)=LOWER(:map)"
        p["map"] = map_name

    kills = [dict(r) for r in conn.execute(sql, p).fetchall()]
    total_kills = len(kills)

    if not kills:
        conn.close()
        return {
            "status":       "no_results",
            "reason":       f"No DEF kills with {weapon} found" +
                            (f" on {map_name}" if map_name else "") + ".",
            "findings":     [],
            "total_kills":  0,
            "threshold":    MIN_N_WEAPON,
        }

    buckets: dict[tuple, dict] = {}
    for k in kills:
        pl  = conn.execute("SELECT ign FROM players WHERE player_id=?", (k["killer_id"],)).fetchone()
        ag  = conn.execute(
            "SELECT agent_id FROM match_players WHERE match_id=? AND player_id=?",
            (k["match_id"], k["killer_id"])
        ).fetchone()
        ign      = pl["ign"] if pl else str(k["killer_id"])
        agent_id = ag["agent_id"] if ag else None
        map_n    = k["map_name"] or ""
        zone     = _classify_y(map_n, k["victim_y"]) or "Unknown"
        timing_s = (k["round_time_ms"] or 0) / 1000

        key = (ign, agent_id, map_n)
        if key not in buckets:
            buckets[key] = {
                "player":   ign,
                "agent_id": agent_id,
                "label":    f"{ign} (agent {agent_id})" if agent_id else ign,
                "map":      map_n,
                "weapon":   weapon,
                "zones":    defaultdict(int),
                "timings":  [],
                "total":    0,
            }
        buckets[key]["zones"][zone] += 1
        buckets[key]["timings"].append(timing_s)
        buckets[key]["total"] += 1

    conn.close()

    below_threshold = 0
    findings: list[dict] = []
    for entry in buckets.values():
        if entry["total"] < MIN_N_WEAPON:
            below_threshold += 1
            continue
        timings = entry.pop("timings")
        entry["zones"]      = dict(entry["zones"])
        entry["avg_timing"] = round(sum(timings) / len(timings), 1)
        entry["small"]      = entry["total"] < 5
        findings.append(entry)

    findings.sort(key=lambda x: (-x["total"], x["player"]))

    if findings:
        status = "found"
        reason = f"{total_kills} total DEF kills with {weapon}. {len(findings)} player/map combination(s) above threshold."
    else:
        status = "insufficient_sample"
        reason = (f"{total_kills} kill(s) with {weapon} on defense, but no player/map "
                  f"combination reached the minimum of {MIN_N_WEAPON}. "
                  f"{below_threshold} combination(s) below threshold.")

    return {
        "status":          status,
        "reason":          reason,
        "findings":        findings,
        "total_kills":     total_kills,
        "below_threshold": below_threshold,
        "threshold":       MIN_N_WEAPON,
    }
