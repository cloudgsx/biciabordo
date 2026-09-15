"""Invariants for the planner. Run: python -m unittest discover tests"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path

from app.gtfs.bikepolicy import BikeClass, classify
from app.gtfs.parse import parse_time
from app.routing import raptor
from app.routing.timetable import COMPASS, Timetable
from app.service import feed_window, nearby_stations

DB = Path("data/bicitren.sqlite")


class TestBikePolicy(unittest.TestCase):
    def test_commuter_feed_is_always_free(self):
        for name in ("C1", "R3", "", "whatever"):
            self.assertEqual(classify(name, "cer"), BikeClass.FREE)

    def test_long_distance_products_need_a_bag(self):
        for name in ("AVE", "AVLO", "ALVIA", "Intercity", "EUROMED", "AVE INT", "TRENCELTA"):
            self.assertEqual(classify(name, "avld"), BikeClass.BAGGED, name)

    def test_conventional_medium_distance_takes_an_assembled_bike(self):
        for name in ("MD", "REGIONAL", "REG.EXP.", "PROXIMDAD"):
            self.assertEqual(classify(name, "avld"), BikeClass.RESERVED, name)

    def test_avant_is_flagged_as_not_guaranteed(self):
        self.assertEqual(classify("AVANT", "avld"), BikeClass.PARTIAL)

    def test_unknown_product_fails_closed(self):
        # Better to hide a train than to send someone to a platform where they
        # will be turned away with an assembled bike.
        self.assertEqual(classify("SOMETHING NEW", "avld"), BikeClass.BAGGED)


class TestParse(unittest.TestCase):
    def test_times_past_midnight(self):
        self.assertEqual(parse_time("25:10:00"), 25 * 3600 + 600)

    def test_unpadded_hours(self):
        self.assertEqual(parse_time("8:30:00"), parse_time("08:30:00"))

    def test_blank(self):
        self.assertIsNone(parse_time(""))


@unittest.skipUnless(DB.exists(), "timetable database not built")
class TestJourneys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.day = dt.date.fromisoformat(feed_window(DB)["default_day"])
        cls.tt = Timetable.load(DB, cls.day, num_days=4)
        cls.journeys = raptor.search(
            cls.tt,
            (41.3792, 2.1400),      # Barcelona
            (43.3603, -5.8448),     # Oviedo
            dt.datetime.combine(cls.day, dt.time(7, 0)),
            max_bike_class=int(BikeClass.RESERVED),
        )

    def test_finds_a_way_across_the_country(self):
        self.assertTrue(self.journeys, "no bike-carrying route Barcelona -> Oviedo")

    def test_never_uses_a_train_that_needs_the_bike_bagged(self):
        for journey in self.journeys:
            for leg in journey.legs:
                if leg.kind == "ride":
                    self.assertLessEqual(leg.bike_class, int(BikeClass.RESERVED))

    def test_no_two_bike_legs_in_a_row(self):
        # Chained transfers used to appear when reconstruction spliced labels
        # from different rounds: it would tell you to ride 35 km and then a
        # further 20 km rather than simply riding to the far station.
        for journey in self.journeys:
            kinds = [leg.kind for leg in journey.legs]
            for a, b in zip(kinds, kinds[1:]):
                self.assertFalse(a == "bike" and b == "bike", kinds)

    def test_legs_are_in_chronological_order(self):
        for journey in self.journeys:
            for a, b in zip(journey.legs, journey.legs[1:]):
                self.assertLessEqual(a.arr, b.dep)
                self.assertLessEqual(a.dep, a.arr)

    def test_connecting_rides_stay_in_daylight_by_default(self):
        for journey in self.journeys:
            for leg in journey.legs[1:-1]:
                if leg.kind != "bike" or leg.km <= 0:
                    continue
                start = leg.dep % 86400
                end = leg.arr % 86400
                self.assertGreaterEqual(start, raptor.RIDE_EARLIEST)
                self.assertLessEqual(end, raptor.RIDE_LATEST)

    def test_transfer_budget_is_respected(self):
        journeys = raptor.search(
            self.tt, (41.3792, 2.1400), (43.3603, -5.8448),
            dt.datetime.combine(self.day, dt.time(7, 0)),
            max_bike_class=int(BikeClass.RESERVED),
            access_km=5.0, transfer_budget_km=10.0,
        )
        for journey in journeys:
            between = sum(l.km for l in journey.legs[1:-1] if l.kind == "bike")
            self.assertLessEqual(between, 10.0 + 1e-6)


@unittest.skipUnless(DB.exists(), "timetable database not built")
class TestPlacesWithoutAStation(unittest.TestCase):
    """Berga lost its line decades ago; the nearest served station is ~28 km off."""

    BERGA = (42.1051, 1.8458)

    @classmethod
    def setUpClass(cls):
        day = dt.date.fromisoformat(feed_window(DB)["default_day"])
        cls.tt = Timetable.load(DB, day, num_days=2)

    def test_no_station_within_the_default_radius(self):
        self.assertEqual(self.tt.nearest_stops(*self.BERGA, max_km=20.0), [])

    def test_nearest_any_still_answers(self):
        # Distance ordering only, so this stays independent of the network.
        options, ranked = nearby_stations(self.tt, *self.BERGA, use_elevation=False)
        self.assertTrue(options)
        self.assertFalse(ranked)
        self.assertGreater(options[0]["km"], 20.0)
        # Nearest-first, and each carries what a rider needs to decide.
        self.assertEqual([o["km"] for o in options], sorted(o["km"] for o in options))
        for option in options:
            self.assertIn(option["direction"], COMPASS)
            self.assertGreater(option["ride_minutes"], 0)

    def test_widening_the_radius_makes_it_reachable(self):
        reach = self.tt.nearest_stops(*self.BERGA, max_km=32.0)
        self.assertTrue(reach, "widening to 32 km should reach the Ripoll/Toses line")


if __name__ == "__main__":
    unittest.main()
