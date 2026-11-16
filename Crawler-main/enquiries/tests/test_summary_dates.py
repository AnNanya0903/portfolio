"""The summary report's custom dates are Indian days (12:00 AM to 11:59 PM IST), like the times it shows."""
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from enquiries import views

IST = ZoneInfo("Asia/Kolkata")

# (id, received in UTC, the day it is shown on in the report, which is Indian time)
ROWS = [
    {"enquiry_id": "A", "received_at_dt": datetime(2026, 11, 10, 20, 0, tzinfo=dt_timezone.utc)},  # 11 Nov 01:30 IST
    {"enquiry_id": "B", "received_at_dt": datetime(2026, 11, 11, 10, 0, tzinfo=dt_timezone.utc)},  # 11 Nov 15:30 IST
    {"enquiry_id": "C", "received_at_dt": datetime(2026, 11, 11, 19, 0, tzinfo=dt_timezone.utc)},  # 12 Nov 00:30 IST
    {"enquiry_id": "D", "received_at_dt": datetime(2026, 11, 12, 5, 0, tzinfo=dt_timezone.utc)},  # 12 Nov 10:30 IST
    {"enquiry_id": "E", "received_at_dt": datetime(2026, 11, 11, 18, 29, 59, tzinfo=dt_timezone.utc)},  # 11 Nov 23:59:59 IST
    {"enquiry_id": "F", "received_at_dt": datetime(2026, 11, 10, 18, 30, tzinfo=dt_timezone.utc)},  # 11 Nov 00:00:00 IST
]


def _listed(start="", end=""):
    found = views._filter_summary_enquiries(ROWS, period="custom", start_date=start, end_date=end)
    return sorted(row["enquiry_id"] for row in found)


def _by_shown_date(start="", end=""):
    def day(row):
        return row["received_at_dt"].astimezone(IST).date().isoformat()
    return sorted(r["enquiry_id"] for r in ROWS if (not start or day(r) >= start) and (not end or day(r) <= end))


class CustomDateRangeTests(SimpleTestCase):
    def test_a_day_is_midnight_to_midnight_in_india(self):
        self.assertEqual(_listed("2026-11-11", "2026-11-11"), ["A", "B", "E", "F"])
        self.assertEqual(_listed("2026-11-12", "2026-11-12"), ["C", "D"])

    def test_the_list_matches_the_dates_the_report_shows(self):
        for start, end in (("2026-11-11", "2026-11-11"), ("2026-11-12", "2026-11-12"), ("2026-11-11", "2026-11-12"),
                           ("2026-11-11", ""), ("", "2026-11-11"), ("2026-11-10", "2026-11-10"), ("", "")):
            self.assertEqual(_listed(start, end), _by_shown_date(start, end), f"{start or '-'} to {end or '-'}")

    def test_the_exact_edges_of_a_day(self):
        start, end = views._resolve_summary_filter_bounds("custom", start_date="2026-11-11", end_date="2026-11-11")
        self.assertEqual(start.astimezone(dt_timezone.utc), datetime(2026, 11, 10, 18, 30, tzinfo=dt_timezone.utc))
        self.assertEqual(end.astimezone(dt_timezone.utc),
                         datetime(2026, 11, 11, 18, 29, 59, 999999, tzinfo=dt_timezone.utc))

    def test_the_fixed_offset_is_the_display_zone(self):
        sample = datetime(2026, 11, 11, 12, 0)
        self.assertEqual(int(sample.replace(tzinfo=ZoneInfo(views.SUMMARY_DISPLAY_TIME_ZONE)).utcoffset().total_seconds() // 60),
                         views.SUMMARY_DISPLAY_UTC_OFFSET_MINUTES)

    def test_weekly_monthly_and_all_are_unchanged(self):
        self.assertEqual(views._resolve_summary_filter_bounds("all"), (None, None))
        for period, days in (("weekly", 7), ("monthly", 30)):
            start, end = views._resolve_summary_filter_bounds(period)
            self.assertEqual(round((end - start).total_seconds() / 86400), days)

    def test_a_wrongly_written_date_is_still_an_error(self):
        with self.assertRaises(ValueError):
            views._filter_summary_enquiries(ROWS, period="custom", start_date="11-11-2026")
