"""Anonymous search statistics: what people try to plan, and where it fails.

Deliberately not analytics about *people*. Nothing here identifies anyone: no IP,
no user agent, no cookie, no session, and never the raw text somebody typed --
that last one matters, because a free-text box is where a home address would
turn up. What gets stored is the place we already resolved (a station or a
municipality) plus coordinates rounded to ~1 km, the hour rather than the
instant, and the shape of the answer.

The useful part is the failures. A planner that records only successful journeys
tells you what the network already does; recording the searches that came back
empty tells you where cyclists want to go and cannot, which is the thing nobody
else can publish.

Kept in its own database: the timetable is rebuilt from scratch every night, and
history must outlive it.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

DB_PATH = Path("data/stats.sqlite")

# ~1.1 km at these latitudes. Enough to place a search on a map, not enough to
# point at a doorway.
COORD_PLACES = 2

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY,
    hour        TEXT NOT NULL,      -- when it was searched, truncated to the hour
    day         TEXT NOT NULL,
    from_label  TEXT NOT NULL,
    from_lat    REAL, from_lon REAL,
    to_label    TEXT NOT NULL,
    to_lat      REAL, to_lon REAL,
    travel_day  TEXT,               -- the day they wanted to travel
    whole_day   INTEGER DEFAULT 0,
    other_ops   INTEGER DEFAULT 0,
    free_only   INTEGER DEFAULT 0,
    allow_avant INTEGER DEFAULT 0,
    night       INTEGER DEFAULT 0,
    access_km   REAL,
    found       INTEGER NOT NULL DEFAULT 0,
    best_seconds INTEGER,
    best_trains  INTEGER,
    best_bike_km REAL,
    from_ui      INTEGER DEFAULT 0,   -- request came from the site, not a bare API call
    uncovered    INTEGER DEFAULT 0,  -- an endpoint had no station within reach
    uncovered_place TEXT,
    closure_line TEXT                -- a bus-replaced corridor was in the way
);

CREATE INDEX IF NOT EXISTS idx_searches_day ON searches(day);
CREATE INDEX IF NOT EXISTS idx_searches_pair ON searches(from_label, to_label);
CREATE INDEX IF NOT EXISTS idx_searches_found ON searches(found);
"""


# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS will
# not touch an existing table, so they have to be added by hand -- without
# throwing away the rows already collected.
_LATER_COLUMNS = {
    "from_ui": "INTEGER DEFAULT 0",
}


def _migrate(conn: sqlite3.Connection) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(searches)")}
    for column, decl in _LATER_COLUMNS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE searches ADD COLUMN {column} {decl}")
    conn.commit()


def _connect(db_path: Path = DB_PATH, read_only: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only and db_path.exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.executescript(SCHEMA)
        _migrate(conn)
    # Six gunicorn workers share this file; WAL plus a wait beats a stray
    # "database is locked" breaking a search that otherwise worked.
    conn.execute("PRAGMA busy_timeout = 4000")
    return conn


def _round(value) -> float | None:
    try:
        return round(float(value), COORD_PLACES)
    except (TypeError, ValueError):
        return None


def record(body: dict, journeys: list[dict], uncovered: list[dict],
           closure_line: str | None = None, from_ui: bool = False,
           db_path: Path = DB_PATH) -> None:
    """Log one search. Never raises: statistics must not break the planner."""
    try:
        now = dt.datetime.now()
        origin = body.get("origin") or {}
        dest = body.get("destination") or {}
        best = journeys[0] if journeys else None

        row = (
            now.strftime("%Y-%m-%d %H:00"), now.strftime("%Y-%m-%d"),
            (origin.get("name") or "?")[:120],
            _round(origin.get("lat")), _round(origin.get("lon")),
            (dest.get("name") or "?")[:120],
            _round(dest.get("lat")), _round(dest.get("lon")),
            body.get("date"),
            int(bool(body.get("whole_day"))), int(bool(body.get("other_operators"))),
            int(bool(body.get("free_only"))), int(bool(body.get("allow_avant"))),
            int(bool(body.get("night_riding"))),
            float(body.get("access_km") or 0),
            len(journeys),
            best.get("duration_s") if best else None,
            best.get("n_trains") if best else None,
            best.get("bike_km") if best else None,
            int(bool(from_ui)),
            int(bool(uncovered)),
            (uncovered[0].get("place") if uncovered else None),
            closure_line,
        )
        conn = _connect(db_path)
        conn.execute(
            "INSERT INTO searches (hour, day, from_label, from_lat, from_lon,"
            " to_label, to_lat, to_lon, travel_day, whole_day, other_ops,"
            " free_only, allow_avant, night, access_km, found, best_seconds,"
            " best_trains, best_bike_km, from_ui, uncovered, uncovered_place,"
            " closure_line) VALUES (" + ",".join("?" * 23) + ")", row)
        conn.commit()
        conn.close()
    except Exception:                       # noqa: BLE001 -- never break a search
        pass


# ------------------------------------------------------------------ queries


def _median(values: list) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2


def dashboard(days: int = 90, db_path: Path = DB_PATH) -> dict:
    """Everything the admin page shows, in one pass."""
    if not db_path.exists():
        return {"empty": True}

    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    # Bring the schema up to date before reading. Migrations need a writable
    # connection, and the dashboard opens read-only, so without this the page
    # queries a column that does not exist yet and 500s -- right up until the
    # next search happens to migrate the table from the write path.
    _connect(db_path).close()
    conn = _connect(db_path, read_only=True)
    q = lambda sql, args=(): conn.execute(sql, args).fetchall()  # noqa: E731

    total, solved, uncovered_n = q(
        "SELECT COUNT(*), SUM(found > 0), SUM(uncovered) FROM searches WHERE day >= ?",
        (since,))[0]
    total = total or 0

    # 1. Corridors people actually ask for, with coordinates so they can be drawn.
    corridors = [
        {"from": r[0], "to": r[1], "n": r[2], "solved": r[3] or 0,
         "from_lat": r[4], "from_lon": r[5], "to_lat": r[6], "to_lon": r[7]}
        for r in q("""
            SELECT from_label, to_label, COUNT(*) n, SUM(found > 0),
                   AVG(from_lat), AVG(from_lon), AVG(to_lat), AVG(to_lon)
            FROM searches WHERE day >= ?
            GROUP BY from_label, to_label ORDER BY n DESC LIMIT 25""", (since,))]

    # 2. The failures: asked for, and no bike-carrying journey exists.
    failures = [
        {"from": r[0], "to": r[1], "n": r[2],
         "from_lat": r[3], "from_lon": r[4], "to_lat": r[5], "to_lon": r[6]}
        for r in q("""
            SELECT from_label, to_label, COUNT(*) n,
                   AVG(from_lat), AVG(from_lon), AVG(to_lat), AVG(to_lon)
            FROM searches WHERE day >= ? AND found = 0 AND uncovered = 0
            GROUP BY from_label, to_label ORDER BY n DESC LIMIT 20""", (since,))]

    # 3. Places with no station in reach: what the network does not cover at all.
    stationless = [{"place": r[0], "n": r[1]} for r in q("""
        SELECT uncovered_place, COUNT(*) n FROM searches
        WHERE day >= ? AND uncovered = 1 AND uncovered_place IS NOT NULL
        GROUP BY uncovered_place ORDER BY n DESC LIMIT 20""", (since,))]

    # 4. Searches that ran into a line closed for works.
    closures = [{"line": r[0], "n": r[1]} for r in q("""
        SELECT closure_line, COUNT(*) n FROM searches
        WHERE day >= ? AND closure_line IS NOT NULL
        GROUP BY closure_line ORDER BY n DESC LIMIT 15""", (since,))]

    # 5. What crossing Spain with a bike actually costs. Medians, not averages:
    # one 60-hour outlier should not define the typical trip.
    rows = q("""SELECT best_seconds, best_trains, best_bike_km FROM searches
                WHERE day >= ? AND found > 0""", (since,))
    cost = {
        "median_hours": round((_median([r[0] for r in rows]) or 0) / 3600, 1),
        "median_trains": _median([r[1] for r in rows]),
        "median_bike_km": _median([r[2] for r in rows]),
        "sample": len(rows),
    }

    # 6. Which options people reach for.
    opts = q("""SELECT SUM(whole_day), SUM(other_ops), SUM(free_only),
                       SUM(allow_avant), SUM(night) FROM searches WHERE day >= ?""",
             (since,))[0]
    options = [
        {"name": "Mejor combinación del día", "n": opts[0] or 0},
        {"name": "Otros operadores (FGC, Euskotren…)", "n": opts[1] or 0},
        {"name": "Solo Cercanías/Rodalies", "n": opts[2] or 0},
        {"name": "Aceptar Avant", "n": opts[3] or 0},
        {"name": "Pedalear de noche", "n": opts[4] or 0},
    ]

    daily = [{"day": r[0], "n": r[1], "failed": r[2] or 0} for r in q("""
        SELECT day, COUNT(*), SUM(found = 0) FROM searches
        WHERE day >= ? GROUP BY day ORDER BY day""", (since,))]

    # When searches happen, hour by hour. People cluster in waking hours; a
    # script is either flat across the clock or spikes at four in the morning,
    # so the shape of this is the cheapest bot-detector available without
    # storing anything identifying.
    counts = dict(q("""SELECT CAST(substr(hour, 12, 2) AS INTEGER), COUNT(*)
                       FROM searches WHERE day >= ? GROUP BY 1""", (since,)))
    hourly = [{"hour": h, "n": counts.get(h, 0)} for h in range(24)]

    # Requests that arrived with this site as their Origin/Referer, versus bare
    # calls to the endpoint. Not proof of a human, but it separates "somebody
    # used the website" from "something called the API".
    from_ui = q("SELECT SUM(from_ui), COUNT(*) FROM searches WHERE day >= ?",
                (since,))[0]

    busiest = [{"place": r[0], "n": r[1]} for r in q("""
        SELECT label, COUNT(*) n FROM (
            SELECT from_label AS label FROM searches WHERE day >= ?
            UNION ALL
            SELECT to_label FROM searches WHERE day >= ?)
        GROUP BY label ORDER BY n DESC LIMIT 15""", (since, since))]

    conn.close()
    return {
        "empty": total == 0,
        "days": days,
        "total": total,
        "solved": solved or 0,
        "uncovered": uncovered_n or 0,
        "corridors": corridors,
        "failures": failures,
        "stationless": stationless,
        "closures": closures,
        "cost": cost,
        "options": options,
        "daily": daily,
        "hourly": hourly,
        "from_ui": from_ui[0] or 0,
        "not_from_ui": (from_ui[1] or 0) - (from_ui[0] or 0),
        "busiest": busiest,
    }
