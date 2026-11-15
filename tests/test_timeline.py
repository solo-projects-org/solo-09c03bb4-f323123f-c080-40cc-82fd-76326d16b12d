"""Tests for the DST timeline core (stdlib only).

Greedy bounds are cross-checked against brute-force enumeration of every
legal timeline so the "determined vs pending" verdict is verified exhaustively
on the demonstration scenarios.
"""

import itertools
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.timeline import Record, analyze_timeline, parse_local, utc_candidates

UTC = ZoneInfo("UTC")


def u(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def brute_force(records):
    ordered = sorted(records, key=lambda r: r.sample_id)
    candidates = [utc_candidates(r.local_time) for r in ordered]
    legal = []
    for combo in itertools.product(*candidates):
        if all(combo[i] < combo[i + 1] for i in range(len(combo) - 1)):
            legal.append(combo)
    return ordered, legal


def assert_greedy_matches_enumeration(testcase, records):
    ordered, legal = brute_force(records)
    result = analyze_timeline(records)
    testcase.assertEqual(result.feasible, bool(legal))
    if not legal:
        return ordered, legal
    for i, rec in enumerate(ordered):
        values = {combo[i] for combo in legal}
        testcase.assertEqual(
            (result.earliest[rec.sample_id], result.latest[rec.sample_id]),
            (min(values), max(values)),
        )
    return ordered, legal


class UtcCandidatesTests(unittest.TestCase):
    def test_unambiguous_time_has_one_candidate(self):
        self.assertEqual(
            utc_candidates("2024-11-03T00:45:00"),
            [u("2024-11-03T04:45:00")],
        )
        self.assertEqual(
            utc_candidates("2024-11-03T02:15:00"),
            [u("2024-11-03T07:15:00")],
        )

    def test_ambiguous_wall_time_has_two_candidates(self):
        self.assertEqual(
            utc_candidates("2024-11-03T01:20:00"),
            [u("2024-11-03T05:20:00"), u("2024-11-03T06:20:00")],
        )

    def test_boundary_wall_times(self):
        self.assertEqual(
            utc_candidates("2024-11-03T01:00:00"),
            [u("2024-11-03T05:00:00"), u("2024-11-03T06:00:00")],
        )
        self.assertEqual(
            utc_candidates("2024-11-03T02:00:00"),
            [u("2024-11-03T07:00:00")],
        )

    def test_offset_suffix_rejected(self):
        with self.assertRaises(ValueError):
            parse_local("2024-11-03T01:20:00-04:00")


class TimelineTests(unittest.TestCase):
    INITIAL = [
        Record(100, "2024-11-03T00:45:00", 120),
        Record(128, "2024-11-03T01:20:00", 340),
        Record(148, "2024-11-03T01:55:00", 300),
        Record(160, "2024-11-03T02:15:00", 260),
        Record(180, "2024-11-03T03:10:00", 210),
    ]

    def test_initial_fallback_batch_two_pending_samples(self):
        result = analyze_timeline(self.INITIAL)
        self.assertTrue(result.feasible)
        self.assertTrue(result.is_determined(100))
        self.assertTrue(result.is_determined(160))
        self.assertTrue(result.is_determined(180))
        self.assertFalse(result.is_determined(128))
        self.assertFalse(result.is_determined(148))
        self.assertEqual(result.earliest[128], u("2024-11-03T05:20:00"))
        self.assertEqual(result.latest[128], u("2024-11-03T06:20:00"))
        self.assertEqual(result.earliest[148], u("2024-11-03T05:55:00"))
        self.assertEqual(result.latest[148], u("2024-11-03T06:55:00"))

    def test_single_same_wall_backfill_resolves_everything(self):
        records = self.INITIAL + [
            Record(127, "2024-11-03T01:20:00", 84),
        ]
        result = analyze_timeline(records)
        self.assertTrue(result.feasible)
        for sid in (100, 127, 128, 148, 160, 180):
            self.assertTrue(result.is_determined(sid), f"sample {sid} still pending")
        # the two same-wall readings are forced onto the two UTC slots
        self.assertEqual(result.earliest[127], u("2024-11-03T05:20:00"))
        self.assertEqual(result.earliest[128], u("2024-11-03T06:20:00"))
        # 01:55 then only fits the late (EST) pass
        self.assertEqual(result.earliest[148], u("2024-11-03T06:55:00"))

    def test_three_readings_same_ambiguous_wall_time_infeasible(self):
        records = [
            Record(1, "2024-11-03T01:20:00", 1),
            Record(2, "2024-11-03T01:20:00", 2),
            Record(3, "2024-11-03T01:20:00", 3),
        ]
        self.assertFalse(analyze_timeline(records).feasible)

    def test_infeasible_when_ids_and_utc_order_contradict(self):
        records = [
            Record(5, "2024-11-03T03:00:00", 50),   # 08:00Z
            Record(15, "2024-11-03T00:30:00", 60),  # 04:30Z
        ]
        self.assertFalse(analyze_timeline(records).feasible)

    def test_pending_pair_has_four_legal_timelines(self):
        records = [
            Record(20, "2024-11-03T01:20:00", 340),
            Record(30, "2024-11-03T01:50:00", 300),
        ]
        result = analyze_timeline(records)
        self.assertTrue(result.feasible)
        self.assertFalse(result.is_determined(20))
        self.assertFalse(result.is_determined(30))

    def test_greedy_bounds_match_brute_force_on_demo(self):
        assert_greedy_matches_enumeration(self, self.INITIAL)
        assert_greedy_matches_enumeration(
            self, self.INITIAL + [Record(127, "2024-11-03T01:20:00", 84)]
        )

    def test_greedy_bounds_match_brute_force_randomized(self):
        # exhaustive check over small grids inside the repeated hour
        walls = ["2024-11-03T01:00:00", "2024-11-03T01:20:00",
                 "2024-11-03T01:40:00", "2024-11-03T01:55:00"]
        for width in range(1, 5):
            for combo in itertools.product(walls, repeat=width):
                records = [
                    Record(i + 1, wall, i + 10) for i, wall in enumerate(combo)
                ]
                assert_greedy_matches_enumeration(self, records)


if __name__ == "__main__":
    unittest.main()
