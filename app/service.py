"""Application layer: timetable cache, place lookup, and journey formatting."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import closures, elevation, gazetteer
from .gtfs.bikepolicy import CLASS_NOTES, BikeClass
from .gtfs.ingest import SCHEMA_VERSION
from .routing import raptor
from .routing.timetable import DAY, Timetable, bearing, haversine_km, ride_seconds

DB_PATH = Path("data/bicitren.sqlite")
GEO_CACHE = Path("data/geocache.sqlite")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's policy asks for an identifiable agent with a way to reach whoever
# is calling. Now that this is a public site, it names the site.
USER_AGENT = "biciabordo/1.0 (+https://biciabordo.es)"

# Their hard limit is one request a second, enforced by blocking the IP. The
# gazetteer means we rarely come here at all, but a burst of unusual queries
# must not be what gets the tunnel's egress banned, so requests are spaced out
# process-wide rather than trusted to stay polite on their own.
_NOMINATIM_MIN_INTERVAL_S = 1.1
_nominatim_lock = threading.Lock()
_nominatim_last = 0.0

_lock = threading.Lock()
_cache: dict[tuple[str, int], Timetable] = {}
_cache_stamp: int | None = None       # mtime of the database the cache was built from


# ----------------------------------------------------------------- timetable


def get_timetable(day0: dt.date, num_days: int, db_path: Path = DB_PATH) -> Timetable:
    """Timetables are expensive to build and tiny to keep, so cache per window.

    Keyed on the database's mtime as well as the window, because the nightly
    refresh swaps the file underneath a long-running server: without it the
    workers would go on answering from the timetable they parsed on boot, and a
    line closed since would stay open for as long as the process lived.
    """
    global _cache_stamp
    key = (day0.isoformat(), num_days)
    stamp = db_path.stat().st_mtime_ns if db_path.exists() else 0
    with _lock:
        if stamp != _cache_stamp:
            _cache.clear()
            closures.reset_caches()
            _cache_stamp = stamp
        tt = _cache.get(key)
        if tt is None:
            # Renfe withdraws a bus-replaced train from the Cercanías feed but
            # keeps publishing it in the long-distance one, so the shut track has
            # to be read off one feed and applied to the other.
            window = [day0 + dt.timedelta(days=i) for i in range(num_days)]
            tt = Timetable.load(
                db_path, day0, num_days,
                blocked_hops=closures.bus_bridged_hops(db_path, window))
            # Keep the cache small; windows are usually re-requested in bursts.
            if len(_cache) > 6:
                _cache.pop(next(iter(_cache)))
            _cache[key] = tt
        return tt


def feed_window(db_path: Path = DB_PATH) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    # The Cercanías feed is a rolling ~30-day window and is the binding
    # constraint: past its last day, commuter legs silently vanish.
    row = conn.execute(
        "SELECT MAX(day) FROM service_days WHERE service_id LIKE 'cer:%'"
    ).fetchone()
    conn.close()
    meta["cercanias_last_day"] = row[0] or ""

    # The feed still carries days that have already passed; default to today so
    # nobody plans a trip into a window Renfe has stopped publishing.
    first = meta.get("first_day") or ""
    last = meta.get("last_day") or ""
    today = dt.date.today().isoformat()
    meta["default_day"] = min(max(today, first), last) if first and last else today
    return meta


# --------------------------------------------------------------------- places


class DatabaseStale(RuntimeError):
    """The timetable database predates the running code."""


def check_database(db_path: Path = DB_PATH) -> None:
    """Raise with an actionable message if the database is missing or stale.

    Far better than letting SQLite raise "no such column" from deep inside the
    router and returning an HTML 500 page to a caller expecting JSON.
    """
    if not db_path.exists():
        raise DatabaseStale(
            f"No existe {db_path}. Constrúyela con: python -m app.gtfs.ingest"
        )
    try:
        found = feed_window(db_path).get("schema_version")
    except sqlite3.DatabaseError as exc:
        raise DatabaseStale(f"{db_path} no se puede leer ({exc}).") from exc
    if found != SCHEMA_VERSION:
        raise DatabaseStale(
            f"La base de datos es del esquema {found or '(sin versión)'} y el "
            f"código espera el {SCHEMA_VERSION}. Reconstrúyela con: "
            f"python -m app.gtfs.ingest"
        )


def loaded_feeds(db_path: Path = DB_PATH) -> set[str]:
    """Feed keys present in the database; a login-gated feed may be absent."""
    return {k for k in (feed_window(db_path).get("feeds") or "").split(",") if k}


def _geocache() -> sqlite3.Connection:
    GEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(GEO_CACHE)
    conn.execute("CREATE TABLE IF NOT EXISTS geo (q TEXT PRIMARY KEY, payload TEXT)")
    return conn


def find_places(query: str, limit: int = 6) -> list[dict]:
    """Local place lookup: the gazetteer, falling back to OSM only if absent."""
    hits = gazetteer.search(query, limit=limit) if gazetteer.available() else []
    if hits or gazetteer.available():
        return hits
    return geocode(query, limit=limit)


def geocode(query: str, limit: int = 5) -> list[dict]:
    """Resolve a free-text place name via OSM, with a disk cache and a throttle.

    Only reached when the gazetteer has no answer, which for Spanish places is
    rare: it is the escape hatch for things like a vía verde or a mountain pass,
    not the common path.
    """
    query = query.strip()
    if not query:
        return []

    conn = _geocache()
    row = conn.execute("SELECT payload FROM geo WHERE q = ?", (query.lower(),)).fetchone()
    if row:
        conn.close()
        return json.loads(row[0])

    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "countrycodes": "es,pt,ad,fr",
        "limit": limit,
        "addressdetails": 1,
    })
    req = urllib.request.Request(f"{NOMINATIM}?{params}", headers={"User-Agent": USER_AGENT})
    global _nominatim_last
    try:
        with _nominatim_lock:
            wait = _NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - _nominatim_last)
            if wait > 0:
                time.sleep(wait)
            _nominatim_last = time.monotonic()
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.load(resp)
    except Exception:
        conn.close()
        return []

    out = [
        {
            "name": item.get("display_name", ""),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "kind": "place",
        }
        for item in raw
    ]
    conn.execute("INSERT OR REPLACE INTO geo VALUES (?,?)", (query.lower(), json.dumps(out)))
    conn.commit()
    conn.close()
    return out


def resolve_place(query: str, tt: Timetable) -> dict | None:
    """A station if one matches, else a local place, else OSM as a last resort."""
    stations = tt.find_stops_by_name(query, limit=1)
    if stations:
        s = stations[0]
        return {"name": s.name, "lat": s.lat, "lon": s.lon, "kind": "station", "stop_id": s.stop_id}
    local = gazetteer.search(query, limit=1) if gazetteer.available() else []
    if local:
        return local[0]
    # Resolving happens once, when a search is actually run -- never per
    # keystroke -- so this is a fair moment to ask someone else's server.
    hits = geocode(query, limit=1)
    return hits[0] if hits else None


# ------------------------------------------------------------------ planning


def nearby_stations(
    tt: Timetable,
    lat: float,
    lon: float,
    limit: int = 6,
    use_elevation: bool = True,
) -> tuple[list[dict], bool]:
    """Closest served stations to a point, ordered by riding effort.

    Many places worth cycling to lost their line decades ago (Berga, Cuenca), so
    "no station here" needs the real answer: which station you would actually
    ride to, how far, how much climbing, and in which direction.

    Candidates are gathered by distance but ranked by estimated riding time, so a
    flat 42 km beats a 28 km mountain pass. Returns ``(stations, ranked_by_effort)``;
    the flag is False when elevation was unavailable and the order is distance
    only, so the UI can say so instead of implying more precision than it has.
    """
    # Look much wider than `limit`: the best option by effort is often well
    # outside the closest few, which is the entire point. From Berga, Manresa is
    # only 16th by distance.
    candidates = tt.nearest_any(lat, lon, max(limit * 4, 24))

    out = []
    for idx, km in candidates:
        s = tt.stops[idx]
        out.append({
            "name": s.name,
            "stop_id": s.stop_id,
            "lat": s.lat,
            "lon": s.lon,
            "km": round(km, 1),
            "ride_minutes": ride_seconds(km) // 60,
            "direction": bearing(lat, lon, s.lat, s.lon),
        })

    ranked = False
    if use_elevation and out:
        # A fixed, modest sample count keeps this to a couple of HTTP round
        # trips across two dozen candidates; it is coarse for a profile but
        # ample for telling a valley apart from a col.
        samples = 10
        try:
            # Warm the whole set in one batched call, so the per-station
            # profiles below are pure cache reads instead of 24 requests.
            warm: list[tuple[float, float]] = []
            for station in out:
                warm.extend(
                    elevation.sample_line((lat, lon), (station["lat"], station["lon"]), samples)
                )
            elevation.elevations(warm)

            for station in out:
                prof = elevation.profile(
                    (lat, lon), (station["lat"], station["lon"]), station["km"], samples
                )
                station["ascent_m"] = prof["ascent_m"]
                station["high_m"] = prof["high_m"]
                station["ride_minutes"] = prof["minutes"]
            out.sort(key=lambda st: st["ride_minutes"])
            ranked = True
        except elevation.ElevationUnavailable:
            # Terrain unknown: keep the distance ordering rather than guess.
            for station in out:
                station.pop("ascent_m", None)
                station.pop("high_m", None)

    return out[:limit], ranked


def tolerance(free_only: bool = False, allow_avant: bool = False) -> int:
    """The worst bike class the traveller will accept, as a ``max_bike_class``.

    ``free_only`` is the Cercanías/Rodalies filter: only trains you roll onto
    with no paperwork at all. It tightens where ``allow_avant`` loosens, so if a
    request somehow sets both it gets the stricter of the two rather than a
    silently arbitrary answer.
    """
    if free_only:
        return int(BikeClass.FREE)
    return int(BikeClass.PARTIAL if allow_avant else BikeClass.RESERVED)


def suppression_notes(tt: Timetable, points, radius_km: float = 50.0,
                      limit: int = 2) -> list[str]:
    """Tell the traveller when a closure took away trains the feed still lists.

    Dropping the phantom runs silently would be its own trap: someone who knows
    the 06:48 exists needs to be told it is a coach today, not left wondering why
    the planner has stopped seeing it.
    """
    notes = []
    for (a_idx, b_idx), count in sorted(tt.suppressed.items(), key=lambda kv: -kv[1]):
        a, b = tt.stops[a_idx], tt.stops[b_idx]
        near = min(haversine_km(lat, lon, station.lat, station.lon)
                   for lat, lon in points for station in (a, b))
        if near > radius_km:
            continue
        notes.append(
            f"Entre {a.name} y {b.name} hay bus sustitutorio por obras. Se "
            f"{'ha' if count == 1 else 'han'} descartado {count} "
            f"tren{'' if count == 1 else 'es'} que Renfe sigue publicando por "
            f"ese tramo como si circulara; el bus no admite bicis."
        )
        if len(notes) >= limit:
            break
    return notes


def _strengths(journeys: list[raptor.Journey]) -> list[list[str]]:
    """What each journey is the best at, when more than one is on offer.

    The search returns a Pareto set, so every journey in it wins at something --
    but a list sorted by time reads as "the good one, then progressively worse
    ones" unless each says what it is for. Rubí to Girona offers a 2h30 that
    cycles 5.3 km through Barcelona and a 3h06 with no cycling and no
    reservation; the second is not a worse answer, it is a different trade.

    Only axes the set actually differs on are labelled. Tagging the only journey
    as the first to arrive, or every journey as having the least cycling because
    they all ride the same distance, is noise.

    Callers pass one departure's alternatives at a time, never a whole timetable:
    across the hourly Barcelona-Girona service the odd trains all arrive first in
    turn and the label stops meaning anything.
    """
    if len(journeys) < 2:
        return [[] for _ in journeys]

    def spread(value, best=min):
        seen = {value(j) for j in journeys}
        return (best(seen), len(seen) > 1)

    soonest, varies_arrival = spread(lambda j: j.arrive)
    latest_start, varies_departure = spread(lambda j: j.depart, max)
    least_km, varies_km = spread(lambda j: j.bike_km)
    fewest, varies_trains = spread(lambda j: j.n_trains)
    easiest, varies_class = spread(lambda j: j.worst_class)

    out: list[list[str]] = []
    for journey in journeys:
        tags: list[str] = []
        # Arrival, not duration: on a journey with a night in it the shortest
        # elapsed time belongs to whoever set off latest, so labelling by
        # duration hands "the best one" to a journey that turns up a day later.
        if varies_arrival and journey.arrive == soonest:
            tags.append("earliest")
        # Departure is one of the axes the Pareto filter keeps, so a journey can
        # be on the list purely for being the one still catchable after lunch.
        # Say that, or it reads as the list padding itself out.
        if varies_departure and journey.depart == latest_start:
            tags.append("later_start")
        if varies_km and journey.bike_km == least_km:
            tags.append("least_cycling")
        if varies_trains and journey.n_trains == fewest:
            tags.append("fewest_trains")
        # Only worth saying when it is genuinely paperwork-free, not merely the
        # least demanding of a set that all need a reservation.
        if varies_class and journey.worst_class == easiest == int(BikeClass.FREE):
            tags.append("no_reservation")
        out.append(tags)
    return out


def plan(
    tt: Timetable,
    origin: tuple[float, float],
    destination: tuple[float, float],
    depart_at: dt.datetime,
    whole_day: bool = False,
    departures: int = 1,
    **kwargs,
) -> list[dict]:
    """Journeys from a fixed departure, or the best of the whole day.

    With ``whole_day`` the requested time is ignored and the day is searched for
    the shortest journeys, which is what you want when the departure time is
    negotiable: Barcelona-València is direct at 09:33 but needs two trains and
    34 km of riding if you insist on leaving at 04:00.

    Otherwise ``departures`` is how many successive departures to return, each
    with its own alternatives. One is the soonest thing you can catch; more turns
    the answer into a short timetable, which is what an hourly service deserves.
    """
    # Journeys arrive grouped by departure, because that is the set each label
    # compares within: on an hourly service, tagging every other train "arrives
    # first" is noise, while telling apart the alternatives for the 08:16 is
    # the whole reason the labels exist.
    if whole_day:
        # The requested time becomes a floor rather than a fixed departure: set
        # 00:00 for the genuine whole day, 09:00 to rule out a dawn start.
        journeys = raptor.search_profile(tt, origin, destination, depart_at.date(),
                                         earliest=depart_at.time(), **kwargs)
        # Keep the best handful, but present them as a timetable: a regular
        # service can offer a dozen equally good departures and the useful
        # answer is "which trains can I catch", in time order. Which handful is
        # decided on arrival: ranking a multi-day journey by elapsed time puts
        # the one that leaves at seven in the evening and turns up a day late
        # above the one that gets there tomorrow night.
        groups = [sorted(
            sorted(journeys,
                   key=lambda j: (j.arrive, j.n_trains, j.bike_km, -j.depart))[:8],
            key=lambda j: j.depart)]
    else:
        groups = raptor.search_next(tt, origin, destination, depart_at,
                                    count=departures, **kwargs)

    out = []
    for group in groups:
        for journey, tags in zip(group, _strengths(group)):
            item = serialise(tt, journey)
            item["strengths"] = tags
            out.append(item)
    # A departure's alternatives can straddle the next one, so the list is
    # ordered once at the end rather than round by round.
    out.sort(key=lambda item: (item["depart"], item["arrive"]))
    return out


def _abs_to_dt(tt: Timetable, seconds: int) -> dt.datetime:
    return dt.datetime.combine(tt.day0, dt.time.min) + dt.timedelta(seconds=seconds)


def _wait_kind(tt: Timetable, start: int, end: int) -> str:
    """Distinguish a connection, a long daytime layover, and a night stop.

    Only a wait that actually covers the small hours means "find a bed"; a five
    hour gap over lunch is a layover and should not be labelled as a night.
    """
    if end - start < raptor.OVERNIGHT_WAIT_S:
        return "wait"
    # Does the gap cover any part of 01:00-05:00 on some day?
    for day in range(start // DAY, end // DAY + 1):
        night_start = day * DAY + 1 * 3600
        night_end = day * DAY + 5 * 3600
        if start < night_end and end > night_start:
            return "overnight"
    return "layover"


def serialise(tt: Timetable, journey: raptor.Journey) -> dict:
    """Turn a Journey into the JSON the UI renders, inserting wait/stopover gaps."""
    legs: list[dict] = []
    prev_arr: int | None = None
    prev_place: str | None = None

    for leg in journey.legs:
        # A zero-length access/egress ride means the endpoint *is* the station.
        if leg.kind == "bike" and leg.km <= 0.05 and leg.arr <= leg.dep:
            continue

        if prev_arr is not None and leg.dep > prev_arr:
            wait = leg.dep - prev_arr
            legs.append({
                "kind": _wait_kind(tt, prev_arr, leg.dep),
                "at": prev_place,
                "from": _abs_to_dt(tt, prev_arr).isoformat(),
                "to": _abs_to_dt(tt, leg.dep).isoformat(),
                "seconds": wait,
            })

        a = tt.stops[leg.from_stop] if leg.from_stop is not None else None
        b = tt.stops[leg.to_stop] if leg.to_stop is not None else None
        common = {
            "dep": _abs_to_dt(tt, leg.dep).isoformat(),
            "arr": _abs_to_dt(tt, leg.arr).isoformat(),
            "seconds": leg.arr - leg.dep,
            "from": {"name": a.name, "lat": a.lat, "lon": a.lon, "stop_id": a.stop_id} if a
                    else {"name": "Origen", "lat": None, "lon": None, "stop_id": None},
            "to": {"name": b.name, "lat": b.lat, "lon": b.lon, "stop_id": b.stop_id} if b
                  else {"name": "Destino", "lat": None, "lon": None, "stop_id": None},
        }

        if leg.kind == "bike":
            # Under ~100 m is not a ride, it is a platform or forecourt change
            # between two stations of the same complex -- which is exactly how
            # Renfe and FGC/Euskotren interchanges show up, e.g. Lleida-Pirineus
            # to FGC Lleida. Calling it "0.0 km en bici" reads as a glitch.
            kind = "change" if leg.km < 0.1 else "bike"
            legs.append({**common, "kind": kind, "km": round(leg.km, 1)})
        else:
            cls = BikeClass(leg.bike_class)
            legs.append({
                **common,
                "kind": "ride",
                "service": leg.route_short or "Tren",
                "line": leg.route_long,
                "headsign": leg.headsign,
                "operator": leg.operator,
                "bike_class": int(cls),
                "bike_note": leg.bike_note or CLASS_NOTES[cls],
                "stops_skipped": len(leg.intermediate),
                "path": [
                    {"name": tt.stops[i].name, "lat": tt.stops[i].lat, "lon": tt.stops[i].lon}
                    for i in [leg.from_stop, *leg.intermediate, leg.to_stop]
                ],
            })
        prev_arr = leg.arr
        prev_place = (b.name if b else "Destino")

    reserved = sum(1 for l in legs if l.get("bike_class") == int(BikeClass.RESERVED))
    partial = sum(1 for l in legs if l.get("bike_class") == int(BikeClass.PARTIAL))
    operators = list(dict.fromkeys(l["operator"] for l in legs if l.get("operator")))

    return {
        "operators": operators,
        # Filled in by plan(), which can see the other journeys on offer and so
        # knows what this one is for. Present here so every journey has the same
        # shape whoever built it.
        "strengths": [],
        "depart": _abs_to_dt(tt, journey.depart).isoformat(),
        "arrive": _abs_to_dt(tt, journey.arrive).isoformat(),
        "duration_s": journey.duration_s,
        "n_trains": journey.n_trains,
        "bike_km": journey.bike_km,
        "days": (_abs_to_dt(tt, journey.arrive).date() - _abs_to_dt(tt, journey.depart).date()).days + 1,
        "reservations_needed": reserved,
        "unguaranteed_legs": partial,
        "legs": legs,
    }
