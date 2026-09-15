"""Find lines that are closed and replaced by buses, anywhere in Spain.

There is no national "line closures" API, but the closure is already in the
timetable if you know where to look: Renfe publishes rail-replacement coaches
*inside* the Cercanías GTFS as ordinary routes carrying the line's own number,
distinguished only by ``route_type=3``. So a station that has bus departures on
a given day and no train departures is, that day, cut off from the rail network.

That is the whole detector, and it needs no alerts feed, no scraping and no
per-operator special casing — it works for every operator whose feed encodes
replacement services the same way.

For a cyclist the useful output is not "the line is closed" but "how far up the
line can I still get by train", because a replacement coach will not take an
assembled bike. Each closure therefore reports its *railheads*: the nearest
stations on the same line that still have trains. For the R3 that is La Garriga,
which is exactly where you have to start.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .routing.timetable import haversine_km


# Two cut stations further apart than this are treated as separate closures, not
# one long one. Comfortably larger than the gap between consecutive stations on a
# closed stretch, and far smaller than the distance between núcleos.
CLUSTER_KM = 45.0

# Beyond this, a same-line "railhead" is in another province and is not the
# station anyone would actually ride to.
SAME_LINE_RAILHEAD_KM = 60.0


@dataclass
class Station:
    stop_id: str
    name: str
    lat: float
    lon: float


def _cluster(stations: list[Station], threshold_km: float = CLUSTER_KM) -> list[list[Station]]:
    """Single-linkage clustering, so one closed corridor stays one closure."""
    remaining = list(stations)
    clusters: list[list[Station]] = []
    while remaining:
        group = [remaining.pop()]
        changed = True
        while changed:
            changed = False
            for candidate in list(remaining):
                if any(haversine_km(candidate.lat, candidate.lon, member.lat, member.lon)
                       <= threshold_km for member in group):
                    group.append(candidate)
                    remaining.remove(candidate)
                    changed = True
        clusters.append(group)
    return clusters


@dataclass
class LineClosure:
    line: str
    operator: str
    cut: list[Station] = field(default_factory=list)       # bus only, no trains
    railheads: list[tuple[Station, float]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        head = self.railheads[0][0].name if self.railheads else "—"
        return f"{self.line}: {len(self.cut)} estaciones sin tren, cabecera {head}"


def _served(conn: sqlite3.Connection, day: str) -> dict[tuple[str, str, bool], dict[str, Station]]:
    """(line, operator, is_bus) -> stations with departures that day."""
    rows = conn.execute(
        """
        SELECT TRIM(r.short_name), r.operator, r.route_type = 3,
               s.stop_id, s.name, s.lat, s.lon
        FROM routes r
        JOIN trips t        ON t.route_id = r.route_id
        JOIN service_days sd ON sd.service_id = t.service_id AND sd.day = ?
        JOIN stop_times st  ON st.trip_id = t.trip_id
        JOIN stops s        ON s.stop_id = st.stop_id
        GROUP BY 1, 2, 3, 4
        """,
        (day,),
    )
    out: dict[tuple[str, str, bool], dict[str, Station]] = defaultdict(dict)
    for line, operator, is_bus, stop_id, name, lat, lon in rows:
        out[(line or "?", operator, bool(is_bus))][stop_id] = Station(stop_id, name, lat, lon)
    return out


def find(db_path: Path, day: dt.date) -> list[LineClosure]:
    """Lines running replacement buses on ``day``, with their railheads."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    served = _served(conn, day.isoformat())
    conn.close()

    # Every station with a train that day, used when a replacement service does
    # not carry its line's name (FGC's "BusBV", Renfe's generic "BUS") and so
    # has no same-line railhead to point at.
    any_rail: dict[str, Station] = {}
    for (_line, _op, is_bus), stations in served.items():
        if not is_bus:
            any_rail.update(stations)

    closures: list[LineClosure] = []
    for (line, operator, is_bus), stations in served.items():
        if not is_bus:
            continue
        rail = served.get((line, operator, False), {})
        # Cut means no train AT ALL that day, from any line or operator -- not
        # merely no train on the line the bus is named after. FGC's replacement
        # service is called "BusBV", which has no rail namesake, so comparing
        # within the line marked every station it touches as cut: Sarrià, Gràcia
        # and Muntaner were reported as cut off while the L6, L7, S1 and S2 were
        # running through them all day. Renfe's generic "BUS" had the same flaw.
        cut = [st for sid, st in stations.items() if sid not in any_rail]
        if not cut:
            # Buses run, but every station still sees trains: a partial
            # reinforcement rather than a closure. Not worth alarming anyone.
            continue

        # Renfe reuses line codes across núcleos -- there is a C3 in Santander
        # and another in Valencia, a C2 in Murcia and another in Valencia -- so
        # grouping by name alone welds unrelated closures 400 km apart into one
        # entry and picks a railhead in the wrong province. Split each line's
        # cut stations into geographic clusters and report them separately.
        for cluster in _cluster(cut):
            centre_lat = sum(s.lat for s in cluster) / len(cluster)
            centre_lon = sum(s.lon for s in cluster) / len(cluster)

            def near(pool, lat=centre_lat, lon=centre_lon):
                return sorted(
                    ((st, haversine_km(lat, lon, st.lat, st.lon)) for st in pool.values()),
                    key=lambda pair: pair[1],
                )[:4]

            # Railheads on the same line, nearest to this cluster: the "how far
            # up the line can I still get by train" answer.
            heads = near(rail)
            # If the same line has no train anywhere near, the whole corridor is
            # closed (or the bus does not carry the line's name), so fall back to
            # the nearest station on any line that still has trains.
            if not heads or heads[0][1] > SAME_LINE_RAILHEAD_KM:
                fallback = {sid: st for sid, st in any_rail.items() if sid not in stations}
                heads = near(fallback) or heads

            closures.append(LineClosure(
                line=line, operator=operator,
                cut=sorted(cluster, key=lambda s: s.name),
                railheads=heads,
            ))

    closures.sort(key=lambda c: (-len(c.cut), c.line))
    return closures


def _stops_with_service(conn: sqlite3.Connection, day: str, buses: bool) -> set[str]:
    """Stations with a departure that day, either by coach or by train."""
    rows = conn.execute(
        """
        SELECT DISTINCT st.stop_id
        FROM routes r
        JOIN trips t         ON t.route_id = r.route_id
        JOIN service_days sd ON sd.service_id = t.service_id AND sd.day = ?
        JOIN stop_times st   ON st.trip_id = t.trip_id
        WHERE (r.route_type = 3) = ?
        """,
        (day, 1 if buses else 0),
    )
    return {row[0] for row in rows}


def bus_bridged_hops(db_path: Path, days) -> dict[str, set[frozenset[str]]]:
    """Per day, the station pairs a replacement coach links across dead track.

    Renfe only withdraws the trains it replaces from the *Cercanías* feed. The
    long-distance feed keeps publishing the same run as if the line were open:
    on 2026-08-28 the MD 37072 still claims to leave Santander at 06:48 for
    Valladolid, while Renfe's own booking page says that train starts at Palencia
    and that Santander, Torrelavega, Reinosa and Aguilar travel by road. Nothing
    in that feed hints at it, so the planner offered a train that will not be
    there -- exactly the silent failure this detector exists to prevent, one feed
    over.

    The closure is in the data, though, in the other feed: a coach runs
    Maliaño-Reinosa calling at Las Caldas, Molledo, Pesquera and Lantueno, none
    of which sees a train that day. That is the inference. If a coach links A and
    B and calls at a trainless station on the way, the track between A and B is
    shut, whoever else still publishes a train over it. It needs no alerts feed
    and no scraping, like the rest of this module.

    Deliberately narrow: it takes a coach *itinerary* joining two stations, not
    mere proximity to a closed line. Around Barcelona the R3 runs a few
    kilometres from the R2 for twenty of them, and any geometric rule that
    tolerated that gap would have condemned the whole Girona corridor.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out: dict[str, set[frozenset[str]]] = {}
    for day in days:
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        cut = _stops_with_service(conn, iso, buses=True) - _stops_with_service(conn, iso, buses=False)
        if not cut:
            continue

        itineraries: set[tuple[str, ...]] = set()
        current: list[str] = []
        last_trip = None
        for trip_id, stop_id in conn.execute(
            """
            SELECT st.trip_id, st.stop_id
            FROM routes r
            JOIN trips t         ON t.route_id = r.route_id
            JOIN service_days sd ON sd.service_id = t.service_id AND sd.day = ?
            JOIN stop_times st   ON st.trip_id = t.trip_id
            WHERE r.route_type = 3
            ORDER BY st.trip_id, st.seq
            """,
            (iso,),
        ):
            if trip_id != last_trip:
                if len(current) > 2:
                    itineraries.add(tuple(current))
                current, last_trip = [], trip_id
            current.append(stop_id)
        if len(current) > 2:
            itineraries.add(tuple(current))

        pairs: set[frozenset[str]] = set()
        for stops in itineraries:
            for i in range(len(stops) - 2):
                if stops[i] in cut:
                    continue        # nothing calls here today; no hop to block
                for j in range(i + 2, len(stops)):
                    if stops[j] in cut:
                        continue
                    if any(s in cut for s in stops[i + 1:j]):
                        pairs.add(frozenset((stops[i], stops[j])))
        if pairs:
            out[iso] = pairs
    conn.close()
    return out


_rail_cache: dict[tuple[str, str], list[Station]] = {}


def reset_caches() -> None:
    """Forget everything read from the database, after it has been rebuilt."""
    _rail_cache.clear()


def rail_stations(db_path: Path, day: dt.date) -> list[Station]:
    """Every station with a train that day. Cached: it is fixed per day."""
    key = (str(db_path), day.isoformat())
    cached = _rail_cache.get(key)
    if cached is None:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        served = _served(conn, day.isoformat())
        conn.close()
        index: dict[str, Station] = {}
        for (_line, _op, is_bus), stations in served.items():
            if not is_bus:
                index.update(stations)
        cached = list(index.values())
        _rail_cache[key] = cached
    return cached


def affecting(
    closures: list[LineClosure],
    lat: float,
    lon: float,
    rail: list[Station],
    radius_km: float = 25.0,
    tolerance_km: float = 1.0,
) -> list[tuple[LineClosure, Station, float]]:
    """Closures that plausibly affect someone starting or finishing at a point.

    Proximity alone is far too loose in a city: a closed FGC section 2 km from
    Barcelona-Sants would warn on every journey out of Barcelona, even though
    Sants itself has hundreds of trains. A closure is only worth mentioning if it
    took away something *nearer* than the closest station still running trains --
    that is, if it plausibly removed the option the traveller would have used.
    """
    nearest_rail = min(
        (haversine_km(lat, lon, st.lat, st.lon) for st in rail), default=float("inf")
    )

    hits = []
    for closure in closures:
        nearest = min(
            ((st, haversine_km(lat, lon, st.lat, st.lon)) for st in closure.cut),
            key=lambda pair: pair[1],
            default=None,
        )
        if not nearest or nearest[1] > radius_km:
            continue
        if nearest[1] > nearest_rail + tolerance_km:
            continue        # a working station is at least as close; not relevant
        hits.append((closure, nearest[0], nearest[1]))
    hits.sort(key=lambda h: h[2])
    return hits


def as_dict(closure: LineClosure) -> dict:
    return {
        "line": closure.line,
        "operator": closure.operator,
        "cut": [{"name": s.name, "lat": s.lat, "lon": s.lon} for s in closure.cut],
        "railheads": [
            {"name": s.name, "lat": s.lat, "lon": s.lon, "km": round(km, 1)}
            for s, km in closure.railheads
        ],
    }
