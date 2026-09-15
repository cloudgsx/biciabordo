"""Command line planner: bicitren plan "Barcelona" "Oviedo" --date 2026-08-12."""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

from .gtfs.bikepolicy import BikeClass
from . import closures
from .routing.raptor import DEFAULT_MAX_ROUNDS
from .service import (DB_PATH, feed_window, get_timetable, nearby_stations, plan,
                      resolve_place, suppression_notes, tolerance)

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CYAN, YELLOW, GREEN = "\033[36m", "\033[33m", "\033[32m"

#: Why a journey is on the list, keyed as the planner reports it.
STRENGTHS = {
    "earliest": "llega antes",
    "later_start": "sales más tarde",
    "least_cycling": "menos bici",
    "fewest_trains": "menos trenes",
    "no_reservation": "sin reserva",
}


def hm(iso: str) -> str:
    return dt.datetime.fromisoformat(iso).strftime("%a %d %b %H:%M")


def dur(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m}min"


def render(journey: dict) -> None:
    trains = f"{journey['n_trains']} tren" + ("" if journey["n_trains"] == 1 else "es")
    head = (f"{BOLD}{trains} · {dur(journey['duration_s'])} · "
            f"{journey['bike_km']} km en bici · sale {hm(journey['depart'])}, "
            f"llega {hm(journey['arrive'])}{RESET}")
    if journey["days"] > 1:
        head += f" {YELLOW}({journey['days']} días){RESET}"
    # Every journey on the list wins at something, or it would not have survived
    # the Pareto filter. Saying what stops a slower one reading as a worse one.
    tags = " · ".join(STRENGTHS[s] for s in journey.get("strengths", []) if s in STRENGTHS)
    print("\n" + head + (f"  {GREEN}{tags}{RESET}" if tags else ""))
    if journey["reservations_needed"]:
        print(f"  {DIM}{journey['reservations_needed']} trayecto(s) requieren el extra Tren+Bici (plazas limitadas){RESET}")

    for leg in journey["legs"]:
        if leg["kind"] == "wait":
            print(f"  {DIM}      espera {dur(leg['seconds'])} en {leg['at']}{RESET}")
        elif leg["kind"] == "overnight":
            print(f"  {YELLOW}      🛏  noche en {leg['at']} ({dur(leg['seconds'])}){RESET}")
        elif leg["kind"] == "layover":
            print(f"  {YELLOW}      ⏸  parada de {dur(leg['seconds'])} en {leg['at']}{RESET}")
        elif leg["kind"] == "change":
            print(f"  {GREEN}{hm(leg['dep'])}{RESET} ↪  cambio a {leg['to']['name']}"
                  f"  {DIM}({dur(leg['seconds'])}){RESET}")
        elif leg["kind"] == "bike":
            print(f"  {GREEN}{hm(leg['dep'])}{RESET} 🚲 {leg['km']:>5.1f} km  "
                  f"{leg['from']['name']} → {leg['to']['name']}  {DIM}({dur(leg['seconds'])}){RESET}")
        else:
            flag = "" if leg["bike_class"] == BikeClass.FREE else f" {YELLOW}[reserva]{RESET}"
            op = "" if leg.get("operator", "").startswith("Renfe") else f" {leg['operator']}"
            print(f"  {CYAN}{hm(leg['dep'])}{RESET} 🚆 {leg['service']:<9}{op} "
                  f"{leg['from']['name']} → {leg['to']['name']}  "
                  f"{DIM}llega {hm(leg['arr'])} ({dur(leg['seconds'])}){RESET}{flag}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bicitren")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="plan a bike-carrying journey")
    p.add_argument("origin")
    p.add_argument("destination")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: first day of the feed window)")
    p.add_argument("--time", default="07:00")
    p.add_argument("--days", type=int, default=4, help="search horizon in days")
    p.add_argument("--access-km", type=float, default=20.0)
    p.add_argument("--transfer-km", type=float, default=60.0)
    p.add_argument("--allow-avant", action="store_true",
                   help="also use Avant (bike space only on converted units)")
    p.add_argument("--free-only", action="store_true",
                   help="only Cercanías, Rodalies and ancho métrico: the bike "
                        "rolls on free, with no Tren+Bici reservation")
    p.add_argument("--night-riding", action="store_true",
                   help="allow connecting bike rides after dark")
    p.add_argument("--other-operators", action="store_true",
                   help="also use FGC, Euskotren and the other regional operators")
    p.add_argument("--max-trains", type=int, default=DEFAULT_MAX_ROUNDS,
                   help=f"most trains a journey may use (default {DEFAULT_MAX_ROUNDS})")
    p.add_argument("--departures", type=int, default=1, metavar="N",
                   help="show the next N departures, not just the first one you "
                        "can catch (ignored with --best)")
    p.add_argument("--best", action="store_true",
                   help="find the best combinations of the day; --time becomes "
                        "the earliest acceptable departure rather than a fixed one")

    sub.add_parser("info", help="show the loaded feed window")

    cl = sub.add_parser("closures", help="lines closed and replaced by buses")
    cl.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    args = ap.parse_args(argv)

    if args.cmd == "info":
        meta = feed_window()
        print(f"database built : {meta.get('built_at')}")
        print(f"service window : {meta.get('first_day')} .. {meta.get('last_day')}")
        print(f"cercanías until: {meta.get('cercanias_last_day')}  "
              f"{DIM}(commuter legs disappear past this date){RESET}")
        return 0

    meta = feed_window()

    if args.cmd == "closures":
        day = dt.date.fromisoformat(args.date or meta["default_day"])
        found = closures.find(DB_PATH, day)
        if not found:
            print(f"sin cortes detectados el {day}.")
            return 0
        print(f"{BOLD}Líneas con servicio sustitutorio por carretera el {day}{RESET}")
        print(f"{DIM}El bus no admite bicis: estas estaciones están, hoy, fuera "
              f"de la red para quien viaja con bicicleta.{RESET}\n")
        for c in found:
            print(f"  {YELLOW}{c.line:<7}{RESET}{DIM}{c.operator}{RESET}")
            print(f"    sin tren: {', '.join(s.name for s in c.cut)}")
            if c.railheads:
                heads = ", ".join(f"{s.name} ({km:.0f} km)" for s, km in c.railheads[:3])
                print(f"    {GREEN}llega el tren hasta:{RESET} {heads}")
            print()
        return 0

    day = dt.date.fromisoformat(args.date or meta["default_day"])
    depart = dt.datetime.combine(day, dt.time.fromisoformat(args.time))

    tt = get_timetable(day, args.days, DB_PATH)
    origin = resolve_place(args.origin, tt)
    dest = resolve_place(args.destination, tt)
    if not origin:
        print(f"no encuentro '{args.origin}'", file=sys.stderr)
        return 1
    if not dest:
        print(f"no encuentro '{args.destination}'", file=sys.stderr)
        return 1

    when = (f"mejores combinaciones del {depart:%a %d %b}, no antes de {depart:%H:%M}"
            if args.best else f"saliendo {depart:%a %d %b %H:%M}")
    print(f"{BOLD}{origin['name']}{RESET} → {BOLD}{dest['name']}{RESET}   {DIM}{when}{RESET}")

    if args.date and args.date > meta.get("cercanias_last_day", ""):
        print(f"{YELLOW}aviso: fuera de la ventana de Cercanías ({meta['cercanias_last_day']}); "
              f"solo media/larga distancia{RESET}")

    # Somewhere with no line within reach needs the real answer, not a shrug.
    for point, label in ((origin, args.origin), (dest, args.destination)):
        if tt.nearest_stops(point["lat"], point["lon"], max_km=args.access_km):
            continue
        stations, ranked = nearby_stations(tt, point["lat"], point["lon"], limit=6)
        print(f"\n{YELLOW}{label} no tiene estación con servicio a menos de "
              f"{args.access_km:.0f} km.{RESET}")
        print(f"{DIM}Estaciones más cercanas"
              f"{' (ordenadas por esfuerzo real)' if ranked else ''}:{RESET}")
        for st in stations:
            climb = f"↗{st['ascent_m']:>5} m" if st.get("ascent_m") is not None else " " * 8
            print(f"  {st['name']:<34.34s} {st['km']:>5.1f} km {st['direction']:<2} "
                  f"{climb}  {dur(st['ride_minutes'] * 60)} en bici")
        if stations:
            print(f"{DIM}Repite con --access-km {math.ceil(min(s['km'] for s in stations)) + 1} "
                  f"o planifica directamente hasta una de ellas.{RESET}")
        return 1

    cut_warnings = closures.find(DB_PATH, day)
    cut_rail = closures.rail_stations(DB_PATH, day)
    for point, label in ((origin, args.origin), (dest, args.destination)):
        for closure, station, km in closures.affecting(
                cut_warnings, point["lat"], point["lon"], cut_rail):
            head = closure.railheads[0][0].name if closure.railheads else "?"
            print(f"{YELLOW}aviso: {closure.line} está cortada cerca de {label} "
                  f"({station.name}, a {km:.0f} km). Bus sustitutorio, sin bicis. "
                  f"El tren llega hasta {head}.{RESET}")
            break

    for note in suppression_notes(tt, ((origin["lat"], origin["lon"]),
                                      (dest["lat"], dest["lon"]))):
        print(f"{YELLOW}aviso: {note}{RESET}")

    journeys = plan(
        tt,
        (origin["lat"], origin["lon"]),
        (dest["lat"], dest["lon"]),
        depart,
        max_bike_class=tolerance(args.free_only, args.allow_avant),
        access_km=args.access_km,
        transfer_budget_km=args.transfer_km,
        night_riding=args.night_riding,
        renfe_only=not args.other_operators,
        max_rounds=max(1, args.max_trains),
        whole_day=args.best,
        departures=max(1, args.departures),
    )
    if not journeys:
        print("\nsin combinaciones con bici montada en este horizonte.")
        return 1
    for journey in journeys:
        render(journey)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
