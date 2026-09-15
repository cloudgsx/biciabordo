"""Which trains you can roll an assembled bike onto.

Renfe's GTFS feeds do NOT publish the optional ``bikes_allowed`` field on
trips.txt, so the policy has to be inferred from the commercial product name
(``routes.route_short_name``). Rules as of the July 2025 normativa:

  * Cercanías / Rodalies / ancho métrico (ex-FEVE): assembled bike, free, no
    reservation, no counted quota.
  * Media Distancia conventional (MD, REGIONAL, REG.EXP., PROXIMDAD): assembled
    bike, free, but you must add the "Tren+Bici" extra and places are limited
    (3 per train, sometimes fewer).
  * Avant: assembled bike on the units already converted -- treat as allowed but
    flag it as not guaranteed.
  * AVE / Avlo / Alvia / Intercity / Euromed / Trenceltà: bike must be
    disassembled and bagged. Useless for loaded touring, so excluded by default.

``BikeClass`` is ordered by how much hassle it is, which lets the router prefer
the easy options when journeys are otherwise equivalent.
"""

from __future__ import annotations

import enum
import re
import unicodedata


class BikeClass(enum.IntEnum):
    FREE = 0        # roll on, no paperwork
    RESERVED = 1    # assembled, but needs the Tren+Bici extra and a limited slot
    PARTIAL = 2     # assembled in theory, rolling stock dependent
    BAGGED = 3      # must be disassembled into a bag
    BUS = 4         # rail replacement coach: the bike does not travel at all


#: Human-facing explanation per class, shown on each leg in the UI.
CLASS_NOTES = {
    BikeClass.FREE: "Bici montada, gratis y sin reserva.",
    BikeClass.RESERVED: "Bici montada. Requiere el extra Tren+Bici (gratis), plazas limitadas (~3/tren).",
    BikeClass.PARTIAL: "Bici montada solo en las unidades ya adaptadas. No garantizado.",
    BikeClass.BAGGED: "Solo bici desmontada y embalada en funda.",
    BikeClass.BUS: "Servicio sustitutorio por carretera: el autobús no admite bicicletas.",
}

_PRODUCT_RULES: list[tuple[re.Pattern[str], BikeClass]] = [
    (re.compile(r"^AVE\b|^AVLO|^EUROMED|^ALVIA|^INTERCITY|^TRENCELTA"), BikeClass.BAGGED),
    (re.compile(r"^AVANT"), BikeClass.PARTIAL),
    (re.compile(r"^MD\b|^REGIONAL|^REG\.?\s*EXP|^PROXIMDAD|^PROXIMIDAD"), BikeClass.RESERVED),
]


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", value).strip().upper()


# GTFS route_type -> bike class for the regional operators. Unlike Renfe, these
# do not encode a product name we can key on, and none of them publishes
# machine-readable bike rules, so this is a deliberately cautious reading of
# their published conditions: heavy rail carries an assembled bike, while trams,
# metros and rack railways are peak-restricted or space-limited enough that they
# should not be presented as a sure thing.
_REGIONAL_BY_TYPE = {
    0: BikeClass.PARTIAL,   # tram
    1: BikeClass.PARTIAL,   # metro
    2: BikeClass.FREE,      # heavy rail
    5: BikeClass.PARTIAL,   # cable tram
    7: BikeClass.PARTIAL,   # funicular / rack
}   # route_type 3 (bus) is handled earlier, for every feed


def classify(route_short_name: str, feed: str, route_type: int | None = None) -> BikeClass:
    """Map a GTFS route to a bike class.

    Renfe feeds are keyed on the commercial product name; regional operators are
    keyed on ``route_type`` because they publish nothing equivalent.
    """
    # Checked first, and for every feed. Renfe publishes rail-replacement coaches
    # inside the Cercanías feed as ordinary routes with route_type=3 -- 201 of
    # them nationwide at the time of writing, 46 on the R3 alone. They carry the
    # line's own name and number, so anything keyed on the line would wave them
    # through as normal commuter trains. A replacement coach will not take an
    # assembled bike at all, which makes this the single most important
    # exclusion in the whole planner.
    if route_type == 3:
        return BikeClass.BUS

    if feed == "cer":
        # The rest of this feed is commuter or narrow-gauge rail.
        return BikeClass.FREE

    if feed == "avld":
        name = _norm(route_short_name)
        for pattern, cls in _PRODUCT_RULES:
            if pattern.search(name):
                return cls
        # Unknown product in the long-distance feed: assume the strict rule
        # rather than sending someone to a platform where they will be turned
        # away with an assembled bike.
        return BikeClass.BAGGED

    return _REGIONAL_BY_TYPE.get(route_type if route_type is not None else -1, BikeClass.PARTIAL)


def allowed(cls: BikeClass, max_class: BikeClass) -> bool:
    return int(cls) <= int(max_class)
