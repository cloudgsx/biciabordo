"""Download every configured GTFS feed and normalise them into one SQLite database.

Renfe's two feeds share a station-code namespace and merge on ``stop_id``. The
regional operators (FGC, Euskotren, TRAM d'Alacant) do not, so their stops are
namespaced and interchange is left to the geographic transfer builder. Every
route and trip is tagged with its operator so the planner can be asked for
Renfe-only or for the whole network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sqlite3
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .bikepolicy import CLASS_NOTES, BikeClass, classify
from .feeds import FEEDS, FeedSpec
from .parse import parse_time, read_rows, service_dates

# Bumped whenever the tables change shape. The database lives in a Docker volume
# and outlives the image, so without a stamped version a container running new
# code silently queries columns an older build never created, and every request
# 500s with "no such column".
SCHEMA_VERSION = "3"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS stops (
    stop_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    lat       REAL NOT NULL,
    lon       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id   TEXT PRIMARY KEY,
    feed       TEXT NOT NULL,
    operator   TEXT NOT NULL,
    is_renfe   INTEGER NOT NULL,
    short_name TEXT,
    long_name  TEXT,
    color      TEXT,
    route_type INTEGER,
    bike_class INTEGER NOT NULL,
    bike_note  TEXT
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id    TEXT PRIMARY KEY,
    route_id   TEXT NOT NULL,
    service_id TEXT NOT NULL,
    feed       TEXT NOT NULL,
    headsign   TEXT,
    bike_class INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stop_times (
    trip_id  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    stop_id  TEXT NOT NULL,
    arr      INTEGER,
    dep      INTEGER,
    PRIMARY KEY (trip_id, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS service_days (
    service_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    PRIMARY KEY (service_id, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_stop_times_stop ON stop_times(stop_id);
CREATE INDEX IF NOT EXISTS idx_trips_service ON trips(service_id);
CREATE INDEX IF NOT EXISTS idx_service_days_day ON service_days(day);
"""


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("ftp://"):
        with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": "bicitren/0.1"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    return dest


def _prefix(feed: str, value: str) -> str:
    return f"{feed}:{value}"


def _stop_key(spec: FeedSpec, stop_id: str) -> str:
    return _prefix(spec.key, stop_id) if spec.prefix_stops else stop_id


def ingest_feed(conn: sqlite3.Connection, spec: FeedSpec, folder: Path) -> dict[str, int]:
    counts = {"stops": 0, "routes": 0, "trips": 0, "stop_times": 0, "service_days": 0}

    # --- stops. Platform-level stops are collapsed into their parent station so
    # results name a station once instead of listing each platform separately.
    raw = list(read_rows(folder / "stops.txt"))
    parent_of: dict[str, str] = {}
    for row in raw:
        parent = (row.get("parent_station") or "").strip()
        if parent:
            parent_of[row["stop_id"]] = parent

    stop_rows = []
    for row in raw:
        stop_id = row["stop_id"]
        if stop_id in parent_of:
            continue                      # represented by its parent
        if (row.get("location_type") or "0") not in ("0", "1", ""):
            continue                      # entrances, nodes, boarding areas
        try:
            lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        except (KeyError, ValueError):
            continue
        if not row.get("stop_name"):
            continue
        stop_rows.append((_stop_key(spec, stop_id), row["stop_name"], lat, lon))

    conn.executemany("INSERT OR REPLACE INTO stops VALUES (?,?,?,?)", stop_rows)
    counts["stops"] = len(stop_rows)

    # --- routes, with the bike class derived per operator.
    route_bike: dict[str, BikeClass] = {}
    route_rows = []
    for row in read_rows(folder / "routes.txt"):
        rid = _prefix(spec.key, row["route_id"])
        try:
            route_type = int(row.get("route_type") or -1)
        except ValueError:
            route_type = -1
        cls = classify(row.get("route_short_name", ""), spec.key, route_type)
        route_bike[rid] = cls
        note = spec.note or CLASS_NOTES[cls]
        if spec.note and cls is BikeClass.BAGGED:
            note = CLASS_NOTES[cls]
        route_rows.append((
            rid, spec.key, spec.operator, int(spec.is_renfe),
            row.get("route_short_name"), row.get("route_long_name"),
            row.get("route_color"), route_type, int(cls), note,
        ))
    conn.executemany("INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?,?,?,?,?,?)", route_rows)
    counts["routes"] = len(route_rows)

    # --- trips
    trip_rows = []
    for row in read_rows(folder / "trips.txt"):
        rid = _prefix(spec.key, row["route_id"])
        cls = route_bike.get(rid, BikeClass.BAGGED)
        trip_rows.append((
            _prefix(spec.key, row["trip_id"]), rid, _prefix(spec.key, row["service_id"]),
            spec.key, row.get("trip_headsign") or None, int(cls),
        ))
    conn.executemany("INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?,?)", trip_rows)
    counts["trips"] = len(trip_rows)

    # --- stop_times, streamed: the Cercanías file alone is ~280 MB of padded CSV.
    def stop_time_rows():
        for row in read_rows(folder / "stop_times.txt"):
            arr = parse_time(row.get("arrival_time", ""))
            dep = parse_time(row.get("departure_time", ""))
            if arr is None and dep is None:
                continue
            stop_id = row["stop_id"]
            stop_id = parent_of.get(stop_id, stop_id)
            yield (
                _prefix(spec.key, row["trip_id"]),
                int(row["stop_sequence"]),
                _stop_key(spec, stop_id),
                arr if arr is not None else dep,
                dep if dep is not None else arr,
            )
            counts["stop_times"] += 1

    conn.executemany("INSERT OR REPLACE INTO stop_times VALUES (?,?,?,?,?)", stop_time_rows())

    # --- calendars
    days = service_dates(folder / "calendar.txt", folder / "calendar_dates.txt")
    conn.executemany(
        "INSERT OR REPLACE INTO service_days VALUES (?,?)",
        [(_prefix(spec.key, sid), day.isoformat())
         for sid, dayset in days.items() for day in dayset],
    )
    counts["service_days"] = sum(len(v) for v in days.values())
    return counts


def build(db_path: Path, cache_dir: Path, use_cache: bool = False,
          only: list[str] | None = None) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Build beside the live database and swap it in at the end, rather than into
    # it. Building in place meant deleting the very file the web container is
    # serving from: for the minute or so a rebuild takes, every request hit a
    # missing file and then a half-built one, once a day, quietly.
    target = db_path
    db_path = target.with_name(target.name + ".building")
    for suffix in ("", "-wal", "-shm"):
        stale = db_path.with_name(db_path.name + suffix)
        if stale.exists():
            stale.unlink()

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA synchronous = OFF")

    loaded: list[str] = []
    skipped: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        for key, spec in FEEDS.items():
            if only and key not in only:
                continue
            archive = cache_dir / f"{key}.zip"

            if spec.url is None:
                # Login-gated feed: use it only if the user dropped it in.
                if not archive.exists():
                    skipped.append(f"{key} (sin {archive}, ver README)")
                    continue
            elif not (use_cache and archive.exists()):
                print(f"[{key}] downloading {spec.url}")
                try:
                    download(spec.url, archive)
                except Exception as exc:                     # noqa: BLE001
                    # One unreachable regional feed must not lose the whole build.
                    skipped.append(f"{key} ({exc})")
                    continue

            print(f"[{key}] {archive.stat().st_size / 1e6:.1f} MB -> extracting")
            folder = Path(tmp) / key
            try:
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(folder)
                counts = ingest_feed(conn, spec, folder)
            except Exception as exc:                          # noqa: BLE001
                skipped.append(f"{key} ({exc})")
                continue

            conn.commit()
            loaded.append(key)
            print(f"[{key}] " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    print("building indexes...")
    conn.executescript(INDEXES)

    horizon = conn.execute("SELECT MIN(day), MAX(day) FROM service_days").fetchone()
    conn.executemany(
        "INSERT OR REPLACE INTO meta VALUES (?,?)",
        [
            ("built_at", dt.datetime.now().isoformat(timespec="seconds")),
            ("first_day", horizon[0] or ""),
            ("last_day", horizon[1] or ""),
            ("feeds", ",".join(loaded)),
            ("schema_version", SCHEMA_VERSION),
        ],
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()

    # Closing cleanly folds the write-ahead log back into the file, so what gets
    # swapped in is self-contained. The log left over from the replaced database
    # has to go with it: an unrelated -wal sitting next to a database is exactly
    # the "results are undefined" case in SQLite's own documentation. A reader
    # that still has it open keeps its own copy of the inode and is unaffected.
    os.replace(db_path, target)
    for suffix in ("-wal", "-shm"):
        stale = target.with_name(target.name + suffix)
        if stale.exists():
            stale.unlink()
    db_path = target

    print(f"done: {db_path} ({db_path.stat().st_size / 1e6:.0f} MB), "
          f"service window {horizon[0]} .. {horizon[1]}")
    print(f"feeds loaded: {', '.join(loaded)}")
    for item in skipped:
        print(f"  skipped: {item}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the bicitren timetable database")
    ap.add_argument("--db", default="data/bicitren.sqlite", type=Path)
    ap.add_argument("--cache-dir", default="data/feeds", type=Path)
    ap.add_argument("--use-cache", action="store_true", help="reuse already-downloaded zips")
    ap.add_argument("--only", nargs="*", choices=sorted(FEEDS), help="ingest only these feeds")
    args = ap.parse_args()
    build(args.db, args.cache_dir, args.use_cache, args.only)


if __name__ == "__main__":
    main()
