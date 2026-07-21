#!/usr/bin/env python3
"""Static and fake-transport release safety tests for staged manual activation."""

from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
from apps.wb_autoanswers_production_ui_flow import _deduplicate_feedback_candidates


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json"
SERVICE = ROOT / "artifacts/registry_upload_http_entrypoint/systemd/wb-core-registry-http.service"
READONLY_SERVICE = ROOT / "artifacts/registry_upload_http_entrypoint/systemd/wb-core-autoanswers-readonly-sync.service"
READONLY_TIMER = ROOT / "artifacts/registry_upload_http_entrypoint/systemd/wb-core-autoanswers-readonly-sync.timer"
WORKER_SERVICE = ROOT / "artifacts/registry_upload_http_entrypoint/systemd/wb-core-autoanswers-worker.service"
WORKER_TIMER = ROOT / "artifacts/registry_upload_http_entrypoint/systemd/wb-core-autoanswers-worker.timer"
PUBLIC_ROUTES = ROOT / "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json"


class ReleaseSafetyTest(unittest.TestCase):
    def test_ui_media_candidates_include_media_filters_first_and_deduplicate(self) -> None:
        photo = {"id": "photo"}
        video = {"id": "video"}
        ordinary = {"id": "ordinary"}
        self.assertEqual(
            _deduplicate_feedback_candidates(
                [photo],
                [video, photo],
                [ordinary, video],
            ),
            [photo, video, ordinary],
        )

    def test_production_target_http_and_worker_remove_force_off_for_manual_activation(self) -> None:
        target_payload = json.loads(TARGET.read_text(encoding="utf-8"))
        self.assertEqual(target_payload["runtime_env"]["WB_AUTOANSWERS_FORCE_OFF"], "false")
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("WB_AUTOANSWERS_FORCE_OFF=false", service)
        self.assertNotIn("wb_autoanswers_worker.py", service)
        readonly_service = READONLY_SERVICE.read_text(encoding="utf-8")
        self.assertIn("WB_AUTOANSWERS_FORCE_OFF=true", readonly_service)
        self.assertIn("apps/wb_autoanswers_readonly.py --operation steady", readonly_service)
        self.assertNotIn("wb_autoanswers_worker.py", readonly_service)
        self.assertNotIn("feedbacks/answer", readonly_service)
        timer = READONLY_TIMER.read_text(encoding="utf-8")
        self.assertIn("OnActiveSec=5min", timer)
        self.assertIn("OnUnitActiveSec=5min", timer)
        managed = {item["name"]: item for item in target_payload["managed_systemd_units"]}
        self.assertFalse(managed["wb-core-autoanswers-readonly-sync.timer"]["enable"])
        self.assertFalse(managed["wb-core-autoanswers-readonly-sync.timer"]["restart"])
        worker = WORKER_SERVICE.read_text(encoding="utf-8")
        self.assertIn("WB_AUTOANSWERS_FORCE_OFF=false", worker)
        self.assertIn("apps/wb_autoanswers_worker.py --run-once", worker)
        self.assertFalse(managed["wb-core-autoanswers-worker.timer"]["enable"])
        self.assertFalse(managed["wb-core-autoanswers-worker.timer"]["restart"])
        self.assertIn("OnUnitActiveSec=1min", WORKER_TIMER.read_text(encoding="utf-8"))

    def test_public_allowlist_uses_exact_bounded_autoanswers_routes(self) -> None:
        payload = json.loads(PUBLIC_ROUTES.read_text(encoding="utf-8"))
        routes = {
            str(item["path"]): item
            for item in payload["routes"]
            if "/feedbacks/autoanswers/" in str(item.get("path") or "")
            or str(item.get("path") or "").endswith(
                ("/feedbacks/local", "/feedbacks/detail", "/feedbacks/media")
            )
        }
        expected = {
            "/v1/sheet-vitrina-v1/feedbacks/local": ["GET"],
            "/v1/sheet-vitrina-v1/feedbacks/detail": ["GET"],
            "/v1/sheet-vitrina-v1/feedbacks/media": ["GET"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings": ["GET", "POST"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/sync-now": ["POST"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/backlog/preview": ["POST"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/backlog/enqueue": ["POST"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/transition/preview": ["POST"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/review/approve": ["POST"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/manual/generate": ["POST"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/manual/regenerate": ["POST"],
            "/v1/sheet-vitrina-v1/feedbacks/autoanswers/manual/edit": ["POST"],
        }
        self.assertEqual(set(routes), set(expected))
        for path, methods in expected.items():
            self.assertEqual(routes[path]["match"], "exact")
            self.assertEqual(routes[path]["methods"], methods)

    def test_hosted_runner_exposes_only_whitelisted_readonly_operations(self) -> None:
        for operation in ("status", "canary", "steady", "backfill"):
            args = hosted.build_arg_parser().parse_args(["autoanswers-readonly", operation])
            self.assertIs(args.handler, hosted.run_autoanswers_readonly_command)
            self.assertEqual(args.operation, operation)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hosted.build_arg_parser().parse_args(["autoanswers-readonly", "publish"])
        ui_args = hosted.build_arg_parser().parse_args(
            ["autoanswers-ui-flow", "--evidence-dir", "/tmp/wb-autoanswers-ui-test"]
        )
        self.assertIs(ui_args.handler, hosted.run_autoanswers_ui_flow_command)
        self.assertEqual(ui_args.expected_state, "off-force")
        unforced_ui_args = hosted.build_arg_parser().parse_args(
            [
                "autoanswers-ui-flow",
                "--evidence-dir",
                "/tmp/wb-autoanswers-ui-unforced-test",
                "--expected-state",
                "off-unforced",
            ]
        )
        self.assertIs(unforced_ui_args.handler, hosted.run_autoanswers_ui_flow_command)
        self.assertEqual(unforced_ui_args.expected_state, "off-unforced")
        timer_args = hosted.build_arg_parser().parse_args(["autoanswers-readonly-timer", "enable"])
        self.assertIs(timer_args.handler, hosted.run_autoanswers_readonly_timer_command)
        lifecycle_args = hosted.build_arg_parser().parse_args(
            ["autoanswers-lifecycle", "activate-manual"]
        )
        self.assertIs(lifecycle_args.handler, hosted.run_autoanswers_lifecycle_command)
        self.assertEqual(lifecycle_args.action, "activate-manual")

    def test_remote_readonly_command_reasserts_force_off_and_has_no_write_worker(self) -> None:
        target = hosted.load_hosted_runtime_target(TARGET)
        evidence = {
            "status": "passed",
            "runtime": {
                "settings": {
                    "master_enabled": False,
                    "force_off": True,
                    "effective_enabled": False,
                },
                "capabilities": {"wb_get": True, "wb_post_patch": False, "openai": False},
            },
        }
        captured: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(evidence), stderr="")

        with patch.object(hosted.subprocess, "run", side_effect=fake_run):
            result = hosted._run_remote_autoanswers_readonly(
                target,
                operation="canary",
                page_size=50,
                max_pages=1,
                min_request_interval_seconds=1.0,
            )
        self.assertEqual(result["status"], "passed")
        command = " ".join(captured[0])
        self.assertIn("WB_AUTOANSWERS_FORCE_OFF=true", command)
        self.assertIn("WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true", command)
        self.assertIn("apps/wb_autoanswers_readonly.py", command)
        self.assertNotIn("wb_autoanswers_worker.py", command)
        self.assertNotIn("feedbacks/answer", command)

    def test_deploy_prepares_locked_node_boundary_and_schema_before_restart(self) -> None:
        target = hosted.load_hosted_runtime_target(TARGET)
        plan = hosted.deploy_current_checkout(
            target,
            target_file=TARGET,
            dry_run=True,
            allow_dirty=True,
        )
        os_dependency_command = " ".join(plan["commands"]["autoanswers_os_dependencies"])
        dependency_command = " ".join(plan["commands"]["autoanswers_node_dependencies"])
        capacity_command = " ".join(plan["commands"]["autoanswers_prepare_capacity"])
        migration_command = " ".join(plan["commands"]["autoanswers_prepare_deploy"])
        self.assertIn("nodejs.org/dist/v22.21.1", os_dependency_command)
        self.assertIn("680d3f30b24a7ff24b98db5e96f294c0070f8f9078df658da1bce1b9c9873c88", os_dependency_command)
        self.assertIn("e660365729b434af422bcd2e8e14228637ecf24a1de2cd7c916ad48f2a0521e1", os_dependency_command)
        self.assertIn("sha256sum --check --status", os_dependency_command)
        self.assertIn("apt-get install -y ca-certificates curl xz-utils zstd ffmpeg", os_dependency_command)
        self.assertIn("mktemp -d /opt/wb-core-runtime/node-runtimes/.install", os_dependency_command)
        self.assertIn("command -v npm", os_dependency_command)
        self.assertIn("command -v ffmpeg", os_dependency_command)
        self.assertIn("npm ci --omit=dev --ignore-scripts", dependency_command)
        self.assertIn("Number(process.versions.node.split", dependency_command)
        self.assertIn("command -v ffmpeg", dependency_command)
        self.assertIn("WB_AUTOANSWERS_FORCE_OFF=true", capacity_command)
        self.assertIn("wb_autoanswers_activation.py prepare-capacity", capacity_command)
        self.assertIn("WB_AUTOANSWERS_FORCE_OFF=true", migration_command)
        self.assertIn("wb_autoanswers_activation.py prepare-deploy", migration_command)
        self.assertIn("ServerAliveInterval=30", capacity_command)
        source = (ROOT / "apps" / "registry_upload_http_entrypoint_hosted_runtime.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"autoanswers-schema-preflight"', source)
        self.assertGreaterEqual(source.count("allow_transport_reconciliation=False"), 1)
        deploy_sequence = source[source.index('run_stage("dependencies", autoanswers_node_dependencies_command)') :]
        self.assertNotIn('run_stage(\n        "autoanswers-capacity"', deploy_sequence)
        activation_source = (ROOT / "apps" / "wb_autoanswers_activation.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("shutil.disk_usage(backup_root).free", activation_source)
        self.assertIn("_create_current_compressed_schema_backup", activation_source)
        self.assertIn('not bool(locked_before.get("autoanswers_initialized"))', activation_source)

    def test_manual_activation_uses_get_only_canary_before_enabling_worker_timer(self) -> None:
        original = hosted.load_hosted_runtime_target(TARGET)
        target = replace(
            original,
            runtime_env={**original.runtime_env, "WB_AUTOANSWERS_FORCE_OFF": "false"},
        )
        runtime = {
            "settings": {
                "master_enabled": True,
                "force_off": False,
                "effective_enabled": True,
                "mode": "manual",
            },
            "ai_jobs": {},
            "publication_jobs": {},
        }
        stdout = "\n".join(
            (
                json.dumps({"status": "activated", "runtime": runtime}),
                json.dumps({"status": "passed", "operation": "manual-canary", "runtime": runtime}),
                json.dumps({"status": "ready", "runtime": runtime}),
                "enabled",
                "active",
            )
        )
        captured: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with patch.object(hosted, "_current_live_publication_invariant_blockers", return_value=[]), patch.object(
            hosted.subprocess, "run", side_effect=fake_run
        ):
            result = hosted._run_remote_autoanswers_lifecycle(target, action="activate-manual")
        self.assertEqual(result["status"], "ok")
        command = " ".join(captured[0])
        self.assertIn("wb_autoanswers_readonly.py --operation manual-canary", command)
        self.assertNotIn("systemctl start wb-core-autoanswers-worker.service", command)
        self.assertIn("systemctl enable --now wb-core-autoanswers-worker.timer", command)


if __name__ == "__main__":
    unittest.main(verbosity=2)
