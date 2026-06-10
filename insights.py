"""
Conditional probability insight engine.

Discovers patterns of the form:
  "Given [condition], this team executes [site] X% of the time  (baseline: Y%)"

Conditions:
  - Round state  (economy, score, momentum, ult availability)
  - Event triggers  (first-contact location, which player got first blood, timing)
  - Kill sequence  (2+ kill lead)
  - Site repetition

Returns structured data: insights ranked by z-score, category breakdowns, and
auto-generated pattern sentences — consumed by both CLI and web report.
"""

import json
import math
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "valorant.db"

MIN_N        = 5    # minimum rounds matching condition (show nothing below this)
MIN_DELTA_PP = 10   # minimum pp swing from baseline
MIN_Z        = 1.2  # minimum z-score
SMALL_N      = 8    # warn flag below this

# Y-coordinate site thresholds per map (same as antistrat._MAP_SITE_THRESHOLDS)
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


def _z(p: float, base: float, n: int) -> float:
    se = math.sqrt(base * (1 - base) / n) if n > 0 else 0
    return (p - base) / se if se > 0 else 0.0


def _stars(z: float) -> str:
    az = abs(z)
    if az >= 2.5: return "★★★"
    if az >= 1.8: return "★★"
    return "★"


def _confidence(z: float) -> str:
    if z >= 2.0:  return "High"
    if z >= 1.4:  return "Medium"
    return "Low"


def _kill_side(map_name: str, vy) -> str | None:
    if vy is None:
        return None
    thresh = _SITE_Y.get((map_name or "").lower())
    if not thresh:
        return None
    if vy > thresh["a_min"]: return "A"
    if vy < thresh["b_max"]: return "B"
    return "Mid"


def _pattern_sentence(ins: dict) -> str:
    """One coach-readable sentence describing an insight."""
    site  = ins["site"]
    pct   = ins["conditional_pct"]
    base  = ins["baseline_pct"]
    n     = ins["n"]
    label = ins["label"]
    cat   = ins["category"]

    if cat == "First Contact":
        if "A-side" in label:
            return (f"When first pressure lands on A-side, they almost never rotate — "
                    f"{pct}% A finish vs {base}% baseline ({n} rounds).")
        if "B-side" in label:
            return (f"B-side opening contact strongly predicts a B execute — "
                    f"{pct}% vs {base}% baseline ({n} rounds).")
        if "Mid" in label:
            return (f"Mid contact almost always ends as a {site} execute — "
                    f"{pct}% vs {base}% baseline ({n} rounds).")

    if cat == "Economy":
        return (f"On {label.lower()}, they show a clear {site}-site preference — "
                f"{pct}% vs {base}% baseline ({n} rounds).")

    if cat == "Ult Availability":
        player = label.split(" has")[0]
        return (f"With {player}'s ultimate ready, they heavily favour {site} site — "
                f"{pct}% vs {base}% baseline ({n} rounds).")

    if cat == "Player Trigger":
        player = label.split(" gets")[0]
        return (f"{player} drives {site}-side executes — {pct}% {site} finish "
                f"when he gets first blood ({n} rounds).")

    if cat == "Opening Duel":
        if "Win" in label:
            return (f"After winning the opening duel, they commit {site}-side — "
                    f"{pct}% vs {base}% baseline ({n} rounds).")
        return (f"Even after losing first contact, they favour {site} heavily — "
                f"{pct}% vs {base}% baseline ({n} rounds). No reliable reset pattern.")

    if cat == "Momentum":
        trigger = "a win" if "win" in label else "a loss"
        return (f"Off {trigger}, they lean {site} — {pct}% vs {base}% baseline ({n} rounds).")

    if cat == "Score State":
        return (f"When {label.lower()}, they favour {site} — "
                f"{pct}% vs {base}% baseline ({n} rounds).")

    if cat == "Site Pattern":
        prev = "A" if "A site" in label else "B"
        return (f"After an {prev} execute, they switch to {site} {pct}% of the time ({n} rounds).")

    if cat == "Contact Timing":
        pace = "rush pace" if "20s" in label else "patient play"
        return (f"On {pace}, they favour {site} — {pct}% vs {base}% baseline ({n} rounds).")

    if cat == "Scoreline":
        if "2+" in label:
            return (f"After establishing a 2+ kill lead, they finish on {site} — "
                    f"{pct}% vs {base}% baseline ({n} rounds).")
        return (f"Without a kill advantage, they lean {site} — "
                f"{pct}% vs {base}% baseline ({n} rounds).")

    if cat == "Round Phase":
        return (f"During {label.lower()}, they lean {site} — "
                f"{pct}% vs {base}% baseline ({n} rounds).")

    return (f"Given {label}: {site} site {pct}% vs {base}% baseline ({n} rounds).")


def find_insights(team_name: str, map_name: str | None = None) -> dict:
    """
    Mine conditional tendencies for a team (and optionally a single map).

    Returns a dict with:
      team, map, total, n_a, n_b, baseline_a, baseline_b,
      plant_total, baseline_plant_rate, n_matches, opponents,
      insights     — list of finding dicts sorted by z-score (strongest first)
      patterns     — list of auto-generated coach-readable sentences
      breakdowns   — raw count tables by category for supporting evidence

    Each insight has:
      rank, category, label, site, conditional_pct, baseline_pct,
      n, count_a, count_b, delta, z, bar, stars, confidence, small_sample,
      plant_rate, baseline_plant_rate, win_rate,
      rounds — [{match_id, round, site, won}] contributing rounds for clip links.
    """
    conn = _db()
    try:
        return _compute(conn, team_name, map_name)
    finally:
        conn.close()


def _compute(conn, team_name: str, map_name: str | None) -> dict:
    map_clause = "AND m.map_name = :map" if map_name else ""
    params: dict = {"name": team_name}
    if map_name:
        params["map"] = map_name

    # ── Matches ───────────────────────────────────────────────────────────────
    matches = conn.execute(f"""
        SELECT m.match_id,
               CASE WHEN LOWER(s.team1_name)=LOWER(:name) THEN 1 ELSE 2 END AS team_num,
               s.team1_name, s.team2_name, s.start_date, m.map_name
        FROM matches m JOIN series s ON s.series_id = m.series_id
        WHERE (LOWER(s.team1_name)=LOWER(:name) OR LOWER(s.team2_name)=LOWER(:name))
          {map_clause}
        ORDER BY s.start_date
    """, params).fetchall()
    matches = [dict(m) for m in matches]
    if not matches:
        return {}

    # ── Roster ────────────────────────────────────────────────────────────────
    pid_to_ign: dict[int, str] = {}
    team_pids:  set[int]       = set()
    for m in matches:
        rows = conn.execute("""
            SELECT mp.player_id, p.ign FROM match_players mp
            JOIN players p ON p.player_id = mp.player_id
            WHERE mp.match_id = ? AND mp.team_number = ?
        """, (m["match_id"], m["team_num"])).fetchall()
        for r in rows:
            short = r["ign"].split()[-1] if " " in r["ign"] else r["ign"]
            pid_to_ign[r["player_id"]] = short
            team_pids.add(r["player_id"])

    # ── ATK rounds with inferred site ─────────────────────────────────────────
    site_rows = conn.execute(f"""
        SELECT rs.match_id, rs.round_number, rs.economy_tier,
               rs.score_self, rs.score_opp, rs.won_prev_round,
               rs.ult_players_json, rs.site_executed, rs.won_round
        FROM round_states rs
        JOIN matches m ON m.match_id = rs.match_id
        JOIN series  s ON s.series_id = rs.series_id
        WHERE rs.side = 'atk'
          AND rs.site_executed IN ('A', 'B')
          AND (
            (LOWER(s.team1_name)=LOWER(:name) AND rs.team_number = 1)
            OR (LOWER(s.team2_name)=LOWER(:name) AND rs.team_number = 2)
          )
          {map_clause}
        ORDER BY rs.match_id, rs.round_number
    """, params).fetchall()

    if not site_rows:
        return {}

    # ── win_condition per round ───────────────────────────────────────────────
    win_cond: dict[tuple, str] = {}
    for m in matches:
        rows = conn.execute(
            "SELECT round_number, win_condition FROM rounds WHERE match_id = ?",
            (m["match_id"],)
        ).fetchall()
        for r in rows:
            win_cond[(m["match_id"], r["round_number"])] = r["win_condition"]

    # ── Kill sequence per (match_id, round_number) ────────────────────────────
    round_kills: dict[tuple, dict] = {}
    for m in matches:
        mid, tnum = m["match_id"], m["team_num"]
        map_n = (m.get("map_name") or "").strip()
        rows = conn.execute("""
            SELECT round_number, round_time_ms, killer_team, victim_team,
                   killer_id, victim_y, is_first_kill
            FROM kills
            WHERE match_id = ? AND side = 'atk'
            ORDER BY round_number, round_time_ms
        """, (mid,)).fetchall()
        for k in rows:
            key = (mid, k["round_number"])
            if key not in round_kills:
                round_kills[key] = {"team": tnum, "map": map_n, "kills": []}
            round_kills[key]["kills"].append(dict(k))

    # ── Build enriched round list ─────────────────────────────────────────────
    rounds: list[dict] = []
    for r in site_rows:
        mid  = r["match_id"]
        rnum = r["round_number"]
        ult_ids = json.loads(r["ult_players_json"] or "[]")

        rk    = round_kills.get((mid, rnum), {})
        kills = rk.get("kills", [])
        tnum  = rk.get("team")
        map_n = rk.get("map", "")

        first_kill    = next((k for k in kills if k.get("is_first_kill")), kills[0] if kills else None)
        fc_side:       str | None = None
        fc_timing_ms:  int | None = None
        fb_killer_ign: str | None = None
        fb_result:     str | None = None

        if first_kill:
            fc_side      = _kill_side(map_n, first_kill.get("victim_y"))
            fc_timing_ms = first_kill.get("round_time_ms")
            kt           = first_kill.get("killer_team")
            fb_result    = "won" if kt == tnum else "lost"
            if kt == tnum:
                fb_killer_ign = pid_to_ign.get(first_kill.get("killer_id"))

        net = 0
        max_lead = 0
        for k in kills:
            if k.get("killer_team") == tnum:   net += 1
            elif k.get("victim_team") == tnum: net -= 1
            max_lead = max(max_lead, net)

        wc      = win_cond.get((mid, rnum))
        planted = wc in ("bomb", "defuse")

        rounds.append({
            "match_id":      mid,
            "round":         rnum,
            "site":          r["site_executed"],
            "econ":          r["economy_tier"],
            "score_self":    r["score_self"],
            "score_opp":     r["score_opp"],
            "won_prev":      r["won_prev_round"],
            "won_round":     r["won_round"],
            "ult_igns":      [pid_to_ign[p] for p in ult_ids
                              if p in pid_to_ign and p in team_pids],
            "fb":            fb_result,
            "fc_side":       fc_side,
            "fc_timing_ms":  fc_timing_ms,
            "fb_killer_ign": fb_killer_ign,
            "max_lead":      max_lead,
            "planted":       planted,
        })

    # ── Previous ATK-round lookup ─────────────────────────────────────────────
    prev_atk: dict[tuple, dict] = {}
    per_match: dict[int, list]  = {}
    for r in rounds:
        per_match.setdefault(r["match_id"], []).append(r)
    for mid, mrs in per_match.items():
        mrs.sort(key=lambda x: x["round"])
        for i, r in enumerate(mrs):
            if i > 0:
                prev_atk[(mid, r["round"])] = mrs[i - 1]

    # ── Baseline ──────────────────────────────────────────────────────────────
    n_total        = len(rounds)
    n_a            = sum(1 for r in rounds if r["site"] == "A")
    baseline_a     = n_a / n_total
    baseline_b     = 1.0 - baseline_a
    baseline_plant = sum(1 for r in rounds if r["planted"]) / n_total

    # ── Insight builder ───────────────────────────────────────────────────────
    raw: list[dict] = []

    def check(label: str, category: str, cond) -> None:
        sub   = [r for r in rounds if cond(r)]
        n     = len(sub)
        if n < MIN_N:
            return
        sub_a = sum(1 for r in sub if r["site"] == "A")
        pct_a = sub_a / n
        pct_b = 1.0 - pct_a
        za    = _z(pct_a, baseline_a, n)
        zb    = _z(pct_b, baseline_b, n)
        if pct_a >= baseline_a:
            site, pct, base, z = "A", pct_a, baseline_a, za
        else:
            site, pct, base, z = "B", pct_b, baseline_b, zb
        delta_pp = round((pct - base) * 100)
        if abs(delta_pp) < MIN_DELTA_PP or abs(z) < MIN_Z:
            return
        plant_rate = sum(1 for r in sub if r.get("planted")) / n
        win_rate   = sum(1 for r in sub if r.get("won_round")) / n
        raw.append({
            "category":            category,
            "label":               label,
            "site":                site,
            "conditional_pct":     round(pct  * 100),
            "baseline_pct":        round(base * 100),
            "n":                   n,
            "count_a":             sub_a,
            "count_b":             n - sub_a,
            "delta":               delta_pp,
            "z":                   round(abs(z), 1),
            "bar":                 min(100, round(abs(z) / 3.5 * 100)),
            "stars":               _stars(z),
            "confidence":          _confidence(abs(z)),
            "small_sample":        n < SMALL_N,
            "plant_rate":          round(plant_rate   * 100),
            "baseline_plant_rate": round(baseline_plant * 100),
            "win_rate":            round(win_rate * 100),
            "rounds":              [{"match_id": r["match_id"],
                                     "round":    r["round"],
                                     "site":     r["site"],
                                     "won":      bool(r["won_round"])}
                                    for r in sorted(sub, key=lambda x:
                                                    (x["match_id"], x["round"]))],
        })

    # ── Conditions ────────────────────────────────────────────────────────────
    check("Eco round",  "Economy", lambda r: r["econ"] == "eco")
    check("Half-buy",   "Economy", lambda r: r["econ"] == "half")
    check("Full buy",   "Economy", lambda r: r["econ"] == "full")
    check("Heavy buy",  "Economy", lambda r: r["econ"] == "heavy")

    check("Coming off a round win",  "Momentum", lambda r: r["won_prev"] == 1)
    check("Coming off a round loss", "Momentum", lambda r: r["won_prev"] == 0)

    check("Leading on the scoreboard",  "Score State", lambda r: r["score_self"] > r["score_opp"])
    check("Trailing on the scoreboard", "Score State", lambda r: r["score_self"] < r["score_opp"])
    check("Score tied",                 "Score State", lambda r: r["score_self"] == r["score_opp"])

    check("Win first contact",  "Opening Duel", lambda r: r["fb"] == "won")
    check("Lose first contact", "Opening Duel", lambda r: r["fb"] == "lost")

    all_igns = sorted({i for r in rounds for i in r["ult_igns"]})
    for ign in all_igns:
        check(f"{ign} has ultimate ready", "Ult Availability",
              lambda r, i=ign: i in r["ult_igns"])

    check("Pistol round (R1 or R13)", "Round Phase",
          lambda r: r["round"] in (1, 13))
    check("Early half (rounds 2–4 or 14–16)", "Round Phase",
          lambda r: r["round"] in (2, 3, 4, 14, 15, 16))
    check("Late half (rounds 10–12 or 22–24)", "Round Phase",
          lambda r: r["round"] in (10, 11, 12, 22, 23, 24))
    check("Overtime (12–12 or later)", "Round Phase",
          lambda r: r["score_self"] >= 12 and r["score_opp"] >= 12)

    check("Previous ATK round was A site", "Site Pattern",
          lambda r: (prev_atk.get((r["match_id"], r["round"])) or {}).get("site") == "A")
    check("Previous ATK round was B site", "Site Pattern",
          lambda r: (prev_atk.get((r["match_id"], r["round"])) or {}).get("site") == "B")

    check("first contact on A-side", "First Contact",
          lambda r: r.get("fc_side") == "A")
    check("first contact on B-side", "First Contact",
          lambda r: r.get("fc_side") == "B")
    check("first contact in Mid",    "First Contact",
          lambda r: r.get("fc_side") == "Mid")

    check("rush pace (first contact under 20s)", "Contact Timing",
          lambda r: r.get("fc_timing_ms") is not None and r["fc_timing_ms"] < 20_000)
    check("patient play (first contact 40s+)", "Contact Timing",
          lambda r: r.get("fc_timing_ms") is not None and r["fc_timing_ms"] >= 40_000)

    for ign in sorted({r.get("fb_killer_ign") for r in rounds if r.get("fb_killer_ign")}):
        check(f"{ign} gets first blood", "Player Trigger",
              lambda r, i=ign: r.get("fb_killer_ign") == i)

    check("2+ kill lead established", "Scoreline",
          lambda r: r.get("max_lead", 0) >= 2)
    check("no kill lead at any point", "Scoreline",
          lambda r: r.get("max_lead", 0) == 0)

    # ── Sort and rank ─────────────────────────────────────────────────────────
    raw.sort(key=lambda x: x["z"], reverse=True)
    for i, ins in enumerate(raw, 1):
        ins["rank"] = i

    passing_cats = {ins["category"] for ins in raw}

    # ── Category breakdowns (supporting evidence for passing categories only) ──
    def _bd(subset: list) -> dict:
        a = sum(1 for r in subset if r["site"] == "A")
        return {"n": len(subset), "a": a, "b": len(subset) - a}

    all_breakdowns: dict = {}

    # First Contact
    fc_rounds = [r for r in rounds if r.get("fc_side")]
    if fc_rounds:
        bd = {s: _bd([r for r in fc_rounds if r["fc_side"] == s])
              for s in ("A", "B", "Mid") if any(r["fc_side"] == s for r in fc_rounds)}
        if bd:
            all_breakdowns["First Contact"] = {"label": "First contact → site", "data": bd}

    # Economy
    econ_groups: dict = {}
    for r in rounds:
        econ_groups.setdefault(r["econ"] or "?", []).append(r)
    bd_econ = {t: _bd(econ_groups[t]) for t in ("eco","half","full","heavy")
               if t in econ_groups and len(econ_groups[t]) >= 2}
    if bd_econ:
        all_breakdowns["Economy"] = {"label": "Economy tier → site", "data": bd_econ}

    # Opening duel
    bd_fb = {}
    for outcome, key in (("won", "won FB"), ("lost", "lost FB")):
        sub = [r for r in rounds if r.get("fb") == outcome]
        if len(sub) >= 2:
            bd_fb[key] = _bd(sub)
    if bd_fb:
        all_breakdowns["Opening Duel"] = {"label": "Opening duel → site", "data": bd_fb}

    # Scoreline
    bd_sc: dict = {}
    for r in rounds:
        if r["score_self"] > r["score_opp"]:   key = "leading"
        elif r["score_self"] < r["score_opp"]: key = "trailing"
        else:                                   key = "tied"
        bd_sc.setdefault(key, []).append(r)
    bd_sc_out = {k: _bd(v) for k, v in bd_sc.items() if len(v) >= 2}
    if bd_sc_out:
        all_breakdowns["Score State"] = {"label": "Score state → site", "data": bd_sc_out}

    # Only include breakdowns for categories that have at least one passing insight
    breakdowns = {cat: bd for cat, bd in all_breakdowns.items() if cat in passing_cats}

    # ── Opponents ─────────────────────────────────────────────────────────────
    opponents: list[str] = []
    seen:      set[str]  = set()
    for m in matches:
        opp = m["team2_name"] if team_name.lower() in (m["team1_name"] or "").lower() \
              else m["team1_name"]
        if opp and opp not in seen:
            opponents.append(opp)
            seen.add(opp)

    patterns = [_pattern_sentence(ins) for ins in raw]

    return {
        "team":                team_name,
        "map":                 map_name or "All Maps",
        "total":               n_total,
        "n_a":                 n_a,
        "n_b":                 n_total - n_a,
        "baseline_a":          round(baseline_a * 100),
        "baseline_b":          round(baseline_b * 100),
        "plant_total":         sum(1 for r in rounds if r["planted"]),
        "baseline_plant_rate": round(baseline_plant * 100),
        "n_matches":           len(matches),
        "opponents":           opponents,
        "insights":            raw,
        "patterns":            patterns,
        "breakdowns":          breakdowns,
    }
