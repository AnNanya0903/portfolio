from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.db import ProgrammingError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from compounding.models import CompoundingIoTRun, CompoundingPlan
from dashboard.models import UserServiceAccess
from magicwand.views import _coerce_iot_timestamp

PUBLIC_MACHINE_ID = "mixing-mill-2"
SOURCE_MACHINE_ID = "extrusion-mill-1"


class IoTDashboardDataTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        # These tests cover the live bridge/local path; the sample store has its own tests.
        sync_patch = patch("compounding.iot_samples.sync_recent")
        sync_patch.start()
        self.addCleanup(sync_patch.stop)
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="iot-user",
            email="iot@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

        access, _created = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save(update_fields=["restrict", "allowed_services", "updated_at"])

    def test_iot_timestamp_coercion_normalizes_epoch_units(self) -> None:
        self.assertEqual(_coerce_iot_timestamp(1710000000), 1710000000000)
        self.assertEqual(_coerce_iot_timestamp("1710000000000"), 1710000000000)
        self.assertEqual(_coerce_iot_timestamp(1710000000000000), 1710000000000)
        self.assertEqual(_coerce_iot_timestamp("2024-03-09T16:00:00+00:00"), 1710000000000)

    @override_settings(
        DEBUG=True,
        IOT_PORTAL_URL="",
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.get_payload_history")
    @patch("magicwand.views.get_latest_payload")
    def test_dashboard_data_uses_local_payloads_in_debug(self, mock_latest, mock_history) -> None:
        payload = {
            "machineId": SOURCE_MACHINE_ID,
            "ts": 1710000000000,
            "registers": [1, 2, 3, 4],
            "raw": {
                "avg_current": 64.2,
                "ch1_temperature": 41.0,
                "ch2_temperature": 40.9,
            },
        }
        mock_latest.return_value = payload
        mock_history.return_value = [payload]

        response = self.client.get(reverse("iot_dashboard_data"))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["source"], "local")
        self.assertEqual(body["machineId"], PUBLIC_MACHINE_ID)
        self.assertEqual(body["latest"]["machineId"], PUBLIC_MACHINE_ID)
        self.assertEqual(body["latest"]["signalsByKey"]["avg_current"], 64.2)
        # History points are compact: only the charted values, no signal tables or registers.
        self.assertEqual(
            body["history"][0],
            {
                "ts": 1710000000000,
                "avg_current": 64.2,
                "t_watt": None,
                "t_kwh_import": None,
                "ch1_temperature": 41.0,
                "ch2_temperature": 40.9,
            },
        )

    @override_settings(
        DEBUG=True,
        IOT_PORTAL_URL="",
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.get_payload_history")
    @patch("magicwand.views.get_latest_payload")
    def test_dashboard_data_passes_time_range_to_local_store(self, mock_latest, mock_history) -> None:
        payload = {
            "machineId": SOURCE_MACHINE_ID,
            "ts": 1710000060000,
            "registers": [1, 2, 3, 4],
            "raw": {"avg_current": 20.0},
        }
        mock_latest.return_value = payload
        mock_history.return_value = [payload]

        response = self.client.get(
            reverse("iot_dashboard_data"),
            {"startTs": "1710000030000", "endTs": "1710000120000", "limit": 300},
        )

        self.assertEqual(response.status_code, 200)
        mock_latest.assert_called_once_with(start_ts=1710000030000, end_ts=1710000120000)
        mock_history.assert_called_once_with(limit=300, start_ts=1710000030000, end_ts=1710000120000)

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_PROXY_TIMEOUT=12,
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.requests.get")
    def test_dashboard_data_uses_bridge_in_production(self, mock_get) -> None:
        upstream_response = Mock()
        upstream_response.status_code = 200
        upstream_response.json.return_value = {
            "ok": True,
            "latest": {
                "machineId": SOURCE_MACHINE_ID,
                "ts": 1710000000000,
                "registers": [1, 2, 3, 4],
                "raw": {
                    "avg_current": 64.2,
                    "ch1_temperature": 41.0,
                    "ch2_temperature": 40.9,
                },
            },
            "history": [
                {
                    "machineId": SOURCE_MACHINE_ID,
                    "ts": 1710000000000,
                    "registers": [1, 2, 3, 4],
                    "raw": {
                        "avg_current": 64.2,
                        "ch1_temperature": 41.0,
                        "ch2_temperature": 40.9,
                    },
                }
            ],
            "limit": 25,
        }
        mock_get.return_value = upstream_response

        response = self.client.get(reverse("iot_dashboard_data"), {"limit": 25})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_get.call_args.args[0], "https://iot.magicwand.biz/dashboard-data")
        self.assertEqual(
            mock_get.call_args.kwargs["params"],
            {"machineId": SOURCE_MACHINE_ID, "limit": 25, "maxPoints": 2000},
        )
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Authorization"],
            "Bearer bridge-token",
        )

        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["source"], "bridge")
        self.assertEqual(body["machineId"], PUBLIC_MACHINE_ID)
        self.assertEqual(body["latest"]["machineId"], PUBLIC_MACHINE_ID)
        self.assertEqual(body["latest"]["signalsByKey"]["avg_current"], 64.2)

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_PROXY_TIMEOUT=12,
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.requests.get")
    def test_dashboard_data_passes_time_range_to_bridge(self, mock_get) -> None:
        upstream_response = Mock()
        upstream_response.status_code = 200
        upstream_response.json.return_value = {
            "ok": True,
            "latest": {
                "machineId": SOURCE_MACHINE_ID,
                "ts": 1710000120000,
                "registers": [1, 2, 3, 4],
                "raw": {"avg_current": 30.0},
            },
            "history": [
                {
                    "machineId": SOURCE_MACHINE_ID,
                    "ts": 1710000060000,
                    "registers": [1, 2, 3, 4],
                    "raw": {"avg_current": 20.0},
                },
                {
                    "machineId": SOURCE_MACHINE_ID,
                    "ts": 1710000120000,
                    "registers": [1, 2, 3, 4],
                    "raw": {"avg_current": 30.0},
                },
            ],
            "limit": 300,
            "startTs": 1710000030000,
            "endTs": 1710000120000,
        }
        mock_get.return_value = upstream_response

        response = self.client.get(
            reverse("iot_dashboard_data"),
            {"limit": 300, "startTs": 1710000030000, "endTs": 1710000120000},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            mock_get.call_args.kwargs["params"],
            {
                "machineId": SOURCE_MACHINE_ID,
                "limit": 300,
                "startTs": 1710000030000,
                "endTs": 1710000120000,
                "maxPoints": 2000,
            },
        )
        self.assertEqual(response.json()["machineId"], PUBLIC_MACHINE_ID)
        self.assertEqual(response.json()["latest"]["signalsByKey"]["avg_current"], 30.0)

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_PROXY_TIMEOUT=12,
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.requests.get")
    def test_dashboard_data_uses_unbounded_limit_for_selected_range(self, mock_get) -> None:
        upstream_response = Mock()
        upstream_response.status_code = 200
        upstream_response.json.return_value = {
            "ok": True,
            "latest": None,
            "history": [],
            "limit": 0,
            "historyCount": 0,
            "historyLimited": False,
            "startTs": 1710000030000,
            "endTs": 1710000120000,
        }
        mock_get.return_value = upstream_response

        response = self.client.get(
            reverse("iot_dashboard_data"),
            {"limit": 0, "startTs": 1710000030000, "endTs": 1710000120000},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            mock_get.call_args.kwargs["params"],
            {
                "machineId": SOURCE_MACHINE_ID,
                "limit": 0,
                "startTs": 1710000030000,
                "endTs": 1710000120000,
                "maxPoints": 2000,
            },
        )
        body = response.json()
        self.assertEqual(body["limit"], 0)
        self.assertFalse(body["historyLimited"])

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_PROXY_TIMEOUT=12,
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.requests.get")
    def test_history_export_streams_raw_bridge_export(self, mock_get) -> None:
        upstream_response = Mock()
        upstream_response.status_code = 200
        upstream_response.iter_content.return_value = [b"machineId,ts\n", b"extrusion-mill-1,1710000000000\n"]
        upstream_response.close = Mock()
        mock_get.return_value = upstream_response

        response = self.client.get(
            reverse("iot_history_export"),
            {"machineId": PUBLIC_MACHINE_ID, "startTs": 1710000000000, "endTs": 1710000060000, "format": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            mock_get.call_args.kwargs["params"],
            {
                "machineId": SOURCE_MACHINE_ID,
                "format": "csv",
                "startTs": 1710000000000,
                "endTs": 1710000060000,
            },
        )
        self.assertTrue(response["Content-Disposition"].endswith(".csv\""))
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn("extrusion-mill-1,1710000000000", body)
        upstream_response.close.assert_called_once()

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="",
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    def test_dashboard_data_requires_bridge_token_in_production(self) -> None:
        response = self.client.get(reverse("iot_dashboard_data"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "IOT_SERVICE_TOKEN is not configured")

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.AIReplyService")
    @patch("magicwand.views._fetch_iot_dashboard_data")
    def test_dashboard_summary_generates_ai_summary_from_current_snapshot(self, mock_fetch_data, mock_ai_service) -> None:
        mock_fetch_data.return_value = {
            "ok": True,
            "source": "bridge",
            "machineId": "mixing-mill-2",
            "limit": 120,
            "latest": {
                "machineId": "mixing-mill-2",
                "ts": 1710000000000,
                "registers": [1, 2, 3, 4],
                "signalRows": [
                    {"key": "avg_current", "label": "Average Current", "displayValue": "64.2 A"},
                    {"key": "ch1_temperature", "label": "CH-1 Temperature", "displayValue": "41.0 °C"},
                    {"key": "ch2_temperature", "label": "CH-2 Temperature", "displayValue": "40.9 °C"},
                ],
                "signalsByKey": {
                    "avg_current": 64.2,
                    "ch1_temperature": 41.0,
                    "ch2_temperature": 40.9,
                    "frequency": 50.0,
                    "avg_pf": 0.91,
                },
                "registerRows": [],
            },
            "history": [
                {
                    "machineId": "mixing-mill-2",
                    "ts": 1710000000000,
                    "signalsByKey": {
                        "avg_current": 62.0,
                        "ch1_temperature": 40.0,
                        "ch2_temperature": 39.8,
                        "frequency": 50.0,
                        "avg_pf": 0.90,
                    },
                },
                {
                    "machineId": "mixing-mill-2",
                    "ts": 1710000005000,
                    "signalsByKey": {
                        "avg_current": 64.2,
                        "ch1_temperature": 41.0,
                        "ch2_temperature": 40.9,
                        "frequency": 50.0,
                        "avg_pf": 0.91,
                    },
                },
            ],
        }
        mock_ai_service.return_value.generate_reply.return_value = "The extrusion mill appears to be running steadily."

        response = self.client.get(reverse("iot_dashboard_summary"))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["machineId"], "mixing-mill-2")
        self.assertEqual(body["summary"], "The extrusion mill appears to be running steadily.")
        self.assertFalse(body["cached"])

        ai_request = mock_ai_service.return_value.generate_reply.call_args.args[0]
        self.assertEqual(ai_request.purpose, "assistant")
        self.assertIn("Average Current: 64.2 A", ai_request.thread_summary)
        self.assertIn("Current state:", ai_request.thread_summary)
        self.assertIn("Recent trends:", ai_request.thread_summary)

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.AIReplyService")
    @patch("magicwand.views._fetch_iot_dashboard_data")
    def test_dashboard_summary_uses_cache_for_same_snapshot(self, mock_fetch_data, mock_ai_service) -> None:
        mock_fetch_data.return_value = {
            "ok": True,
            "source": "bridge",
            "machineId": "mixing-mill-2",
            "limit": 120,
            "latest": {
                "machineId": "mixing-mill-2",
                "ts": 1710000000000,
                "registers": [1, 2, 3],
                "signalRows": [
                    {"key": "avg_current", "label": "Average Current", "displayValue": "12.3 A"},
                ],
                "signalsByKey": {"avg_current": 12.3},
                "registerRows": [],
            },
            "history": [
                {
                    "machineId": "mixing-mill-2",
                    "ts": 1710000000000,
                    "signalsByKey": {"avg_current": 12.3},
                }
            ],
        }
        mock_ai_service.return_value.generate_reply.return_value = "Cached summary"

        first = self.client.get(reverse("iot_dashboard_summary"))
        second = self.client.get(reverse("iot_dashboard_summary"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["cached"])
        self.assertTrue(second.json()["cached"])
        self.assertEqual(mock_ai_service.return_value.generate_reply.call_count, 1)

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.AIReplyService")
    @patch("magicwand.views._fetch_iot_dashboard_data")
    def test_dashboard_summary_includes_logged_production_context(self, mock_fetch_data, mock_ai_service) -> None:
        plan = CompoundingPlan.objects.create(plan_date="2026-04-06", cjc_number=700, created_by=self.user)
        CompoundingIoTRun.objects.create(
            plan=plan,
            machine_id=PUBLIC_MACHINE_ID,
            compound_code="AG-5520",
            started_at=timezone.now() - timedelta(hours=2),
            ended_at=timezone.now() - timedelta(hours=1),
            stock_quality="good",
            notes="Batch looked stable and clean.",
            created_by=self.user,
        )
        mock_fetch_data.return_value = {
            "ok": True,
            "source": "bridge",
            "machineId": PUBLIC_MACHINE_ID,
            "limit": 120,
            "latest": {
                "machineId": PUBLIC_MACHINE_ID,
                "ts": 1710000000000,
                "registers": [1, 2, 3, 4],
                "signalRows": [
                    {"key": "avg_current", "label": "Average Current", "displayValue": "64.2 A"},
                ],
                "signalsByKey": {"avg_current": 64.2, "frequency": 50.0},
                "registerRows": [],
            },
            "history": [
                {
                    "machineId": PUBLIC_MACHINE_ID,
                    "ts": 1710000000000,
                    "signalsByKey": {"avg_current": 64.2, "frequency": 50.0},
                },
            ],
        }
        mock_ai_service.return_value.generate_reply.return_value = "Summary with production context."

        response = self.client.get(reverse("iot_dashboard_summary"))

        self.assertEqual(response.status_code, 200)
        ai_request = mock_ai_service.return_value.generate_reply.call_args.args[0]
        self.assertIn("Compound prepared: AG-5520", ai_request.thread_summary)
        self.assertIn("Quality of stock produced: Good", ai_request.thread_summary)
        self.assertIn("Operator notes: Batch looked stable and clean.", ai_request.thread_summary)
        self.assertEqual(response.json()["production_run"]["compound_code"], "AG-5520")

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views._generate_iot_summary_text", side_effect=RuntimeError("summary boom"))
    @patch("magicwand.views._fetch_iot_dashboard_data")
    def test_dashboard_summary_returns_json_on_unexpected_failure(self, mock_fetch_data, _mock_generate) -> None:
        mock_fetch_data.return_value = {
            "ok": True,
            "source": "bridge",
            "machineId": PUBLIC_MACHINE_ID,
            "limit": 120,
            "latest": {
                "machineId": PUBLIC_MACHINE_ID,
                "ts": 1710000000000,
                "registers": [1, 2, 3],
                "signalRows": [],
                "signalsByKey": {"avg_current": 12.3},
                "registerRows": [],
            },
            "history": [
                {
                    "machineId": PUBLIC_MACHINE_ID,
                    "ts": 1710000000000,
                    "signalsByKey": {"avg_current": 12.3},
                }
            ],
        }

        response = self.client.get(reverse("iot_dashboard_summary"))

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertFalse(response.json()["ok"])
        self.assertIn("AI summary failed unexpectedly", response.json()["error"])

    @override_settings(
        DEBUG=False,
        IOT_PORTAL_URL="https://iot.magicwand.biz",
        IOT_SERVICE_TOKEN="bridge-token",
        IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
        IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    )
    @patch("magicwand.views.AIReplyService")
    @patch("magicwand.views.CompoundingIoTRun.objects.filter", side_effect=ProgrammingError('relation "compounding_compoundingiotrun" does not exist'))
    @patch("magicwand.views._fetch_iot_dashboard_data")
    def test_dashboard_summary_ignores_missing_compounding_run_table(self, mock_fetch_data, _mock_filter, mock_ai_service) -> None:
        mock_fetch_data.return_value = {
            "ok": True,
            "source": "bridge",
            "machineId": PUBLIC_MACHINE_ID,
            "limit": 120,
            "latest": {
                "machineId": PUBLIC_MACHINE_ID,
                "ts": 1710000000000,
                "registers": [1, 2, 3],
                "signalRows": [],
                "signalsByKey": {"avg_current": 12.3},
                "registerRows": [],
            },
            "history": [
                {
                    "machineId": PUBLIC_MACHINE_ID,
                    "ts": 1710000000000,
                    "signalsByKey": {"avg_current": 12.3},
                }
            ],
        }
        mock_ai_service.return_value.generate_reply.return_value = "Summary without compounding context."

        response = self.client.get(reverse("iot_dashboard_summary"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIsNone(response.json()["production_run"])
        self.assertEqual(response.json()["summary"], "Summary without compounding context.")


def _bridge_payload(ts: int, current: float = 72.5) -> dict:
    """A realistic bridge sample: a full register block plus the named raw map."""
    return {
        "machineId": SOURCE_MACHINE_ID,
        "ts": ts,
        "registers": list(range(120)),
        "raw": {
            "avg_current": current,
            "t_watt": 41000.5,
            "t_kwh_import": 1234.567,
            "r_irms": 71.0,
            "y_irms": 72.0,
            "b_irms": 74.0,
            "avg_pf": 0.92,
            "a_vrms_ll": 415.2,
            "frequency": 50.01,
            "ch1_temperature": 40.1,
            "ch2_temperature": 39.8,
        },
    }


@override_settings(
    DEBUG=False,
    IOT_PORTAL_URL="https://iot.magicwand.biz",
    IOT_SERVICE_TOKEN="bridge-token",
    IOT_MACHINE_ID_DEFAULT=PUBLIC_MACHINE_ID,
    IOT_MACHINE_ID_SOURCE=SOURCE_MACHINE_ID,
    IOT_DASHBOARD_TIME_ZONE="Asia/Kolkata",
)
class IoTHistoryPayloadTests(TestCase):
    def setUp(self) -> None:
        sync_patch = patch("compounding.iot_samples.sync_recent")
        sync_patch.start()
        self.addCleanup(sync_patch.stop)
        self.user = get_user_model().objects.create_user(username="iot-viewer", password="secret123")
        access, _ = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save()
        self.client.force_login(self.user)

    def _bridge_response(self, history: list, **extra) -> Mock:
        upstream = Mock()
        upstream.status_code = 200
        upstream.json.return_value = {"ok": True, "latest": history[-1] if history else None, "history": history, **extra}
        return upstream

    @patch("magicwand.views.requests.get")
    def test_history_of_3600_samples_stays_under_300_kb(self, mock_get) -> None:
        history = [_bridge_payload(1795000000000 + index * 2000) for index in range(3600)]
        mock_get.return_value = self._bridge_response(history)

        response = self.client.get(
            reverse("iot_dashboard_data"), {"machineId": PUBLIC_MACHINE_ID, "limit": 5000}
        )

        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 300 * 1024)
        body = response.json()
        self.assertLessEqual(len(body["history"]), 2000)
        self.assertTrue(body["historyDownsampled"])
        self.assertEqual(
            set(body["history"][0]),
            {"ts", "avg_current", "t_watt", "t_kwh_import", "ch1_temperature", "ch2_temperature"},
        )
        # The full signal table is still there for the latest sample.
        self.assertIn("signalRows", body["latest"])
        self.assertIn("registers", body["latest"])

    @patch("magicwand.views.requests.get")
    def test_date_parameter_becomes_the_ist_day_range(self, mock_get) -> None:
        mock_get.return_value = self._bridge_response([])

        response = self.client.get(reverse("iot_dashboard_data"), {"date": "2026-11-27"})

        self.assertEqual(response.status_code, 200)
        params = mock_get.call_args.kwargs["params"]
        # 27 Nov 2026 00:00 IST is 26 Nov 18:30 UTC.
        self.assertEqual(params["startTs"], 1795717800000)
        self.assertEqual(params["endTs"], 1795717800000 + 86_400_000 - 1)
        self.assertEqual(params["limit"], 0)
        body = response.json()
        self.assertEqual(body["startTs"], 1795717800000)
        self.assertEqual(body["endTs"], 1795717800000 + 86_400_000 - 1)

    @patch("magicwand.views.requests.get")
    def test_from_to_range_is_kept_inside_the_date(self, mock_get) -> None:
        mock_get.return_value = self._bridge_response([])
        day_start = 1795717800000

        self.client.get(
            reverse("iot_dashboard_data"),
            {"date": "2026-11-27", "startTs": day_start + 3_600_000, "endTs": day_start + 7_200_000},
        )

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["startTs"], day_start + 3_600_000)
        self.assertEqual(params["endTs"], day_start + 7_200_000)

    def test_bad_date_is_rejected(self) -> None:
        response = self.client.get(reverse("iot_dashboard_data"), {"date": "27-11-2026"})
        self.assertEqual(response.status_code, 400)

    def test_dashboard_page_renders_machine_id_without_waiting_for_data(self) -> None:
        response = self.client.get(reverse("compounding:iot_dashboard"))
        self.assertContains(response, f'data-machine-id="{PUBLIC_MACHINE_ID}">{PUBLIC_MACHINE_ID}</div>', html=False)

    def test_past_date_opens_the_chart_on_that_day(self) -> None:
        response = self.client.get(reverse("compounding:iot_dashboard"), {"date": "2026-11-20"})
        # escapejs writes the hyphens as \u002D.
        self.assertContains(response, r'const initialRangeDay = "2026\u002D11\u002D20";')


class IoTIngestTests(TestCase):
    def setUp(self) -> None:
        cache.clear()

    @override_settings(DEBUG=True, IOT_SERVICE_TOKEN="")
    def test_iot_ingest_accepts_local_posts_in_debug(self) -> None:
        response = self.client.post(
            reverse("iot_ingest"),
            data='{"machineId":"extrusion-mill-1","ts":1710000000000,"registers":[1,2],"raw":{"frequency":50.0}}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["latest"]["machineId"], PUBLIC_MACHINE_ID)  # the source id is shown under its public name

    @override_settings(DEBUG=False, IOT_SERVICE_TOKEN="prod-token")
    def test_iot_ingest_requires_bearer_token_in_production(self) -> None:
        response = self.client.post(
            reverse("iot_ingest"),
            data='{"machineId":"extrusion-mill-1","ts":1710000000000,"registers":[1,2],"raw":{"frequency":50.0}}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Missing bearer token")

    @override_settings(DEBUG=False, IOT_SERVICE_TOKEN="prod-token")
    def test_iot_ingest_rejects_invalid_bearer_token_in_production(self) -> None:
        response = self.client.post(
            reverse("iot_ingest"),
            data='{"machineId":"extrusion-mill-1","ts":1710000000000,"registers":[1,2],"raw":{"frequency":50.0}}',
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer wrong-token",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid bearer token")

    @override_settings(DEBUG=False, IOT_SERVICE_TOKEN="prod-token")
    def test_iot_ingest_accepts_valid_bearer_token_in_production(self) -> None:
        response = self.client.post(
            reverse("iot_ingest"),
            data='{"machineId":"extrusion-mill-1","ts":1710000000000,"registers":[1,2],"raw":{"frequency":50.0}}',
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer prod-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["latest"]["machineId"], PUBLIC_MACHINE_ID)

    @override_settings(DEBUG=False, IOT_SERVICE_TOKEN="prod-token")
    def test_iot_ingest_get_remains_local_only_in_production(self) -> None:
        response = self.client.get(reverse("iot_ingest"))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "Local-only endpoint")
