import unittest
from datetime import datetime, timezone

from data.window_comparator import WindowComparator

from data.conditions.debris import DebrisCalculator, DebrisObservation, MeteoroidSample
from data.window import Window


class TestWindowAndComparator(unittest.TestCase):
    def test_window_normalizes_utc_and_rejects_invalid_duration(self):
        w = Window(
            "a",
            datetime(2024, 5, 1, 12, tzinfo=timezone.utc),
            datetime(2024, 5, 1, 13, 30, tzinfo=timezone.utc),
        )
        self.assertIsNone(w.start.tzinfo)
        self.assertEqual(w.duration_minutes, 90)
        with self.assertRaises(ValueError):
            Window("bad", datetime(2024, 5, 1), datetime(2024, 5, 1, 0, 30))

    def test_debris_metrics_explain_conjunction_and_flux(self):
        w = Window("a", datetime(2024, 5, 1, 12), datetime(2024, 5, 1, 13))
        m = DebrisCalculator.calculate_metrics(
            w,
            [DebrisObservation(datetime(2024, 5, 1, 12, 20), 10, "25544-1")],
            [MeteoroidSample(datetime(2024, 5, 1, 12, 30), 1e-8)],
        )
        self.assertEqual(m.n_conjunctions, 1)
        self.assertEqual(m.closest_distance_km, 10)
        self.assertGreater(m.risk_score, 0)

    def test_comparator_requires_equal_duration_and_can_defer(self):
        a = Window("a", datetime(2024, 5, 1, 12), datetime(2024, 5, 1, 13))
        b = Window("b", datetime(2024, 5, 1, 14), datetime(2024, 5, 1, 15))
        result = WindowComparator.compare(
            [a, b],
            {
                "a": {
                    "debris": type(
                        "R", (), {"risk_score": 10, "data_coverage_pct": 100}
                    )()
                },
                "b": {
                    "debris": type(
                        "R", (), {"risk_score": 50, "data_coverage_pct": 100}
                    )()
                },
            },
        )
        self.assertEqual(result.preferred_window_id, "a")
        self.assertIn("Минимальный", result.reason)
