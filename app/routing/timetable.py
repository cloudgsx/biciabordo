"""Build an in-memory, RAPTOR-ready timetable for a window of days.

Two things make this different from a textbook transit timetable:

1. **Multi-day.** A bike-only crossing of Spain (Barcelona -> Oviedo) cannot be
   done in a day, so trips are expanded into *instances* -- one per service day --
   on a single absolute-seconds timeline. An overnight stop then falls out of the
   search naturally as a very long wait at a station.

2. **Bike transfers.** The interesting connections for cycle touring are not just
   platform-to-platform: riding 12 km from one station to the next line over is
   often the only way through. Transfers therefore include cycling legs between
   distinct stations, costed at a configurable speed with a detour factor.
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DAY = 86400

# Renfe publishes a number of trains in both of its feeds at once: the same
# physical run appears in the Cercanías feed as a commuter line and in the
# long-distance feed as Media Distancia. See _drop_duplicate_listings.
COMMUTER_FEED = "cer"
LONG_DISTANCE_FEED = "avld"

# Cycling model used for transfers and for access/egress from arbitrary points.
BIKE_KMH = 15.0
BIKE_DETOUR = 1.30          # straight-line km -> plausible road km
MAX_TRANSFER_KM = 35.0      # furthest we will suggest riding between stations
SAME_STATION_MIN_S = 300    # 5 min to get a loaded bike between platforms
MIN_BIKE_TRANSFER_S = 600   # even 500 m across town costs you time


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ride_seconds(km: float, kmh: float = BIKE_KMH) -> int:
    return int(round(km * BIKE_DETOUR / kmh * 3600))


COMPASS = ("N", "NE", "E", "SE", "S", "SO", "O", "NO")


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Rough compass direction from the first point to the second."""
    dy = lat2 - lat1
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    angle = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return COMPASS[int((angle + 22.5) % 360 // 45)]


@dataclass
class Stop:
    idx: int
    stop_id: str
    name: str
    lat: float
    lon: float


@dataclass
class TripInstance:
    """One physical run of a train on one calendar day."""
    trip_key: str          # "<trip_id>@<date>"
    route_short: str
    route_long: str
    headsign: str | None
    bike_class: int
    operator: str
    is_renfe: bool
    bike_note: str
    feed: str
    day: dt.date
    route_type: int = 2        # 3 is a replacement coach, not a train
    dep: list[int] = field(default_factory=list)   # absolute seconds, per position
    arr: list[int] = field(default_factory=list)


@dataclass
class Pattern:
    """A distinct stop sequence, with all trip instances serving it."""
    stops: tuple[int, ...]
    trips: list[TripInstance] = field(default_factory=list)


def _covers(
    commuter: Pattern, c_trip: TripInstance,
    other: Pattern, o_trip: TripInstance,
) -> bool:
    """Is the commuter run the same physical train as the long-distance one?

    True when the commuter listing goes everywhere the other one does, calling at
    the same stops at the same minutes. Compared are the times a traveller can
    act on: a departure at every stop they could board, an arrival at every stop
    they could get off. The shared terminus is the exception -- the shorter
    listing records its arrival as equal to its departure because the run ends
    there, while the listing that carries on has a real arrival and a dwell -- so
    it is enough that the commuter train gets there no later.
    """
    n = len(other.stops)
    if len(commuter.stops) < n or commuter.stops[:n] != other.stops:
        return False
    if c_trip.dep[:n - 1] != o_trip.dep[:n - 1]:
        return False
    if c_trip.arr[1:n - 1] != o_trip.arr[1:n - 1]:
        return False
    return c_trip.arr[n - 1] <= o_trip.arr[n - 1]


def _drop_duplicate_listings(patterns: list[Pattern]) -> list[Pattern]:
    """Remove trains Renfe publishes in both of its feeds at once.

    The 08:16 Barcelona-Sants -> Portbou is in the Cercanías feed as ``R11`` and
    in the long-distance feed as ``MD``: same stops, same minutes, and the same
    train number inside both ids (``cer:5127L15802R11`` / ``avld:15802...``). The
    bike rules attached to the two listings disagree -- Rodalies takes an
    assembled bike for free, Media Distancia wants the Tren+Bici extra and has
    about three places -- so whichever listing the search happened to reach first
    decided whether the traveller was told to reserve a place on a train they can
    simply roll onto. On the Barcelona-Girona corridor every morning departure is
    doubled this way.

    The commuter listing wins: a train that appears in the Cercanías feed *is* a
    Cercanías/Rodalies service, whatever the other feed calls it. It is also
    always the one that goes at least as far, so nothing is lost by keeping it.

    Matching on the timetable rather than on the train number in the id, which is
    formatted differently in each feed and does not always agree. Sharing a first
    stop and departure minute is *not* enough on its own: an ALVIA and a C1 leave
    Madrid-Chamartín at the same minute 607 times in a four-day window and are
    entirely different trains. Requiring the whole sequence to match, to the
    minute, cuts that to 332, all of them genuine -- two trains cannot run the
    same track at the same time, so identical times at eight or more consecutive
    stops leaves nothing else it could be.

    Deliberately limited to Renfe's two feeds. Duplicates *within* the
    long-distance feed exist too -- a handful of runs are published as both
    Intercity and MD -- but there the two listings are equally authoritative, and
    picking the laxer one would send someone to a platform with an assembled bike
    on the strength of a guess. Those are left alone: the class filter already
    hides the stricter listing and keeps the usable one.
    """
    commuter: dict[tuple[int, int], list[tuple[Pattern, TripInstance]]] = {}
    for pattern in patterns:
        for trip in pattern.trips:
            if trip.feed == COMMUTER_FEED:
                commuter.setdefault((pattern.stops[0], trip.dep[0]), []).append((pattern, trip))

    kept_patterns: list[Pattern] = []
    for pattern in patterns:
        kept = [
            trip for trip in pattern.trips
            if not (trip.feed == LONG_DISTANCE_FEED and any(
                _covers(cp, ct, pattern, trip)
                for cp, ct in commuter.get((pattern.stops[0], trip.dep[0]), ())
            ))
        ]
        if kept:
            pattern.trips = kept
            kept_patterns.append(pattern)
    return kept_patterns


class Timetable:
    def __init__(self, day0: dt.date, num_days: int) -> None:
        self.day0 = day0
        self.num_days = num_days
        self.stops: list[Stop] = []
        self.stop_by_id: dict[str, Stop] = {}
        self.patterns: list[Pattern] = []
        # stop index -> [(pattern index, position within pattern), ...]
        self.stop_patterns: dict[int, list[tuple[int, int]]] = {}
        # stop index -> [(other stop index, seconds, km), ...]; km == 0 means same station
        self.transfers: dict[int, list[tuple[int, int, float]]] = {}
        # (from stop, to stop) -> how many published runs were dropped over that
        # hop because a replacement coach covers it. Kept so the UI can say why
        # a train it would expect to see is missing.
        self.suppressed: dict[tuple[int, int], int] = {}

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(
        cls,
        db_path: Path,
        day0: dt.date,
        num_days: int = 3,
        max_transfer_km: float = MAX_TRANSFER_KM,
        blocked_hops: dict[str, set[frozenset[str]]] | None = None,
    ) -> "Timetable":
        """Build the window. ``blocked_hops`` maps a day to station pairs whose
        track is shut, as ``closures.bus_bridged_hops`` works them out; a run
        published over one of them is dropped for that day."""
        tt = cls(day0, num_days)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")

        for stop_id, name, lat, lon in conn.execute(
            "SELECT stop_id, name, lat, lon FROM stops ORDER BY stop_id"
        ):
            stop = Stop(len(tt.stops), stop_id, name, lat, lon)
            tt.stops.append(stop)
            tt.stop_by_id[stop_id] = stop

        last_day = day0 + dt.timedelta(days=num_days - 1)
        rows = conn.execute(
            """
            SELECT st.trip_id, sd.day, st.seq, st.stop_id, st.arr, st.dep,
                   t.bike_class, t.headsign, r.short_name, r.long_name,
                   r.operator, r.is_renfe, r.bike_note, t.feed, r.route_type
            FROM service_days sd
            JOIN trips t       ON t.service_id = sd.service_id
            JOIN routes r      ON r.route_id = t.route_id
            JOIN stop_times st ON st.trip_id = t.trip_id
            WHERE sd.day BETWEEN ? AND ?
            ORDER BY st.trip_id, sd.day, st.seq
            """,
            (day0.isoformat(), last_day.isoformat()),
        )

        # Group consecutive rows into trip instances, then bucket instances by
        # their stop sequence so RAPTOR can scan a pattern in one pass.
        by_pattern: dict[tuple[int, ...], Pattern] = {}
        cur_key: tuple[str, str] | None = None
        cur_stops: list[int] = []
        cur_trip: TripInstance | None = None

        def flush() -> None:
            if cur_trip is None or len(cur_stops) < 2:
                return
            # Coaches are exempt: a replacement service running express over the
            # stretch it exists to replace is the one vehicle that legitimately
            # skips it, and counting those as suppressed trains reported 284
            # phantom cancellations on an R3 that had none.
            shut = (blocked_hops.get(cur_trip.day.isoformat())
                    if blocked_hops and cur_trip.route_type != 3 else None)
            if shut:
                for a, b in zip(cur_stops, cur_stops[1:]):
                    if frozenset((tt.stops[a].stop_id, tt.stops[b].stop_id)) not in shut:
                        continue
                    # The run is dropped whole rather than truncated at the cut.
                    # Which fragment still moves is not in the feed -- Renfe's
                    # own notice for the 37072 says it starts at Palencia, two
                    # stations beyond where the coach itinerary ends -- and
                    # guessing would put someone on a platform with a bike and no
                    # train, which is the failure this whole project avoids.
                    tt.suppressed[(a, b)] = tt.suppressed.get((a, b), 0) + 1
                    return
            key = tuple(cur_stops)
            pattern = by_pattern.get(key)
            if pattern is None:
                pattern = Pattern(stops=key)
                by_pattern[key] = pattern
                tt.patterns.append(pattern)
            pattern.trips.append(cur_trip)

        for (trip_id, day_s, _seq, stop_id, arr, dep, bike_class, headsign,
             short, long, operator, is_renfe, bike_note, feed, route_type) in rows:
            key = (trip_id, day_s)
            if key != cur_key:
                flush()
                cur_key = key
                cur_stops = []
                day = dt.date.fromisoformat(day_s)
                cur_trip = TripInstance(
                    trip_key=f"{trip_id}@{day_s}",
                    route_short=(short or "").strip(),
                    route_long=(long or "").strip(),
                    headsign=(headsign or "").strip() or None,
                    bike_class=bike_class,
                    operator=operator,
                    is_renfe=bool(is_renfe),
                    bike_note=bike_note or "",
                    feed=feed,
                    day=day,
                    route_type=route_type if route_type is not None else 2,
                )
            stop = tt.stop_by_id.get(stop_id)
            if stop is None:
                continue
            offset = (dt.date.fromisoformat(day_s) - day0).days * DAY
            cur_stops.append(stop.idx)
            cur_trip.arr.append((arr if arr is not None else dep) + offset)
            cur_trip.dep.append((dep if dep is not None else arr) + offset)
        flush()
        conn.close()

        # Drop patterns whose trips have non-monotonic times (a handful of feed
        # glitches) and sort the rest so RAPTOR can take the first departure it
        # finds after a given time.
        clean: list[Pattern] = []
        for pattern in tt.patterns:
            good = [t for t in pattern.trips if all(
                t.dep[i] >= t.arr[i] and t.arr[i + 1] >= t.dep[i] for i in range(len(t.dep) - 1)
            )]
            if not good:
                continue
            good.sort(key=lambda t: t.dep[0])
            pattern.trips = good
            clean.append(pattern)

        # Done before the stop -> pattern index is built, so a pattern left with
        # no trips at all disappears instead of being indexed as served.
        tt.patterns = _drop_duplicate_listings(clean)

        for p_idx, pattern in enumerate(tt.patterns):
            for pos, stop_idx in enumerate(pattern.stops):
                tt.stop_patterns.setdefault(stop_idx, []).append((p_idx, pos))

        tt._build_transfers(max_transfer_km)
        return tt

    # ------------------------------------------------------------- transfers

    def _build_transfers(self, max_km: float) -> None:
        """Same-station changes plus rideable links between nearby stations.

        Only stations that actually see service in this window are considered, and
        a uniform grid keeps the neighbour search near-linear instead of O(n^2).
        """
        served = sorted(self.stop_patterns.keys())
        cell = max_km / 111.0  # degrees latitude covering max_km
        grid: dict[tuple[int, int], list[int]] = {}
        for idx in served:
            s = self.stops[idx]
            grid.setdefault((int(s.lat / cell), int(s.lon / cell)), []).append(idx)

        for idx in served:
            s = self.stops[idx]
            out: list[tuple[int, int, float]] = []
            gy, gx = int(s.lat / cell), int(s.lon / cell)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for other in grid.get((gy + dy, gx + dx), ()):
                        if other == idx:
                            continue
                        o = self.stops[other]
                        km = haversine_km(s.lat, s.lon, o.lat, o.lon)
                        if km > max_km:
                            continue
                        if km < 0.15:
                            # Effectively the same station (Renfe lists some
                            # complexes under several codes).
                            out.append((other, SAME_STATION_MIN_S, 0.0))
                        else:
                            out.append((other, max(MIN_BIKE_TRANSFER_S, ride_seconds(km)), km))
            self.transfers[idx] = out

    # ------------------------------------------------------------- geocoding

    def nearest_stops(self, lat: float, lon: float, max_km: float, limit: int = 12) -> list[tuple[int, float]]:
        """Stations within cycling reach of a point, nearest first."""
        found = []
        for idx in self.stop_patterns:
            s = self.stops[idx]
            km = haversine_km(lat, lon, s.lat, s.lon)
            if km <= max_km:
                found.append((idx, km))
        found.sort(key=lambda t: t[1])
        return found[:limit]

    def nearest_any(self, lat: float, lon: float, limit: int = 6) -> list[tuple[int, float]]:
        """Nearest served stations at any distance.

        Used to answer "this village has no station, so what are my options?"
        rather than failing the search with nothing useful to say.
        """
        found = [
            (idx, haversine_km(lat, lon, self.stops[idx].lat, self.stops[idx].lon))
            for idx in self.stop_patterns
        ]
        found.sort(key=lambda t: t[1])
        return found[:limit]

    def find_stops_by_name(self, query: str, limit: int = 10) -> list[Stop]:
        import unicodedata

        def norm(v: str) -> str:
            return unicodedata.normalize("NFKD", v.lower()).encode("ascii", "ignore").decode()

        q = norm(query)
        scored = []
        for idx in self.stop_patterns:
            s = self.stops[idx]
            n = norm(s.name)
            if q in n:
                scored.append((0 if n.startswith(q) else 1, len(n), s))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [s for _, _, s in scored[:limit]]

    @property
    def stats(self) -> dict[str, int]:
        return {
            "stops": len(self.stops),
            "served_stops": len(self.stop_patterns),
            "patterns": len(self.patterns),
            "trip_instances": sum(len(p.trips) for p in self.patterns),
            "transfer_edges": sum(len(v) for v in self.transfers.values()),
        }
