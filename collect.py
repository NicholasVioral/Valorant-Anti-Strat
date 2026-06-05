"""
Phase 2: Pull VCT match data — by event or by team.

Usage:
  python collect.py                         # scrape default TARGET_EVENTS
  python collect.py --list-teams            # list teams in DB
  python collect.py --team "NRG" --last 7  # scrape NRG's last 7 series

Known event slugs/IDs (from rib.gg URLs):
  5228 champions-tour-2025-americas-kickoff
  5232 champions-tour-2025-pacific-kickoff
  5469 champions-tour-2025-pacific-stage-1
  5574 champions-tour-2025-masters-toronto
"""

import logging
import sqlite3
import sys
from pathlib import Path

import scraper

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

DB_PATH = Path(__file__).parent / "valorant.db"

# (event_slug, event_id, max_series_to_pull)
TARGET_EVENTS = [
    ("champions-tour-2025-americas-kickoff", 5228, 3),
    ("champions-tour-2025-pacific-kickoff",  5232, 3),
    ("champions-tour-2025-pacific-stage-1",  5469, 2),
    ("champions-tour-2025-masters-toronto",  5574, 3),
]

# Fallback hardcoded series (used if event discovery returns nothing)
FALLBACK_SERIES = [
    83369,   # Paper Rex vs T1        Pacific Kickoff
    83350,   # MIBR vs 100 Thieves   Americas Kickoff
    89921,   # RRQ vs Paper Rex       Pacific Stage 1
]


def collect():
    scraper.init_db()

    pulled_total = 0
    skipped_total = 0
    failed_events = []

    for slug, event_id, limit in TARGET_EVENTS:
        print(f"\n{'─'*60}")
        print(f"  Event {event_id}: {slug}")
        print(f"{'─'*60}")

        series_ids = scraper.get_series_ids_from_event(slug, event_id)

        if not series_ids:
            log.warning("Event discovery failed for %d — will try fallbacks", event_id)
            failed_events.append(event_id)
            continue

        pulled = 0
        for sid in series_ids:
            if pulled >= limit:
                break
            if scraper.is_scraped(sid):
                log.info("Series %d already in DB — skipping", sid)
                skipped_total += 1
                pulled += 1
                continue
            ok = scraper.scrape_series(sid)
            if ok:
                pulled += 1
                pulled_total += 1

    # Fallbacks for any events that failed discovery
    if failed_events or pulled_total == 0:
        print(f"\n{'─'*60}")
        print("  Fallback: scraping hardcoded series IDs")
        print(f"{'─'*60}")
        for sid in FALLBACK_SERIES:
            if scraper.is_scraped(sid):
                log.info("Series %d already in DB — skipping", sid)
                skipped_total += 1
            else:
                ok = scraper.scrape_series(sid)
                if ok:
                    pulled_total += 1

    print(f"\n{'='*60}")
    print(f"  Done. Scraped: {pulled_total}  Skipped (cached): {skipped_total}")
    print(f"{'='*60}")
    _print_summary()


def _print_summary():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            s.event_name,
            s.team1_name,
            s.team2_name,
            GROUP_CONCAT(DISTINCT m.map_name ORDER BY m.match_id) AS maps,
            COUNT(DISTINCT m.match_id) AS map_count,
            COUNT(k.id) AS kill_count,
            SUM(CASE WHEN k.victim_x IS NOT NULL THEN 1 ELSE 0 END) AS coord_count
        FROM series s
        JOIN matches m ON m.series_id = s.series_id
        LEFT JOIN kills k ON k.match_id = m.match_id
        GROUP BY s.series_id
        ORDER BY s.series_id
    """).fetchall()

    print(f"\n  {'Event':<38} {'Matchup':<38} {'Maps':>4} {'Kills':>6} {'w/XY':>6}")
    print("  " + "─" * 95)
    for r in rows:
        ev = (r["event_name"] or "?")[:37]
        mu = f"{r['team1_name']} vs {r['team2_name']}"[:37]
        print(f"  {ev:<38} {mu:<38} {r['map_count']:>4}"
              f" {r['kill_count']:>6} {r['coord_count']:>6}")

    total_kills = conn.execute("SELECT COUNT(*) FROM kills WHERE victim_x IS NOT NULL").fetchone()[0]
    total_maps  = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"\n  Total: {total_maps} maps  |  {total_kills:,} kills with XY coordinates")

    teams = conn.execute("""
        SELECT DISTINCT name FROM (
            SELECT team1_name AS name FROM series
            UNION SELECT team2_name FROM series
        ) WHERE name IS NOT NULL ORDER BY name
    """).fetchall()
    print(f"  Teams: {', '.join(r[0] for r in teams)}")
    print()
    conn.close()


def list_teams():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT name,
               COUNT(DISTINCT series_id) AS series,
               COUNT(DISTINCT match_id)  AS maps,
               COUNT(kill_id) AS kills
        FROM (
            SELECT s.team1_name AS name, s.series_id, m.match_id, k.id AS kill_id
            FROM series s
            JOIN matches m ON m.series_id = s.series_id
            LEFT JOIN kills k ON k.match_id = m.match_id AND k.killer_team = 1
            UNION ALL
            SELECT s.team2_name, s.series_id, m.match_id, k.id
            FROM series s
            JOIN matches m ON m.series_id = s.series_id
            LEFT JOIN kills k ON k.match_id = m.match_id AND k.killer_team = 2
        )
        WHERE name IS NOT NULL
        GROUP BY name
        ORDER BY series DESC, name
    """).fetchall()

    print(f"\n  {'Team':<30} {'Series':>7} {'Maps':>6} {'Kills':>7}")
    print("  " + "─" * 52)
    for r in rows:
        print(f"  {r[0]:<30} {r[1]:>7} {r[2]:>6} {r[3]:>7}")
    print(f"\n  Analyze: python analyze.py --team \"<Team Name>\"")
    conn.close()


def collect_team(team_name: str, limit: int = 7,
                  team_id: int | None = None, team_slug: str | None = None):
    """
    Scrape the most recent N series for a specific team.

    team_id / team_slug can be supplied directly from the rib.gg URL:
      https://www.rib.gg/teams/{slug}/matches/{id}
    If omitted, the function tries the local DB then rib.gg search.
    """
    scraper.init_db()

    print(f"\n{'─'*60}")
    print(f"  Looking up team: {team_name}")
    print(f"{'─'*60}")

    # 1. Caller supplied ID directly
    if team_id:
        log.info("Using supplied team_id=%d slug=%s", team_id, team_slug)

    # 2. Already in DB from a previous scrape
    elif (team_id := scraper.get_team_id_from_db(team_name)):
        log.info("Found team_id=%d for '%s' in local DB", team_id, team_name)

    # 3. Search rib.gg
    else:
        log.info("Team not in DB — searching rib.gg for '%s'", team_name)
        team_id, team_slug = scraper.search_team(team_name)
        if not team_id:
            print(f"\nERROR: Could not find '{team_name}' on rib.gg.")
            print("Tip: find the team page on rib.gg and pass the ID and slug directly:")
            print(f"  python collect.py --team \"{team_name}\" --team-id <ID> --team-slug <slug>")
            print("  e.g. for https://www.rib.gg/teams/nrg-esports/matches/116")
            print(f"  python collect.py --team \"{team_name}\" --team-id 116 --team-slug nrg-esports")
            return
        print(f"Found: {team_name} (id={team_id}, slug={team_slug})")

    print(f"\n{'─'*60}")
    print(f"  Fetching last {limit} series for {team_name} (id={team_id})")
    print(f"{'─'*60}")

    series_ids = scraper.get_team_recent_series(team_id, team_slug=team_slug, limit=limit)

    if not series_ids:
        print("\nERROR: Could not extract series list from the team page.")
        print("Check api_dumps/ for the team page JSON to inspect the structure.")
        return

    print(f"Series IDs found: {series_ids}\n")

    pulled, skipped = 0, 0
    for sid in series_ids:
        if scraper.is_scraped(sid):
            log.info("Series %d already in DB — skipping", sid)
            skipped += 1
        else:
            ok = scraper.scrape_series(sid)
            if ok:
                pulled += 1

    print(f"\n{'='*60}")
    print(f"  Done. Scraped: {pulled}  Skipped (cached): {skipped}")
    print(f"{'='*60}")
    _print_summary()


if __name__ == "__main__":
    if "--list-teams" in sys.argv:
        list_teams()
    elif "--team" in sys.argv:
        idx = sys.argv.index("--team")
        if idx + 1 >= len(sys.argv):
            print("Usage: python collect.py --team \"NRG Esports\" --team-id 116 --team-slug nrg-esports --last 7")
            sys.exit(1)
        _team  = sys.argv[idx + 1]
        _limit = 7
        _tid   = None
        _tslug = None
        if "--last" in sys.argv:
            i = sys.argv.index("--last")
            _limit = int(sys.argv[i + 1])
        if "--team-id" in sys.argv:
            i = sys.argv.index("--team-id")
            _tid = int(sys.argv[i + 1])
        if "--team-slug" in sys.argv:
            i = sys.argv.index("--team-slug")
            _tslug = sys.argv[i + 1]
        collect_team(_team, _limit, team_id=_tid, team_slug=_tslug)
    else:
        collect()
