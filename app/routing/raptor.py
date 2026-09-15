"""RAPTOR journey search with bike constraints.

Differences from stock RAPTOR:

* Trips whose bike class exceeds the traveller's tolerance are skipped entirely,
  so an AVE that would force you to bag the bike never appears in a result.
* Transfers are bike rides between stations, not just walks, and the total
  distance ridden between trains is capped by a budget the user sets. A
  connecting ride that cannot be finished in daylight is deferred to the next
  morning, which turns into an explicit overnight stopover.
* The timeline spans several days, so a journey may legitimately include a long
  wait. Waits past a threshold are reported as stopovers, not hidden inside a
  connection.

Labels carry a direct pointer to the label they were built from, so a journey is
reconstructed by walking parents. Indexing back through the per-round arrays
instead would splice together labels created in different rounds and invent
impossible legs, such as two consecutive bike transfers.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Literal

from .timetable import DAY, Pattern, Timetable, haversine_km, ride_seconds

INF = 1 << 40
OVERNIGHT_WAIT_S = 4 * 3600      # a wait this long is a stopover, not a connection

# One round buys one train, so this is the most trains a journey may use. It was
# 7, which quietly truncated the long crossings this planner exists for: Portbou
# -> Algeciras came back as 78h because the 54h answer needs eight trains, and
# Portbou -> Ferrol lost five hours the same way. Rounds stop early once no stop
# improves, so raising the ceiling costs nothing measurable (both of those
# queries run in 0.05 s either way) and only changes journeys that were being
# cut off.
DEFAULT_MAX_ROUNDS = 12

# Daylight window for connecting bike rides. Sending someone down an unlit
# N-road at 00:30 with loaded panniers is not a connection anyone would accept,
# so by default a ride that cannot finish in daylight waits for the morning.
RIDE_EARLIEST = 7 * 3600
RIDE_LATEST = 21 * 3600 + 1800

# Labels kept per station. The multi-criteria search keeps every way of reaching
# a station that is best at something, and at a hub like Barcelona-Sants that set
# is large and mostly uninteresting -- dozens of ways to arrive a few minutes
# apart having ridden a few hundred metres more. This bounds the work per station
# to a constant; eight is where the Rubí-Girona alternatives all still appear and
# a whole-day search still runs in well under a second.
MAX_LABELS_PER_STOP = 8

# How much later than the best answer a journey may arrive and still count as an
# alternative. Without this the search has no reason to stop exploring: saving a
# kilometre of cycling is an improvement on one axis however many hours it costs,
# so a label arriving tomorrow stays alive and drags a whole extra day of
# timetable through the scan. Getting there half a day later is not a variant of
# the trip, it is a different trip.
SLACK_MIN_S = 2 * 3600
SLACK_FRACTION = 4          # ...or a quarter of the journey so far, whichever is more


# --------------------------------------------------------------------- legs


@dataclass
class RideLeg:
    from_stop: int
    to_stop: int
    dep: int
    arr: int
    route_short: str
    route_long: str
    headsign: str | None
    bike_class: int
    trip_key: str
    operator: str = ""
    bike_note: str = ""
    intermediate: list[int] = field(default_factory=list)
    kind: Literal["ride"] = "ride"


@dataclass
class BikeLeg:
    from_stop: int | None      # None => starting from the origin point
    to_stop: int | None        # None => finishing at the destination point
    dep: int
    arr: int
    km: float
    kind: Literal["bike"] = "bike"


@dataclass
class Journey:
    legs: list
    depart: int
    arrive: int
    bike_km: float
    n_trains: int
    worst_class: int = 0     # the most demanding bike rule the journey depends on

    @property
    def duration_s(self) -> int:
        return self.arrive - self.depart

    def signature(self) -> tuple:
        return tuple(
            (l.kind, l.from_stop, l.to_stop, l.dep, l.arr,
             getattr(l, "trip_key", ""))
            for l in self.legs
        )


@dataclass
class _Label:
    time: int = INF
    bike_km: float = 0.0            # everything ridden so far, access included
    transfer_km: float = 0.0        # ridden *between* trains, which is what the budget caps
    worst: int = 0                  # most demanding bike class used so far
    via: tuple | None = None        # describes the leg that produced this label
    parent: "_Label | None" = None  # the label this was built from


def _dominates(a: _Label, b: _Label) -> bool:
    """Is ``a`` at least as good as ``b`` everywhere?

    Extending a label can only push every axis the wrong way -- later, further
    ridden, and onto a stricter bike rule -- so a label no better than another on
    any axis can never grow into a journey that beats it, and can be dropped the
    moment it is created.
    """
    return a.time <= b.time and a.bike_km <= b.bike_km and a.worst <= b.worst


def _insert(bag: list[_Label], label: _Label, cap: int) -> bool:
    """Put a label in a Pareto bag, or reject it if the bag already beats it."""
    for kept in bag:
        if _dominates(kept, label):
            return False
    bag[:] = [kept for kept in bag if not _dominates(label, kept)]
    bag.append(label)
    if len(bag) > cap:
        # A busy interchange can be reached in many not-quite-comparable ways and
        # the bag would grow all round. Earliest arrival is the tie-break to keep,
        # being the axis every other one is traded against.
        bag.sort(key=lambda l: (l.time, l.bike_km, l.worst))
        del bag[cap:]
        return any(kept is label for kept in bag)
    return True


def _ride_start(base_time: int, secs: int, km: float, night_riding: bool) -> int | None:
    """When a connecting ride may actually begin, or None if it never fits."""
    if night_riding or km <= 0.0:
        return base_time
    if secs > RIDE_LATEST - RIDE_EARLIEST:
        return None
    day, local = divmod(base_time, DAY)
    if local < RIDE_EARLIEST:
        return day * DAY + RIDE_EARLIEST
    if local + secs <= RIDE_LATEST:
        return base_time
    return (day + 1) * DAY + RIDE_EARLIEST


def _dep_matrix(pattern: Pattern) -> list[list[int]]:
    """Per-position departure times across a pattern's trips, for bisect.

    Cached on the pattern; trips are sorted by first departure and do not
    overtake within a pattern, so each column is sorted too.
    """
    cached = getattr(pattern, "_dep_matrix", None)
    if cached is None:
        cached = [[t.dep[pos] for t in pattern.trips] for pos in range(len(pattern.stops))]
        setattr(pattern, "_dep_matrix", cached)
    return cached


# ------------------------------------------------------------------ search


def search(
    tt: Timetable,
    origin: tuple[float, float],
    destination: tuple[float, float],
    depart_at: dt.datetime,
    max_bike_class: int = 1,
    access_km: float = 20.0,
    transfer_budget_km: float = 60.0,
    night_riding: bool = False,
    renfe_only: bool = True,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    max_labels: int = MAX_LABELS_PER_STOP,
) -> list[Journey]:
    """Find bike-carrying journeys between two arbitrary points.

    ``access_km`` is how far the traveller will ride to reach the first station
    or leave the last; ``transfer_budget_km`` caps the total ridden *between*
    trains. ``renfe_only`` excludes the regional operators (FGC, Euskotren,
    TRAM d'Alacant), which is the default because their bike rules are less
    uniform and not everyone wants a journey that depends on them.

    Each station keeps a Pareto *bag* of labels rather than its earliest arrival,
    so a journey is retained when it is best at anything: soonest, least ridden,
    or least demanding of the bike. Textbook RAPTOR optimises arrival alone, and
    with one label per station that means a route reaching the destination a
    minute later is deleted while it is still half-built, whatever else it had
    going for it. Rubí to Girona was the case that showed it up: the answer was a
    5.3 km ride into Barcelona and out again, while R8 to Granollers and R11 up
    the coast -- two trains, no cycling, no reservation -- was thrown away for
    arriving 70 minutes later, and could never be offered because it was never
    built.
    """
    t0 = int((depart_at - dt.datetime.combine(tt.day0, dt.time.min)).total_seconds())
    horizon = tt.num_days * DAY
    if t0 < 0 or t0 > horizon:
        return []

    n = len(tt.stops)
    best: list[list[_Label]] = [[] for _ in range(n)]
    prev: list[list[_Label]] = [[] for _ in range(n)]

    access = dict(tt.nearest_stops(*origin, max_km=access_km))
    egress = dict(tt.nearest_stops(*destination, max_km=access_km))
    if not access or not egress:
        return []

    # --- access: ride from the origin to every station in reach
    for idx, km in access.items():
        label = _Label(t0 + ride_seconds(km), km, 0.0, 0, ("access", km), None)
        prev[idx] = [label]
        best[idx] = [label]

    # Journeys already reaching the destination, used to prune. A label no better
    # than a finished journey on any axis cannot grow into a better one.
    target: list[_Label] = []
    deadline = INF          # arrive later than this and you are not an alternative

    def hopeless(label: _Label) -> bool:
        if label.time > deadline:
            return True
        return any(_dominates(done, label) for done in target)

    marked = set(access)
    journeys: list[Journey] = []
    last_departure = t0 + horizon

    for _rnd in range(1, max_rounds + 1):
        if not marked:
            break
        cur: list[list[_Label]] = [[] for _ in range(n)]

        # Patterns touched by a stop improved last round, with the earliest
        # position at which we could board.
        queue: dict[int, int] = {}
        for stop_idx in marked:
            for p_idx, pos in tt.stop_patterns.get(stop_idx, ()):
                if p_idx not in queue or pos < queue[p_idx]:
                    queue[p_idx] = pos
        marked = set()

        # --- scan each pattern once, carrying a bag of trips being ridden
        for p_idx, start_pos in queue.items():
            pattern = tt.patterns[p_idx]
            deps = _dep_matrix(pattern)
            trips = pattern.trips
            stops = pattern.stops
            # (trip index, boarding position, label boarded with, worst class so far).
            # An earlier trip is better downstream, so the trip index is this
            # bag's time axis; a later trip is worth carrying only when it was
            # boarded with less riding or a gentler bike rule behind it.
            riding: list[tuple[int, int, _Label, int]] = []

            for pos in range(start_pos, len(stops)):
                stop_idx = stops[pos]

                for trip_i, board_pos, board, worst in riding:
                    arr = trips[trip_i].arr[pos]
                    # Checked on the raw numbers before a label is built: this is
                    # the innermost loop in the whole search and most candidates
                    # die here.
                    if arr > deadline:
                        continue
                    km = board.bike_km
                    if any(d.time <= arr and d.bike_km <= km and d.worst <= worst
                           for d in target):
                        continue
                    if any(k.time <= arr and k.bike_km <= km and k.worst <= worst
                           for k in best[stop_idx]):
                        continue
                    label = _Label(arr, km, board.transfer_km, worst,
                                   ("ride", p_idx, trip_i, board_pos, pos), board)
                    if _insert(best[stop_idx], label, max_labels):
                        _insert(cur[stop_idx], label, max_labels)
                        marked.add(stop_idx)

                # --- board here with whatever the previous round left behind
                waited = prev[stop_idx]
                if not waited:
                    continue
                col = deps[pos]
                ncol = len(col)
                for waiting in waited:
                    cand = bisect_left(col, waiting.time)
                    # Skip trips we may not take: bike class too strict, or an
                    # operator the traveller has not opted into.
                    while cand < ncol and (
                        trips[cand].bike_class > max_bike_class
                        or (renfe_only and not trips[cand].is_renfe)
                    ):
                        cand += 1
                    if cand >= ncol or col[cand] > last_departure:
                        continue
                    worst = max(waiting.worst, trips[cand].bike_class)
                    if any(t <= cand and lbl.bike_km <= waiting.bike_km and w <= worst
                           for t, _p, lbl, w in riding):
                        continue
                    riding = [
                        entry for entry in riding
                        if not (cand <= entry[0] and waiting.bike_km <= entry[2].bike_km
                                and worst <= entry[3])
                    ]
                    riding.append((cand, pos, waiting, worst))
                    if len(riding) > max_labels:
                        riding.sort(key=lambda e: (e[0], e[2].bike_km, e[3]))
                        del riding[max_labels:]

        # --- relax bike transfers, only from stops a train improved this round,
        # so one transfer never chains onto another.
        sources = [(s, list(cur[s])) for s in marked]
        for stop_idx, bag in sources:
            for base in bag:
                for other, secs, km in tt.transfers.get(stop_idx, ()):
                    total_transfer = base.transfer_km + km
                    if total_transfer > transfer_budget_km:
                        continue
                    # A ride can only start later than this, never sooner, so a
                    # candidate already beaten at its earliest possible arrival is
                    # beaten at its real one. Worth knowing before working out
                    # when the ride actually fits into daylight.
                    soonest = base.time + secs
                    if soonest > deadline:
                        continue
                    ridden = base.bike_km + km
                    if any(d.time <= soonest and d.bike_km <= ridden and d.worst <= base.worst
                           for d in target):
                        continue
                    if any(k.time <= soonest and k.bike_km <= ridden and k.worst <= base.worst
                           for k in best[other]):
                        continue
                    start = _ride_start(base.time, secs, km, night_riding)
                    if start is None:
                        continue
                    label = _Label(start + secs, ridden, total_transfer,
                                   base.worst, ("bike", stop_idx, secs, km, start), base)
                    if hopeless(label):
                        continue
                    if _insert(best[other], label, max_labels):
                        _insert(cur[other], label, max_labels)
                        marked.add(other)

        # --- anything standing at a station within reach of the destination is a
        # finished journey. Every label in the bag is offered, not just the
        # earliest, which is where the alternatives come from.
        for idx, km in egress.items():
            for label in cur[idx]:
                arrive = label.time + ride_seconds(km)
                finished = _Label(arrive, label.bike_km + km, label.transfer_km,
                                  label.worst, None, label)
                if not _insert(target, finished, max_labels):
                    continue
                journey = _build(tt, label, idx, km, t0, origin, destination)
                if journey is not None:
                    journeys.append(journey)

        if target:
            soonest = min(done.time for done in target)
            deadline = soonest + max(SLACK_MIN_S, (soonest - t0) // SLACK_FRACTION)

        prev = cur

    # Keep journeys that nothing else beats outright. Bike class is one of the
    # axes: a slower way of getting there without needing a Tren+Bici place is a
    # real option, not a worse version of the fast one.
    journeys.sort(key=lambda j: (j.arrive, j.n_trains, j.bike_km, j.worst_class))
    kept: list[Journey] = []
    seen: set[tuple] = set()
    for j in journeys:
        sig = j.signature()
        if sig in seen:
            continue
        if any(o.arrive <= j.arrive and o.n_trains <= j.n_trains
               and o.bike_km <= j.bike_km and o.worst_class <= j.worst_class
               for o in kept):
            continue
        seen.add(sig)
        kept.append(j)
    return kept


def _merge_bike_runs(
    tt: Timetable,
    legs: list,
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> list:
    """Collapse consecutive bike legs into one direct ride.

    A transfer to a nearby station followed by the ride to the destination is
    two legs describing one continuous piece of cycling. By the triangle
    inequality the direct line is never longer than the dogleg, so merging is
    both simpler to follow and never overstates the effort.
    """
    def point(stop_idx: int | None, fallback: tuple[float, float]) -> tuple[float, float]:
        if stop_idx is None:
            return fallback
        s = tt.stops[stop_idx]
        return (s.lat, s.lon)

    out: list = []
    run: list[BikeLeg] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            out.append(run[0])
        else:
            first, last = run[0], run[-1]
            a = point(first.from_stop, origin)
            b = point(last.to_stop, destination)
            km = haversine_km(*a, *b)
            secs = ride_seconds(km)
            out.append(BikeLeg(first.from_stop, last.to_stop, first.dep, first.dep + secs, km))
        run.clear()

    for leg in legs:
        if leg.kind == "bike":
            run.append(leg)
        else:
            flush()
            out.append(leg)
    flush()
    return out


def pareto(journeys: list[Journey], keep_ties: bool = False) -> list[Journey]:
    """Drop journeys beaten on arrival, patience, simplicity and effort at once.

    The two time axes are *arrival* and *departure*, not duration. Duration is
    the obvious measure and it is wrong the moment a journey sleeps somewhere,
    because it counts the night in the hostel: leaving twelve hours later for an
    arrival an hour later scores as the shorter journey and deletes the one that
    got there first. Barcelona to León was the case that showed it up. The 09:33
    reaches León at 16:04, in time for the 16:58 that is the only train of the
    day carrying a bike into Galicia; the 19:30 reaches it at 19:48, misses that
    train and costs a whole day -- and on duration alone (24h18 against 30h31) it
    dominated the good one out of the results.

    Arriving no later while leaving no earlier already implies a duration no
    longer, so nothing duration would have caught escapes. What survives now is
    the trade duration was hiding: setting off later against getting there
    sooner.

    ``keep_ties`` now only decides what happens to journeys equal on *every*
    axis -- two parallel services, same departure, same arrival, same effort.
    Weak domination keeps one of them; with ties kept, both stand.
    """
    ordered = sorted(journeys,
                     key=lambda j: (j.arrive, -j.depart, j.n_trains, j.bike_km,
                                    j.worst_class))
    kept: list[Journey] = []
    for journey in ordered:
        def beaten_by(other: Journey) -> bool:
            no_worse = (other.arrive <= journey.arrive
                        and other.depart >= journey.depart
                        and other.n_trains <= journey.n_trains
                        and other.bike_km <= journey.bike_km
                        and other.worst_class <= journey.worst_class)
            if not keep_ties:
                return no_worse
            better_somewhere = (other.arrive < journey.arrive
                                or other.depart > journey.depart
                                or other.n_trains < journey.n_trains
                                or other.bike_km < journey.bike_km
                                or other.worst_class < journey.worst_class)
            return no_worse and better_somewhere

        if any(beaten_by(other) for other in kept):
            continue
        kept.append(journey)
    return kept


def search_next(
    tt: Timetable,
    origin: tuple[float, float],
    destination: tuple[float, float],
    depart_at: dt.datetime,
    count: int = 4,
    **kwargs,
) -> list[list[Journey]]:
    """What you can catch at ``depart_at``, plus the next few departures after it.

    One query answers "what leaves now", which hides the rest of the timetable:
    Barcelona to Girona runs hourly, and showing only the 08:16 makes a
    turn-up-and-go service look like a single train you must not miss.

    Returns **one list per departure**, in order, each being that query's full
    Pareto set -- the alternatives, not just the soonest. The cursor then jumps
    past the first train the round used, exactly as the whole-day walk does,
    which forces the next round onto a strictly later train.

    Nothing is filtered *across* rounds, unlike the whole-day search: the 09:16
    being slower than the 08:16 does not make it a worse answer, it makes it the
    next train, which is the entire point of the list. Rounds are kept apart
    rather than flattened because the callers that label journeys ("arrives
    first", "least cycling") are comparing alternatives for one departure; across
    a timetable of hourly trains the same label lands on every other one and says
    nothing.

    With ``count`` of 1 this is a single query and behaves as it always did.
    """
    midnight = dt.datetime.combine(tt.day0, dt.time.min)
    horizon = midnight + dt.timedelta(seconds=tt.num_days * DAY)
    cursor = depart_at

    rounds: list[list[Journey]] = []
    seen: set[tuple] = set()

    for _ in range(max(1, count)):
        if cursor >= horizon:
            break
        batch = search(tt, origin, destination, cursor, **kwargs)
        if not batch:
            break

        # A round starting mid-window can rediscover an alternative an earlier
        # round already offered; it belongs to the departure that found it first.
        fresh = [j for j in batch if j.signature() not in seen]
        seen.update(j.signature() for j in batch)
        if fresh:
            rounds.append(sorted(fresh, key=lambda j: (j.depart, j.arrive)))

        first_trains = [leg.dep for journey in batch for leg in journey.legs
                        if leg.kind == "ride"]
        if not first_trains:
            break
        cursor = max(cursor + dt.timedelta(minutes=1),
                     midnight + dt.timedelta(seconds=min(first_trains) + 60))

    return rounds


def search_profile(
    tt: Timetable,
    origin: tuple[float, float],
    destination: tuple[float, float],
    day: dt.date,
    earliest: dt.time = dt.time(0, 0),
    latest: dt.time = dt.time(23, 59),
    max_queries: int = 40,
    **kwargs,
) -> list[Journey]:
    """Best journeys across a whole day, whatever time the traveller leaves.

    A single RAPTOR query answers "earliest arrival if I leave at t", which is
    the wrong question when the departure time is negotiable: leaving Barcelona
    at 04:00 arrives in València sooner than leaving at 09:00, but takes two
    trains and 34 km of riding, where the 09:00 is direct.

    Rather than sweeping the clock, this walks the timetable: run a query, then
    restart just after the first train that query used, so each round is forced
    onto a strictly later departure. Every distinct useful departure is visited,
    usually in a dozen or so queries instead of hundreds.
    """
    midnight = dt.datetime.combine(tt.day0, dt.time.min)
    start = dt.datetime.combine(day, earliest)
    cursor = start
    limit = dt.datetime.combine(day, latest)

    found: list[Journey] = []
    seen: set[tuple] = set()

    for _ in range(max_queries):
        if cursor > limit:
            break
        batch = search(tt, origin, destination, cursor, **kwargs)
        if not batch:
            break

        for journey in batch:
            # A query late in the day can legitimately return tomorrow's train as
            # the earliest arrival. That is a different day's plan, not an option
            # for the day asked about, so keep only departures inside the window.
            departs = midnight + dt.timedelta(seconds=journey.depart)
            if not (start <= departs <= limit):
                continue
            signature = journey.signature()
            if signature not in seen:
                seen.add(signature)
                found.append(journey)

        # Advance past the earliest first train any of these used; anything at or
        # before it can only reproduce a journey already collected.
        first_trains = [leg.dep for journey in batch for leg in journey.legs
                        if leg.kind == "ride"]
        if not first_trains:
            break
        cursor = max(cursor + dt.timedelta(minutes=1),
                     midnight + dt.timedelta(seconds=min(first_trains) + 60))

    # Ties kept, then ordered by departure: the answer to "what can I catch
    # today" is a timetable of usable departures, not a single winner.
    return sorted(pareto(found, keep_ties=True), key=lambda j: j.depart)


def _build(
    tt: Timetable,
    end: _Label,
    best_stop: int,
    best_km: float,
    t0: int,
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> Journey | None:
    """Walk parent pointers back from a label standing at the last station."""
    best_arrive = end.time + ride_seconds(best_km)

    legs: list = []
    label: _Label | None = end
    stop_idx = best_stop
    guard = 0
    while label is not None and label.via is not None and guard < 64:
        guard += 1
        via = label.via
        if via[0] == "access":
            km = via[1]
            legs.append(BikeLeg(None, stop_idx, t0, t0 + ride_seconds(km), km))
            break
        if via[0] == "bike":
            src, secs, km, start = via[1], via[2], via[3], via[4]
            legs.append(BikeLeg(src, stop_idx, start, start + secs, km))
            stop_idx = src
        else:
            _, p_idx, trip_i, board_pos, alight_pos = via
            pattern = tt.patterns[p_idx]
            trip = pattern.trips[trip_i]
            legs.append(RideLeg(
                from_stop=pattern.stops[board_pos],
                to_stop=pattern.stops[alight_pos],
                dep=trip.dep[board_pos],
                arr=trip.arr[alight_pos],
                route_short=trip.route_short,
                route_long=trip.route_long,
                headsign=trip.headsign,
                bike_class=trip.bike_class,
                trip_key=trip.trip_key,
                operator=trip.operator,
                bike_note=trip.bike_note,
                intermediate=list(pattern.stops[board_pos + 1:alight_pos]),
            ))
            stop_idx = pattern.stops[board_pos]
        label = label.parent

    legs.reverse()
    legs.append(BikeLeg(best_stop, None, best_arrive - ride_seconds(best_km),
                        best_arrive, best_km))

    if not any(l.kind == "ride" for l in legs):
        return None

    legs = _merge_bike_runs(tt, legs, origin, destination)

    # Leave as late as still catches the first train, rather than riding to the
    # station at the queried time and standing on the platform for an hour. This
    # also makes the reported duration mean the journey itself, which is what the
    # whole-day profile search compares.
    if len(legs) > 1 and legs[0].kind == "bike" and legs[0].from_stop is None:
        first_ride = next((l for l in legs if l.kind == "ride"), None)
        if first_ride is not None and legs[0].to_stop == first_ride.from_stop:
            span = legs[0].arr - legs[0].dep
            legs[0] = BikeLeg(None, legs[0].to_stop,
                              first_ride.dep - span, first_ride.dep, legs[0].km)

    bike_km = sum(l.km for l in legs if l.kind == "bike")
    return Journey(
        legs=legs,
        depart=legs[0].dep,
        arrive=legs[-1].arr,
        bike_km=round(bike_km, 1),
        n_trains=sum(1 for l in legs if l.kind == "ride"),
        worst_class=max((l.bike_class for l in legs if l.kind == "ride"), default=0),
    )
