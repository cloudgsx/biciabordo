"""Elevation lookup, so "nearest station" can mean nearest by effort, not by ruler.

Straight-line distance is actively misleading in mountain country. From Berga the
two closest stations on paper are Toses and La Molina at ~28 km — both over the
Collada de Toses at 1.800 m. Manresa is 42 km away and flat the whole way down
the Llobregat, and is obviously the ride you would actually do.

Sampling elevation along the straight line between two points is enough to catch
this: the line crosses the same ridges the road would have to. It is not a real
route profile, but it separates "flat valley" from "mountain pass", which is the
distinction that matters here.

Results are cached in SQLite forever — terrain does not move — so repeat queries
cost nothing and the app degrades to distance-only ordering if the network or the
provider is unavailable.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_PATH = Path("data/elevation.sqlite")
TIMEOUT_S = float(os.environ.get("BICITREN_ELEVATION_TIMEOUT", "8"))
BATCH = 100

# Cache key precision: 4 decimal places is ~11 m, well below the resolution of
# any of these datasets, and makes repeated station lookups exact hits.
QUANT = 4

# A loaded touring bike climbs far slower than it rolls. Time is modelled as
# flat-rolling plus a vertical rate (VAM), which is transparent and easy to
# argue with, rather than an opaque "effort score".
FLAT_KMH = 15.0
CLIMB_VAM_MH = 450.0     # metres of ascent per hour, loaded, sustained
DETOUR = 1.30            # straight-line km -> plausible road km


class ElevationUnavailable(RuntimeError):
    """No provider answered; callers should fall back to distance ordering."""


# ----------------------------------------------------------------- providers


def _open_elevation(points: list[tuple[float, float]]) -> list[float | None]:
    body = json.dumps({
        "locations": [{"latitude": lat, "longitude": lon} for lat, lon in points]
    }).encode()
    req = urllib.request.Request(
        "https://api.open-elevation.com/api/v1/lookup",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "bicitren/0.1"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.load(resp)
    # The API does not guarantee input order, so match results back by position.
    results = data.get("results") or []
    if len(results) != len(points):
        raise ElevationUnavailable("open-elevation returned a short result set")
    return [r.get("elevation") for r in results]


def _open_meteo(points: list[tuple[float, float]]) -> list[float | None]:
    params = urllib.parse.urlencode({
        "latitude": ",".join(f"{lat:.5f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.5f}" for _, lon in points),
    })
    req = urllib.request.Request(
        f"https://api.open-meteo.com/v1/elevation?{params}",
        headers={"User-Agent": "bicitren/0.1"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.load(resp)
    values = data.get("elevation") or []
    if len(values) != len(points):
        raise ElevationUnavailable("open-meteo returned a short result set")
    return list(values)


def _open_topo_data(points: list[tuple[float, float]]) -> list[float | None]:
    locations = "|".join(f"{lat:.5f},{lon:.5f}" for lat, lon in points)
    req = urllib.request.Request(
        f"https://api.opentopodata.org/v1/eudem25m?{urllib.parse.urlencode({'locations': locations})}",
        headers={"User-Agent": "bicitren/0.1"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.load(resp)
    results = data.get("results") or []
    if len(results) != len(points):
        raise ElevationUnavailable("opentopodata returned a short result set")
    return [r.get("elevation") for r in results]


PROVIDERS = {
    "open-elevation": _open_elevation,
    "open-meteo": _open_meteo,
    "opentopodata": _open_topo_data,
}

# Open-Elevation first because it is the one asked for; the others exist so a
# mountain pass does not silently become flat when the public instance is down.
DEFAULT_CHAIN = ("open-elevation", "open-meteo", "opentopodata")


def _chain() -> tuple[str, ...]:
    override = os.environ.get("BICITREN_ELEVATION_PROVIDER")
    if not override:
        return DEFAULT_CHAIN
    names = tuple(n.strip() for n in override.split(",") if n.strip() in PROVIDERS)
    return names or DEFAULT_CHAIN


# --------------------------------------------------------------------- cache


def _cache() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS elev ("
        " lat REAL NOT NULL, lon REAL NOT NULL, m REAL,"
        " PRIMARY KEY (lat, lon)) WITHOUT ROWID"
    )
    return conn


def _key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, QUANT), round(lon, QUANT))


def elevations(points: list[tuple[float, float]]) -> list[float | None]:
    """Metres above sea level for each point, cached indefinitely."""
    if not points:
        return []

    keys = [_key(lat, lon) for lat, lon in points]
    conn = _cache()
    known: dict[tuple[float, float], float | None] = {}
    for key in set(keys):
        row = conn.execute("SELECT m FROM elev WHERE lat=? AND lon=?", key).fetchone()
        if row is not None:
            known[key] = row[0]

    missing = [k for k in dict.fromkeys(keys) if k not in known]
    if missing:
        fetched: dict[tuple[float, float], float | None] = {}
        last_error: Exception | None = None
        for name in _chain():
            provider = PROVIDERS[name]
            try:
                for start in range(0, len(missing), BATCH):
                    chunk = missing[start:start + BATCH]
                    for key, value in zip(chunk, provider(chunk)):
                        fetched[key] = value
                break
            except (urllib.error.URLError, OSError, ValueError,
                    KeyError, ElevationUnavailable) as exc:
                last_error = exc
                fetched.clear()
                continue
        if not fetched:
            conn.close()
            raise ElevationUnavailable(str(last_error) if last_error else "no provider available")
        conn.executemany(
            "INSERT OR REPLACE INTO elev VALUES (?,?,?)",
            [(lat, lon, m) for (lat, lon), m in fetched.items()],
        )
        conn.commit()
        known.update(fetched)

    conn.close()
    return [known.get(k) for k in keys]


# ------------------------------------------------------------------ profiles


def sample_line(
    a: tuple[float, float], b: tuple[float, float], samples: int
) -> list[tuple[float, float]]:
    """Evenly spaced points from a to b. Linear interpolation is fine at these
    distances (tens of km), where great-circle curvature is negligible."""
    samples = max(2, samples)
    return [
        (a[0] + (b[0] - a[0]) * i / (samples - 1),
         a[1] + (b[1] - a[1]) * i / (samples - 1))
        for i in range(samples)
    ]


def profile(
    a: tuple[float, float],
    b: tuple[float, float],
    straight_km: float,
    samples: int | None = None,
) -> dict:
    """Ascent, descent and a time estimate for riding between two points.

    Raises ElevationUnavailable if no provider answers, so the caller can fall
    back rather than quietly presenting a mountain crossing as flat.
    """
    if samples is None:
        # Roughly a sample every 2 km, bounded so one query stays cheap.
        samples = max(6, min(20, int(straight_km / 2) + 2))

    points = sample_line(a, b, samples)
    heights = [h for h in elevations(points) if h is not None]
    if len(heights) < 2:
        raise ElevationUnavailable("no usable elevation samples")

    ascent = sum(max(0.0, y - x) for x, y in zip(heights, heights[1:]))
    descent = sum(max(0.0, x - y) for x, y in zip(heights, heights[1:]))
    road_km = straight_km * DETOUR
    hours = road_km / FLAT_KMH + ascent / CLIMB_VAM_MH

    return {
        "ascent_m": int(round(ascent)),
        "descent_m": int(round(descent)),
        "high_m": int(round(max(heights))),
        "start_m": int(round(heights[0])),
        "end_m": int(round(heights[-1])),
        "road_km": round(road_km, 1),
        "minutes": int(round(hours * 60)),
    }
