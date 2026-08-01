#!/usr/bin/env python3
"""Static and fake-transport release safety tests for staged manual activation."""

from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
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
        self.assertFalse(ui_args.verify_limit_save)
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
        auto_all_ui_args = hosted.build_arg_parser().parse_args(
            [
                "autoanswers-ui-flow",
                "--evidence-dir",
                "/tmp/wb-autoanswers-ui-auto-all-test",
                "--expected-state",
                "auto_all",
            ]
        )
        self.assertIs(
            auto_all_ui_args.handler,
            hosted.run_autoanswers_ui_flow_command,
        )
        self.assertEqual(auto_all_ui_args.expected_state, "auto_all")
        limit_save_ui_args = hosted.build_arg_parser().parse_args(
            [
                "autoanswers-ui-flow",
                "--evidence-dir",
                "/tmp/wb-autoanswers-ui-limit-save-test",
                "--expected-state",
                "auto_all",
                "--verify-limit-save",
            ]
        )
        self.assertTrue(limit_save_ui_args.verify_limit_save)
        timer_args = hosted.build_arg_parser().parse_args(["autoanswers-readonly-timer", "enable"])
        self.assertIs(timer_args.handler, hosted.run_autoanswers_readonly_timer_command)
        for lifecycle_action in ("status", "reconcile", "suspend"):
            lifecycle_args = hosted.build_arg_parser().parse_args(
                ["autoanswers-lifecycle", lifecycle_action]
            )
            self.assertIs(
                lifecycle_args.handler, hosted.run_autoanswers_lifecycle_command
            )
            self.assertEqual(lifecycle_args.action, lifecycle_action)
        recovery_args = hosted.build_arg_parser().parse_args(
            [
                "autoanswers-prefilter-skip-recovery",
                "dry-run",
                "--transition-run-id",
                "incident-run",
                "--expected-rows",
                "5",
            ]
        )
        self.assertIs(
            recovery_args.handler,
            hosted.run_autoanswers_prefilter_skip_recovery_command,
        )
        self.assertEqual(recovery_args.action, "dry-run")
        self.assertEqual(recovery_args.expected_rows, 5)
        latch_args = hosted.build_arg_parser().parse_args(
            [
                "autoanswers-prefilter-skip-recovery",
                "release-dry-run",
                "--transition-run-id",
                "incident-run",
                "--expected-rows",
                "5",
                "--source-fingerprint",
                "sha256:" + "b" * 64,
            ]
        )
        self.assertEqual(latch_args.action, "release-dry-run")
        self.assertEqual(
            latch_args.source_fingerprint,
            "sha256:" + "b" * 64,
        )
        backlog_args = hosted.build_arg_parser().parse_args(
            [
                "autoanswers-backlog-recovery",
                "dry-run",
                "--expected-deployed-sha",
                "a" * 40,
                "--manifest-file",
                "/tmp/wb-autoanswers-t0.json",
            ]
        )
        self.assertIs(
            backlog_args.handler,
            hosted.run_autoanswers_backlog_recovery_command,
        )
        self.assertEqual(backlog_args.action, "dry-run")
        answered_inventory_args = hosted.build_arg_parser().parse_args(
            [
                "autoanswers-answered-inventory-recovery",
                "dry-run",
                "--expected-deployed-sha",
                "a" * 40,
                "--manifest-file",
                "/tmp/wb-autoanswers-answered-inventory.json",
            ]
        )
        self.assertIs(
            answered_inventory_args.handler,
            hosted.run_autoanswers_answered_inventory_recovery_command,
        )
        self.assertEqual(answered_inventory_args.action, "dry-run")

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
        self.assertIn("command -v zstd", os_dependency_command)
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

    def test_feature_lifecycle_reconcile_is_the_only_remote_timer_owner(self) -> None:
        original = hosted.load_hosted_runtime_target(TARGET)
        target = replace(
            original,
            runtime_env={**original.runtime_env, "WB_AUTOANSWERS_FORCE_OFF": "false"},
        )
        lifecycle = {
            "process_key": "autoanswers",
            "business_mode": "manual",
            "lifecycle_state": "starting",
            "drift_status": "matched",
            "components": {
                "readonly_sync": {"desired": True, "actual": True},
                "worker": {"desired": True, "actual": True},
            },
        }
        stdout = json.dumps({"status": "reconciled", "lifecycle": lifecycle})
        captured: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with patch.object(hosted.subprocess, "run", side_effect=fake_run):
            result = hosted._run_remote_autoanswers_lifecycle(
                target, action="reconcile"
            )
        self.assertEqual(result["status"], "reconciled")
        command = " ".join(captured[0])
        self.assertIn("apps/wb_autoanswers_lifecycle.py reconcile", command)
        self.assertNotIn("systemctl enable", command)
        self.assertNotIn("wb_autoanswers_worker.py", command)

    def test_backlog_recovery_wrapper_streams_external_exact_scope_and_human_gate(self) -> None:
        from apps.wb_autoanswers_backlog_recovery import _fingerprint

        target = hosted.load_hosted_runtime_target(TARGET)
        deployed_sha = "a" * 40
        fingerprint = "sha256:" + "b" * 64
        manifest = {
            "contract": "wb_autoanswers_t0_manifest_v1",
            "captured_at": "2026-08-01T12:00:00Z",
            "items": [
                {
                    "feedback_id": "feedback-1",
                    "wb_detail_content_hash": "c" * 64,
                }
            ],
        }
        manifest["manifest_sha256"] = _fingerprint(manifest)
        captured: list[tuple[list[str], dict[str, object]]] = []
        response = {
            "contract": "wb_autoanswers_backlog_recovery_v1",
            "status": "applied",
            "manifest_sha256": manifest["manifest_sha256"],
            "deployed_runtime": {"runtime_sha": deployed_sha},
        }

        def fake_run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(response),
                stderr="",
            )

        with TemporaryDirectory() as directory:
            temp = Path(directory)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            plan_path = temp / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "coverage_confirmed": True,
                        "plan_fingerprint": fingerprint,
                        "manifest_sha256": manifest["manifest_sha256"],
                        "deployed_runtime": {"runtime_sha": deployed_sha},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(hosted.subprocess, "run", side_effect=fake_run):
                result = hosted._run_remote_autoanswers_backlog_recovery(
                    target,
                    action="apply",
                    expected_deployed_sha=deployed_sha,
                    manifest_path=manifest_path,
                    reviewed_plan_path=plan_path,
                    fingerprint=fingerprint,
                    approval_reference="github-pr-gate-comment-123",
                    actor="release-train",
                )
        self.assertEqual(result["status"], "applied")
        command = " ".join(captured[0][0])
        self.assertIn("WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true", command)
        self.assertIn("wb_autoanswers_backlog_recovery.py apply", command)
        self.assertIn("--manifest-stdin", command)
        self.assertIn("--expected-deployed-sha " + deployed_sha, command)
        self.assertIn("--approval-reference github-pr-gate-comment-123", command)
        self.assertEqual(json.loads(str(captured[0][1]["input"])), manifest)
        self.assertNotIn("feedbacks/answer", command)

    def test_answered_inventory_wrapper_requires_external_exact_scope_and_human_gate(self) -> None:
        from apps.wb_autoanswers_answered_inventory_recovery import _fingerprint

        target = hosted.load_hosted_runtime_target(TARGET)
        deployed_sha = "a" * 40
        fingerprint = "sha256:" + "b" * 64
        manifest = {
            "contract": "wb_autoanswers_processed_inventory_manifest_v2",
            "captured_at": "2026-08-01T12:00:00Z",
            "items": [
                {
                    "feedback_id": "feedback-1",
                    "content_hash": "c" * 64,
                    "resolution_kind": "answer_observed",
                    "answer_sha256": "d" * 64,
                }
            ],
        }
        manifest["manifest_sha256"] = _fingerprint(manifest)
        captured: list[tuple[list[str], dict[str, object]]] = []
        response = {
            "contract": "wb_autoanswers_answered_inventory_recovery_v2",
            "status": "applied",
            "manifest_sha256": manifest["manifest_sha256"],
            "deployed_runtime": {"runtime_sha": deployed_sha},
        }

        def fake_run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(response),
                stderr="",
            )

        with TemporaryDirectory() as directory:
            temp = Path(directory)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            plan_path = temp / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "coverage_confirmed": True,
                        "plan_fingerprint": fingerprint,
                        "manifest_sha256": manifest["manifest_sha256"],
                        "deployed_sha": deployed_sha,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(hosted.subprocess, "run", side_effect=fake_run):
                result = hosted._run_remote_autoanswers_answered_inventory_recovery(
                    target,
                    action="apply",
                    expected_deployed_sha=deployed_sha,
                    manifest_path=manifest_path,
                    reviewed_plan_path=plan_path,
                    fingerprint=fingerprint,
                    approval_reference="github-pr-gate-comment-456",
                    actor="release-train",
                )
        self.assertEqual(result["status"], "applied")
        command = " ".join(captured[0][0])
        self.assertIn("WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true", command)
        self.assertIn("wb_autoanswers_answered_inventory_recovery.py apply", command)
        self.assertIn("--manifest-stdin", command)
        self.assertIn("--expected-deployed-sha " + deployed_sha, command)
        self.assertIn("--approval-reference github-pr-gate-comment-456", command)
        self.assertEqual(json.loads(str(captured[0][1]["input"])), manifest)
        self.assertNotIn("feedbacks/answer", command)

    def test_prefilter_skip_recovery_uses_repo_owned_bounded_runner(self) -> None:
        target = hosted.load_hosted_runtime_target(TARGET)
        payload = {
            "candidate_count": 5,
            "coverage_confirmed": True,
            "plan_fingerprint": "sha256:" + "a" * 64,
        }
        captured: list[list[str]] = []

        def fake_run(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(payload),
                stderr="",
            )

        with patch.object(hosted.subprocess, "run", side_effect=fake_run):
            result = hosted._run_remote_autoanswers_prefilter_skip_recovery(
                target,
                action="dry-run",
                transition_run_id="incident-run",
                expected_rows=5,
            )
        self.assertEqual(result, payload)
        command = " ".join(captured[0])
        self.assertIn(
            "apps/wb_autoanswers_prefilter_skip_recovery.py dry-run",
            command,
        )
        self.assertIn("--transition-run-id incident-run", command)
        self.assertIn("--expected-rows 5", command)
        self.assertNotIn("--fingerprint", command)

        captured.clear()
        with patch.object(hosted.subprocess, "run", side_effect=fake_run):
            hosted._run_remote_autoanswers_prefilter_skip_recovery(
                target,
                action="release-dry-run",
                transition_run_id="incident-run",
                expected_rows=5,
                source_fingerprint="sha256:" + "b" * 64,
            )
        command = " ".join(captured[0])
        self.assertIn("release-dry-run", command)
        self.assertIn("--source-fingerprint", command)


if __name__ == "__main__":
    unittest.main(verbosity=2)
