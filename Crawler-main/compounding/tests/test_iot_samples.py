from __future__ import annotations

import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding import iot_samples
from compounding.models import IoTSample
from dashboard.models import UserServiceAccess

MACHINE = "mixing-mill-2"
SOURCE = "extrusion-mill-1"
# 27 Nov 2026 00:00 IST
DAY_START = 1795717800000

BRIDGE = dict(
    DEBUG=False,
    IOT_PORTAL_URL="https://iot.magicwand.biz",
    IOT_SERVICE_TOKEN="bridge-token",
    IOT_MACHINE_ID_DEFAULT=MACHINE,
    IOT_MACHINE_ID_SOURCE=SOURCE,
    IOT_DASHBOARD_TIME_ZONE="Asia/Kolkata",
)


def payload(ts: int, current: float = 70.0, kwh: float = 100.0, **raw) -> dict:
    return {
        "machineId": SOURCE,
        "ts": ts,
        "registers": [],
        "raw": {"avg_current": current, "t_kwh_import": kwh, "t_watt": 40000.0, "pv_channel_1": 405, **raw},
    }


@override_settings(**BRIDGE)
class SampleStoreTests(TestCase):
    def setUp(self) -> None:
        cache.clear()

    def test_store_decodes_named_values_and_skips_duplicates(self) -> None:
        stored = iot_samples.store_payloads(MACHINE, [payload(DAY_START), payload(DAY_START), payload(DAY_START + 2000)])
        again = iot_samples.store_payloads(MACHINE, [payload(DAY_START + 2000), payload(DAY_START + 4000)])

        self.assertEqual(stored, 2)
        self.assertEqual(again, 1)
        sample = IoTSample.objects.get(machine_id=MACHINE, ts_ms=DAY_START)
        self.assertEqual(sample.avg_current, 70.0)
        self.assertEqual(sample.t_kwh_import, 100.0)
        # pv_channel_1 is the alias for ch1_temperature, sent as tenths of a degree.
        self.assertEqual(sample.ch1_temperature, 40.5)

    @patch("compounding.iot_samples.requests.get")
    def test_sync_pulls_the_range_from_the_bridge_in_chunks(self, mock_get) -> None:
        def answer(url, params, headers, timeout):
            self.assertEqual(url, "https://iot.magicwand.biz/history")
            self.assertEqual(params["machineId"], SOURCE)
            self.assertEqual(params["limit"], 0)
            self.assertEqual(headers["Authorization"], "Bearer bridge-token")
            response = Mock(status_code=200)
            start, end = params["startTs"], params["endTs"]
            response.json.return_value = {
                "history": [payload(ts) for ts in range(start - start % 2000 + 2000, end + 1, 2000)][:5]
            }
            return response

        mock_get.side_effect = answer
        with self.settings(IOT_SYNC_CHUNK_S=600):
            result = iot_samples.sync_from_bridge(MACHINE, start_ts=DAY_START, end_ts=DAY_START + 30 * 60 * 1000 - 1)

        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(result["stored"], 15)
        self.assertEqual(IoTSample.objects.filter(machine_id=MACHINE).count(), 15)
        self.assertIsNotNone(iot_samples.cached_latest_payload(MACHINE))

    @patch("compounding.iot_samples.requests.get")
    def test_sync_continues_after_the_newest_stored_sample(self, mock_get) -> None:
        iot_samples.store_payloads(MACHINE, [payload(DAY_START)])
        mock_get.return_value = Mock(status_code=200, json=Mock(return_value={"history": []}))

        iot_samples.sync_from_bridge(MACHINE, end_ts=DAY_START + 60_000)

        self.assertEqual(mock_get.call_args.kwargs["params"]["startTs"], DAY_START + 1)

    def test_downsampling_keeps_the_loading_spike(self) -> None:
        points = [{"ts": DAY_START + i * 2000, "avg_current": 70.0} for i in range(5000)]
        points[2500]["avg_current"] = 117.0

        reduced = iot_samples.downsample_points(points, 500)

        self.assertLessEqual(len(reduced), 501)
        self.assertIn(117.0, [point["avg_current"] for point in reduced])
        self.assertEqual(reduced[-1]["ts"], points[-1]["ts"])


@override_settings(**BRIDGE)
class StoredHistoryApiTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.user = get_user_model().objects.create_user(username="iot-history", password="secret123")
        access, _ = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save()
        self.client.force_login(self.user)

    @patch("compounding.iot_samples.sync_recent")
    @patch("magicwand.views.requests.get")
    def test_a_day_is_served_from_the_store_including_old_samples(self, mock_bridge, _sync) -> None:
        # A whole morning, far more than the bridge's per-request 3600-sample cap.
        iot_samples.store_payloads(
            MACHINE, [payload(DAY_START + 9 * 3_600_000 + i * 2000, current=20.0 + i % 50) for i in range(6000)]
        )

        response = self.client.get(reverse("iot_dashboard_data"), {"date": "2026-11-27"})

        self.assertEqual(response.status_code, 200)
        mock_bridge.assert_not_called()
        body = response.json()
        self.assertEqual(body["source"], "database")
        self.assertEqual(body["historyCount"], 6000)
        self.assertLessEqual(len(body["history"]), 2001)
        self.assertEqual(body["history"][0]["ts"], DAY_START + 9 * 3_600_000)
        self.assertLess(len(response.content), 300 * 1024)
        self.assertEqual(body["latest"]["machineId"], MACHINE)

    @patch("compounding.iot_samples.sync_recent")
    def test_since_ts_returns_only_newer_points(self, _sync) -> None:
        with patch("magicwand.views.time.time", return_value=(DAY_START + 120_000) / 1000):
            iot_samples.store_payloads(MACHINE, [payload(DAY_START + i * 2000) for i in range(30)])
            response = self.client.get(
                reverse("iot_dashboard_data"), {"limit": 900, "sinceTs": DAY_START + 50_000}
            )

        body = response.json()
        self.assertEqual(body["source"], "database")
        self.assertEqual([point["ts"] for point in body["history"]], [DAY_START + i * 2000 for i in range(26, 30)])

    @patch("magicwand.views.requests.get")
    def test_falls_back_to_the_bridge_before_anything_is_stored(self, mock_bridge) -> None:
        mock_bridge.return_value = Mock(
            status_code=200, json=Mock(return_value={"ok": True, "latest": None, "history": []})
        )
        with patch("compounding.iot_samples.sync_recent"):
            response = self.client.get(reverse("iot_dashboard_data"), {"date": "2026-11-20"})

        self.assertEqual(response.json()["source"], "bridge")


@override_settings(DEBUG=True, IOT_PORTAL_URL="", IOT_MACHINE_ID_DEFAULT=MACHINE, IOT_MACHINE_ID_SOURCE=SOURCE)
class IngestStoresSamplesTests(TestCase):
    def test_ingest_keeps_every_sample(self) -> None:
        for index in range(3):
            self.client.post(
                reverse("iot_ingest"), data=json.dumps(payload(DAY_START + index * 2000)), content_type="application/json"
            )
        self.assertEqual(IoTSample.objects.filter(machine_id=MACHINE).count(), 3)


@override_settings(**BRIDGE)
class IoTCronTests(TestCase):
    def test_only_app_engine_cron_may_call(self) -> None:
        url = reverse("compounding:iot_cron_sync")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url, HTTP_X_APPENGINE_CRON="false").status_code, 403)

    @patch("compounding.iot_samples.sync_from_bridge", return_value={"stored": 4})
    def test_cron_syncs_samples(self, mock_sync) -> None:
        response = self.client.get(reverse("compounding:iot_cron_sync"), HTTP_X_APPENGINE_CRON="true")
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once_with(MACHINE)
        self.assertEqual(response.json()["sync"], {"stored": 4})
