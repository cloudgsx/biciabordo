"""Rail-replacement buses, and the closures they reveal."""

from __future__ import annotations

import datetime as dt
import sqlite3
import unittest
from pathlib import Path

from app import closures
from app.gtfs.bikepolicy import BikeClass, classify
from app.routing import raptor
from app.routing.timetable import Timetable
from app.service import feed_window

DB = Path("data/bicitren.sqlite")


class TestBusPolicy(unittest.TestCase):
    def test_replacement_bus_is_never_bikeable(self):
        # Renfe files replacement coaches inside the Cercanías feed under the
        # line's own name, so only route_type distinguishes them. Classifying
        # by feed alone once labelled all 201 of them "bici montada, gratis".
        for feed in ("cer", "avld", "fgc", "euskotren"):
            self.assertEqual(classify("R3", feed, route_type=3), BikeClass.BUS, feed)

    def test_bus_outranks_every_tolerance_the_ui_offers(self):
        # The UI's most permissive setting is PARTIAL; a bus must stay excluded.
        self.assertGreater(int(BikeClass.BUS), int(BikeClass.PARTIAL))
        self.assertGreater(int(BikeClass.BUS), int(BikeClass.BAGGED))

    def test_rail_on_the_same_line_is_unaffected(self):
        self.assertEqual(classify("R3", "cer", route_type=2), BikeClass.FREE)


@unittest.skipUnless(DB.exists(), "timetable database not built")
class TestDatabase(unittest.TestCase):
    def test_bus_routes_are_flagged_in_the_database(self):
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        wrong = conn.execute(
            "SELECT COUNT(*) FROM routes WHERE route_type = 3 AND bike_class != ?",
            (int(BikeClass.BUS),),
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM routes WHERE route_type = 3"
        ).fetchone()[0]
        conn.close()
        self.assertGreater(total, 0, "expected replacement buses in the feeds")
        self.assertEqual(wrong, 0, "a bus route escaped the BUS class")


@unittest.skipUnless(DB.exists(), "timetable database not built")
class TestClosureDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.day = dt.date.fromisoformat(feed_window(DB)["default_day"])
        cls.found = closures.find(DB, cls.day)

    def test_detects_closures(self):
        self.assertTrue(self.found, "no closures detected at all")

    def test_every_closure_names_somewhere_to_reach_by_train(self):
        for closure in self.found:
            self.assertTrue(closure.cut)
            self.assertTrue(closure.railheads,
                            f"{closure.line} has no railhead to point at")

    def test_railheads_are_plausibly_close(self):
        # A railhead in another province is not one anyone would ride to; this
        # guards the geographic clustering that keeps Santander's C3 apart from
        # Valencia's C3.
        for closure in self.found:
            self.assertLess(closure.railheads[0][1], 120.0, closure.line)

    def test_a_cut_station_is_not_also_a_railhead(self):
        for closure in self.found:
            cut_names = {s.name for s in closure.cut}
            for station, _km in closure.railheads:
                self.assertNotIn(station.name, cut_names, closure.line)


@unittest.skipUnless(DB.exists(), "timetable database not built")
class TestRelevance(unittest.TestCase):
    """A closure is only worth mentioning if it took away a nearer option."""

    SANTS = (41.3792, 2.1400)              # hundreds of trains
    GRANOLLERS_CAN = (41.6100, 2.2800)     # sits in the R3 cut

    @classmethod
    def setUpClass(cls):
        cls.day = dt.date.fromisoformat(feed_window(DB)["default_day"])
        cls.found = closures.find(DB, cls.day)
        cls.rail = closures.rail_stations(DB, cls.day)

    def test_a_station_full_of_trains_is_not_warned_about(self):
        # An FGC section is closed ~2 km from Sants, which warned on every
        # journey out of Barcelona until relevance was taken into account.
        hits = closures.affecting(self.found, *self.SANTS, self.rail)
        self.assertEqual(hits, [], f"spurious closure warning at Sants: {hits}")

    def test_a_station_inside_a_cut_is_still_warned_about(self):
        hits = closures.affecting(self.found, *self.GRANOLLERS_CAN, self.rail)
        self.assertTrue(hits, "R3 closure not reported at Granollers-Canovelles")
        self.assertEqual(hits[0][0].line, "R3")

    def test_stations_with_trains_are_never_listed_as_cut(self):
        # Sarrià and Gràcia are on the replacement bus route but keep their
        # trains (L12/S1/S2 and L6/L7); only stations with no train at all
        # from any line count as cut.
        served = {st.name for st in self.rail}
        for closure in self.found:
            for station in closure.cut:
                self.assertNotIn(station.name, served,
                                 f"{station.name} has trains but is listed as cut")


@unittest.skipUnless(DB.exists(), "timetable database not built")
class TestPlanningAroundAClosure(unittest.TestCase):
    """Granollers sits in the R3 cut; the trains start at La Garriga."""

    GRANOLLERS = (41.6100, 2.2800)
    VIC = (41.9300, 2.2540)

    @classmethod
    def setUpClass(cls):
        day = dt.date.fromisoformat(feed_window(DB)["default_day"])
        cls.tt = Timetable.load(DB, day, num_days=2)
        cls.journeys = raptor.search(
            cls.tt, cls.GRANOLLERS, cls.VIC,
            dt.datetime.combine(day, dt.time(7, 0)),
            max_bike_class=int(BikeClass.PARTIAL),   # most permissive setting
        )

    def test_never_puts_the_bike_on_a_replacement_bus(self):
        for journey in self.journeys:
            for leg in journey.legs:
                if leg.kind == "ride":
                    self.assertNotEqual(leg.bike_class, int(BikeClass.BUS),
                                        f"routed onto a bus: {leg.route_short}")

    def test_still_finds_a_way_by_riding_to_the_railhead(self):
        self.assertTrue(self.journeys, "no way around the R3 closure")


if __name__ == "__main__":
    unittest.main()
