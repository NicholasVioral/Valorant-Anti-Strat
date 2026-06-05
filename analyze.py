"""
Phase 3: Analyze positional patterns for a team from kill coordinate data.

Position signal: victim X/Y at each kill event.
  - Where a player dies = where they were standing = their position.
  - First kills per round (is_first_kill=1) give early-round positions.
  - Round time buckets approximate the engagement phase.
  - Attacker/defender side (from kill.side) separates offensive and defensive patterns.

Map coordinates are Valorant engine units. Each match has x_origin/y_origin offsets
(stored in the matches table) for converting to minimap space. The analysis normalises
coordinates to [0,1] across all matches so you can compare patterns without needing
per-map calibration.

Usage:
  python analyze.py --team "Paper Rex"
  python analyze.py --team "T1"
  python analyze.py --list-teams
"""

import argparse
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent / "valorant.db"

EARLY_ROUND_MS = 30_000   # first 30 seconds
MID_ROUND_MS   = 60_000   # 30 to 60 seconds


# ── DB helpers ────────────────────────────────────────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_team_matches(conn, team_name: str) -> list[dict]:
    rows = conn.execute("""
        SELECT
            m.match_id, m.series_id, m.map_name,
            m.winning_team, m.x_origin, m.y_origin,
            s.team1_id, s.team2_id, s.team1_name, s.team2_name,
            s.event_name, s.start_date,
            CASE
                WHEN LOWER(s.team1_name) = LOWER(:n) THEN 1
                WHEN LOWER(s.team2_name) = LOWER(:n) THEN 2
            END AS team_number
        FROM matches m
        JOIN series s ON s.series_id = m.series_id
        WHERE LOWER(s.team1_name) = LOWER(:n)
           OR LOWER(s.team2_name) = LOWER(:n)
        ORDER BY s.start_date, m.match_id
    """, {"n": team_name}).fetchall()
    return [dict(r) for r in rows]


def get_kills_for_team(conn, match_id: int, team_number: int) -> list[dict]:
    """Kills where the VICTIM is on the target team (= their positions when dying)."""
    rows = conn.execute("""
        SELECT round_number, round_time_ms, victim_x, victim_y,
               killer_team, victim_team, side, is_first_kill, weapon
        FROM kills
        WHERE match_id = ? AND victim_team = ?
          AND victim_x IS NOT NULL AND victim_y IS NOT NULL
        ORDER BY round_number, round_time_ms
    """, (match_id, team_number)).fetchall()
    return [dict(r) for r in rows]


def get_kills_by_team(conn, match_id: int, team_number: int) -> list[dict]:
    """Kills made BY the target team (= positions they reached to make the kill)."""
    rows = conn.execute("""
        SELECT round_number, round_time_ms, victim_x, victim_y,
               killer_team, victim_team, side, is_first_kill, weapon
        FROM kills
        WHERE match_id = ? AND killer_team = ?
          AND victim_x IS NOT NULL AND victim_y IS NOT NULL
        ORDER BY round_number, round_time_ms
    """, (match_id, team_number)).fetchall()
    return [dict(r) for r in rows]


def get_player_stats(conn, match_id: int, team_number: int) -> list[dict]:
    rows = conn.execute("""
        SELECT round_number, player_id, side, kills, deaths,
               first_kills, first_deaths, plants, damage
        FROM player_stats
        WHERE match_id = ? AND team_number = ?
        ORDER BY round_number
    """, (match_id, team_number)).fetchall()
    return [dict(r) for r in rows]


def get_rounds(conn, match_id: int) -> list[dict]:
    rows = conn.execute("""
        SELECT round_number, winning_team, win_condition
        FROM rounds WHERE match_id = ? ORDER BY round_number
    """, (match_id,)).fetchall()
    return [dict(r) for r in rows]


# ── Maths ─────────────────────────────────────────────────────────────────────

def fmean(v): return statistics.mean(v) if v else 0.0
def fstdev(v): return statistics.stdev(v) if len(v) > 1 else 0.0

def dist2d(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)

def centroid(pts):
    if not pts: return (0.0, 0.0)
    return (fmean([p[0] for p in pts]), fmean([p[1] for p in pts]))

def bounds(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)

def norm(pts, mn_x, mn_y, mx_x, mx_y):
    rx = (mx_x - mn_x) or 1.0; ry = (mx_y - mn_y) or 1.0
    return [((p[0]-mn_x)/rx, (p[1]-mn_y)/ry) for p in pts]

def mean_pairwise(pts):
    if len(pts) < 2: return 0.0
    total = 0.0; n = 0
    for i in range(len(pts)):
        for j in range(i+1, len(pts)):
            total += dist2d(pts[i], pts[j]); n += 1
    return total / n


# ── Simple k-means (no sklearn needed) ────────────────────────────────────────

def kmeans(pts, k=2, iters=20):
    if len(pts) < k:
        return list(range(len(pts)))
    centers = [pts[i * len(pts) // k] for i in range(k)]
    labels = [0] * len(pts)
    for _ in range(iters):
        for i, p in enumerate(pts):
            labels[i] = min(range(k), key=lambda ci: dist2d(p, centers[ci]))
        new_centers = []
        for ci in range(k):
            cluster = [pts[i] for i, l in enumerate(labels) if l == ci]
            new_centers.append(centroid(cluster) if cluster else centers[ci])
        if new_centers == centers: break
        centers = new_centers
    return labels, centers


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_team(team_name: str):
    conn = db()
    matches = get_team_matches(conn, team_name)

    if not matches:
        print(f"\nNo matches found for '{team_name}'.")
        print("Run: python collect.py --list-teams")
        conn.close()
        return

    # Gather all victim (death) positions to get global normalisation bounds
    all_raw: list[tuple] = []
    for m in matches:
        for k in get_kills_for_team(conn, m["match_id"], m["team_number"]):
            all_raw.append((k["victim_x"], k["victim_y"]))

    if not all_raw:
        print(f"\nKill data found but no coordinates. Check DB with: python scraper.py --summary")
        conn.close()
        return

    mn_x, mn_y, mx_x, mx_y = bounds(all_raw)

    def n(pts):  # normalise helper
        return norm(pts, mn_x, mn_y, mx_x, mx_y)

    # ── Aggregate data ────────────────────────────────────────────────────────
    death_positions:  list[tuple] = []   # where team dies (defence positions)
    kill_positions:   list[tuple] = []   # where team kills (attack reach)
    early_deaths:     list[tuple] = []   # first 30s
    mid_deaths:       list[tuple] = []   # 30-60s
    atk_deaths:       list[tuple] = []   # when team is attacking
    def_deaths:       list[tuple] = []   # when team is defending
    first_kill_times: list[int]   = []   # round_time_ms for first kills made BY team
    plant_rounds:     int         = 0
    total_rounds:     int         = 0
    round_wins:       int         = 0
    map_data: dict[str, dict]     = defaultdict(lambda: defaultdict(list))

    for m in matches:
        mid      = m["match_id"]
        tnum     = m["team_number"]
        mapname  = m["map_name"] or "Unknown"
        rounds   = get_rounds(conn, mid)
        total_rounds += len(rounds)
        round_wins += sum(1 for r in rounds if r["winning_team"] == tnum)

        # Death positions (where this team's players die)
        for k in get_kills_for_team(conn, mid, tnum):
            pt = (k["victim_x"], k["victim_y"])
            death_positions.append(pt)
            if k["round_time_ms"] <= EARLY_ROUND_MS:
                early_deaths.append(pt)
            elif k["round_time_ms"] <= MID_ROUND_MS:
                mid_deaths.append(pt)
            if k["side"] == "atk":
                atk_deaths.append(pt)
            else:
                def_deaths.append(pt)
            map_data[mapname]["deaths"].append(pt)

        # Kill positions (where this team kills opponents = how far they push)
        for k in get_kills_by_team(conn, mid, tnum):
            pt = (k["victim_x"], k["victim_y"])
            kill_positions.append(pt)
            if k["is_first_kill"] and k["side"] == "atk":
                first_kill_times.append(k["round_time_ms"])
            if k["side"] == "atk":
                map_data[mapname]["kill_push"].append(pt)

        # Plant data
        ps_rows = get_player_stats(conn, mid, tnum)
        plant_rounds += sum(1 for ps in ps_rows if ps.get("plants", 0) > 0 and ps.get("side") == "atk")

    # Normalise all coordinate sets
    d_norm  = n(death_positions)
    k_norm  = n(kill_positions)
    e_norm  = n(early_deaths)
    md_norm = n(mid_deaths)
    ak_norm = n(atk_deaths)
    df_norm = n(def_deaths)

    # ── Print ─────────────────────────────────────────────────────────────────
    events = sorted(set(m["event_name"] for m in matches if m["event_name"]))
    maps   = sorted(set(m["map_name"] for m in matches if m["map_name"]))

    print(f"\n{'='*65}")
    print(f"  POSITIONAL ANALYSIS  |  {team_name.upper()}")
    print(f"{'='*65}")
    print(f"  Matches   : {len(matches)} maps ({', '.join(maps)})")
    print(f"  Events    : {', '.join(events)}")
    print(f"  Win rate  : {round_wins}/{total_rounds} rounds ({round_wins/total_rounds:.0%})")
    print(f"  Data      : {len(death_positions):,} death positions  |  {len(kill_positions):,} kill positions")
    print(f"  Coord sys : X [{mn_x:.0f}, {mx_x:.0f}]  Y [{mn_y:.0f}, {mx_y:.0f}]  (raw engine units)")

    # ── 1. Overall death position centroid ────────────────────────────────────
    print(f"\n{'-'*65}")
    print("  DEATH POSITIONS  (where the team's players die)")
    print("  Normalised 0-1: 0=low coord end, 1=high coord end")
    print(f"{'-'*65}")

    def row(label, pts):
        if not pts:
            print(f"  {label:<24} no data")
            return
        cx, cy = centroid(pts)
        sx = fstdev([p[0] for p in pts])
        sy = fstdev([p[1] for p in pts])
        bias = "right-heavy" if cx > 0.6 else ("left-heavy" if cx < 0.4 else "balanced")
        print(f"  {label:<24} ctr ({cx:.2f},{cy:.2f})  sigma ({sx:.2f},{sy:.2f})"
              f"  n={len(pts):>5}  [{bias}]")

    row("All deaths:", d_norm)
    row("Early deaths (0-30s):", e_norm)
    row("Mid deaths (30-60s):", md_norm)
    row("Attacking-side deaths:", ak_norm)
    row("Defending-side deaths:", df_norm)

    # ── 2. Kill reach ─────────────────────────────────────────────────────────
    print(f"\n{'-'*65}")
    print("  KILL POSITIONS  (where team kills opponents = how far they push)")
    print(f"{'-'*65}")
    row("All kills:", k_norm)

    # ── 3. Site clustering via 2-means ────────────────────────────────────────
    print(f"\n{'-'*65}")
    print("  SITE CLUSTERING  (k-means k=2 on all death positions)")
    print("  Reveals whether the team concentrates in one area or splits")
    print(f"{'-'*65}")

    if len(d_norm) >= 4:
        labels, centers = kmeans(d_norm, k=2)
        c0 = [p for p, l in zip(d_norm, labels) if l == 0]
        c1 = [p for p, l in zip(d_norm, labels) if l == 1]
        for ci, (cluster, ctr) in enumerate([(c0, centers[0]), (c1, centers[1])], 1):
            pct = len(cluster) / len(d_norm) * 100
            side = "right" if ctr[0] > 0.5 else "left"
            print(f"  Cluster {ci}: {len(cluster):>4} deaths ({pct:.0f}%)  "
                  f"center ({ctr[0]:.2f}, {ctr[1]:.2f})  [{side} side]")
        # Balance score: how unequal the clusters are
        ratio = max(len(c0), len(c1)) / len(d_norm)
        if ratio > 0.70:
            print("  -> SITE STACK: >70% of deaths in one cluster. Strong site preference.")
        elif ratio > 0.60:
            print("  -> SLIGHT LEAN: 60-70% bias toward one side.")
        else:
            print("  -> BALANCED: Deaths split relatively evenly across both clusters.")

    # ── 4. Timing ─────────────────────────────────────────────────────────────
    print(f"\n{'-'*65}")
    print("  ENGAGEMENT TIMING  (first-kill round times when attacking)")
    print(f"{'-'*65}")

    if first_kill_times:
        avg_s  = fmean(first_kill_times) / 1000
        fast_s = min(first_kill_times) / 1000
        slow_s = max(first_kill_times) / 1000
        very_early = sum(1 for t in first_kill_times if t < 20_000)
        full_def   = sum(1 for t in first_kill_times if t > 60_000)
        print(f"  First engagements : {len(first_kill_times)}")
        print(f"  Avg time          : {avg_s:.1f}s")
        print(f"  Range             : {fast_s:.1f}s - {slow_s:.1f}s")
        print(f"  < 20s (rush/early): {very_early} ({very_early/len(first_kill_times):.0%})")
        print(f"  > 60s (late)      : {full_def}  ({full_def/len(first_kill_times):.0%})")
    else:
        print("  No first-kill timing data (attacking side kills only)")

    if plant_rounds:
        print(f"  Spike plants      : {plant_rounds} rounds (attacker side)")

    # ── 5. Per-map breakdown ──────────────────────────────────────────────────
    if len(maps) > 1:
        print(f"\n{'-'*65}")
        print("  PER-MAP BREAKDOWN")
        print(f"{'-'*65}")
        for mapname in maps:
            deaths = n(map_data[mapname].get("deaths", []))
            pushes = n(map_data[mapname].get("kill_push", []))
            n_rounds = len([m for m in matches if m["map_name"] == mapname])
            print(f"\n  {mapname}  ({n_rounds} maps played)")
            row(f"  Deaths:", deaths)
            row(f"  Kill-reach:", pushes)

    # ── 6. Counter-strat summary ──────────────────────────────────────────────
    print(f"\n{'-'*65}")
    print("  COUNTER-STRAT TAKEAWAYS")
    print(f"{'-'*65}")

    if d_norm and ak_norm:
        atk_cx = fmean([p[0] for p in ak_norm])
        if atk_cx > 0.60:
            print("  ATTACK BIAS (right/X-axis): Dies predominantly on right side while attacking.")
            print("  -> Stack the right-side site. Watch that angle once they commit hard.")
        elif atk_cx < 0.40:
            print("  ATTACK BIAS (left/X-axis): Dies predominantly on left side while attacking.")
            print("  -> Stack left site early, maintain mid presence.")
        else:
            print("  BALANCED ATTACK: Deaths distributed across both sites. Hard to pre-rotate.")

    if len(d_norm) >= 4:
        ratio = max(len(c0), len(c1)) / len(d_norm)
        if ratio > 0.65:
            print("  SITE STACKER: >65% of deaths cluster at one site across all rounds.")
            print("  -> Read their initial movement direction (first 15s) and match it fast.")
        else:
            print("  SITE SPLITTER: Deaths fairly distributed. Hold positions, don't over-commit.")

    if first_kill_times:
        avg_s = fmean(first_kill_times) / 1000
        if avg_s < 25:
            print(f"  AGGRO PACE ({avg_s:.0f}s avg first kill): They initiate before defenses settle.")
            print("  -> Sit on aggressive early angles; don't play passive.")
        elif avg_s > 55:
            print(f"  PATIENT PACE ({avg_s:.0f}s avg): They slow-default, gather info, late execute.")
            print("  -> Force early duels; don't let them farm info uncontested.")
        else:
            print(f"  MEDIUM PACE ({avg_s:.0f}s avg): Mix of timings -- read the round start.")

    print()
    conn.close()


# ── List teams ────────────────────────────────────────────────────────────────

def list_teams():
    conn = db()
    rows = conn.execute("""
        SELECT name, COUNT(DISTINCT series_id) AS s, COUNT(DISTINCT match_id) AS m
        FROM (
            SELECT team1_name AS name, series_id, match_id
            FROM series JOIN matches USING(series_id)
            UNION ALL
            SELECT team2_name, series_id, match_id
            FROM series JOIN matches USING(series_id)
        )
        WHERE name IS NOT NULL
        GROUP BY name ORDER BY s DESC, name
    """).fetchall()
    print(f"\n  {'Team':<30} {'Series':>7} {'Maps':>6}")
    print("  " + "─" * 45)
    for r in rows:
        print(f"  {r[0]:<30} {r[1]:>7} {r[2]:>6}")
    print(f"\n  Analyze: python analyze.py --team \"<Team Name>\"")
    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Valorant positional analyzer")
    g  = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--team",       type=str, help="Team name (case-insensitive)")
    g.add_argument("--list-teams", action="store_true")
    args = ap.parse_args()
    if args.list_teams:
        list_teams()
    else:
        analyze_team(args.team)


if __name__ == "__main__":
    main()
