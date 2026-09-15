"""A local gazetteer of Spanish places, so typing a town name costs nothing.

Station names are matched against the timetable, but the interesting
destinations for cycle touring are villages that never had a station. Those used
to fall through to OSM's public Nominatim on *every keystroke*, which is both
slow and squarely against its usage policy (1 request/second, no heavy use,
enforced by blocking the IP).

GeoNames publishes every populated place in Spain -- some 31.000 of them, down to
hamlets -- as a 3 MB download under CC BY. Loading it locally answers the common
case instantly, offline, and without asking anything of anyone else's server.

Alternate names are indexed alongside the official one, which matters here: a
planner for Spain has to find Lleida from "Lérida", Donostia from "San
Sebastián", and Ourense from "Orense".
"""

from __future__ import annotations

import argparse
import io
import re
import sqlite3
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

DB_PATH = Path("data/places.sqlite")
GEONAMES_URL = "https://download.geonames.org/export/dump/ES.zip"
USER_AGENT = "biciabordo/1.0 (+https://biciabordo.es)"

# GeoNames' tab-separated columns we care about.
COL_NAME, COL_ASCII, COL_ALT = 1, 2, 3
COL_LAT, COL_LON = 4, 5
COL_CLASS, COL_CODE = 6, 7
COL_ADMIN2, COL_POP = 11, 14

# "P" is populated places: cities, towns, villages and hamlets. Everything else
# in the file is terrain, buildings and administrative boundaries.
PLACE_CLASS = "P"

_PROVINCE_PREFIX = re.compile(r"^(Province of|Provincia de|Província de|Provincia d'|Probintzia)\s+", re.I)

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS places (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    province   TEXT,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    population INTEGER NOT NULL DEFAULT 0
);

-- One row per way of spelling a place, so regional and Castilian names both
-- find it. `primary` ranks the official spelling above the variants.
CREATE TABLE IF NOT EXISTS names (
    norm      TEXT NOT NULL,
    place_id  INTEGER NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_names_norm ON names(norm);
CREATE INDEX IF NOT EXISTS idx_places_pop ON places(population DESC);
"""


def normalise(value: str) -> str:
    """Lowercase, unaccented, collapsed: how names are compared and indexed."""
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", value.lower()).strip()


def _clean_province(name: str) -> str:
    return _PROVINCE_PREFIX.sub("", name or "").strip()


def build(db_path: Path = DB_PATH, url: str = GEONAMES_URL) -> dict[str, int]:
    """Download GeoNames' Spain extract and load the populated places."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = resp.read()

    rows = []
    provinces: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        with zf.open("ES.txt") as fh:
            for raw in io.TextIOWrapper(fh, encoding="utf-8"):
                f = raw.rstrip("\n").split("\t")
                if len(f) <= COL_POP:
                    continue
                # Province names come from the file's own ADM2 entries, so no
                # second download is needed to say "Toledo" instead of "TO".
                if f[COL_CODE] == "ADM2":
                    provinces[f[COL_ADMIN2]] = _clean_province(f[COL_NAME])
                if f[COL_CLASS] == PLACE_CLASS:
                    rows.append(f)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    places, names = [], []
    for pid, f in enumerate(rows, start=1):
        try:
            lat, lon = float(f[COL_LAT]), float(f[COL_LON])
            pop = int(f[COL_POP] or 0)
        except ValueError:
            continue
        name = f[COL_NAME].strip()
        if not name:
            continue
        places.append((pid, name, provinces.get(f[COL_ADMIN2], ""), lat, lon, pop))

        seen = set()
        for spelling, primary in [(name, 1), (f[COL_ASCII], 0)]:
            norm = normalise(spelling)
            if norm and norm not in seen:
                seen.add(norm)
                names.append((norm, pid, primary))
        for alt in (f[COL_ALT] or "").split(","):
            norm = normalise(alt)
            # Skip the noise: single letters, and codes longer than any place name.
            if norm and 2 <= len(norm) <= 60 and norm not in seen:
                seen.add(norm)
                names.append((norm, pid, 0))

    conn.executemany("INSERT INTO places VALUES (?,?,?,?,?,?)", places)
    conn.executemany("INSERT INTO names VALUES (?,?,?)", names)
    conn.executescript(INDEXES)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('source', ?)", (url,))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('licence', 'GeoNames CC BY 4.0')")
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    return {"places": len(places), "names": len(names), "provinces": len(provinces)}


def available(db_path: Path = DB_PATH) -> bool:
    return db_path.exists()


def search(query: str, limit: int = 6, db_path: Path = DB_PATH) -> list[dict]:
    """Places matching a query, biggest first.

    Prefix matches come first because that is what someone half-way through
    typing means; a contained match is the fallback for "serena" -> "Villanueva
    de la Serena".
    """
    norm = normalise(query)
    if len(norm) < 2 or not db_path.exists():
        return []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        """
        SELECT p.name, p.province, p.lat, p.lon, p.population,
               MAX(n.is_primary)              AS primary_hit,
               MIN(CASE WHEN n.norm = ? THEN 0
                        WHEN n.norm LIKE ? THEN 1
                        ELSE 2 END)           AS quality
        FROM names n
        JOIN places p ON p.id = n.place_id
        WHERE n.norm LIKE ?
        GROUP BY p.id
        ORDER BY quality, primary_hit DESC, p.population DESC
        LIMIT ?
        """,
        (norm, f"{norm}%", f"%{norm}%", limit),
    ).fetchall()
    conn.close()

    out = []
    for name, province, lat, lon, _pop, _primary, _quality in rows:
        label = f"{name}, {province}" if province and province != name else name
        out.append({"name": label, "lat": lat, "lon": lon, "kind": "place"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the local place gazetteer")
    ap.add_argument("--db", default=DB_PATH, type=Path)
    args = ap.parse_args()
    counts = build(args.db)
    size = args.db.stat().st_size / 1e6
    print(f"done: {args.db} ({size:.1f} MB) — "
          + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
