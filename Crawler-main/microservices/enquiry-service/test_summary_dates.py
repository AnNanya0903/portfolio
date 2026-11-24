"""Custom summary dates are Indian days (12:00 AM to 11:59 PM IST); the service itself works in naive UTC."""
import unittest
from datetime import datetime

import storage


class CustomDateRangeTests(unittest.TestCase):
    def test_a_day_is_cut_at_indian_midnight(self) -> None:
        period, start, end = storage._resolve_summary_date_range(
            period="custom", start_date="2026-11-11", end_date="2026-11-11")
        self.assertEqual(period, "custom")
        self.assertEqual(start, datetime(2026, 11, 10, 18, 30))  # 11 Nov 00:00 IST
        self.assertEqual(end, datetime(2026, 11, 11, 18, 29, 59, 999999))  # 11 Nov 23:59:59 IST

    def test_only_one_end_may_be_given(self) -> None:
        _, start, end = storage._resolve_summary_date_range(period="custom", start_date="2026-11-11")
        self.assertEqual((start, end), (datetime(2026, 11, 10, 18, 30), None))
        _, start, end = storage._resolve_summary_date_range(period="custom", end_date="2026-11-11")
        self.assertEqual((start, end), (None, datetime(2026, 11, 11, 18, 29, 59, 999999)))

    def test_a_range_of_days(self) -> None:
        _, start, end = storage._resolve_summary_date_range(
            period="custom", start_date="2026-11-11", end_date="2026-11-12")
        self.assertEqual((start, end), (datetime(2026, 11, 10, 18, 30), datetime(2026, 11, 12, 18, 29, 59, 999999)))

    def test_the_other_periods_and_the_errors_are_unchanged(self) -> None:
        self.assertEqual(storage._resolve_summary_date_range(period="all"), ("all", None, None))
        for kwargs in ({"period": "custom"}, {"period": "custom", "start_date": "11-11-2026"},
                       {"period": "custom", "start_date": "2026-11-12", "end_date": "2026-11-11"},
                       {"period": "yearly"}):
            with self.assertRaises(ValueError):
                storage._resolve_summary_date_range(**kwargs)
        period, start, end = storage._resolve_summary_date_range(period="weekly")
        self.assertEqual(round((end - start).total_seconds() / 86400), 7)

    def test_the_edge_helper(self) -> None:
        from datetime import date, time
        self.assertEqual(storage._ist_day_edge_as_utc(date(2026, 3, 1), time.min), datetime(2026, 2, 28, 18, 30))


if __name__ == "__main__":
    unittest.main()
