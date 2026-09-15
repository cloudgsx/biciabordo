"""Whole-day search: best combination regardless of departure time."""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path

from app.gtfs.bikepolicy import BikeClass
from app.routing import raptor
from app.routing.timetable import Timetable
from app.service import feed_window

DB = Path("data/bicitren.sqlite")

BARCELONA = (41.3792, 2.1400)
VALENCIA = (39.4660, -0.3770)


@unittest.skipUnless(DB.exists(), "timetable database not built")
class TestProfileSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        window = feed_window(DB)
        cls.day = dt.date.fromisoformat(window["default_day"])
        cls.tt = Timetable.load(DB, cls.day, num_days=3)
        cls.opts = {"max_bike_class": int(BikeClass.RESERVED)}
        cls.profile = raptor.search_profile(
            cls.tt, BARCELONA, VALENCIA, cls.day, **cls.opts)

    def test_finds_something(self):
        self.assertTrue(self.profile)

    def test_beats_an_awkward_fixed_departure(self):
        """The point of the feature: 04:00 forces a worse journey than the day's best."""
        at_four = raptor.search(
            self.tt, BARCELONA, VALENCIA,
            dt.datetime.combine(self.day, dt.time(4, 0)), **self.opts)
        if not at_four:
            self.skipTest("no 04:00 journey to compare against")
        best_fixed = min(j.duration_s for j in at_four)
        best_profile = min(j.duration_s for j in self.profile)
        self.assertLessEqual(best_profile, best_fixed)

    def test_results_are_pareto_optimal(self):
        """No journey survives that another beats on every axis at once.

        Across a whole day, *leaving later* is itself a virtue: a slower journey
        that departs at 15:00 is not beaten by a faster one at 09:33, because
        someone who cannot leave before lunch can only take the first. Judging on
        duration alone declared half the timetable dominated. The bike rule the
        journey depends on counts too -- a slower option that needs no
        reservation is a real alternative, not a worse answer.
        """
        def axes(j):
            return (-j.depart, j.arrive, j.n_trains, j.bike_km, j.worst_class)

        for a in self.profile:
            for b in self.profile:
                if a is b:
                    continue
                no_worse = all(x <= y for x, y in zip(axes(b), axes(a)))
                better_somewhere = axes(b) != axes(a)
                self.assertFalse(no_worse and better_somewhere,
                                 f"dominated journey survived: {axes(a)} beaten by {axes(b)}")

    def test_departures_stay_within_the_requested_day(self):
        midnight = dt.datetime.combine(self.tt.day0, dt.time.min)
        for journey in self.profile:
            departs = midnight + dt.timedelta(seconds=journey.depart)
            self.assertEqual(departs.date(), self.day)

    def test_duration_excludes_waiting_for_the_first_train(self):
        # The reported departure is the latest that still catches the first
        # train, so duration measures the journey rather than the query time.
        for journey in self.profile:
            first_ride = next(l for l in journey.legs if l.kind == "ride")
            self.assertLessEqual(journey.depart, first_ride.dep)
            self.assertGreaterEqual(journey.depart, first_ride.dep - 3 * 3600)

    def test_terminates_without_exhausting_the_query_budget(self):
        # Self-pruning: each round jumps past the previous first train, so a day
        # costs a dozen or so queries, not one per minute.
        journeys = raptor.search_profile(
            self.tt, BARCELONA, VALENCIA, self.day, max_queries=15, **self.opts)
        self.assertTrue(journeys)


if __name__ == "__main__":
    unittest.main()
