from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding import iot_samples, iot_segments
from compounding.models import DetectedBatch
from compounding.tests.iot_fixtures import DAY, build_payloads
from compounding.tests.test_iot_reconcile import SETTINGS, user_with

MACHINE = "mixing-mill-2"


@override_settings(**SETTINGS)
class IoTPageTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        iot_samples.store_payloads(MACHINE, build_payloads())
        iot_segments.detect_day(MACHINE, DAY)
        self.batch = DetectedBatch.objects.order_by("start_at").first()

    def test_detail_tagging_and_standards_pages(self) -> None:
        self.client.force_login(user_with(["compounding_planning"], "sup"))
        detail = self.client.get(reverse("compounding:iot_detected_batch", args=[self.batch.pk]))
        self.assertContains(detail, "Current vs standard")
        self.assertContains(detail, "Tag this batch")
        self.client.post(reverse("compounding:iot_tag_detected_batch", args=[self.batch.pk]),
                         {"compound_code": "AG-7020"})
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.label_compound, "AG-7020")
        tag_page = self.client.get(reverse("compounding:iot_tag_batches"), {"date": DAY.isoformat()})
        self.assertContains(tag_page, "Save tags")
        self.assertContains(self.client.get(reverse("compounding:iot_standards")), "AG-7020")

    def test_operators_cannot_tag(self) -> None:
        self.client.force_login(user_with(["compounding_board"], "op"))
        self.assertNotContains(self.client.get(reverse("compounding:iot_detected_batch", args=[self.batch.pk])),
                               "Tag this batch")
        self.client.post(reverse("compounding:iot_tag_detected_batch", args=[self.batch.pk]), {"compound_code": "X"})
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.tagged_compound, "")
