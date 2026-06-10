"""
analyze_g2_lotus.py
-------------------
Cross-match pattern analysis of G2 Esports on Lotus for counter-strat building.

Patterns extracted:
  1. Ultimate cast locations (with Viper Pit cast-position correction)
  2. Unconventional weapon purchases (Operator, Judge, other outliers) + positioning
  3. Eco / low-money stack locations
  4. General stack locations (all round types, defense)
  5. Site execute tendencies (site, economy, round timing, entry style)

Output: g2_lotus_stats.json (structured) + console summary.
"""

import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent / "valorant.db"

# match_id -> series_id  (the 5 requested G2 Lotus maps)
TARGETS = {
    239731: 104004,   # vs NRG
    239296: 103866,   # vs KRU
    239192: 103837,   # vs 100T
    232695: 101589,   # vs C9
    232849: 101640,   # 5th match
}

AGENT_NAMES = {
    1: "Breach", 2: "Raze", 3: "Cypher", 4: "Sova", 5: "Killjoy",
    6: "Viper", 7: "Phoenix", 8: "Brimstone", 9: "Sage", 10: "Reyna",
    11: "Omen", 12: "Jett", 13: "Skye", 14: "Yoru", 15: "Astra",
    16: "KAY/O", 17: "Chamber", 18: "Neon", 19: "Fade", 20: "Harbor",
    21: "Gekko", 22: "Deadlock", 23: "Iso", 25: "Clove", 26: "Vyse",
    27: "Tejo", 28: "Waylay", 29: "Veto", 33: "Miks",
}

# rib.gg loadout bands (verified against round_loadouts buy values for 239296):
# 1 = 0-5k, 2 = 5-10k, 3 = 10-20k, 4 = 20k+. Band 3 spans plenty of forced /
# light buys (e.g. 10.8k two-rifle save), so "low buy" cannot be read off the
# band alone — see MatchData round low_buy, which compares actual team values.
TIER_NAMES = {1: "eco", 2: "semi-eco", 3: "semi-buy", 4: "full"}

# A defensive round counts as low-buy when G2's parsed team loadout value is
# under this fraction of the opponent's (or band says eco/semi-eco / pistol).
LOW_BUY_RATIO = 0.6

WEIRD_WEAPONS = {"Operator", "Judge", "Outlaw", "Marshal", "Bucky", "Shorty",
                 "Odin", "Ares", "Bulldog", "Bandit"}

# Weird PRIMARY buys (from buy-phase data). Shorty excluded: it is a standard
# pocket secondary on pro full buys (confirmed in round_loadouts).
WEIRD_PRIMARY = {"Operator", "Judge", "Outlaw", "Marshal", "Bucky",
                 "Odin", "Ares", "Bulldog"}

# Site-zone callouts: being here with 3+ attackers = executing that site
SITE_ZONES = {
    "A": {"A Site", "A Hut", "A Tree", "A Drop"},
    "B": {"B Site", "B Upper"},
    "C": {"C Site", "C Bend"},
}

# Forward/aggressive callouts when held by DEFENDERS early in a round
FORWARD_DEF_CALLOUTS = {
    "A Main", "A Lobby", "A Root", "A Rubble",
    "B Main", "B Pillars",
    "C Main", "C Mound", "C Lobby", "C Door",
    "Attacker Spawn",
}


# ── setup ─────────────────────────────────────────────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_callouts():
    with open(Path(__file__).parent / "lotus_callouts.json", encoding="utf-8") as f:
        data = json.load(f)
    return data["callouts"]


CALLOUTS = load_callouts()
# world-coord callout list: (name, superRegion, wx, wy)
CALLOUT_PTS = [(c["name"], c["superRegionName"], c["location"]["x"], c["location"]["y"])
               for c in CALLOUTS]


def nearest_callout(wx, wy):
    """Return (callout_name, super_region) for world coords."""
    best = min(CALLOUT_PTS, key=lambda c: (c[2] - wx) ** 2 + (c[3] - wy) ** 2)
    return best[0], best[1]


# ── per-match context ─────────────────────────────────────────────────────────

class MatchData:
    def __init__(self, conn, match_id):
        self.match_id = match_id
        row = conn.execute("""
            SELECT m.map_name, m.series_id, s.team1_name, s.team2_name, s.event_name
            FROM matches m JOIN series s ON s.series_id = m.series_id
            WHERE m.match_id = ?""", (match_id,)).fetchone()
        self.ok = row is not None and row["map_name"] == "Lotus"
        if not row:
            return
        self.map_name = row["map_name"]
        self.series_id = row["series_id"]
        t1, t2 = row["team1_name"] or "", row["team2_name"] or ""
        self.g2_team = 1 if "g2" in t1.lower() else 2
        self.opponent = t2 if self.g2_team == 1 else t1
        self.label = f"vs {self.opponent}"

        # players
        self.players = {}   # player_id -> {ign, agent, team}
        for r in conn.execute("""
            SELECT mp.player_id, mp.team_number, mp.agent_id, p.ign
            FROM match_players mp JOIN players p ON p.player_id = mp.player_id
            WHERE mp.match_id = ?""", (match_id,)):
            self.players[r["player_id"]] = {
                "ign": r["ign"], "team": r["team_number"],
                "agent": AGENT_NAMES.get(r["agent_id"], f"Agent{r['agent_id']}"),
            }

        # rounds: attacking team + G2 loadout tier
        self.rounds = {}
        for r in conn.execute("""
            SELECT round_number, attacking_team, winning_team,
                   team1_loadout, team2_loadout
            FROM rounds WHERE match_id = ?""", (match_id,)):
            rn = r["round_number"]
            tier_num = r["team1_loadout"] if self.g2_team == 1 else r["team2_loadout"]
            tier = TIER_NAMES.get(tier_num, "unknown")
            if rn in (1, 13):
                tier = "pistol"
            self.rounds[rn] = {
                "atk_team": r["attacking_team"],
                "g2_side": "atk" if r["attacking_team"] == self.g2_team else "def",
                "g2_won": r["winning_team"] == self.g2_team,
                "tier": tier,
            }

        # site_executed from round_states (G2 atk rounds)
        self.site_executed = {}
        for r in conn.execute("""
            SELECT round_number, site_executed FROM round_states
            WHERE match_id = ? AND team_number = ? AND side = 'atk'""",
                              (match_id, self.g2_team)):
            self.site_executed[r["round_number"]] = r["site_executed"]

        # positions: per (player, round) sorted frame list — G2 players only.
        # NOTE: positions.team_number comes from the 2D replay config whose
        # team order does not always match the series team order, so filter
        # by G2 player ids from match_players instead.
        g2_pids = [pid for pid, p in self.players.items()
                   if p["team"] == self.g2_team]
        self.frames = defaultdict(list)   # (player_id, round) -> [(t, x, y, alive, ult, ultmax)]
        ph = ",".join("?" * len(g2_pids))
        for r in conn.execute(f"""
            SELECT round_number, game_time_ms, player_id, x, y, alive,
                   ult_charges, ult_max
            FROM positions
            WHERE match_id = ? AND player_id IN ({ph}) AND x IS NOT NULL
            ORDER BY player_id, round_number, game_time_ms""",
                              (match_id, *g2_pids)):
            self.frames[(r["player_id"], r["round_number"])].append(
                (r["game_time_ms"], r["x"], r["y"], r["alive"],
                 r["ult_charges"], r["ult_max"]))

        self.has_positions = len(self.frames) > 0

        # kills by G2 players
        self.g2_kills = conn.execute("""
            SELECT round_number, round_time_ms, killer_id, weapon, is_first_kill,
                   victim_x, victim_y
            FROM kills WHERE match_id = ? AND killer_team = ?
            ORDER BY round_number, round_time_ms""",
                                     (match_id, self.g2_team)).fetchall()

        # buy-phase loadouts (parsed from ROUND_STARTING replay frames)
        self.loadouts = {}   # (player_id, round) -> {primary, secondary, value}
        for r in conn.execute(f"""
            SELECT round_number, player_id, primary_weapon, secondary_weapon,
                   loadout_value
            FROM round_loadouts
            WHERE match_id = ? AND player_id IN ({ph})""",
                              (match_id, *g2_pids)):
            self.loadouts[(r["player_id"], r["round_number"])] = {
                "primary": r["primary_weapon"],
                "secondary": r["secondary_weapon"],
                "value": r["loadout_value"],
            }
        self.has_loadouts = len(self.loadouts) > 0

        # per-round team buy totals (both teams) → low_buy flag on each round.
        # The rib loadout band on rounds is too coarse: band 3 (10-20k) covers
        # forced saves like 2 rifles + 3 pistols, which is why band-based eco
        # detection only ever matched pistols.
        team_buy = defaultdict(lambda: [0, 0])   # round -> [g2_total, opp_total]
        for r in conn.execute("""
            SELECT rl.round_number, rl.loadout_value, mp.team_number
            FROM round_loadouts rl
            JOIN match_players mp
              ON mp.match_id = rl.match_id AND mp.player_id = rl.player_id
            WHERE rl.match_id = ?""", (match_id,)):
            idx = 0 if r["team_number"] == self.g2_team else 1
            team_buy[r["round_number"]][idx] += r["loadout_value"] or 0
        for rn, rinfo in self.rounds.items():
            g2_buy, opp_buy = team_buy.get(rn, (None, None))
            rinfo["g2_buy"] = g2_buy
            rinfo["opp_buy"] = opp_buy
            rinfo["low_buy"] = (
                rinfo["tier"] in ("pistol", "eco", "semi-eco")
                or (bool(g2_buy) and bool(opp_buy)
                    and g2_buy < LOW_BUY_RATIO * opp_buy))

        # all kills with victim coords (fallback exec-site inference)
        self.all_kills = conn.execute("""
            SELECT round_number, round_time_ms, victim_x, victim_y
            FROM kills WHERE match_id = ? AND victim_x IS NOT NULL
            ORDER BY round_number, round_time_ms""", (match_id,)).fetchall()

    def ign(self, pid):
        p = self.players.get(pid)
        return p["ign"] if p else f"#{pid}"

    def agent(self, pid):
        p = self.players.get(pid)
        return p["agent"] if p else "?"


# ── 1. ult casts ──────────────────────────────────────────────────────────────

def viper_pit_cast_pos(frames, drop_idx):
    """
    Viper's charge drop = pit EXIT, not cast. Walk backward from the drop
    frame to find the last near-stationary window (>=4s, avg speed <150 u/s)
    and return its centroid — that is where the pit was placed.
    """
    SPEED_MAX = 150.0     # units/sec ~ slow-walk threshold (run ~675 u/s)
    MIN_WINDOW_MS = 4000

    i = drop_idx
    win_end = None
    win_pts = []
    while i > 0:
        t1, x1, y1 = frames[i][0], frames[i][1], frames[i][2]
        t0, x0, y0 = frames[i - 1][0], frames[i - 1][1], frames[i - 1][2]
        dt = max(1, t1 - t0)
        speed = math.hypot(x1 - x0, y1 - y0) / dt * 1000.0
        if speed < SPEED_MAX:
            if win_end is None:
                win_end = t1
                win_pts = []
            win_pts.append((x1, y1))
            if win_end - t0 >= MIN_WINDOW_MS:
                # keep extending until movement starts
                pass
        else:
            if win_end is not None and (win_end - t1) >= MIN_WINDOW_MS:
                break  # found a long stationary window before a movement burst
            win_end = None
            win_pts = []
        i -= 1

    if win_pts and win_end is not None:
        xs = [p[0] for p in win_pts]
        ys = [p[1] for p in win_pts]
        return sum(xs) / len(xs), sum(ys) / len(ys), True
    # fallback: drop position
    return frames[drop_idx][1], frames[drop_idx][2], False


def detect_ults(md: MatchData):
    casts = []
    for (pid, rn), frames in md.frames.items():
        prev = None
        for idx, fr in enumerate(frames):
            t, x, y, alive, ult, ultmax = fr
            if (prev is not None and ultmax > 0 and prev >= ultmax
                    and ult < ultmax and alive):
                agent = md.agent(pid)
                corrected = False
                if agent == "Viper":
                    x, y, corrected = viper_pit_cast_pos(frames, idx)
                callout, region = nearest_callout(x, y)
                # Lotus minimap transform (rotation -90)
                px = round((y * 0.000072 + 0.454789) * 100, 2)
                py = round((x * -0.000072 + 0.917752) * 100, 2)
                casts.append({
                    "match": md.label, "player": md.ign(pid), "agent": agent,
                    "round": rn, "side": md.rounds.get(rn, {}).get("g2_side", "?"),
                    "time_s": t // 1000, "callout": callout, "region": region,
                    "map_px": px, "map_py": py,
                    "viper_corrected": corrected,
                })
                break  # one ult per round per player
            prev = ult
    return casts


# ── 2. weird weapon buys ──────────────────────────────────────────────────────

def player_pos_at(md, pid, rn, t_ms):
    """Position closest to t_ms (within 6s)."""
    frames = md.frames.get((pid, rn))
    if not frames:
        return None
    best = min(frames, key=lambda f: abs(f[0] - t_ms))
    if abs(best[0] - t_ms) > 6000:
        return None
    return best[1], best[2]


def player_early_callout(md, pid, rn, t_lo=4000, t_hi=20000):
    """Modal callout during the early-round window."""
    frames = md.frames.get((pid, rn))
    if not frames:
        return None
    names = [nearest_callout(f[1], f[2])[0] for f in frames
             if t_lo <= f[0] <= t_hi and f[3]]
    if not names:
        return None
    return Counter(names).most_common(1)[0][0]


def player_play_area(md, pid, rn, t_lo=15000):
    """Modal callout while alive after the opening seconds — where they play."""
    frames = md.frames.get((pid, rn))
    if not frames:
        return None
    names = [nearest_callout(f[1], f[2])[0] for f in frames
             if f[0] >= t_lo and f[3]]
    if not names:
        return None
    return Counter(names).most_common(1)[0][0]


def detect_weird_buys(md: MatchData):
    """
    Primary source: buy-phase inventory (round_loadouts) — the actual purchase,
    read from the last ROUND_STARTING frame of each round.
    Fallback (matches without 2D replay): kill-feed inference.
    """
    out = []
    if md.has_loadouts:
        for (pid, rn), lo in sorted(md.loadouts.items(), key=lambda x: (x[0][0], x[0][1])):
            w = lo["primary"]
            if w not in WEIRD_PRIMARY:
                continue
            rinfo = md.rounds.get(rn, {})
            kill_spots = []
            for k in md.g2_kills:
                if k["killer_id"] == pid and k["round_number"] == rn and k["weapon"] == w:
                    pos = player_pos_at(md, pid, rn, k["round_time_ms"])
                    if pos:
                        kill_spots.append(nearest_callout(*pos)[0])
            out.append({
                "match": md.label, "player": md.ign(pid), "agent": md.agent(pid),
                "weapon": w, "round": rn,
                "side": rinfo.get("g2_side", "?"), "tier": rinfo.get("tier", "?"),
                "loadout_value": lo["value"],
                "kill_callout": kill_spots[0] if kill_spots else None,
                "early_callout": player_early_callout(md, pid, rn),
                "play_area": player_play_area(md, pid, rn),
                "source": "buy-phase",
            })
        return out

    # fallback: infer from kill feed (no replay data for this match)
    seen = set()
    for k in md.g2_kills:
        w = k["weapon"]
        if w not in WEIRD_PRIMARY:
            continue
        pid, rn = k["killer_id"], k["round_number"]
        key = (pid, rn, w)
        if key in seen:
            continue
        seen.add(key)
        rinfo = md.rounds.get(rn, {})
        kill_callout = (nearest_callout(k["victim_x"], k["victim_y"])[0] + "*"
                        if k["victim_x"] is not None else None)
        out.append({
            "match": md.label, "player": md.ign(pid), "agent": md.agent(pid),
            "weapon": w, "round": rn,
            "side": rinfo.get("g2_side", "?"), "tier": rinfo.get("tier", "?"),
            "loadout_value": None,
            "kill_callout": kill_callout, "early_callout": None,
            "play_area": None,
            "source": "killfeed",
        })
    return out


# ── 3/4. stacks ───────────────────────────────────────────────────────────────

def round_setup_regions(md: MatchData, rn, t_snap=15000):
    """Each alive G2 player's (callout, region) at ~t_snap into the round."""
    setup = {}
    for pid in md.players:
        if md.players[pid]["team"] != md.g2_team:
            continue
        frames = md.frames.get((pid, rn))
        if not frames:
            continue
        cand = [f for f in frames if abs(f[0] - t_snap) <= 5000 and f[3]]
        if not cand:
            continue
        f = min(cand, key=lambda f: abs(f[0] - t_snap))
        callout, region = nearest_callout(f[1], f[2])
        setup[md.ign(pid)] = (callout, region)
    return setup


def detect_stacks(md: MatchData):
    """Per G2 DEF round: region counts at 15s, stack flag, aggression flag."""
    out = []
    for rn, rinfo in md.rounds.items():
        if rinfo["g2_side"] != "def":
            continue
        setup = round_setup_regions(md, rn)
        if len(setup) < 4:
            continue
        regions = Counter(reg for _, reg in setup.values())
        stack_region, stack_n = (regions.most_common(1)[0]
                                 if regions else (None, 0))
        forward = [ign for ign, (co, _) in setup.items()
                   if co in FORWARD_DEF_CALLOUTS]
        out.append({
            "match": md.label, "round": rn, "tier": rinfo["tier"],
            "low_buy": rinfo["low_buy"],
            "g2_buy": rinfo["g2_buy"], "opp_buy": rinfo["opp_buy"],
            "won": rinfo["g2_won"],
            "regions": dict(regions),
            "setup": {ign: co for ign, (co, _) in setup.items()},
            "stack_region": stack_region if stack_n >= 3 else None,
            "stack_n": stack_n,
            "aggressive_players": forward,
        })
    return out


# ── 5. site execs ─────────────────────────────────────────────────────────────

def detect_execs(md: MatchData):
    """Per G2 ATK round: executed site, entry time, approach lanes."""
    out = []
    g2_pids = [pid for pid, p in md.players.items() if p["team"] == md.g2_team]
    for rn, rinfo in md.rounds.items():
        if rinfo["g2_side"] != "atk":
            continue

        # timeline: for each player, list of (t, callout)
        tracks = {}
        for pid in g2_pids:
            frames = md.frames.get((pid, rn))
            if frames:
                tracks[pid] = [(f[0], *nearest_callout(f[1], f[2]))
                               for f in frames if f[3]]

        exec_site, exec_t = None, None
        if tracks:
            # scan in 2s steps: first moment >=3 players inside one site zone
            tmax = max(tr[-1][0] for tr in tracks.values() if tr)
            t = 8000
            while t <= tmax and exec_site is None:
                for site, zone in SITE_ZONES.items():
                    n = 0
                    for tr in tracks.values():
                        near = [e for e in tr if abs(e[0] - t) <= 2500]
                        if near and min(near, key=lambda e: abs(e[0] - t))[1] in zone:
                            n += 1
                    if n >= 3:
                        exec_site, exec_t = site, t
                        break
                t += 2000

        source = "positions"
        if exec_site is None:
            se = md.site_executed.get(rn)
            if se:
                exec_site, source = se.upper()[:1], "round_states"
        if exec_site is None and not tracks:
            # no replay data: majority site region of kill locations >=15s in
            regs = Counter()
            for k in md.all_kills:
                if k["round_number"] == rn and (k["round_time_ms"] or 0) >= 15000:
                    reg = nearest_callout(k["victim_x"], k["victim_y"])[1]
                    if reg in ("A", "B", "C"):
                        regs[reg] += 1
            if regs and regs.most_common(1)[0][1] >= 2:
                exec_site, source = regs.most_common(1)[0][0], "kill_locations"

        # approach lanes: callout each exec player held 6-10s before entry
        approaches = []
        if exec_site and exec_t and tracks:
            zone = SITE_ZONES[exec_site]
            for tr in tracks.values():
                near = [e for e in tr if abs(e[0] - exec_t) <= 2500]
                if not (near and min(near, key=lambda e: abs(e[0] - exec_t))[1] in zone):
                    continue
                before = [e for e in tr if exec_t - 11000 <= e[0] <= exec_t - 5000]
                pre = [e[1] for e in before if e[1] not in zone]
                if pre:
                    approaches.append(Counter(pre).most_common(1)[0][0])

        out.append({
            "match": md.label, "round": rn, "tier": rinfo["tier"],
            "low_buy": rinfo["low_buy"],
            "won": rinfo["g2_won"], "site": exec_site,
            "exec_time_s": exec_t // 1000 if exec_t else None,
            "approaches": sorted(set(approaches)),
            "style": ("split" if len(set(approaches)) >= 2 else
                      "single-lane" if approaches else "unknown"),
            "source": source,
        })
    return out


# ── aggregation ───────────────────────────────────────────────────────────────

def share(counter, n=None):
    total = n if n is not None else sum(counter.values())
    return {k: {"count": v, "pct": round(v * 100.0 / total, 1)}
            for k, v in counter.most_common()} if total else {}


def main():
    conn = db()
    matches, missing = [], []
    for mid in TARGETS:
        md = MatchData(conn, mid)
        if md.ok:
            matches.append(md)
        else:
            missing.append(mid)

    pos_matches = [m for m in matches if m.has_positions]
    print(f"Analyzing {len(matches)} matches: "
          f"{', '.join(m.label for m in matches)}")
    print(f"  with replay positions: {len(pos_matches)} "
          f"({', '.join(m.label for m in pos_matches)})")
    if missing:
        print(f"MISSING from DB: {missing}")

    result = {
        "team": "G2 Esports", "map": "Lotus",
        "matches_analyzed": [
            {"match_id": m.match_id, "series_id": m.series_id,
             "opponent": m.opponent,
             "rounds": len(m.rounds),
             "has_replay_positions": m.has_positions,
             "comp": sorted(f"{p['ign']} ({p['agent']})"
                            for p in m.players.values()
                            if p["team"] == m.g2_team)}
            for m in matches],
        "matches_missing": missing,
    }

    # 1. ults
    all_ults = [u for m in matches for u in detect_ults(m)]
    by_player = defaultdict(list)
    for u in all_ults:
        by_player[f"{u['player']} ({u['agent']})"].append(u)
    ult_summary = {}
    for pl, casts in by_player.items():
        co = Counter(c["callout"] for c in casts)
        reg = Counter(c["region"] for c in casts)
        ult_summary[pl] = {
            "total_casts": len(casts),
            "by_callout": share(co),
            "by_region": share(reg),
            "casts": casts,
        }
    result["ult_locations"] = ult_summary

    # 2. weird buys
    all_buys = [b for m in matches for b in detect_weird_buys(m)]
    buys_summary = defaultdict(lambda: defaultdict(list))
    for b in all_buys:
        buys_summary[b["weapon"]][f"{b['player']} ({b['agent']})"].append(b)
    result["weird_buys"] = {
        w: {pl: {"rounds": len(lst),
                 "round_list": [f"{x['match']} R{x['round']}" for x in lst],
                 "kill_callouts": dict(Counter(x["kill_callout"] for x in lst if x["kill_callout"])),
                 "early_callouts": dict(Counter(x["early_callout"] for x in lst if x["early_callout"])),
                 "play_areas": dict(Counter(x["play_area"] for x in lst if x["play_area"])),
                 "tiers": dict(Counter(x["tier"] for x in lst)),
                 "sides": dict(Counter(x["side"] for x in lst)),
                 "sources": dict(Counter(x["source"] for x in lst)),
                 "detail": lst}
            for pl, lst in pls.items()}
        for w, pls in buys_summary.items()}

    # 3+4. stacks
    all_stacks = [s for m in matches for s in detect_stacks(m)]
    eco_rounds = [s for s in all_stacks if s["low_buy"]]
    eco_stacked = [s for s in eco_rounds if s["stack_region"]]
    result["eco_stacks"] = {
        "def_eco_pistol_rounds": len(eco_rounds),
        "rounds_with_3plus_stack": len(eco_stacked),
        "stack_regions": share(Counter(s["stack_region"] for s in eco_stacked)),
        "aggression": {
            "rounds_with_forward_player": sum(1 for s in eco_rounds if s["aggressive_players"]),
            "forward_players": dict(Counter(p for s in eco_rounds for p in s["aggressive_players"])),
        },
        "detail": eco_rounds,
    }
    stacked = [s for s in all_stacks if s["stack_region"]]
    result["general_stacks"] = {
        "def_rounds_analyzed": len(all_stacks),
        "rounds_with_3plus_stack": len(stacked),
        "stack_regions": share(Counter(s["stack_region"] for s in stacked)),
        "stack_by_tier": {tier: dict(Counter(s["stack_region"] for s in stacked
                                             if s["tier"] == tier))
                          for tier in ("pistol", "eco", "semi-eco", "semi-buy", "full")},
        "avg_region_spread": share(Counter(
            reg for s in all_stacks for reg, n in s["regions"].items() for _ in range(n))),
        "detail": all_stacks,
    }

    # 5. execs
    all_execs = [e for m in matches for e in detect_execs(m)]
    known = [e for e in all_execs if e["site"]]
    result["site_execs"] = {
        "atk_rounds": len(all_execs),
        "site_known": len(known),
        "by_site": share(Counter(e["site"] for e in known)),
        "by_site_tier": {
            tier: dict(Counter(e["site"] for e in known if e["tier"] == tier))
            for tier in ("pistol", "eco", "semi-eco", "semi-buy", "full")},
        "pistol_sites": [{"match": e["match"], "round": e["round"], "site": e["site"],
                          "won": e["won"]} for e in known if e["tier"] == "pistol"],
        "win_rate_by_site": {
            site: {"won": sum(1 for e in known if e["site"] == site and e["won"]),
                   "total": sum(1 for e in known if e["site"] == site)}
            for site in ("A", "B", "C")},
        "style": share(Counter(e["style"] for e in known)),
        "common_approaches": share(Counter(a for e in known for a in e["approaches"])),
        "avg_exec_time_s": round(
            sum(e["exec_time_s"] for e in known if e["exec_time_s"]) /
            max(1, sum(1 for e in known if e["exec_time_s"])), 1),
        "detail": all_execs,
    }

    out_path = Path(__file__).parent / "g2_lotus_stats.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nSaved: {out_path}")

    # console digest
    print("\n=== ULTS ===")
    for pl, s in ult_summary.items():
        top = list(s["by_callout"].items())[:3]
        print(f"  {pl}: {s['total_casts']} casts | " +
              ", ".join(f"{k} x{v['count']}" for k, v in top))
    print("\n=== WEIRD BUYS ===")
    for w, pls in result["weird_buys"].items():
        for pl, s in pls.items():
            print(f"  {w} – {pl}: {s['rounds']} rounds | kill spots {s['kill_callouts']}")
    print("\n=== ECO STACKS ===")
    print(" ", json.dumps(result["eco_stacks"]["stack_regions"]))
    print("\n=== GENERAL STACKS ===")
    print(" ", json.dumps(result["general_stacks"]["stack_regions"]))
    print("\n=== EXECS ===")
    print(" ", json.dumps(result["site_execs"]["by_site"]))
    print("  by tier:", json.dumps(result["site_execs"]["by_site_tier"]))


if __name__ == "__main__":
    main()
