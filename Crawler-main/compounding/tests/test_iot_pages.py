from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding import iot_batches, iot_samples
from compounding.models import (
    CompoundingIoTRun,
    CompoundingMBPlan,
    CompoundingPlan,
    ProcessGroup,
    ProcessGroupCompound,
)
from compounding.tests.test_iot_batches import SETTINGS, batch_profile, ist
from dashboard.models import UserServiceAccess

MACHINE = "mixing-mill-2"


@override_settings(**SETTINGS)
class IoTPageTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.user = get_user_model().objects.create_user(username="lab-tech", password="secret123")
        access, _ = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save()
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date="2026-11-27", cjc_number=920)
        CompoundingMBPlan.objects.create(plan=self.plan, compound_code="AG-7020", total_quantity=100)
        iot_samples.store_payloads(MACHINE, batch_profile(ist(10, 11)))
        self.run = iot_batches.finalize_batch(
            CompoundingIoTRun.objects.create(
                plan=self.plan, machine_id=MACHINE, compound_code="AG-7020", started_at=ist(10, 11),
                ended_at=ist(10, 11) + timedelta(minutes=15.5),
            )
        )

    # Lab result API ---------------------------------------------------------------------------------
    def _post_lab(self, body, batch_id=None):
        return self.client.post(
            reverse("iot_batch_lab_result", args=[batch_id or self.run.pk]),
            data=json.dumps(body), content_type="application/json",
        )

    def test_lab_result_api_saves_result_values_and_remarks(self) -> None:
        response = self._post_lab({"result": "pass", "values": {"hardness": 65, "t90": "6.8"}, "remarks": "OK"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["labResult"], "pass")
        self.run.refresh_from_db()
        self.assertEqual(self.run.lab_result, CompoundingIoTRun.LAB_PASS)
        self.assertEqual(self.run.lab_values, {"hardness": 65, "t90": 6.8})
        self.assertEqual(self.run.lab_remarks, "OK")
        self.assertEqual(self.run.lab_result_by, self.user)
        self.assertIsNotNone(self.run.lab_result_at)

    def test_setting_back_to_pending_clears_who_and_when(self) -> None:
        self._post_lab({"result": "fail"})
        self._post_lab({"result": "pending"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.lab_result, CompoundingIoTRun.LAB_PENDING)
        self.assertIsNone(self.run.lab_result_at)

    def test_lab_result_api_rejects_bad_input(self) -> None:
        self.assertEqual(self._post_lab({"result": "good"}).status_code, 400)
        self.assertEqual(self._post_lab({"result": "pass", "values": ["a"]}).status_code, 400)
        self.assertEqual(self._post_lab({"result": "pass", "values": {"x": {"nested": 1}}}).status_code, 400)
        self.assertEqual(self._post_lab({"result": "pass"}, batch_id=999999).status_code, 404)
        response = self.client.post(
            reverse("iot_batch_lab_result", args=[self.run.pk]), data="{", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_lab_result_api_needs_login(self) -> None:
        self.client.logout()
        response = self._post_lab({"result": "pass"})
        self.assertNotEqual(response.status_code, 200)
        self.run.refresh_from_db()
        self.assertEqual(self.run.lab_result, CompoundingIoTRun.LAB_PENDING)

    def test_form_post_from_the_batch_page(self) -> None:
        response = self.client.post(
            reverse("iot_batch_lab_result", args=[self.run.pk]),
            {"result": "fail", "values": "hardness: 72\nnote = sticky", "remarks": "Too hard"},
        )
        self.assertRedirects(response, reverse("compounding:iot_batch_detail", args=[self.run.pk]),
                             fetch_redirect_response=False)
        self.run.refresh_from_db()
        self.assertEqual(self.run.lab_result, CompoundingIoTRun.LAB_FAIL)
        self.assertEqual(self.run.lab_values, {"hardness": 72.0, "note": "sticky"})

    # Batch detail -----------------------------------------------------------------------------------
    def test_batch_detail_shows_kpis_curve_data_and_lab_form(self) -> None:
        response = self.client.get(reverse("compounding:iot_batch_detail", args=[self.run.pk]))
        self.assertContains(response, f"Batch {self.run.batch_id}")
        self.assertContains(response, "kWh")
        self.assertContains(response, 'id="batch-chart-data"')
        self.assertContains(response, "Save lab result")
        chart = response.context["chart"]
        self.assertTrue(chart["curve"])
        self.assertEqual(chart["curve"][0]["t"], 0)

    def test_unassigned_batch_can_be_named_and_joins_its_group(self) -> None:
        group = ProcessGroup.objects.create(name="NBR")
        ProcessGroupCompound.objects.create(process_group=group, compound_code="AG-7020")
        self.run.compound_code = CompoundingIoTRun.UNASSIGNED_COMPOUND
        self.run.detection_source = CompoundingIoTRun.SOURCE_AUTO
        self.run.save()

        page = self.client.get(reverse("compounding:iot_batch_detail", args=[self.run.pk]))
        self.assertContains(page, "nobody logged it")
        self.client.post(reverse("compounding:iot_batch_assign_compound", args=[self.run.pk]),
                         {"compound_code": "AG-7020", "operator": "Ravi"})

        self.run.refresh_from_db()
        self.assertEqual(self.run.compound_code, "AG-7020")
        self.assertEqual(self.run.operator, "Ravi")
        self.assertEqual(self.run.process_group, group)
        self.assertEqual(self.run.curve_status, CompoundingIoTRun.CURVE_NO_STANDARD)

    def test_assign_rejects_unknown_compounds(self) -> None:
        self.client.post(reverse("compounding:iot_batch_assign_compound", args=[self.run.pk]), {"compound_code": "ZZZ"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.compound_code, "AG-7020")

    # Process groups ---------------------------------------------------------------------------------
    def test_create_group_add_and_remove_compounds(self) -> None:
        response = self.client.post(reverse("compounding:iot_process_groups"), {"name": "NBR 15 min"})
        group = ProcessGroup.objects.get(name="NBR 15 min")
        self.assertRedirects(response, reverse("compounding:iot_process_group_detail", args=[group.pk]),
                             fetch_redirect_response=False)

        url = reverse("compounding:iot_process_group_detail", args=[group.pk])
        self.client.post(url, {"action": "add", "compound_code": "AG-7020"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.process_group, group)
        other = ProcessGroup.objects.create(name="Other")
        self.client.post(reverse("compounding:iot_process_group_detail", args=[other.pk]),
                         {"action": "add", "compound_code": "AG-7020"})
        self.assertEqual(ProcessGroupCompound.objects.get(compound_code="AG-7020").process_group, group)

        page = self.client.get(url)
        self.assertContains(page, "AG-7020")
        self.assertContains(page, 'id="group-scatter"')
        self.assertEqual(page.context["scatter"][0]["compound"], "AG-7020")

        self.client.post(url, {"action": "remove", "compound_code": "AG-7020"})
        self.assertFalse(ProcessGroupCompound.objects.exists())
        self.run.refresh_from_db()
        self.assertIsNone(self.run.process_group)

    def test_duplicate_group_name_is_refused(self) -> None:
        ProcessGroup.objects.create(name="NBR")
        self.client.post(reverse("compounding:iot_process_groups"), {"name": "NBR"})
        self.assertEqual(ProcessGroup.objects.filter(name="NBR").count(), 1)

    def test_rebuild_needs_enough_batches(self) -> None:
        group = ProcessGroup.objects.create(name="NBR")
        response = self.client.post(reverse("compounding:iot_rebuild_curve", args=[group.pk]), follow=True)
        self.assertContains(response, "at least 20 are needed")
        self.assertFalse(group.curves.exists())

    def test_group_list_page(self) -> None:
        group = ProcessGroup.objects.create(name="NBR")
        ProcessGroupCompound.objects.create(process_group=group, compound_code="AG-7020")
        response = self.client.get(reverse("compounding:iot_process_groups"))
        self.assertContains(response, "NBR")
        self.assertContains(response, "Not built")
