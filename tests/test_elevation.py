"""Elevation ranking. No network: providers are stubbed throughout."""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from unittest import mock

from app import elevation
from app.routing.timetable import Timetable
from app.service import feed_window, nearby_stations

DB = Path("data/bicitren.sqlite")


class TestSampling(unittest.TestCase):
    def test_line_hits_both_endpoints(self):
        a, b = (42.0, 1.0), (43.0, 2.0)
        points = elevation.sample_line(a, b, 5)
        self.assertEqual(len(points), 5)
        self.assertAlmostEqual(points[0][0], a[0])
        self.assertAlmostEqual(points[-1][1], b[1])

    def test_never_degenerates(self):
        # A single sample would make ascent undefined.
        self.assertEqual(len(elevation.sample_line((0, 0), (1, 1), 1)), 2)


class TestProfile(unittest.TestCase):
    def test_counts_only_upward_deltas(self):
        # Up over a col and back down: ascent is the climb, not the net change.
        with mock.patch.object(elevation, "elevations",
                               return_value=[100.0, 400.0, 900.0, 300.0]):
            prof = elevation.profile((42.0, 1.0), (42.2, 1.2), 20.0, samples=4)
        self.assertEqual(prof["ascent_m"], 800)
        self.assertEqual(prof["descent_m"], 600)
        self.assertEqual(prof["high_m"], 900)

    def test_climbing_costs_time(self):
        flat = [200.0] * 6
        hilly = [200.0, 500.0, 800.0, 1100.0, 1400.0, 1700.0]
        with mock.patch.object(elevation, "elevations", return_value=flat):
            easy = elevation.profile((42.0, 1.0), (42.2, 1.2), 30.0, samples=6)
        with mock.patch.object(elevation, "elevations", return_value=hilly):
            hard = elevation.profile((42.0, 1.0), (42.2, 1.2), 30.0, samples=6)
        self.assertEqual(easy["ascent_m"], 0)
        self.assertGreater(hard["minutes"], easy["minutes"])
        # 1.500 m of climbing at 450 m/h is over three extra hours.
        self.assertGreater(hard["minutes"] - easy["minutes"], 150)

    def test_unusable_samples_raise(self):
        with mock.patch.object(elevation, "elevations", return_value=[None, None]):
            with self.assertRaises(elevation.ElevationUnavailable):
                elevation.profile((42.0, 1.0), (42.2, 1.2), 10.0, samples=2)


@unittest.skipUnless(DB.exists(), "timetable database not built")
class TestRanking(unittest.TestCase):
    BERGA = (42.1051, 1.8458)

    @classmethod
    def setUpClass(cls):
        day = dt.date.fromisoformat(feed_window(DB)["default_day"])
        cls.tt = Timetable.load(DB, day, num_days=1)

    def test_falls_back_to_distance_when_no_provider_answers(self):
        with mock.patch.object(elevation, "elevations",
                               side_effect=elevation.ElevationUnavailable("offline")):
            stations, ranked = nearby_stations(self.tt, *self.BERGA, limit=5)
        self.assertFalse(ranked, "must not claim effort ranking without elevation")
        self.assertEqual([s["km"] for s in stations],
                         sorted(s["km"] for s in stations))
        for station in stations:
            # No half-populated rows implying knowledge we do not have.
            self.assertNotIn("ascent_m", station)

    def test_opt_out_keeps_distance_order(self):
        stations, ranked = nearby_stations(self.tt, *self.BERGA, limit=5,
                                           use_elevation=False)
        self.assertFalse(ranked)
        self.assertEqual([s["km"] for s in stations],
                         sorted(s["km"] for s in stations))

    def test_a_mountain_pass_loses_to_a_longer_valley(self):
        """The case that motivated this: Toses is nearest, and a terrible idea."""
        def fake(points):
            # Anything north-east of Berga is over the Collada de Toses.
            return [1900.0 if lat > 42.25 else 300.0 for lat, _ in points]

        with mock.patch.object(elevation, "elevations", side_effect=fake):
            stations, ranked = nearby_stations(self.tt, *self.BERGA, limit=6)
        self.assertTrue(ranked)
        names = [s["name"] for s in stations]
        self.assertNotIn("Toses", names)
        self.assertNotIn("La Molina", names)


if __name__ == "__main__":
    unittest.main()
