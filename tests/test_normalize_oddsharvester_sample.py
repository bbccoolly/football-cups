from datetime import UTC, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from scripts.research.normalize_oddsharvester_sample import (
    american_to_decimal,
    correct_history_timestamp,
)


class AmericanOddsTest(unittest.TestCase):
    def test_positive_and_negative_values(self):
        self.assertEqual(american_to_decimal("+600"), 7.0)
        self.assertEqual(american_to_decimal("-500"), 1.2)

    def test_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            american_to_decimal(0)


class HistoryTimestampTest(unittest.TestCase):
    def setUp(self):
        self.kickoff = datetime(2025, 4, 27, 15, 30, tzinfo=UTC)
        self.timezone = ZoneInfo("Europe/Rome")

    def test_wrong_scrape_year_is_corrected(self):
        corrected, status = correct_history_timestamp(
            "2026-04-27T17:29:00",
            self.kickoff,
            self.timezone,
            timedelta(days=180),
        )
        self.assertEqual(corrected, "2025-04-27T15:29:00Z")
        self.assertEqual(status, "year_corrected")

    def test_timestamp_after_kickoff_is_quarantined(self):
        corrected, status = correct_history_timestamp(
            "2026-04-27T18:00:00",
            self.kickoff,
            self.timezone,
            timedelta(days=180),
        )
        self.assertIsNone(corrected)
        self.assertEqual(status, "quarantined")


if __name__ == "__main__":
    unittest.main()
