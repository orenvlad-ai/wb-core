#!/usr/bin/env python3
"""Ownership and single-control-surface regressions for auto-updates."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from apps.business_data_maintenance import (
    FBS_SHADOW_TIMER_UNIT,
    INDEPENDENT_WRITER_TIMER_UNITS,
    POLICY_FILENAME,
    PROCESS_SPECS,
    _independent_writer_timer_restore_plan,
    load_or_initialize_owner_policy,
    update_direct_timer_process_desired_state,
    update_process_desired_state,
)
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_AUTO_UPDATES_MONITORING_PATH,
    DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_PATH,
    _render_sheet_vitrina_settings_ui,
    _render_sheet_vitrina_web_vitrina_ui,
    _user_can_access_path,
)

EU_TARGET = (
    Path(__file__).resolve().parents[1]
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "hosted_runtime_target__europe_api.json"
)
PUBLIC_ROUTES = (
    Path(__file__).resolve().parents[1]
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "nginx"
    / "public_route_allowlist.json"
)


class AutoUpdatesOwnershipTest(unittest.TestCase):
    def test_each_process_has_one_declared_control_owner(self) -> None:
        specs = {str(item["key"]): dict(item) for item in PROCESS_SPECS}
        self.assertEqual(len(specs), len(PROCESS_SPECS))
        self.assertEqual(
            {
                key
                for key, item in specs.items()
                if item["control_capability"] == "manage"
            },
            {
                "vitrina_refresh",
                "vitrina_closure_retry",
                "warehouse_functional",
                "wb_finance_weekly",
                "fbs_shadow",
            },
        )
        self.assertEqual(
            {
                key: item["control_location"]
                for key, item in specs.items()
                if item["control_capability"] == "monitor"
            },
            {
                "feedback_complaints": "Отзывы → Авто-жалобы",
                "autoanswers": "Отзывы → Отзывы",
            },
        )

    def test_monitoring_only_processes_reject_direct_settings_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            (runtime_dir / POLICY_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": "auto_updates_owner_policy_v2",
                        "master_desired": True,
                        "revision": 1,
                        "processes": {},
                    }
                ),
                encoding="utf-8",
            )
            policy = load_or_initialize_owner_policy(runtime_dir)
            for key in ("feedback_complaints", "autoanswers"):
                with self.subTest(process_key=key), self.assertRaisesRegex(
                    RuntimeError, "monitoring-only"
                ):
                    update_process_desired_state(
                        runtime_dir,
                        process_key=key,
                        desired=True,
                        expected_revision=int(policy["revision"]),
                        actor="test",
                        reason="direct bypass attempt",
                    )
            self.assertNotIn("autoanswers_readonly", policy["processes"])
            self.assertNotIn("autoanswers_worker", policy["processes"])

    def test_fbs_direct_control_disables_only_its_timer(self) -> None:
        class FakeSystemd:
            def __init__(self) -> None:
                self.disabled: list[str] = []
                self.enabled: list[str] = []

            def disable_now(self, unit: str) -> None:
                self.disabled.append(unit)

            def enable_now(self, unit: str) -> None:
                self.enabled.append(unit)

        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            (runtime_dir / POLICY_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": "auto_updates_owner_policy_v2",
                        "master_desired": True,
                        "revision": 7,
                        "processes": {
                            "fbs_shadow": {
                                "process_key": "fbs_shadow",
                                "desired": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            systemd = FakeSystemd()
            policy = update_direct_timer_process_desired_state(
                runtime_dir,
                systemd=systemd,
                process_key="fbs_shadow",
                desired=False,
                expected_revision=7,
                actor="test",
                reason="prevent a new FBS generation",
            )
            self.assertEqual(policy["revision"], 8)
            self.assertIs(policy["processes"]["fbs_shadow"]["desired"], False)
            self.assertEqual(systemd.disabled, [FBS_SHADOW_TIMER_UNIT])
            self.assertEqual(systemd.enabled, [])

    def test_fbs_explicit_policy_wins_over_legacy_restore_baseline(self) -> None:
        baseline = {
            "timers": {
                unit: {
                    "is_enabled": "enabled",
                    "is_active": "active",
                }
                for unit in INDEPENDENT_WRITER_TIMER_UNITS
            }
        }
        legacy_plan = _independent_writer_timer_restore_plan(baseline)
        managed_plan = _independent_writer_timer_restore_plan(
            baseline,
            owner_policy={"processes": {"fbs_shadow": {"desired": False}}},
        )
        self.assertIs(legacy_plan[FBS_SHADOW_TIMER_UNIT], True)
        self.assertIs(managed_plan[FBS_SHADOW_TIMER_UNIT], False)

    def test_vitrina_schedule_editor_exists_only_in_settings(self) -> None:
        web_html = _render_sheet_vitrina_web_vitrina_ui(
            read_path="/read",
            operator_path="/operator",
            refresh_path="/refresh",
            job_path="/job",
            role="admin",
            allowed_sections=["web_vitrina", "settings", "feedbacks", "prices"],
        )
        settings_html = _render_sheet_vitrina_settings_ui()
        for forbidden in (
            "data-vitrina-auto-schedule",
            "data-vitrina-auto-schedules-body",
            "loadVitrinaAutoSchedules",
            '"auto_schedules_path":',
            '"auto_schedules_run_now_path":',
        ):
            self.assertNotIn(forbidden, web_html)
        self.assertIn("data-vitrina-schedule-editor", settings_html)
        self.assertIn('"auto_schedules_path":', settings_html)
        self.assertIn('"auto_schedules_run_now_path":', settings_html)
        self.assertIn('"auto_updates_path":', web_html)
        self.assertIn(DEFAULT_AUTO_UPDATES_MONITORING_PATH, web_html)
        self.assertIn("data-feedbacks-auto-schedules-body", web_html)
        self.assertNotIn("data-spp-schedule-enabled", web_html)
        self.assertIn("data-autoanswers-mode", web_html)

    def test_monitoring_cards_render_without_individual_toggle_branch(self) -> None:
        settings_html = _render_sheet_vitrina_settings_ui()
        self.assertIn('control_capability || "manage") === "manage"', settings_html)
        self.assertIn('data-auto-update-toggle="', settings_html)
        self.assertIn("Только мониторинг", settings_html)
        self.assertIn("Управление:", settings_html)

    def test_feature_sections_can_read_monitoring_without_settings_access(self) -> None:
        self.assertTrue(
            _user_can_access_path(
                {"role": "operator", "allowed_sections": ["feedbacks"]},
                DEFAULT_AUTO_UPDATES_MONITORING_PATH,
            )
        )
        self.assertTrue(
            _user_can_access_path(
                {"role": "operator", "allowed_sections": ["prices"]},
                DEFAULT_AUTO_UPDATES_MONITORING_PATH,
            )
        )
        self.assertFalse(
            _user_can_access_path(
                {"role": "operator", "allowed_sections": ["reports"]},
                DEFAULT_AUTO_UPDATES_MONITORING_PATH,
            )
        )

    def test_vitrina_schedule_api_is_settings_owned(self) -> None:
        self.assertTrue(
            _user_can_access_path(
                {"role": "operator", "allowed_sections": ["settings"]},
                DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_PATH,
            )
        )
        self.assertFalse(
            _user_can_access_path(
                {"role": "operator", "allowed_sections": ["vitrina"]},
                DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_PATH,
            )
        )

    def test_feature_monitoring_route_is_public_get_only(self) -> None:
        routes = json.loads(PUBLIC_ROUTES.read_text(encoding="utf-8"))["routes"]
        route = next(
            item
            for item in routes
            if item.get("path") == DEFAULT_AUTO_UPDATES_MONITORING_PATH
        )
        self.assertEqual(route["match"], "exact")
        self.assertEqual(route["methods"], ["GET"])

    def test_deploy_manifest_does_not_override_business_timer_ownership(self) -> None:
        target = json.loads(EU_TARGET.read_text(encoding="utf-8"))
        business_timers = {
            "wb-core-sheet-vitrina-refresh.timer",
            "wb-core-sheet-vitrina-closure-retry.timer",
            "wb-core-feedbacks-auto-complaints-tick.timer",
            "wb-core-wb-finance-weekly.timer",
            "wb-core-fbs-shadow-collector.timer",
            "wb-core-warehouse-functional-sync.timer",
            "wb-core-autoanswers-readonly-sync.timer",
            "wb-core-autoanswers-worker.timer",
        }
        units = {
            str(item["name"]): dict(item)
            for item in target.get("managed_systemd_units", [])
        }
        self.assertTrue(business_timers.issubset(units))
        for unit in business_timers:
            with self.subTest(unit=unit):
                self.assertFalse(units[unit].get("enable"))
                self.assertFalse(units[unit].get("restart"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
