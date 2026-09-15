"""Flask app: planning API plus the map UI."""

from __future__ import annotations

import datetime as dt
import hmac
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from . import closures as closures_mod
from . import stats
from .gtfs.bikepolicy import CLASS_NOTES
from .gtfs.feeds import FEEDS
from .routing.raptor import DEFAULT_MAX_ROUNDS
from .routing.timetable import MAX_TRANSFER_KM
from .service import (DB_PATH, DatabaseStale, check_database, feed_window,
                      find_places, get_timetable, loaded_feeds, nearby_stations,
                      plan, suppression_notes, tolerance)

MAX_DAYS = 5
MAX_TRAINS = 12
# Successive departures the UI may ask for. Each one costs a full RAPTOR query,
# and past a handful the list stops being a shortlist and becomes a timetable
# dump nobody scrolls.
MAX_DEPARTURES = 8


def create_app() -> Flask:
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")

    @app.before_request
    def _guard_database():
        """Fail JSON API calls loudly and legibly, never with an HTML 500 page.

        The database lives in a Docker volume that outlives the image, so a
        container running new code against an old database used to blow up with
        "no such column" and hand the browser an HTML error page, which the
        frontend then failed to parse as JSON.
        """
        if not request.path.startswith("/api/"):
            return None
        try:
            check_database(DB_PATH)
        except DatabaseStale as exc:
            return jsonify({"error": str(exc)}), 503
        return None

    @app.errorhandler(Exception)
    def _json_errors(exc):
        # Ordinary HTTP errors keep their own status. Re-raising them here is
        # what an error handler must not do: Flask catches the re-raise and
        # turns it into a 500, so every mistyped URL answered "internal server
        # error" instead of "not found".
        if isinstance(exc, HTTPException):
            if request.path.startswith("/api/"):
                return jsonify({"error": exc.description}), exc.code
            return exc
        if not request.path.startswith("/api/"):
            raise exc
        app.logger.exception("error serving %s", request.path)
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    def asset_version() -> str:
        """A stamp that changes when the JS or CSS does, to bust browser caches.

        Without it a deploy is invisible: the browser keeps the script it
        already has, and that old script never asks for the new features -- it
        stops sending `departures`, so the API falls back to one and the page
        looks unchanged while the server is new. Silent, and maddening to debug.
        """
        static = Path(app.static_folder)
        stamps = [f.stat().st_mtime for f in static.iterdir() if f.is_file()]
        return str(int(max(stamps))) if stamps else "0"

    @app.get("/")
    def index():
        others = ", ".join(
            spec.operator for key, spec in FEEDS.items()
            if not spec.is_renfe and key in loaded_feeds()
        )
        return render_template("index.html", meta=feed_window(),
                               operators=others or "ninguno cargado",
                               assets=asset_version())

    def _from_ui() -> bool:
        """Did this request come from our own page, or straight at the endpoint?

        A browser sends Origin on a same-origin POST and Referer on navigation;
        a bare curl or a script sends neither unless it bothers to fake them.
        That makes this a weak signal on its own -- trivial to forge -- but a
        useful one in aggregate, and it costs no personal data at all, which
        matters because we deliberately store no IP, session or user agent.
        """
        here = (request.host or "").split(":")[0].lower()
        for header in ("Origin", "Referer"):
            value = request.headers.get(header)
            if not value:
                continue
            host = urlsplit(value).netloc.split(":")[0].lower()
            if host and host == here:
                return True
        return False

    def _admin_allowed() -> bool:
        """Fail closed: with no token configured the dashboard does not exist.

        This is a second lock, not the main one -- put Cloudflare Access in front
        of /admin for real authentication. A token in a URL is fine for a private
        page, but it is a shared secret, not an identity.
        """
        secret = os.environ.get("BICITREN_ADMIN_TOKEN", "")
        if not secret:
            return False
        given = (request.args.get("k") or request.headers.get("X-Admin-Token")
                 or request.cookies.get("bt_admin") or "")
        return hmac.compare_digest(given, secret)

    @app.get("/admin")
    def admin():
        if not _admin_allowed():
            return ("No encontrado", 404)
        days = max(1, min(365, int(request.args.get("days") or 90)))
        page = render_template("admin.html", data=stats.dashboard(days), days=days)
        response = app.make_response(page)
        if request.args.get("k"):
            # Remember it for this browser so the secret stops riding in the URL,
            # where it would end up in history and in any screenshot.
            response.set_cookie("bt_admin", request.args["k"], httponly=True,
                                samesite="Lax", secure=request.is_secure,
                                max_age=60 * 60 * 24 * 30)
        return response

    @app.get("/api/meta")
    def meta():
        info = feed_window()
        info["bike_classes"] = {int(k): v for k, v in CLASS_NOTES.items()}
        return jsonify(info)

    @app.get("/api/closures")
    def closures():
        window = feed_window()
        try:
            day = dt.date.fromisoformat(request.args.get("date") or window["default_day"])
        except ValueError:
            return jsonify({"error": "fecha no válida"}), 400
        found = closures_mod.find(DB_PATH, day)
        return jsonify({"date": day.isoformat(),
                        "closures": [closures_mod.as_dict(c) for c in found]})

    @app.get("/api/places")
    def places():
        """Suggestions for the type-ahead: stations, then the local gazetteer.

        Deliberately offline. This fires on every keystroke, and routing that to
        OSM's public geocoder produced thousands of lookups -- 40% of them for
        half-typed words that matched nothing -- against a service that allows
        one request a second. Free-text places that the gazetteer cannot resolve
        are left to /api/plan, which runs once per search.
        """
        query = (request.args.get("q") or "").strip()
        if len(query) < 2:
            return jsonify([])
        window = feed_window()
        tt = get_timetable(dt.date.fromisoformat(window["default_day"]), 2, DB_PATH)
        out = [
            {"name": s.name, "lat": s.lat, "lon": s.lon, "kind": "station", "stop_id": s.stop_id}
            for s in tt.find_stops_by_name(query, limit=6)
        ]
        if len(out) < 6:
            known = {o["name"].lower() for o in out}
            out += [p for p in find_places(query, limit=6 - len(out))
                    if p["name"].lower() not in known]
        return jsonify(out)

    @app.post("/api/plan")
    def do_plan():
        body = request.get_json(force=True, silent=True) or {}
        try:
            origin = (float(body["origin"]["lat"]), float(body["origin"]["lon"]))
            dest = (float(body["destination"]["lat"]), float(body["destination"]["lon"]))
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "origen y destino son obligatorios"}), 400

        window = feed_window()
        try:
            day = dt.date.fromisoformat(body.get("date") or window["default_day"])
            clock = dt.time.fromisoformat(body.get("time") or "07:00")
        except ValueError:
            return jsonify({"error": "fecha u hora no válida"}), 400

        first = dt.date.fromisoformat(window["first_day"])
        last = dt.date.fromisoformat(window["last_day"])
        if not (first <= day <= last):
            return jsonify({
                "error": f"la fecha debe estar entre {first} y {last}",
            }), 400

        days = max(1, min(MAX_DAYS, int(body.get("days") or 4)))
        access_km = float(body.get("access_km") or 20.0)
        tt = get_timetable(day, days, DB_PATH)

        # Before searching, check both ends actually have a station in reach.
        # Plenty of good cycling country lost its line decades ago, and "no
        # combinations" is a useless answer when we know exactly which station
        # the rider would head for.
        uncovered = []
        for role, point, label in (("origin", origin, body.get("origin", {}).get("name")),
                                   ("destination", dest, body.get("destination", {}).get("name"))):
            if not tt.nearest_stops(*point, max_km=access_km):
                options, by_effort = nearby_stations(tt, *point)
                # Widen to whichever option is actually closest in distance, not
                # the effort-ranked first one, or the slider may still not reach.
                nearest_km = min((o["km"] for o in options), default=None)
                uncovered.append({
                    "role": role,
                    "place": label or ("Origen" if role == "origin" else "Destino"),
                    "stations": options,
                    "ranked_by_effort": by_effort,
                    "suggested_access_km": math.ceil(nearest_km) + 1 if nearest_km else None,
                })
        if uncovered:
            stats.record(body, [], uncovered, from_ui=_from_ui())
            return jsonify({
                "journeys": [],
                "warnings": [],
                "uncovered": uncovered,
                "access_km": access_km,
            })

        max_trains = max(1, min(MAX_TRAINS,
                                int(body.get("max_trains") or DEFAULT_MAX_ROUNDS)))
        journeys = plan(
            tt,
            origin,
            dest,
            dt.datetime.combine(day, clock),
            max_bike_class=tolerance(bool(body.get("free_only")),
                                     bool(body.get("allow_avant"))),
            access_km=access_km,
            transfer_budget_km=float(body.get("transfer_km") or 60.0),
            night_riding=bool(body.get("night_riding")),
            renfe_only=not bool(body.get("other_operators")),
            max_rounds=max_trains,
            whole_day=bool(body.get("whole_day")),
            departures=max(1, min(MAX_DEPARTURES, int(body.get("departures") or 1))),
        )

        warnings = []
        # A replacement coach will not take an assembled bike, so a closure near
        # either end is the likely reason a journey looks worse than expected --
        # or does not exist. Say so before the user has to guess.
        cut_notes = []
        found = closures_mod.find(DB_PATH, day)
        rail = closures_mod.rail_stations(DB_PATH, day)
        for role, point in (("origen", origin), ("destino", dest)):
            for closure, station, km in closures_mod.affecting(found, *point, rail):
                head = closure.railheads[0] if closure.railheads else None
                cut_notes.append(
                    f"La {closure.line} está cortada junto al {role} "
                    f"({station.name}, a {km:.0f} km): bus sustitutorio, que no "
                    f"admite bicis."
                    + (f" El tren llega hasta {head['name'] if isinstance(head, dict) else head[0].name}."
                       if head else "")
                )
                break
        warnings.extend(cut_notes)
        # A closure read off the Cercanías feed also shuts the track for the
        # long-distance trains still published over it; say which ones went.
        warnings.extend(suppression_notes(tt, (origin, dest)))

        # Every journey spending its whole allowance of trains is the shape of an
        # answer the slider cut short, and the cut is invisible in the results.
        # Barcelona to Vilagarcía capped at five hides the six-train answer that
        # arrives a full day earlier, because the sixth train is what catches the
        # one service a day that carries a bike into Galicia.
        if journeys and max_trains < MAX_TRAINS and all(
                j["n_trains"] >= max_trains for j in journeys):
            warnings.append(
                f"Todas las combinaciones gastan los {max_trains} trenes que has "
                f"puesto como máximo, así que el límite puede estar escondiendo "
                f"algo mejor: un tren más a veces ahorra un día entero."
            )

        cer_last = window.get("cercanias_last_day") or ""
        if cer_last and day.isoformat() > cer_last:
            warnings.append(
                f"Renfe solo publica Cercanías hasta el {cer_last}. Para esta fecha "
                f"solo hay media distancia, así que faltarán enlaces de cercanías."
            )
        if not journeys:
            if body.get("free_only"):
                # The most restrictive box is the one to try first, and it is the
                # likeliest culprit outside the commuter networks: most of Spain
                # is only reachable by Media Distancia.
                warnings.append(
                    "Sin combinaciones solo con Cercanías y Rodalies. Fuera de los "
                    "núcleos de cercanías casi todo depende de la media distancia: "
                    "desmarca la casilla para incluirla."
                )
            else:
                warnings.append(
                    "Sin combinaciones con la bici montada. Prueba a ampliar el radio en "
                    "bici, el presupuesto de kilómetros o el horizonte de días."
                )
        stats.record(body, journeys, [],
                     closure_line=cut_notes[0].split()[1] if cut_notes else None,
                     from_ui=_from_ui())
        return jsonify({"journeys": journeys, "warnings": warnings,
                        "max_transfer_km": MAX_TRANSFER_KM})

    return app


app = create_app()
