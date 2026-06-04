"""
pull_g2.py — Pull all G2 Esports VCT 2026 matches and report Pearl stats.

Uses the already-cached api_dumps/g2_team_page.json (captured on first run)
to identify which series contain Pearl, then scrapes each one.

Run:  python pull_g2.py            # full pull
      python pull_g2.py --report   # just show the stats, no scraping
"""

import json
import sqlite3
import sys
from pathlib import Path

import scraper
from scraper import fetch_next_data, is_scraped, scrape_series

DB_PATH      = Path(__file__).parent / "valorant.db"
DUMP_DIR     = Path(__file__).parent / "api_dumps"
TEAM_PAGE    = DUMP_DIR / "g2_team_page.json"
TEAM_URL     = "https://www.rib.gg/teams/g2-esports/9060"
MIN_DATE     = "2026-01-01"
TARGET_MAP   = "Pearl"


# ── Discovery ─────────────────────────────────────────────────────────────────

def load_team_page_data() -> list[dict]:
    """Return series list from cached dump, or re-fetch if missing."""
    if TEAM_PAGE.exists():
        print(f"  Using cached team page: {TEAM_PAGE}")
        raw = json.loads(TEAM_PAGE.read_text(encoding="utf-8"))
    else:
        print(f"  Fetching team page via Playwright: {TEAM_URL}")
        raw = fetch_next_data(TEAM_URL, headless=False)
        if raw:
            DUMP_DIR.mkdir(exist_ok=True)
            TEAM_PAGE.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")

    if not raw:
        return []

    series_wrapper = raw.get("props", {}).get("pageProps", {}).get("series", {})
    return series_wrapper.get("data", []) if isinstance(series_wrapper, dict) else []


def find_series_with_map(all_series: list[dict], map_name: str, min_date: str) -> list[dict]:
    """
    Return deduplicated list of series from min_date+ that contain a map_name game.
    Each entry: {series_id, start_date, event, opponent, maps}.
    """
    seen: set[int] = set()
    result = []

    for s in all_series:
        start = s.get("startDate") or ""
        if start < min_date:
            continue

        sid = s.get("id")
        if not sid or sid in seen:
            continue

        match_maps = []
        for m in s.get("matches") or []:
            mn = (m.get("map") or {}).get("name") or (m.get("map") or {}).get("displayName") or ""
            if mn:
                match_maps.append(mn)

        if map_name in match_maps:
            seen.add(sid)
            t1 = (s.get("team1") or {}).get("name") or "?"
            t2 = (s.get("team2") or {}).get("name") or "?"
            opp = t2 if "g2" in t1.lower() else t1
            result.append({
                "series_id":  sid,
                "start_date": start[:10],
                "event":      s.get("eventName") or "?",
                "opponent":   opp,
                "maps":       match_maps,
            })

    result.sort(key=lambda x: x["start_date"])
    return result


# ── Scraping ──────────────────────────────────────────────────────────────────

def pull(series_list: list[dict]) -> tuple[int, int, int]:
    new = skipped = failed = 0
    n = len(series_list)
    for i, meta in enumerate(series_list, 1):
        sid  = meta["series_id"]
        maps = "  ".join(meta.get("maps") or [])
        print(f"\n  [{i}/{n}] Series {sid}  {meta['start_date']}  vs {meta['opponent']}")
        print(f"          Maps: {maps}")

        if is_scraped(sid):
            print(f"          Already in DB — skipping")
            skipped += 1
            continue

        ok = scrape_series(sid, headless=False)
        if ok:
            new += 1
        else:
            failed += 1
            print(f"          FAILED")

    return new, skipped, failed


# ── Reporting ─────────────────────────────────────────────────────────────────

def report():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    line = "=" * 65
    print(f"\n{line}")
    print(f"  G2 ESPORTS  |  {TARGET_MAP.upper()}  |  VCT 2026 (Jan+)")
    print(f"{line}")

    # G2 series in DB
    g2_series = conn.execute("""
        SELECT s.series_id, s.start_date, s.event_name, s.team1_name, s.team2_name
        FROM series s
        WHERE LOWER(s.team1_name) LIKE '%g2%' OR LOWER(s.team2_name) LIKE '%g2%'
        ORDER BY s.start_date
    """).fetchall()

    if not g2_series:
        print("\n  No G2 series in database yet.")
        conn.close()
        return

    print(f"\n  SCRAPED SERIES ({len(g2_series)})")
    all_map_counts: dict[str, int] = {}
    for s in g2_series:
        opp  = s["team2_name"] if "g2" in (s["team1_name"] or "").lower() else s["team1_name"]
        maps = conn.execute(
            "SELECT map_name FROM matches WHERE series_id=? ORDER BY match_id",
            (s["series_id"],)
        ).fetchall()
        map_str = "  ".join(r["map_name"] or "?" for r in maps)
        has_target = TARGET_MAP in (r["map_name"] for r in maps)
        marker = f"  << has {TARGET_MAP}" if has_target else ""
        print(f"    {(s['start_date'] or '')[:10]}  vs {opp:<20} {(s['event_name'] or '')[:34]}{marker}")
        print(f"    {'':12}  {map_str}")
        for r in maps:
            mn = r["map_name"] or "?"
            all_map_counts[mn] = all_map_counts.get(mn, 0) + 1

    print(f"\n  MAP COVERAGE ({sum(all_map_counts.values())} total maps scraped)")
    for mn, cnt in sorted(all_map_counts.items(), key=lambda x: -x[1]):
        marker = "  <-- target" if mn == TARGET_MAP else ""
        print(f"    {mn:<16} {cnt} map(s){marker}")

    # Pearl-specific matches
    pearl_matches = conn.execute("""
        SELECT m.match_id, m.winning_team,
               CASE WHEN LOWER(s.team1_name) LIKE '%g2%' THEN 1 ELSE 2 END AS g2_num,
               s.team1_name, s.team2_name, s.event_name, s.start_date
        FROM matches m
        JOIN series s ON s.series_id = m.series_id
        WHERE m.map_name = ?
          AND (LOWER(s.team1_name) LIKE '%g2%' OR LOWER(s.team2_name) LIKE '%g2%')
        ORDER BY s.start_date, m.match_id
    """, (TARGET_MAP,)).fetchall()

    print(f"\n{'-'*65}")
    print(f"  {TARGET_MAP.upper()} MAPS ({len(pearl_matches)} found)")

    if not pearl_matches:
        print(f"\n  No {TARGET_MAP} maps in DB yet.")
        conn.close()
        return

    total_rds = atk_rds = known_rds = 0
    site_counts: dict[str, int] = {}
    win_by_site: dict[str, int] = {}
    econ_site:   dict[str, dict[str, int]] = {}
    prev_site:   dict[str, dict[str, int]] = {}

    for m in pearl_matches:
        mid     = m["match_id"]
        g2_num  = m["g2_num"]
        g2_won  = m["winning_team"] == g2_num
        opp     = m["team2_name"] if g2_num == 1 else m["team1_name"]
        result  = "WIN" if g2_won else "LOSS"

        rds = conn.execute("SELECT COUNT(*) FROM rounds WHERE match_id=?", (mid,)).fetchone()[0]
        total_rds += rds

        atk_rows = conn.execute("""
            SELECT round_number, site_executed, economy_tier, won_round, won_prev_round
            FROM round_states
            WHERE match_id=? AND team_number=? AND side='atk'
        """, (mid, g2_num)).fetchall()

        atk_rds += len(atk_rows)
        for r in atk_rows:
            s = r["site_executed"]
            if s:
                known_rds += 1
                site_counts[s] = site_counts.get(s, 0) + 1
                if r["won_round"]:
                    win_by_site[s] = win_by_site.get(s, 0) + 1
                econ = r["economy_tier"] or "unknown"
                econ_site.setdefault(econ, {})
                econ_site[econ][s] = econ_site[econ].get(s, 0) + 1
                prev = r["won_prev_round"]
                if prev is not None:
                    pk = "after_win" if prev == 1 else "after_loss"
                    prev_site.setdefault(pk, {})
                    prev_site[pk][s] = prev_site[pk].get(s, 0) + 1

        print(f"\n    {(m['start_date'] or '')[:10]}  vs {opp:<18} {result}")
        print(f"    {rds} total rounds  |  {len(atk_rows)} G2 ATK rounds")

    cov = known_rds / atk_rds * 100 if atk_rds else 0

    print(f"\n{'='*65}")
    print(f"  ROUND STATS SUMMARY")
    print(f"{'='*65}")
    print(f"  Total rounds on {TARGET_MAP}          : {total_rds}")
    print(f"  G2 attacking rounds              : {atk_rds}")
    print(f"  Site inference coverage          : {known_rds}/{atk_rds}  ({cov:.0f}%)")

    total_known = sum(site_counts.values())
    if total_known == 0:
        print(f"\n  No site data to report.")
        conn.close()
        return

    print(f"\n{'='*65}")
    print(f"  A/B DISTRIBUTION  ({total_known} rounds with inferred site)")
    print(f"{'='*65}")

    max_pct = max(v / total_known for v in site_counts.values()) if site_counts else 0
    for site in sorted(site_counts):
        c    = site_counts[site]
        pct  = c / total_known * 100
        wins = win_by_site.get(site, 0)
        wr   = wins / c * 100 if c else 0
        bar  = "#" * int(pct / 3)
        star = "  <<" if pct / 100 >= max_pct - 0.01 else ""
        print(f"  {site}:  {c:>3}/{total_known}  ({pct:>5.1f}%)  {bar}{star}")
        print(f"      Win rate on {site}:  {wins}/{c}  ({wr:.0f}%)")

    if total_known < 5:
        print(f"\n  WARNING: Only {total_known} rounds — prediction unreliable.")
        print(f"  Add more G2 Pearl series with: python pull_g2.py")

    # Economy breakdown
    econ_order = ["eco", "half", "full", "heavy", "unknown"]
    if total_known >= 5:
        print(f"\n{'='*65}")
        print(f"  BY ECONOMY")
        print(f"{'='*65}")
        for econ in econ_order:
            if econ not in econ_site:
                continue
            cd = econ_site[econ]
            n  = sum(cd.values())
            dist = "  ".join(
                f"{s}: {v}/{n} ({v/n:.0%})" for s, v in sorted(cd.items())
            )
            print(f"  {econ:<8}  n={n:>3}  {dist}")

    # Previous round effect
    if total_known >= 5 and prev_site:
        print(f"\n{'='*65}")
        print(f"  MOMENTUM EFFECT (does prev round result change site choice?)")
        print(f"{'='*65}")
        for pk in ("after_win", "after_loss"):
            if pk not in prev_site:
                continue
            cd = prev_site[pk]
            n  = sum(cd.values())
            dist = "  ".join(
                f"{s}: {v}/{n} ({v/n:.0%})" for s, v in sorted(cd.items())
            )
            print(f"  {pk:<12}  n={n:>3}  {dist}")

    print()
    conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    report_only = "--report" in sys.argv
    scraper.init_db()

    print("=" * 65)
    print("  G2 ESPORTS  —  VCT 2026 PEARL DATA")
    print("=" * 65)

    if not report_only:
        print("\nStep 1: Identifying Pearl series from team page data...")
        all_series = load_team_page_data()

        if not all_series:
            print("  ERROR: Could not load series data.")
            print("  Run without --report to re-fetch the team page.")
            sys.exit(1)

        pearl_series = find_series_with_map(all_series, TARGET_MAP, MIN_DATE)

        print(f"\n  Found {len(pearl_series)} G2 series containing {TARGET_MAP} (Jan 2026+):")
        for m in pearl_series:
            print(f"    [{m['series_id']}]  {m['start_date']}  vs {m['opponent']:<22}"
                  f"  {m['event'][:40]}")
            print(f"           Maps: {'  '.join(m['maps'])}")

        print(f"\nStep 2: Scraping {len(pearl_series)} series...")
        new, skipped, failed = pull(pearl_series)
        print(f"\n  Scraping complete: {new} new  {skipped} skipped  {failed} failed")

    print("\nStep 3: Generating report...")
    report()
