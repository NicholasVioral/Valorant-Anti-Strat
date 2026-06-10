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
    sites_json   TEXT,
    x_mult       REAL,
    y_mult       REAL,
    x_scalar     REAL,
    y_scalar     REAL
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

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      INTEGER NOT NULL,
    series_id     INTEGER NOT NULL,
    round_number  INTEGER,
    game_time_ms  INTEGER,
    player_id     INTEGER,
    player_ign    TEXT,
    team_number   INTEGER,
    x             REAL,
    y             REAL,
    alive         INTEGER,
    has_spike     INTEGER DEFAULT 0,
    ult_charges   INTEGER DEFAULT 0,
    ult_max       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pos_match_round ON positions(match_id, round_number);
CREATE INDEX IF NOT EXISTS idx_pos_series      ON positions(series_id);

CREATE TABLE IF NOT EXISTS round_loadouts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id         INTEGER NOT NULL,
    series_id        INTEGER NOT NULL,
    round_number     INTEGER,
    player_id        INTEGER,
    player_ign       TEXT,
    team_number      INTEGER,
    primary_weapon   TEXT,
    secondary_weapon TEXT,
    armor            INTEGER,
    credits          INTEGER,
    loadout_value    INTEGER,
    UNIQUE(match_id, round_number, player_id)
);
CREATE INDEX IF NOT EXISTS idx_rl_match  ON round_loadouts(match_id);
CREATE INDEX IF NOT EXISTS idx_rl_series ON round_loadouts(series_id);
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
        # Migrate existing map_meta tables that predate the x_mult columns
        existing = {row[1] for row in conn.execute("PRAGMA table_info(map_meta)")}
        for col in ("x_mult", "y_mult", "x_scalar", "y_scalar"):
            if col not in existing:
                conn.execute(f"ALTER TABLE map_meta ADD COLUMN {col} REAL")
        pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
        for col, typedef in (("ult_charges", "INTEGER DEFAULT 0"), ("ult_max", "INTEGER DEFAULT 0")):
            if col not in pos_cols:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {typedef}")
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

    t1    = series.get("team1") or {}
    t2    = series.get("team2") or {}
    stats = series.get("stats") or {}

    # Skip series with no 2D replay data (no kill coordinates)
    kills_raw   = stats.get("kills") or []
    coord_count = sum(1 for k in kills_raw if k.get("victimLocationX") is not None)
    if coord_count == 0:
        log.warning("Series %d: no kill coordinates found — skipping (no 2D replay data)", series_id)
        return False

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
                x_mult=None, y_mult=None, x_scalar=None, y_scalar=None,
                x_origin=None, y_origin=None, map_size=None,
                bound_min_x=None, bound_max_x=None,
                bound_min_y=None, bound_max_y=None):
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


# ── Team search ───────────────────────────────────────────────────────────────

def search_team(team_name: str, headless: bool = False) -> tuple[int | None, str | None]:
    """
    Search rib.gg for a team by name. Returns (team_id, team_slug) or (None, None).
    Dumps __NEXT_DATA__ to api_dumps/ if the expected structure isn't found.
    """
    import urllib.parse
    url = f"https://www.rib.gg/search?q={urllib.parse.quote(team_name)}"
    data = fetch_next_data(url, headless=headless)
    if not data:
        return None, None

    try:
        pp = data["props"]["pageProps"]
    except (KeyError, TypeError):
        return None, None

    # rib.gg search puts teams under several possible keys
    teams = (
        pp.get("teams")
        or (pp.get("results") or {}).get("teams")
        or []
    )

    if not teams:
        DUMP_DIR.mkdir(exist_ok=True)
        out = DUMP_DIR / f"search_{team_name.lower().replace(' ', '_')}.json"
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.warning("No teams found in search results. Dumped to %s", out)
        log.warning("pageProps keys: %s", list(pp.keys()))
        return None, None

    name_lower = team_name.lower()
    for t in teams:
        if not isinstance(t, dict):
            continue
        t_name = (t.get("name") or t.get("shortName") or "").lower()
        if name_lower in t_name or t_name in name_lower:
            return t.get("id"), t.get("slug") or t_name.replace(" ", "-")

    # Fall back to first result
    first = teams[0]
    if isinstance(first, dict):
        log.warning("Exact match not found, using first result: %s", first.get("name"))
        return first.get("id"), first.get("slug")

    return None, None


def get_team_recent_series(team_id: int, team_slug: str | None = None,
                            limit: int = 7, headless: bool = False) -> list[int]:
    """
    Load a team's rib.gg matches page, intercept API responses, and return
    the most recent series IDs.

    rib.gg loads the series list via be-prod.rib.gg API calls (not __NEXT_DATA__),
    so we use Playwright response interception to capture it.

    Dumps captured data to api_dumps/ when the structure isn't recognised so
    the right field names can be identified.
    """
    sync_playwright = _get_playwright()

    if team_slug:
        url = f"https://www.rib.gg/teams/{team_slug}/matches/{team_id}"
    else:
        url = f"https://www.rib.gg/teams/{team_id}"

    api_calls: list[dict] = []
    next_data: dict | None = None

    with sync_playwright() as p:
        browser = _make_browser(p, headless=headless)
        ctx     = _make_context(browser)
        page    = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        def on_response(resp):
            rurl = resp.url
            if ("be-prod.rib.gg" in rurl or "/api/" in rurl) and resp.status < 400:
                try:
                    body = resp.json()
                    api_calls.append({"url": rurl, "data": body})
                except Exception:
                    pass

        page.on("response", on_response)

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
            next_data = json.loads(raw) if raw else None
        except Exception as e:
            log.error("Failed to load team page %d: %s", team_id, e)
        finally:
            browser.close()

    # ── Try to extract series IDs ─────────────────────────────────────────────

    # ID field names rib.gg may use for series objects
    ID_KEYS = ("id", "seriesId", "series_id", "matchId", "match_id")

    def extract_ids(items) -> list[int]:
        ids = []
        for item in (items or []):
            if not isinstance(item, dict):
                continue
            for k in ID_KEYS:
                if k in item:
                    ids.append(item[k])
                    break
        return ids

    # 1. Try __NEXT_DATA__ pageProps
    pp = {}
    try:
        pp = (next_data or {}).get("props", {}).get("pageProps", {})
    except Exception:
        pass

    for key in ("series", "matches", "recentMatches", "recentSeries", "seriesList"):
        candidate = pp.get(key)
        if candidate:
            ids = extract_ids(candidate)
            if ids:
                log.info("Team %d: found %d series in pageProps[%s]", team_id, len(ids), key)
                return ids[:limit]

    # 2. Try intercepted API responses
    for call in api_calls:
        body = call.get("data") or {}
        if isinstance(body, list):
            ids = extract_ids(body)
            if ids:
                log.info("Team %d: found %d series in API response %s", team_id, len(ids), call["url"])
                return ids[:limit]
        if isinstance(body, dict):
            for key in ("series", "matches", "recentMatches", "data", "results", "seriesList"):
                candidate = body.get(key)
                if candidate and isinstance(candidate, list):
                    ids = extract_ids(candidate)
                    if ids:
                        log.info("Team %d: found %d series in API[%s][%s]",
                                 team_id, len(ids), call["url"], key)
                        return ids[:limit]

    # ── Nothing found — dump everything for inspection ────────────────────────
    DUMP_DIR.mkdir(exist_ok=True)
    summary = {
        "url":             url,
        "pageProps_keys":  list(pp.keys()),
        "api_calls": [
            {"url":      c["url"],
             "top_keys": list(c["data"].keys()) if isinstance(c.get("data"), dict)
                         else f"[list len={len(c['data'])}]" if isinstance(c.get("data"), list)
                         else str(type(c.get("data")))}
            for c in api_calls
        ],
    }
    out_s = DUMP_DIR / f"team_{team_id}_summary.json"
    out_f = DUMP_DIR / f"team_{team_id}_full.json"
    out_s.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    out_f.write_text(
        json.dumps({"next_data": next_data, "api_calls": api_calls},
                   indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    log.warning(
        "Could not find series list for team %d.\n"
        "  pageProps keys : %s\n"
        "  API calls      : %d\n"
        "  Summary dumped : %s\n"
        "  Full dump      : %s",
        team_id, list(pp.keys()), len(api_calls), out_s, out_f
    )
    return []


def get_team_id_from_db(team_name: str) -> int | None:
    """Return the rib.gg team ID if we've already scraped any series for this team."""
    with db_conn() as conn:
        row = conn.execute("""
            SELECT team1_id FROM series WHERE LOWER(team1_name) = LOWER(?)
            UNION
            SELECT team2_id FROM series WHERE LOWER(team2_name) = LOWER(?)
            LIMIT 1
        """, (team_name, team_name)).fetchone()
    return row[0] if row else None


# ── 2D replay fetching ────────────────────────────────────────────────────────

def fetch_2d_map(series_id: int, map_num: int, headless: bool = False) -> dict:
    """
    Load the rib.gg 2D replay page for one map of a series.
    Intercepts all be-prod.rib.gg API responses AND extracts __NEXT_DATA__.

    URL format: https://www.rib.gg/2d/series/{series_id}/map/{map_num}

    Returns:
        {'next_data': {...} or None,
         'api_calls': [{'url': ..., 'data': ...}, ...]}
    """
    sync_playwright = _get_playwright()
    url = f"https://www.rib.gg/2d/series/{series_id}/map/{map_num}"
    api_calls: list[dict] = []

    with sync_playwright() as p:
        browser = _make_browser(p, headless=headless)
        ctx     = _make_context(browser)
        page    = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        def on_response(resp):
            rurl = resp.url
            if ("be-prod.rib.gg" in rurl or "/api/" in rurl) and resp.status < 400:
                try:
                    body = resp.json()
                    api_calls.append({"url": rurl, "data": body})
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            log.info("Loading 2D replay: %s", url)
            page.goto(url, wait_until="load", timeout=45_000)
            page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
            raw = page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? el.textContent : null;
                }
            """)
            next_data = json.loads(raw) if raw else None

            # Fetch every round's frames directly via the rib.gg API.
            # rib.gg only auto-fetches a subset on initial load; the rest must
            # be fetched explicitly. We call the API from within the page
            # context so the browser's session cookies are used automatically.
            if next_data:
                try:
                    import re as _re
                    match_rounds = (
                        next_data.get("props", {})
                        .get("pageProps", {})
                        .get("matchRounds", {})
                        .get("rounds", [])
                    )
                    already_fetched = {
                        int(m.group(1))
                        for c in api_calls
                        if (m := _re.search(r"/round/(\d+)/frames", c.get("url", "")))
                    }
                    total_rounds = [r["round_number"] for r in match_rounds
                                    if isinstance(r, dict) and "round_number" in r]
                    missing = [r for r in total_rounds if r not in already_fetched]
                    if missing:
                        log.info("Fetching %d missing rounds via API: %s", len(missing), missing)
                    for rnum in missing:
                        api_url = (f"https://be-prod.rib.gg/v1/2d-replay/series"
                                   f"/{series_id}/map/{map_num}/round/{rnum}/frames")
                        try:
                            data = page.evaluate(f"""
                                async () => {{
                                    const r = await fetch("{api_url}",
                                        {{credentials: "include"}});
                                    if (!r.ok) return null;
                                    return await r.json();
                                }}
                            """)
                            if data:
                                api_calls.append({"url": api_url, "data": data})
                                log.info("  Round %d fetched (%d frames)",
                                         rnum, len(data.get("frames", [])))
                            else:
                                log.warning("  Round %d returned null", rnum)
                        except Exception as fe:
                            log.warning("  Round %d fetch failed: %s", rnum, fe)
                except Exception as nav_e:
                    log.warning("Round fetch error: %s", nav_e)

        except Exception as e:
            log.error("Failed to load 2D page series %d map %d: %s", series_id, map_num, e)
            next_data = None
        finally:
            browser.close()

    return {"next_data": next_data, "api_calls": api_calls}


def _extract_positions(result: dict, series_id: int, map_num: int) -> list | None:
    """
    Extract player position snapshot data from a fetch_2d_map() result.

    Tries __NEXT_DATA__ pageProps first, then intercepted API responses.
    If the structure is unrecognised, dumps everything to api_dumps/ and returns None.
    The dump lets you inspect the real key names and update this function.
    """
    next_data  = result.get("next_data") or {}
    api_calls  = result.get("api_calls") or []

    try:
        pp = next_data.get("props", {}).get("pageProps", {})
    except Exception:
        pp = {}

    # Common key names for round/position data across rib.gg versions
    rounds_data = (
        pp.get("rounds")
        or pp.get("roundData")
        or pp.get("replayData")
        or pp.get("positions")
        or pp.get("snapshots")
        or pp.get("data")
    )

    if not rounds_data:
        for call in api_calls:
            body = call.get("data") or {}
            if not isinstance(body, dict):
                continue
            rounds_data = (
                body.get("rounds")
                or body.get("roundData")
                or body.get("replayData")
                or body.get("positions")
                or body.get("snapshots")
            )
            if rounds_data:
                log.info("Position data found in API response: %s", call["url"])
                break

    if not rounds_data:
        # Dump everything so the key names can be identified
        DUMP_DIR.mkdir(exist_ok=True)
        summary = {
            "pageProps_keys": list(pp.keys()),
            "api_calls": [
                {"url": c["url"],
                 "top_keys": list(c["data"].keys()) if isinstance(c.get("data"), dict) else str(type(c.get("data")))}
                for c in api_calls
            ],
        }
        out_summary = DUMP_DIR / f"2d_series_{series_id}_map_{map_num}_summary.json"
        out_full    = DUMP_DIR / f"2d_series_{series_id}_map_{map_num}_full.json"
        out_summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        out_full.write_text(
            json.dumps({"next_data": next_data, "api_calls": api_calls}, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        log.warning(
            "2D structure unknown for series %d map %d — dumped to %s\n"
            "  pageProps keys: %s\n"
            "  API calls intercepted: %d",
            series_id, map_num, out_summary,
            list(pp.keys()), len(api_calls)
        )
        return None

    return rounds_data if isinstance(rounds_data, list) else [rounds_data]


def _store_positions(rounds_data: list, series_id: int, match_id: int):
    """
    Persist player position snapshots from 2D replay data.
    Handles multiple possible shapes of the round/snapshot objects.
    """
    rows = []
    for round_obj in rounds_data:
        if not isinstance(round_obj, dict):
            continue

        round_num = (
            round_obj.get("roundNumber")
            or round_obj.get("number")
            or round_obj.get("round")
        )

        # Each round contains an array of timed frames / snapshots
        frames = (
            round_obj.get("positions")
            or round_obj.get("snapshots")
            or round_obj.get("frames")
            or round_obj.get("playerLocations")
            or []
        )

        for frame in frames:
            if not isinstance(frame, dict):
                continue
            t = (
                frame.get("gameTime")
                or frame.get("time")
                or frame.get("timestamp")
                or frame.get("roundTimeMillis")
                or 0
            )
            players = (
                frame.get("players")
                or frame.get("playerPositions")
                or frame.get("locations")
                or []
            )
            for pl in players:
                if not isinstance(pl, dict):
                    continue
                rows.append((
                    match_id, series_id, round_num, t,
                    pl.get("playerId") or pl.get("id"),
                    pl.get("ign") or pl.get("name"),
                    pl.get("teamNumber") or pl.get("team"),
                    pl.get("x") or pl.get("locationX") or pl.get("posX"),
                    pl.get("y") or pl.get("locationY") or pl.get("posY"),
                    int(bool(pl.get("alive", True))),
                    int(bool(pl.get("hasSpike") or pl.get("spike", False))),
                ))

    if not rows:
        log.warning("No position rows extracted for match %d — data shape may differ from expected", match_id)
        return

    with db_conn() as conn:
        conn.execute("DELETE FROM positions WHERE match_id=?", (match_id,))
        conn.executemany("""
            INSERT INTO positions
              (match_id, series_id, round_number, game_time_ms,
               player_id, player_ign, team_number, x, y, alive, has_spike)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
    log.info("Stored %d position snapshots for match %d", len(rows), match_id)


def _parse_ribgg_2d(pp: dict, series_id: int, match_id: int,
                    api_calls: list | None = None) -> int:
    """
    Parse rib.gg's matchRoundFrames format into position rows and store them.

    __NEXT_DATA__ only contains round 1 frames.  All other rounds come from
    intercepted API calls to:
      be-prod.rib.gg/v1/2d-replay/series/{id}/map/{N}/round/{R}/frames
    Pass api_calls from fetch_2d_map() to get complete per-round coverage.

    has_spike column is repurposed to store ult_ready (1 = ult charged).
    """
    import re as _re

    frames      = pp.get("matchRoundFrames", [])
    rounds_meta = pp.get("matchRounds", {}).get("rounds", [])
    config      = (pp.get("matchMetadata") or {}).get("configuration", {})
    players_cfg = config.get("players", [])
    teams_cfg   = config.get("teams", [])

    if not frames or not rounds_meta:
        return 0

    # player_id → config dict
    player_map: dict[int, dict] = {p["player_id"]: p for p in players_cfg}

    # player_id → team_number (1 or 2, based on position in teams list)
    team_map: dict[int, int] = {}
    for tnum, team in enumerate(teams_cfg, start=1):
        for pid in team.get("player_ids", []):
            team_map[pid] = tnum

    # Build sorted (round_number, start_ms) list for round assignment
    round_times = sorted(
        [(r["round_number"], r["round_start_match_time_ms"])
         for r in rounds_meta if "round_start_match_time_ms" in r],
        key=lambda x: x[1],
    )
    round_start_lookup = {rnum: sms for rnum, sms in round_times}

    def assign_round(frame_ms: float) -> tuple[int | None, int]:
        for i, (rnum, start_ms) in enumerate(round_times):
            next_ms = round_times[i + 1][1] if i + 1 < len(round_times) else float("inf")
            if start_ms <= frame_ms < next_ms:
                return rnum, int(frame_ms - start_ms)
        return None, 0

    # Merge SSR frames (round 1) with all intercepted per-round API frames
    all_frame_sources: list[tuple[list, int | None]] = [(frames, None)]
    if api_calls:
        pat = _re.compile(r"/round/(\d+)/frames")
        for call in api_calls:
            m = pat.search(call.get("url", ""))
            if m:
                rnum = int(m.group(1))
                call_frames = call.get("data", {}).get("frames", [])
                if call_frames:
                    all_frame_sources.append((call_frames, rnum))

    rows = []
    last_starting: dict[int, list] = {}  # round_number -> last buy-phase playerStatus

    for frame_list, forced_round in all_frame_sources:
        for frame in frame_list:
            phase = frame.get("phase")

            # Capture last buy-phase snapshot per round (forced_round only, SSR is round 1)
            if phase == "ROUND_STARTING" and forced_round is not None:
                last_starting[forced_round] = frame.get("playerStatus", [])

            if phase != "IN_ROUND":
                continue

            time_str = frame.get("omittingPauses", "0s")
            try:
                frame_ms = float(str(time_str).rstrip("s")) * 1000
            except (ValueError, AttributeError):
                continue

            if forced_round is not None:
                round_num     = forced_round
                start_ms      = round_start_lookup.get(forced_round, frame_ms)
                round_time_ms = max(0, int(frame_ms - start_ms))
            else:
                round_num, round_time_ms = assign_round(frame_ms)

            if round_num is None:
                continue

            for ps in frame.get("playerStatus", []):
                pid   = ps.get("playerId")
                p_cfg = player_map.get(pid, {})
                tnum  = team_map.get(pid)

                abilities = ps.get("abilities", [])
                ult_charges = 0
                ult_max     = 0
                for ab in abilities:
                    slot = ab.get("inventorySlot", "")
                    if slot == "ULTIMATE" or slot == "ABILITY_5":
                        ult_charges = int(ab.get("totalCharges") or 0)
                        ult_max     = int(ab.get("maxCharges")   or 0)
                        break
                if not ult_charges and abilities:
                    last = abilities[-1]
                    ult_charges = int(last.get("totalCharges") or 0)
                    ult_max     = int(last.get("maxCharges")   or 0)

                rows.append((
                    match_id, series_id, round_num, round_time_ms,
                    p_cfg.get("rib_id") or pid,
                    p_cfg.get("display_name"),
                    tnum,
                    ps.get("locationX"),
                    ps.get("locationY"),
                    int(bool(ps.get("isAlive", True))),
                    int(ult_charges > 0),  # has_spike = ult_ready (binary, kept for compat)
                    ult_charges,
                    ult_max,
                ))

    if not rows:
        log.warning("matchRoundFrames found but no IN_ROUND frames extracted for match %d", match_id)
        return 0

    # Build per-player loadout rows from last buy-phase frame of each round
    loadout_rows = []
    for round_num, player_statuses in last_starting.items():
        for ps in player_statuses:
            pid   = ps.get("playerId")
            p_cfg = player_map.get(pid, {})
            tnum  = team_map.get(pid)
            primary = secondary = None
            for item in ps.get("inventory", []):
                slot = item.get("slot", "")
                if slot == "PRIMARY":
                    primary = item.get("displayName")
                elif slot == "SECONDARY":
                    secondary = item.get("displayName")
            loadout_rows.append((
                match_id, series_id, round_num,
                p_cfg.get("rib_id") or pid,
                p_cfg.get("display_name"),
                tnum,
                primary,
                secondary,
                ps.get("armor", 0),
                ps.get("credits", 0),
                ps.get("loadoutValue", 0),
            ))

    with db_conn() as conn:
        conn.execute("DELETE FROM positions WHERE match_id=?", (match_id,))
        conn.executemany("""
            INSERT INTO positions
              (match_id, series_id, round_number, game_time_ms,
               player_id, player_ign, team_number, x, y, alive, has_spike,
               ult_charges, ult_max)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        if loadout_rows:
            conn.execute("DELETE FROM round_loadouts WHERE match_id=?", (match_id,))
            conn.executemany("""
                INSERT OR REPLACE INTO round_loadouts
                  (match_id, series_id, round_number, player_id, player_ign, team_number,
                   primary_weapon, secondary_weapon, armor, credits, loadout_value)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, loadout_rows)

    log.info("Stored %d position frames + %d loadout rows for match %d across %d rounds",
             len(rows), len(loadout_rows), match_id, len(round_times))
    return len(rows)


def scrape_2d_series(series_id: int, headless: bool = False, force: bool = False):
    """
    Fetch 2D replay data for all maps of a series.
    match_ids are read from the DB (populated by the main scrape).
    force=True re-fetches even if positions already exist (use after fixing bugs).
    """
    with db_conn() as conn:
        match_ids = [
            r[0] for r in conn.execute(
                "SELECT match_id FROM matches WHERE series_id=? ORDER BY match_number",
                (series_id,)
            ).fetchall()
        ]

    if not match_ids:
        log.warning("No matches found in DB for series %d — run main scrape first", series_id)
        return

    for map_num, match_id in enumerate(match_ids, start=1):
        with db_conn() as conn:
            kill_count = conn.execute(
                "SELECT COUNT(*) FROM kills WHERE match_id=?", (match_id,)
            ).fetchone()[0]
            existing = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE match_id=?", (match_id,)
            ).fetchone()[0]

        if kill_count == 0:
            log.info("Match %d has no kills (unplayed map) — skipping 2D fetch", match_id)
            continue
        if existing > 0 and not force:
            log.info("2D data already stored for match %d — skipping (use --force-2d to re-fetch)",
                     match_id)
            continue

        log.info("Fetching 2D replay: series %d  map %d  (match_id=%d)",
                 series_id, map_num, match_id)
        result = fetch_2d_map(series_id, map_num, headless=headless)

        # Always save the full dump so we can re-parse without re-fetching
        dump_path = DUMP_DIR / f"2d_series_{series_id}_map_{map_num}_full.json"
        dump_path.write_text(
            json.dumps({"next_data": result.get("next_data"), "api_calls": result.get("api_calls", [])},
                       indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        log.info("Saved 2D dump: %s", dump_path)

        # rib.gg specific format: matchRoundFrames in __NEXT_DATA__
        pp = (result.get("next_data") or {}).get("props", {}).get("pageProps", {})
        api_calls = result.get("api_calls", [])
        if pp.get("matchRoundFrames"):
            stored = _parse_ribgg_2d(pp, series_id, match_id, api_calls=api_calls)
            if stored == 0:
                log.warning("matchRoundFrames present but 0 rows stored for map %d", map_num)
            else:
                log.info("Stored %d frames for map %d (match %d)", stored, map_num, match_id)
        else:
            rounds_data = _extract_positions(result, series_id, map_num)
            if rounds_data:
                _store_positions(rounds_data, series_id, match_id)
            else:
                log.warning("No 2D position data found for map %d", map_num)

        time.sleep(BETWEEN_PAGES_S)


def reparse_2d_from_dumps(series_id: int):
    """
    Re-parse 2D position data from already-saved dump files.
    Uses the intercepted API calls to get all rounds (not just round 1).
    Faster than re-scraping — no browser needed.
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT match_id, match_number FROM matches WHERE series_id=? ORDER BY match_number",
            (series_id,)
        ).fetchall()

    if not rows:
        log.warning("No matches found in DB for series %d", series_id)
        return

    for match_row in rows:
        match_id  = match_row[0]
        map_num   = match_row[1]
        dump_path = DUMP_DIR / f"2d_series_{series_id}_map_{map_num}_full.json"

        if not dump_path.exists():
            log.warning("No dump file for series %d map %d — run scraper first", series_id, map_num)
            continue

        with open(dump_path, encoding="utf-8") as f:
            data = json.load(f)

        pp        = (data.get("next_data") or {}).get("props", {}).get("pageProps", {})
        api_calls = data.get("api_calls", [])

        if not pp.get("matchRoundFrames"):
            log.info("No matchRoundFrames in dump for map %d — skipping", map_num)
            continue

        with db_conn() as conn:
            kill_count = conn.execute(
                "SELECT COUNT(*) FROM kills WHERE match_id=?", (match_id,)
            ).fetchone()[0]

        if kill_count == 0:
            log.info("Match %d has no kills (unplayed) — skipping", match_id)
            continue

        log.info("Re-parsing 2D data for series %d map %d (match %d) from dump...",
                 series_id, map_num, match_id)
        stored = _parse_ribgg_2d(pp, series_id, match_id, api_calls=api_calls)
        log.info("Stored %d frames for match %d", stored, match_id)


# ── Public API ────────────────────────────────────────────────────────────────

def scrape_series(series_id: int, headless: bool = False, save_dump: bool = False) -> bool:
    """
    Scrape a series: main match data first, then 2D replay positions for each map.
    Returns True if the main scrape succeeded (2D failures are non-fatal).
    """
    url  = _series_url(series_id)
    data = fetch_next_data(url, headless=headless)
    if not data:
        return False
    ok = _parse_and_store(data, series_id, save_dump=save_dump)
    if not ok:
        return False
    time.sleep(BETWEEN_PAGES_S)

    # Fetch 2D replay positions for each map
    scrape_2d_series(series_id, headless=headless)
    return True


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
    elif cmd == "--reparse-2d" and len(argv) >= 2:
        result["cmd"] = "reparse2d"
        result["series_id"] = int(argv[1])
    elif cmd == "--force-2d" and len(argv) >= 2:
        result["cmd"] = "force2d"
        result["series_id"] = int(argv[1])
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
    elif args["cmd"] == "reparse2d":
        reparse_2d_from_dumps(args["series_id"])
        print_summary()
    elif args["cmd"] == "force2d":
        scrape_2d_series(args["series_id"], headless=args["headless"], force=True)
        print_summary()
