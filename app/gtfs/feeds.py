"""Which GTFS feeds we ingest, and how each one behaves.

Renfe is the backbone and the default. The regional operators matter because
they reach places Renfe abandoned or never served: FGC runs Lleida–La Pobla de
Segur and the Llobregat-Anoia lines, Euskotren covers the Basque coast, and the
TRAM d'Alacant is the only rail service to Benidorm. They are ingested alongside
Renfe but tagged, so the planner can be asked for Renfe-only or for everything.

Stop identifiers: Renfe's two feeds deliberately share a station-code namespace
and must NOT be prefixed. Every other operator gets a prefix, because their ids
would otherwise collide with Renfe codes. Interchange between operators is
handled geographically by the transfer builder (stations within 150 m are the
same place), so no identifier reconciliation is needed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeedSpec:
    key: str
    operator: str
    label: str
    url: str | None          # None => must be placed in data/feeds/<key>.zip by hand
    is_renfe: bool
    prefix_stops: bool
    note: str = ""


FEEDS: dict[str, FeedSpec] = {
    "cer": FeedSpec(
        key="cer",
        operator="Renfe Cercanías",
        label="Cercanías, Rodalies y ancho métrico (ex-FEVE)",
        url="https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip",
        is_renfe=True,
        prefix_stops=False,
    ),
    "avld": FeedSpec(
        key="avld",
        operator="Renfe",
        label="Alta velocidad, larga y media distancia",
        url="https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip",
        is_renfe=True,
        prefix_stops=False,
    ),
    "fgc": FeedSpec(
        key="fgc",
        operator="FGC",
        label="Ferrocarrils de la Generalitat de Catalunya",
        url="https://www.fgc.cat/google/google_transit.zip",
        is_renfe=False,
        prefix_stops=True,
        note="FGC admite bici montada y gratis, con restricciones en hora punta "
             "laborable en Barcelona-Vallès y Llobregat-Anoia.",
    ),
    "euskotren": FeedSpec(
        key="euskotren",
        operator="Euskotren",
        label="Euskotren (tren, tranvía y funicular)",
        url="ftp://ftp.geo.euskadi.net/cartografia/Transporte/Moveuskadi/Euskotren/google_transit.zip",
        is_renfe=False,
        prefix_stops=True,
        note="Euskotren admite bici montada, con limitaciones de plazas y hora punta.",
    ),
    # The NAP publishes this one behind a login, so it cannot be fetched
    # automatically. Register free at nap.transportes.gob.es, download
    # https://nap.transportes.gob.es/Files/Detail/966 and drop the zip at
    # data/feeds/tram_alacant.zip; the ingest picks it up if it is there.
    "tram_alacant": FeedSpec(
        key="tram_alacant",
        operator="TRAM d'Alacant",
        label="TRAM d'Alacant (FGV) — Alacant, Benidorm, Dénia",
        url=None,
        is_renfe=False,
        prefix_stops=True,
        note="El TRAM admite bici montada fuera de hora punta; plazas limitadas.",
    ),
}

RENFE_KEYS = frozenset(k for k, f in FEEDS.items() if f.is_renfe)
