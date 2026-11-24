"""Gmail AI leads: candidates sort by received date even when some dates have a time zone and some do not
(this raised "can't compare offset-naive and offset-aware datetimes" on the leads page)."""
from datetime import datetime, timezone as dt_timezone
from unittest.mock import MagicMock

from django.test import SimpleTestCase
from django.utils import timezone

from enquiries.views import _coerce_candidate_received_at, _search_gmail_lead_candidates


class CoerceReceivedAtTests(SimpleTestCase):
    def test_every_result_is_timezone_aware(self):
        for value in (datetime(2026, 11, 10, 9, 0), datetime(2026, 11, 10, 9, 0, tzinfo=dt_timezone.utc), None, "", "x", 5):
            self.assertTrue(timezone.is_aware(_coerce_candidate_received_at(value)), value)

    def test_a_naive_date_is_taken_as_utc_and_an_aware_one_is_kept(self):
        naive = _coerce_candidate_received_at(datetime(2026, 11, 10, 9, 0))
        self.assertEqual(naive, datetime(2026, 11, 10, 9, 0, tzinfo=dt_timezone.utc))
        aware = datetime(2026, 11, 10, 9, 0, tzinfo=dt_timezone.utc)
        self.assertIs(_coerce_candidate_received_at(aware), aware)

    def test_missing_dates_sort_last(self):
        newest = _coerce_candidate_received_at(datetime(2026, 11, 10))
        self.assertLess(_coerce_candidate_received_at(None), newest)


class SearchSortsMixedDatesTests(SimpleTestCase):
    def test_mixed_naive_aware_and_missing_dates_do_not_raise(self):
        gmail = MagicMock()
        gmail.search_messages.return_value = [
            {"id": "a", "parsed_date": datetime(2026, 11, 10, 9, 0)},                              # naive
            {"id": "b", "parsed_date": datetime(2026, 11, 12, 9, 0, tzinfo=dt_timezone.utc)},       # aware
            {"id": "c", "parsed_date": None},                                                       # missing
            {"id": "d", "parsed_date": datetime(2026, 11, 11, 9, 0)},                              # naive
        ]
        candidates = _search_gmail_lead_candidates(gmail, user_email="x@y.example")
        self.assertEqual([c["message_id"] for c in candidates], ["b", "d", "a", "c"])  # newest first, missing last
