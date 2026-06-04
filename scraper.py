"""
rib.gg pro Valorant scraper — Playwright edition.

Data source: __NEXT_DATA__ embedded JSON in server-rendered HTML pages.
Requires a real Chromium browser to pass Cloudflare (handled by Playwright).

Data extracted per series:
  series, matches, players, match_players, rounds, kills (with X/Y coords),
  player_stats (per-round), map_geo

NOTE: The old "locations" table (per-player position snapshots) is no longer
served through the page HTML. Kill events carry victim X/Y coordinates, which
are used as the position signal in analyze.py.

Usage:
  python scraper.py --series 83369           # scrape one series
  python scraper.py --event champions-tour-2025-pacific-kickoff 5232
  python scraper.py --event champions-tour-2025-pacific-kickoff 5232 --limit 3
  python scraper.py --summary
"""

import argparse
import json
import logging
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

DB_PATH = Path(__file__).parent / "valorant.db"
DUMP_DIR = Path(__file__).parent / "api_dumps"
PAGE_LOAD_WAIT_MS = 6_000   # ms to wait after "load" event for async JS
BETWEEN_PAGES_S   = 3.0     # seconds between page loads (be kind to rib.gg)

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS series (
    series_id         INTEGER PRIMARY KEY,
    event_id          INTEGER,
    event_name        TEXT,
    parent_event_id   INTEGER,
    parent_event_name TEXT,
    team1_id          INTEGER,
    team2_id          INTEGER,
    team1_name        TEXT,
    team2_name        TEXT,
    best_of           INTEGER,
    start_date        TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    match_id           INTEGER PRIMARY KEY,
    series_id          INTEGER,
    map_name           TEXT,
    map_id             INTEGER,
    winning_team       INTEGER,
    team1_score        INTEGER,
    team2_score        INTEGER,
    attacking_first    INTEGER,
    match_number       INTEGER,
    x_origin           REAL,
    y_origin           REAL
);

CREATE TABLE IF NOT EXISTS map_meta (
    map_id       INTEGER PRIMARY KEY,
    map_name     TEXT,
    minimap_url  TEXT,
    x_origin     REAL,
    y_origin     REAL,
    map_size     REAL,
    rotation     INTEGER,
    sites_json   TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    ign       TEXT
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id    INTEGER,
    player_id   INTEGER,
    team_number INTEGER,
    agent_id    INTEGER,
    PRIMARY KEY (match_id, player_id)
);

CREATE TABLE IF NOT EXISTS rounds (
    id            INTEGER PRIMARY KEY,
    match_id      INTEGER,
    series_id     INTEGER,
    round_number    INTEGER,
    winning_team    INTEGER,
    win_condition   TEXT,
    attacking_team  INTEGER,
    team1_loadout   INTEGER,
    team2_loadout   INTEGER
);

CREATE TABLE IF NOT EXISTS kills (
    id               INTEGER PRIMARY KEY,
    match_id         INTEGER,
    series_id        INTEGER,
    round_id         INTEGER,
    round_number     INTEGER,
    round_time_ms    INTEGER,
    killer_id        INTEGER,
    victim_id        INTEGER,
    killer_team      INTEGER,
    victim_team      INTEGER,
    side             TEXT,
    victim_x         REAL,
    victim_y         REAL,
    weapon           TEXT,
    weapon_category  TEXT,
    ability_type     TEXT,
    is_first_kill    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS round_states (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id          INTEGER,
    series_id         INTEGER,
    team_id           INTEGER,
    team_number       INTEGER,
    round_number      INTEGER,
    map_name          TEXT,
    side              TEXT,
    economy_tier      TEXT,
    score_self        INTEGER,
    score_opp         INTEGER,
    won_prev_round    INTEGER,
    ult_players_json  TEXT,
    site_executed     TEXT,
    won_round         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_rs_series  ON round_states(series_id);
CREATE INDEX IF NOT EXISTS idx_rs_team    ON round_states(team_id);
CREATE INDEX IF NOT EXISTS idx_rs_match   ON round_states(match_id);

CREATE TABLE IF NOT EXISTS player_stats (
    match_id    INTEGER,
    series_id   INTEGER,
    round_id    INTEGER,
    round_number INTEGER,
    player_id   INTEGER,
    team_number INTEGER,
    side        TEXT,
    acs         REAL,
    kills       INTEGER,
    deaths      INTEGER,
    assists     INTEGER,
    first_kills INTEGER,
    first_deaths INTEGER,
    plants      INTEGER,
    damage      INTEGER,
    PRIMARY KEY (match_id, round_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_kills_match    ON kills(match_id);
CREATE INDEX IF NOT EXISTS idx_kills_series   ON kills(series_id);
CREATE INDEX IF NOT EXISTS idx_kills_round    ON kills(match_id, round_number);
CREATE INDEX IF NOT EXISTS idx_kills_team     ON kills(match_id, killer_team);
CREATE INDEX IF NOT EXISTS idx_ps_match       ON player_stats(match_id);
CREATE INDEX IF NOT EXISTS idx_ps_series      ON player_stats(series_id);
CREATE INDEX IF NOT EXISTS idx_mp_player      ON match_players(player_id);
CREATE INDEX IF NOT EXISTS idx_rounds_match   ON rounds(match_id);
"""

# ── DB ────────────────────────────────────────────────────────────────────────

@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_conn() as conn:
        conn.executescript(SCHEMA)
    log.info("DB ready -> %s", DB_PATH)


# ── Playwright helpers ────────────────────────────────────────────────────────

def _get_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise SystemExit(
            "Playwright not installed.\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )


def _make_browser(p, headless=False):
    return p.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--disable-infobars",
        ],
    )


def _make_context(browser):
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    return ctx


def fetch_next_data(url: str, headless: bool = False) -> dict | None:
    """
    Load a rib.gg page in a real Chromium browser, extract and return __NEXT_DATA__.
    Returns None on failure.
    """
    sync_playwright = _get_playwright()
    with sync_playwright() as p:
        browser = _make_browser(p, headless=headless)
        ctx     = _make_context(browser)
        page    = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            log.info("Loading: %s", url)
            page.goto(url, wait_until="load", timeout=45_000)
            page.wait_for_timeout(PAGE_LOAD_WAIT_MS)

            raw = page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? el.textContent : null;
                }
            """)
            if not raw:
                log.error("No __NEXT_DATA__ found at %s", url)
                return None
            return json.loads(raw)

        except Exception as e:
            log.error("Failed to load %s: %s", url, e)
            return None
        finally:
            browser.close()


# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_and_store(next_data: dict, series_id: int, save_dump: bool = False) -> bool:
    """Parse __NEXT_DATA__ from a series page and write all tables."""
    try:
        pp     = next_data["props"]["pageProps"]
        series = pp["series"]
    except (KeyError, TypeError) as e:
        log.error("Unexpected __NEXT_DATA__ structure: %s", e)
        return False

    if save_dump:
        DUMP_DIR.mkdir(exist_ok=True)
        out = DUMP_DIR / f"series_{series_id}.json"
        out.write_text(json.dumps(next_data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Dumped -> %s", out)

    t1   = series.get("team1") or {}
    t2   = series.get("team2") or {}
    stats = series.get("stats") or {}

    with db_conn() as conn:
        # Series
        conn.execute("INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            series.get("id"),
            series.get("eventId"),
            series.get("eventName"),
            series.get("parentEventId"),
            series.get("parentEventName"),
            series.get("team1Id"),
            series.get("team2Id"),
            t1.get("name"),
            t2.get("name"),
            series.get("bestOf"),
            series.get("startDate"),
        ))

        # Matches
        conn.execute("DELETE FROM match_players WHERE match_id IN "
                     "(SELECT match_id FROM matches WHERE series_id=?)", (series_id,))
        conn.execute("DELETE FROM matches WHERE series_id=?", (series_id,))

        for m in series.get("matches") or []:
            mid       = m.get("id")
            map_info  = m.get("map") or {}
            geo       = map_info.get("geoData") or {}
            images    = map_info.get("images") or {}
            map_name  = map_info.get("name") or map_info.get("displayName")
            map_id    = m.get("mapId")

            # Store map metadata (minimap URL, geo data, site positions)
            if map_id and geo:
                # displayIcon is the overhead minimap view; listViewIconTall is loading-screen art
                minimap_url = images.get("displayIcon") or images.get("listViewIconTall")
                conn.execute("""
                    INSERT OR REPLACE INTO map_meta
                      (map_id, map_name, minimap_url, x_origin, y_origin,
                       map_size, rotation, sites_json)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    map_id, map_name, minimap_url,
                    geo.get("xOrigin"), geo.get("yOrigin"),
                    geo.get("size"), geo.get("rotation"),
                    json.dumps(geo.get("sites") or {}),
                ))

            conn.execute("INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                mid, series_id,
                map_name,
                map_id,
                m.get("winningTeamNumber"),
                m.get("team1Score"),
                m.get("team2Score"),
                m.get("attackingFirstTeamNumber"),
                m.get("seriesMatchNumber"),
                geo.get("xOrigin"),
                geo.get("yOrigin"),
            ))

            for p in m.get("players") or []:
                pid    = p.get("playerId") or (p.get("player") or {}).get("id")
                ign    = (p.get("player") or {}).get("ign")
                if pid is None:
                    continue
                conn.execute("INSERT OR IGNORE INTO players VALUES (?,?)", (pid, ign))
                conn.execute("INSERT OR REPLACE INTO match_players VALUES (?,?,?,?)", (
                    mid, pid, p.get("teamNumber"), p.get("agentId"),
                ))

        # Rounds
        conn.execute("DELETE FROM rounds WHERE series_id=?", (series_id,))
        for r in stats.get("rounds") or []:
            conn.execute("INSERT OR REPLACE INTO rounds VALUES (?,?,?,?,?,?,?,?,?)", (
                r.get("id"),
                r.get("matchId"),
                series_id,
                r.get("number") if "number" in r else r.get("roundNumber"),
                r.get("winningTeamNumber"),
                r.get("winCondition"),
                r.get("attackingTeamNumber"),
                r.get("team1LoadoutTier"),
                r.get("team2LoadoutTier"),
            ))

        # Kills (with victim X/Y coordinates)
        conn.execute("DELETE FROM kills WHERE series_id=?", (series_id,))
        conn.executemany("""
            INSERT INTO kills
              (id, match_id, series_id, round_id, round_number, round_time_ms,
               killer_id, victim_id, killer_team, victim_team, side,
               victim_x, victim_y, weapon, weapon_category, ability_type, is_first_kill)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            (
                k.get("id"),
                k.get("matchId"),
                series_id,
                k.get("roundId"),
                None,   # back-filled from rounds table below
                k.get("roundTimeMillis"),
                k.get("killerId"),
                k.get("victimId"),
                k.get("killerTeamNumber"),
                k.get("victimTeamNumber"),
                k.get("side"),
                k.get("victimLocationX"),
                k.get("victimLocationY"),
                k.get("weapon"),
                k.get("weaponCategory"),
                str(k.get("abilityType")) if k.get("abilityType") is not None else None,
                int(bool(k.get("first"))),
            )
            for k in (stats.get("kills") or [])
        ])

        # Back-fill round_number from rounds table using round_id
        conn.execute("""
            UPDATE kills SET round_number = (
                SELECT round_number FROM rounds WHERE rounds.id = kills.round_id
            )
            WHERE series_id = ? AND round_number IS NULL
        """, (series_id,))

        # Player stats (per-round)
        conn.execute("DELETE FROM player_stats WHERE series_id=?", (series_id,))
        conn.executemany("""
            INSERT OR REPLACE INTO player_stats
              (match_id, series_id, round_id, round_number, player_id,
               team_number, side, acs, kills, deaths, assists,
               first_kills, first_deaths, plants, damage)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            (
                ps.get("matchId"),
                series_id,
                ps.get("roundId"),
                ps.get("roundNumber"),
                ps.get("playerId"),
                ps.get("teamNumber"),
                ps.get("side"),
                ps.get("acs"),
                ps.get("kills"),
                ps.get("deaths"),
                ps.get("assists"),
                ps.get("firstKills"),
                ps.get("firstDeaths"),
                ps.get("plants"),
                ps.get("damage"),
            )
            for ps in (series.get("playerStats") or [])
        ])

    kill_count = len(stats.get("kills") or [])
    ps_count   = len(series.get("playerStats") or [])
    log.info(
        "Series %d stored: %d matches, %d kills (with X/Y coords), %d player-round rows",
        series_id,
        len(series.get("matches") or []),
        kill_count,
        ps_count,
    )

    # Build round_states after all base tables are populated
    with db_conn() as conn:
        _compute_round_states(series_id, conn)

    return True


# ── round_states computation ──────────────────────────────────────────────────

LOADOUT_TIER_NAMES = {1: "eco", 2: "half", 3: "full", 4: "heavy"}

def _coord_to_minimap(wx, wy, rotation,
                       x_mult=None, y_mult=None, x_scalar=None, y_scalar=None,
                       x_origin=None, y_origin=None, map_size=None,
                       # bounds kept for signature compat but not used
                       bound_min_x=None, bound_max_x=None,
                       bound_min_y=None, bound_max_y=None):
    """
    Convert world coordinates to minimap fractions using Riot's official
    multipliers (verified to place kills on walkable image pixels).
    Falls back to the rib.gg origin/size formula when multipliers are absent.
    """
    if None in (wx, wy):
        return None, None
    if None not in (x_mult, y_mult, x_scalar, y_scalar):
        if rotation in (-90, 270):
            mx = wy * x_mult + x_scalar
            my = wx * y_mult + y_scalar
        else:
            mx = wx * x_mult + x_scalar
            my = wy * y_mult + y_scalar
    elif None not in (x_origin, y_origin, map_size):
        if rotation in (-90, 270):
            mx = (wy - y_origin) / map_size
            my = 1.0 - (wx - x_origin) / map_size
        else:
            mx = (wx - x_origin) / map_size
            my = 1.0 - (wy - y_origin) / map_size
    else:
        return None, None
    return max(0.0, min(1.0, mx)), max(0.0, min(1.0, my))


def _expand_sites(sites: dict, expansion: float = 0.15) -> dict:
    """Return site boxes expanded by `expansion` fraction of each dimension."""
    expanded = {}
    for name, box in sites.items():
        pad_x = box["width"]  * expansion
        pad_y = box["height"] * expansion
        expanded[name] = {
            "left":   box["left"]   - pad_x,
            "top":    box["top"]    - pad_y,
            "width":  box["width"]  + 2 * pad_x,
            "height": box["height"] + 2 * pad_y,
        }
    return expanded


def _point_in_box(mx: float, my: float, box: dict) -> bool:
    return (box["left"] <= mx <= box["left"] + box["width"] and
            box["top"]  <= my <= box["top"]  + box["height"])


def _site_center(box: dict) -> tuple[float, float]:
    return (box["left"] + box["width"] / 2, box["top"] + box["height"] / 2)


def _dist(a: tuple, b: tuple) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _infer_site(kills_in_round, rotation, sites, atk_team: int | None = None,
                bound_min_x=None, bound_max_x=None,
                bound_min_y=None, bound_max_y=None,
                x_origin=None, y_origin=None, map_size=None):
    """
    Improved site inference.

    Scoring model (applied to all kills in the round):
      - Final 30s of round:  ATK death → +6, DEF death → +3
      - 30–60s before end:   ATK death → +3, DEF death → +2
      - Earlier:             ATK death → +1, DEF death → +1
    Site boxes are expanded by 15% in every direction.
    An outer approach ring (additional 30%) adds half-weight for each hit.

    Winner: site with highest weighted score, provided score >= 2.
    Tie-break: nearest site to the centroid of all scored positions.

    Returns 'A', 'B', ... or None.
    """
    if not kills_in_round or not sites:
        return None

    coord_kills = [k for k in kills_in_round
                   if k["victim_x"] is not None and k["round_time_ms"] is not None]
    if not coord_kills:
        return None

    round_end = max(k["round_time_ms"] for k in coord_kills)
    site_boxes      = _expand_sites(sites, 0.15)   # 15% expanded — primary zone
    approach_boxes  = _expand_sites(sites, 0.45)   # 45% expanded — approach ring

    scores: dict[str, float] = {name: 0.0 for name in sites}
    centroid_pts: list[tuple] = []

    for k in coord_kills:
        mx, my = _coord_to_minimap(k["victim_x"], k["victim_y"], rotation,
                                    x_mult=x_mult, y_mult=y_mult,
                                    x_scalar=x_scalar, y_scalar=y_scalar,
                                    x_origin=x_origin, y_origin=y_origin,
                                    map_size=map_size)
        if mx is None:
            continue

        age = round_end - k["round_time_ms"]           # ms before round end
        is_atk = (atk_team is not None and k.get("victim_team") == atk_team)

        # Time weight: 3× for last 30s, 2× for 30–60s, 1× earlier
        if age <= 30_000:
            tw = 3.0
        elif age <= 60_000:
            tw = 2.0
        else:
            tw = 1.0

        # Team weight: attacker deaths give a direct position; defender deaths imply attacker nearby
        team_w = 2.0 if is_atk else 1.0

        point_score = tw * team_w

        # Check primary zone (15% expanded box)
        for site_name, box in site_boxes.items():
            if _point_in_box(mx, my, box):
                scores[site_name] += point_score
                centroid_pts.append((mx, my, site_name))
                break
        else:
            # Check approach ring (45% expanded minus 15% = outer 30%)
            for site_name, box in approach_boxes.items():
                if _point_in_box(mx, my, box) and not _point_in_box(mx, my, site_boxes[site_name]):
                    scores[site_name] += point_score * 0.5
                    centroid_pts.append((mx, my, site_name))
                    break

    if not any(v > 0 for v in scores.values()):
        return None

    best_site = max(scores, key=scores.get)
    best_score = scores[best_site]

    # Require a minimum weighted score of 2.0 to avoid noise
    if best_score < 2.0:
        return None

    # Tie-break via centroid nearest-site if scores are within 20% of each other
    sorted_sites = sorted(scores.items(), key=lambda x: -x[1])
    if len(sorted_sites) >= 2 and sorted_sites[1][1] >= best_score * 0.80:
        pts_x = [p[0] for p in centroid_pts]
        pts_y = [p[1] for p in centroid_pts]
        if pts_x:
            cx = sum(pts_x) / len(pts_x)
            cy = sum(pts_y) / len(pts_y)
            best_site = min(sites, key=lambda s: _dist((cx, cy), _site_center(site_boxes[s])))

    return best_site


def _compute_round_states(series_id: int, conn):
    """
    Build round_states rows for both teams in every round of every match
    in this series. Called after all base tables are populated.
    """
    conn.execute("DELETE FROM round_states WHERE series_id=?", (series_id,))

    series_row = conn.execute(
        "SELECT team1_id, team2_id FROM series WHERE series_id=?", (series_id,)
    ).fetchone()
    if not series_row:
        return

    team_ids = {1: series_row["team1_id"], 2: series_row["team2_id"]}

    matches = conn.execute(
        "SELECT match_id, map_name FROM matches WHERE series_id=?", (series_id,)
    ).fetchall()

    rows_inserted = 0
    for match in matches:
        mid      = match["match_id"]
        map_name = match["map_name"]

        # Map coordinate metadata
        meta = conn.execute(
            "SELECT x_origin, y_origin, map_size, rotation, sites_json, "
            "x_mult, y_mult, x_scalar, y_scalar "
            "FROM map_meta WHERE map_name=?", (map_name,)
        ).fetchone()
        x_origin = meta["x_origin"] if meta else None
        y_origin = meta["y_origin"] if meta else None
        map_size = meta["map_size"] if meta else None
        rotation = meta["rotation"] if meta else -90
        x_mult   = meta["x_mult"]   if meta else None
        y_mult   = meta["y_mult"]   if meta else None
        x_scalar = meta["x_scalar"] if meta else None
        y_scalar = meta["y_scalar"] if meta else None
        sites    = json.loads(meta["sites_json"]) if meta and meta["sites_json"] else {}

        # All rounds for this match, ordered
        rounds = conn.execute("""
            SELECT id, round_number, winning_team, win_condition,
                   attacking_team, team1_loadout, team2_loadout
            FROM rounds WHERE match_id=? ORDER BY round_number
        """, (mid,)).fetchall()

        # All kills for this match (keyed by round_number)
        kills_by_round: dict[int, list] = {}
        for k in conn.execute("""
            SELECT round_number, round_time_ms, killer_team, victim_team,
                   victim_x, victim_y, ability_type, killer_id
            FROM kills WHERE match_id=?
        """, (mid,)).fetchall():
            rn = k["round_number"]
            if rn not in kills_by_round:
                kills_by_round[rn] = []
            kills_by_round[rn].append(dict(k))

        # Running score trackers
        score = {1: 0, 2: 0}

        for i, r in enumerate(rounds):
            rn            = r["round_number"]
            winning_team  = r["winning_team"]
            atk_team      = r["attacking_team"]
            round_kills   = kills_by_round.get(rn, [])

            # Infer site — pass atk_team so ATK deaths get higher weight
            site = _infer_site(round_kills, rotation, sites, atk_team=atk_team,
                                x_mult=x_mult, y_mult=y_mult,
                                x_scalar=x_scalar, y_scalar=y_scalar,
                                x_origin=x_origin, y_origin=y_origin,
                                map_size=map_size)

            # Players who made ability kills this round (proxy for ult usage)
            ult_players = list({
                k["killer_id"] for k in round_kills
                if k["ability_type"] is not None and k["killer_id"]
            })

            for team_num in (1, 2):
                opp_num   = 2 if team_num == 1 else 1
                side      = "atk" if atk_team == team_num else "def"
                loadout_t = r["team1_loadout"] if team_num == 1 else r["team2_loadout"]
                eco_tier  = LOADOUT_TIER_NAMES.get(loadout_t, "unknown")

                # Previous round outcome
                prev_win = None
                if i > 0:
                    prev_win = int(rounds[i-1]["winning_team"] == team_num)

                conn.execute("""
                    INSERT INTO round_states
                      (match_id, series_id, team_id, team_number, round_number,
                       map_name, side, economy_tier, score_self, score_opp,
                       won_prev_round, ult_players_json, site_executed, won_round)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    mid, series_id,
                    team_ids[team_num], team_num, rn,
                    map_name, side, eco_tier,
                    score[team_num], score[opp_num],
                    prev_win,
                    json.dumps(ult_players),
                    site if side == "atk" else None,  # site only meaningful for attackers
                    int(winning_team == team_num) if winning_team else 0,
                ))
                rows_inserted += 1

            # Update running scores after inserting (score at round START)
            if winning_team:
                score[winning_team] += 1

    log.info("round_states: %d rows inserted for series %d", rows_inserted, series_id)


# ── Series URL helpers ────────────────────────────────────────────────────────

def _series_url(series_id: int, slug: str | None = None) -> str:
    if slug:
        return f"https://www.rib.gg/series/{slug}/{series_id}"
    # Bare-ID URL — rib.gg redirects to the slug URL
    return f"https://www.rib.gg/series/{series_id}"


def _event_url(event_slug: str, event_id: int) -> str:
    return f"https://www.rib.gg/events/{event_slug}/{event_id}"


# ── Public API ────────────────────────────────────────────────────────────────

def scrape_series(series_id: int, headless: bool = False, save_dump: bool = False) -> bool:
    """Load the series page, extract __NEXT_DATA__, store all tables. Returns success."""
    url  = _series_url(series_id)
    data = fetch_next_data(url, headless=headless)
    if not data:
        return False
    ok = _parse_and_store(data, series_id, save_dump=save_dump)
    time.sleep(BETWEEN_PAGES_S)
    return ok


def get_series_ids_from_event(event_slug: str, event_id: int,
                              headless: bool = False) -> list[int]:
    """Load event page, extract series IDs from __NEXT_DATA__."""
    url  = _event_url(event_slug, event_id)
    data = fetch_next_data(url, headless=headless)
    if not data:
        return []

    try:
        pp = data["props"]["pageProps"]
    except (KeyError, TypeError):
        log.error("Unexpected event page structure")
        return []

    # Try common locations for the series list
    series_list = (
        pp.get("series")
        or pp.get("seriesList")
        or pp.get("matches")
        or []
    )

    if not series_list:
        # Dump and let the user inspect
        DUMP_DIR.mkdir(exist_ok=True)
        out = DUMP_DIR / f"event_{event_id}.json"
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.warning("Could not find series list in event page. Dumped to %s", out)
        log.warning("pageProps keys: %s", list(pp.keys()))
        return []

    ids = [s["id"] for s in series_list if isinstance(s, dict) and "id" in s]
    log.info("Event %d: found %d series", event_id, len(ids))
    return ids


def is_scraped(series_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM kills WHERE series_id=?", (series_id,)
    ).fetchone()[0]
    conn.close()
    return n > 0


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary():
    tables = ["series", "matches", "players", "match_players",
              "rounds", "kills", "player_stats"]
    with db_conn() as conn:
        print("\n-- DB row counts -----------------------------------")
        for t in tables:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:<16} {n:>8,}")

        print("\n-- Matches with kill data --------------------------")
        rows = conn.execute("""
            SELECT s.team1_name, s.team2_name, s.event_name,
                   m.map_name, m.match_id,
                   COUNT(k.id) AS kill_count
            FROM matches m
            JOIN series s ON s.series_id = m.series_id
            LEFT JOIN kills k ON k.match_id = m.match_id
            GROUP BY m.match_id
            ORDER BY s.series_id, m.match_id
        """).fetchall()
        for r in rows:
            print(f"  [{r['match_id']}] {r['team1_name']} vs {r['team2_name']}"
                  f"  |  {r['map_name']}  |  {r['kill_count']} kills")

        sample = conn.execute(
            "SELECT * FROM kills WHERE victim_x IS NOT NULL LIMIT 1"
        ).fetchone()
        if sample:
            print("\n-- Sample kill row (shows coordinate data) ---------")
            print(f"  match={sample['match_id']}  round={sample['round_number']}"
                  f"  t={sample['round_time_ms']}ms")
            print(f"  killer(team{sample['killer_team']}) -> victim(team{sample['victim_team']})"
                  f"  weapon={sample['weapon']}")
            print(f"  victim X={sample['victim_x']}  Y={sample['victim_y']}"
                  f"  first={bool(sample['is_first_kill'])}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="rib.gg Valorant scraper (Playwright)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s_cmd = sub.add_parser("--series", help="Scrape one series by ID")
    e_cmd = sub.add_parser("--event",  help="Scrape series from an event page")
    sub.add_parser("--summary",        help="Print DB summary")

    # --series
    ap.add_argument("series_id", nargs="?", type=int)
    # --event
    ap.add_argument("event_slug", nargs="?", type=str)
    ap.add_argument("event_id",   nargs="?", type=int)
    ap.add_argument("--limit",    type=int, default=None)
    ap.add_argument("--dump",     action="store_true")
    ap.add_argument("--headless", action="store_true")

    args = ap.parse_args()

    # Re-parse since subparsers don't mix well with positional args
    args2 = _parse_cli()
    if args2 is None:
        ap.print_help()
        return

    init_db()

    if args2["cmd"] == "summary":
        print_summary()

    elif args2["cmd"] == "series":
        scrape_series(args2["series_id"], headless=args2["headless"], save_dump=args2["dump"])
        print_summary()

    elif args2["cmd"] == "event":
        ids = get_series_ids_from_event(args2["event_slug"], args2["event_id"],
                                        headless=args2["headless"])
        if args2.get("limit"):
            ids = ids[:args2["limit"]]
        for sid in ids:
            if not is_scraped(sid):
                scrape_series(sid, headless=args2["headless"], save_dump=args2["dump"])
            else:
                log.info("Series %d already scraped — skipping", sid)
        print_summary()


def _coverage_stats(conn) -> dict:
    """Return site inference coverage stats across all ATK rounds."""
    rows = conn.execute("""
        SELECT map_name,
               COUNT(*) AS total_atk,
               SUM(CASE WHEN site_executed IS NOT NULL THEN 1 ELSE 0 END) AS known
        FROM round_states
        WHERE side = 'atk'
        GROUP BY map_name
        UNION ALL
        SELECT 'ALL MAPS' AS map_name,
               COUNT(*) AS total_atk,
               SUM(CASE WHEN site_executed IS NOT NULL THEN 1 ELSE 0 END) AS known
        FROM round_states
        WHERE side = 'atk'
    """).fetchall()
    return {r["map_name"]: {"total": r["total_atk"], "known": r["known"]} for r in rows}


def recompute_all_round_states():
    """
    Re-run site inference on all series in the DB without re-scraping.
    Prints before/after coverage rates.
    """
    init_db()
    with db_conn() as conn:
        series_ids = [r[0] for r in conn.execute("SELECT series_id FROM series").fetchall()]

    if not series_ids:
        print("No series in DB. Run: python scraper.py --series <id>")
        return

    print(f"\nRecomputing round_states for {len(series_ids)} series...\n")

    # Capture BEFORE stats
    with db_conn() as conn:
        before = _coverage_stats(conn)

    # Re-run computation for every series
    for sid in series_ids:
        with db_conn() as conn:
            _compute_round_states(sid, conn)
        log.info("Recomputed series %d", sid)

    # Capture AFTER stats
    with db_conn() as conn:
        after = _coverage_stats(conn)

    # Print comparison
    all_maps = sorted(set(before) | set(after))
    print(f"{'Map':<16} {'ATK Rounds':>10}  {'Before':>10}  {'After':>10}  {'Change':>8}")
    print("-" * 60)
    for m in all_maps:
        b = before.get(m, {"total": 0, "known": 0})
        a = after.get(m, {"total": 0, "known": 0})
        total = a["total"]
        b_pct = f"{b['known']}/{b['total']} ({b['known']*100//max(1,b['total'])}%)"
        a_pct = f"{a['known']}/{a['total']} ({a['known']*100//max(1,a['total'])}%)"
        delta = a["known"] - b["known"]
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        bold = "  <--" if m == "ALL MAPS" else ""
        print(f"  {m:<14} {total:>10}  {b_pct:>10}  {a_pct:>10}  {delta_str:>6}{bold}")
    print()


def _parse_cli():
    import sys
    argv = sys.argv[1:]
    result = {"cmd": None, "headless": "--headless" in argv,
              "dump": "--dump" in argv, "limit": None}
    argv = [a for a in argv if a not in ("--headless", "--dump")]

    if "--limit" in argv:
        i = argv.index("--limit")
        result["limit"] = int(argv[i+1])
        argv = argv[:i] + argv[i+2:]

    if not argv:
        return None
    cmd = argv[0]
    if cmd == "--summary":
        result["cmd"] = "summary"
    elif cmd == "--series" and len(argv) >= 2:
        result["cmd"] = "series"
        result["series_id"] = int(argv[1])
    elif cmd == "--event" and len(argv) >= 3:
        result["cmd"] = "event"
        result["event_slug"] = argv[1]
        result["event_id"]   = int(argv[2])
    elif cmd == "--recompute-states":
        result["cmd"] = "recompute"
    else:
        return None
    return result


if __name__ == "__main__":
    import sys
    args = _parse_cli()
    if args is None:
        print(__doc__)
        sys.exit(1)
    init_db()
    if args["cmd"] == "summary":
        print_summary()
    elif args["cmd"] == "series":
        scrape_series(args["series_id"], headless=args["headless"], save_dump=args["dump"])
        print_summary()
    elif args["cmd"] == "event":
        ids = get_series_ids_from_event(args["event_slug"], args["event_id"],
                                        headless=args["headless"])
        if args.get("limit"):
            ids = ids[:args["limit"]]
        for sid in ids:
            if not is_scraped(sid):
                scrape_series(sid, headless=args["headless"], save_dump=args["dump"])
            else:
                log.info("Series %d already scraped", sid)
        print_summary()
    elif args["cmd"] == "recompute":
        recompute_all_round_states()
