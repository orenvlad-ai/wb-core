"""Smoke-check for hosted runtime deploy/probe contract."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from unittest import mock
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps" / "registry_upload_http_entrypoint_hosted_runtime.py"
LIVE_RUNNER = ROOT / "apps" / "registry_upload_http_entrypoint_live.py"
INPUT_BUNDLE = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.registry_upload_http_entrypoint_hosted_runtime as hosted_runtime  # noqa: E402
import apps.registry_upload_http_entrypoint_live as live_runner  # noqa: E402
import apps.finance_partner_production_ui_flow as finance_ui_flow  # noqa: E402
import apps.warehouse_opening_snapshot as warehouse_opening_snapshot  # noqa: E402
import apps.warehouse_stocks_production_ui_flow as warehouse_ui_flow  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.finance_migration_deploy_lease import (  # noqa: E402
    baseline_invalidation_epoch as finance_lease_invalidation_epoch,
    evidence_fingerprint as finance_lease_fingerprint,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


class _ShortReadResponse:
    def __init__(self, payload: bytes, *, chunk_size: int) -> None:
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self._payload = payload
        self._chunk_size = chunk_size
        self._offset = 0

    def read(self, requested: int) -> bytes:
        if self._offset >= len(self._payload):
            return b""
        size = min(requested, self._chunk_size, len(self._payload) - self._offset)
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += size
        return chunk


def _assert_deploy_status_readback_retry() -> None:
    attempts: list[list[str]] = []
    sleeps: list[float] = []

    def recover_on_third_attempt(command: list[str]) -> None:
        attempts.append(command)
        if len(attempts) < 3:
            raise subprocess.CalledProcessError(3, command)

    hosted_runtime._run_deploy_status_readback(
        ["ssh", "target", "systemctl status runtime.service"],
        attempts=3,
        retry_seconds=0.25,
        runner=recover_on_third_attempt,
        sleep=sleeps.append,
    )
    if len(attempts) != 3 or sleeps != [0.25, 0.25]:
        raise AssertionError("deploy status readback must retry only within the exact bound")

    transport_attempts: list[list[str]] = []

    def transport_failure(command: list[str]) -> None:
        transport_attempts.append(command)
        raise subprocess.CalledProcessError(255, command)

    try:
        hosted_runtime._run_deploy_status_readback(
            ["ssh", "target", "systemctl status runtime.service"],
            attempts=3,
            retry_seconds=0,
            runner=transport_failure,
            sleep=lambda _: (_ for _ in ()).throw(
                AssertionError("transport failure must not enter local status retry")
            ),
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 255 or len(transport_attempts) != 1:
            raise AssertionError("SSH 255 must reach exact-SHA reconciliation immediately") from exc
    else:
        raise AssertionError("SSH 255 must remain transport-indeterminate")

    terminal_attempts: list[list[str]] = []

    def persistent_failure(command: list[str]) -> None:
        terminal_attempts.append(command)
        raise subprocess.CalledProcessError(3, command)

    try:
        hosted_runtime._run_deploy_status_readback(
            ["ssh", "target", "systemctl status runtime.service"],
            attempts=2,
            retry_seconds=0,
            runner=persistent_failure,
            sleep=lambda _: None,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 3 or len(terminal_attempts) != 2:
            raise AssertionError("persistent service failure must halt after the exact bound") from exc
    else:
        raise AssertionError("persistent service failure must remain fail-closed")


def _assert_pre_prepare_abort_skips_stale_restore() -> None:
    calls: list[str] = []

    def business_runner(_target: object, *, action: str, **_kwargs: object) -> dict[str, object]:
        calls.append(action)
        if action == "status":
            return {
                "status": "not_quiet",
                "auto_updates": {"revision": 54},
            }
        if action == "barrier-abort":
            return {
                "status": "inactive",
                "active": False,
                "phase": "released",
            }
        raise AssertionError(
            "an unstarted hold must not replay stale maintenance restore"
        )

    with mock.patch.object(
        hosted_runtime,
        "_run_remote_business_data_maintenance_runner",
        side_effect=business_runner,
    ), mock.patch.object(
        hosted_runtime,
        "_run_remote_warehouse_functional_maintenance_action",
        side_effect=AssertionError(
            "warehouse hold cannot exist before core prepare succeeds"
        ),
    ):
        evidence = hosted_runtime._abort_finance_storage_window_acquire(
            object(),
            window_id="unstarted-hold-abort-smoke",
            fingerprint="sha256:" + "7" * 64,
            reason="synthetic core preflight failure",
        )
    if calls != ["status", "barrier-abort"]:
        raise AssertionError(f"unexpected unstarted abort sequence: {calls}")
    if evidence["business_restore"] != {
        "status": "not_required",
        "boundary_kind": "no_maintenance_hold_started",
    }:
        raise AssertionError("unstarted abort must not claim a stale restore")
    if evidence["warehouse_restore"]["status"] != "not_required":
        raise AssertionError("unstarted abort must preserve the warehouse boundary")


def _assert_fbs_status_probe_uses_public_contract() -> None:
    result = {
        "route": "fbs_fulfillment_order_status",
        "method": "GET",
        "url": "http://127.0.0.1/fbs-status",
        "http_status": 200,
        "content_type": "application/json; charset=utf-8",
        "json_body": {
            "status": "ready",
            "active_sku_count": 1,
            "national_demand_scope": "russia_total_orderCount",
            "wb_stock_used": False,
            "facilities": [
                {
                    "facility_id": "fff_moscow",
                    "name": "FF Москва",
                    "calculation_enabled": True,
                    "blockers": [],
                }
            ],
            "sales_history_coverage": {
                "earliest_available_date": "2026-07-01",
                "latest_available_date": "2026-07-15",
            },
            "defaults": {"sales_history_mode": "last_n_days"},
        },
        "network_error": None,
    }
    evaluation = hosted_runtime._evaluate_route_result(result, route_paths={})
    if evaluation.get("ok") is not True:
        raise AssertionError(
            "FBS status deploy probe must accept the public facility name field"
        )


def main() -> None:
    _assert_deploy_status_readback_retry()
    _assert_pre_prepare_abort_skips_stale_restore()
    _assert_fbs_status_probe_uses_public_contract()
    with TemporaryDirectory(
        prefix="finance-canonical-process-bindings-"
    ) as bindings_temp_dir:
        binding_root = Path(bindings_temp_dir)
        raw_path = binding_root / "finance_raw.sqlite3"
        operational_path = binding_root / "operational.sqlite3"
        for path in (raw_path, operational_path):
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE binding_smoke(value TEXT)"
                )

        class _BindingRegistry:
            def load(self, *, require_files: bool = False) -> object:
                if not require_files:
                    raise AssertionError(
                        "canonical process binding must require store files"
                    )
                return object()

            def resolve(
                self,
                logical_store: str,
                *,
                manifest: object,
            ) -> Path:
                del manifest
                return (
                    raw_path
                    if logical_store == "finance_raw"
                    else operational_path
                )

        with mock.patch.object(
            live_runner,
            "StoreRegistry",
            return_value=_BindingRegistry(),
        ):
            bindings = live_runner.FinanceCanonicalStoreBindings(
                binding_root
            )
        try:
            if bindings.paths != (
                raw_path.resolve(),
                operational_path.resolve(),
            ):
                raise AssertionError(
                    "HTTP startup must bind both exact canonical stores"
                )
            if any(
                int(
                    connection.execute(
                        "PRAGMA query_only"
                    ).fetchone()[0]
                )
                != 1
                for connection in bindings._connections
            ):
                raise AssertionError(
                    "HTTP canonical store bindings must stay query-only"
                )
        finally:
            bindings.close()
        if bindings._connections:
            raise AssertionError(
                "HTTP canonical store bindings must close on shutdown"
            )

    if hosted_runtime._warehouse_opening_timeout_seconds("dry-run") != 300.0:
        raise AssertionError("warehouse opening dry-run must retain the bounded read timeout")
    if hosted_runtime._warehouse_opening_timeout_seconds("readback") != 300.0:
        raise AssertionError("warehouse opening readback must retain the bounded read timeout")
    if hosted_runtime._warehouse_opening_timeout_seconds("diagnose-discrepancy") != 300.0:
        raise AssertionError("warehouse opening diagnostic must retain the bounded read timeout")
    if hosted_runtime._warehouse_opening_timeout_seconds("apply") != 1800.0:
        raise AssertionError("warehouse opening apply must allow the coherent production backup to finish")
    if hosted_runtime._warehouse_opening_timeout_seconds("rollback") != 1800.0:
        raise AssertionError("warehouse opening rollback must allow the coherent recovery backup to finish")
    active_target = hosted_runtime.load_hosted_runtime_target(hosted_runtime.DEFAULT_TARGET_FILE)
    durable_transport_calls: list[str] = []
    durable_transport_job_id = ""

    def durable_transport_run(
        command: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal durable_transport_job_id
        command_text = " ".join(str(item) for item in command)
        durable_transport_calls.append(command_text)
        if " submit " in command_text:
            request_payload = json.loads(str(kwargs.get("input") or ""))
            durable_transport_job_id = str(
                request_payload.get("job_id") or ""
            )
            return subprocess.CompletedProcess(
                args=[],
                returncode=255,
                stdout="",
                stderr="transport reset",
            )
        status_count = sum(
            " status " in item for item in durable_transport_calls
        )
        if status_count == 1:
            status_payload = {
                "contract_name": (
                    "wb_core_finance_storage_transport_job_v1"
                ),
                "job_id": durable_transport_job_id,
                "deployed_sha": "a" * 40,
                "status": "running",
                "terminal": False,
                "worker_classification": "active_worker",
            }
        else:
            status_payload = {
                "contract_name": (
                    "wb_core_finance_storage_transport_job_v1"
                ),
                "job_id": durable_transport_job_id,
                "deployed_sha": "a" * 40,
                "status": "succeeded",
                "terminal": True,
                "worker_classification": "terminal_succeeded",
                "result": {
                    "status": "rollback_complete",
                    "global_manifest_switched": True,
                },
            }
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(status_payload),
            stderr="",
        )

    with (
        mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            side_effect=durable_transport_run,
        ),
        mock.patch.object(hosted_runtime.time, "sleep"),
    ):
        durable_result = (
            hosted_runtime._run_remote_finance_storage_transport_job(
                active_target,
                action="rollback-apply",
                runner_args=[
                    "python3",
                    "apps/finance_storage_split.py",
                    "rollback-apply",
                ],
                reviewed_plan_json='{"fingerprint":"sha256:smoke"}',
                deployed_sha="a" * 40,
                timeout_seconds=30,
            )
        )
    if (
        durable_result.get("status") != "rollback_complete"
        or durable_result.get("transport_job", {}).get(
            "transport_disconnect_recovered"
        )
        is not True
        or sum(" submit " in item for item in durable_transport_calls)
        != 1
    ):
        raise AssertionError(
            "Finance hosted transport must observe one exact remote job "
            "through a submit disconnect"
        )
    with (
        mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="active\n1234\n",
                stderr="",
            ),
        ),
        mock.patch.object(
            hosted_runtime,
            "_run_remote_finance_storage_split_action",
            return_value={
                "canonical_source": "monolith",
                "generation_epoch": "rollback-smoke",
                "raw": {
                    "path": "/runtime/generations/rollback-smoke/monolith.sqlite3",
                    "openers": [{"pid": 1234, "fd": 7}],
                },
                "operational": {
                    "path": "/runtime/generations/rollback-smoke/monolith.sqlite3",
                    "openers": [{"pid": 1234, "fd": 7}],
                },
            },
        ),
    ):
        binding = hosted_runtime._restart_finance_cutover_http_service(
            active_target
        )
    if (
        binding.get("main_pid") != 1234
        or binding.get("raw_bound") is not True
        or binding.get("operational_bound") is not True
    ):
        raise AssertionError(
            "Finance HTTP restart must prove MainPID binding to both "
            "canonical manifest stores"
        )
    with (
        mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="active\n1234\n",
                stderr="",
            ),
        ),
        mock.patch.object(
            hosted_runtime,
            "_run_remote_finance_storage_split_action",
            return_value={
                "raw": {"openers": [{"pid": 9999}]},
                "operational": {"openers": [{"pid": 9999}]},
            },
        ),
    ):
        try:
            hosted_runtime._restart_finance_cutover_http_service(
                active_target
            )
        except RuntimeError as exc:
            if "MainPID is not bound" not in str(exc):
                raise
        else:
            raise AssertionError(
                "Finance HTTP restart accepted a stale storage binding"
            )
    maintenance_probe = {
        "route": "web_vitrina_group_refresh_missing_group",
        "method": "POST",
        "url": (
            "http://127.0.0.1:8765"
            + hosted_runtime.DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH
        ),
        "http_status": 423,
        "content_type": "application/json; charset=utf-8",
        "body_excerpt": "",
        "body_truncated": False,
        "body_bytes_read": 0,
        "json_body": {
            "contract_name": "wb_core_business_data_write_barrier_v1",
            "status": "blocked",
            "active": True,
            "phase": "acquiring",
            "window_id": "snapshot-probe-smoke",
            "code": "business_data_maintenance",
            "retryable": True,
            "attempt_audited": True,
        },
        "network_error": None,
    }
    maintenance_evaluation = hosted_runtime._evaluate_route_result(
        maintenance_probe,
        route_paths=active_target.route_paths,
    )
    if maintenance_evaluation["ok"] is not True:
        raise AssertionError(
            "exact audited maintenance 423 must be a healthy POST probe "
            "result"
        )
    unaudited_probe = {
        **maintenance_probe,
        "json_body": {
            **maintenance_probe["json_body"],
            "attempt_audited": False,
        },
    }
    if hosted_runtime._evaluate_route_result(
        unaudited_probe,
        route_paths=active_target.route_paths,
    )["ok"] is not False:
        raise AssertionError(
            "incomplete maintenance 423 evidence must fail deploy verification"
        )
    with TemporaryDirectory(
        prefix="vitrina-incident-hosted-smoke-"
    ) as incident_temp_dir:
        incident_plan = {
            "contract_name": "vitrina_incident_rematerialization",
            "contract_version": 1,
            "mode": "dry_run",
            "date_from_requested": "2026-07-25",
            "date_to": "2026-07-25",
            "max_dates": 14,
            "apply_allowed": True,
            "fingerprint": "sha256:vitrina-incident-reviewed",
        }
        incident_plan_path = Path(incident_temp_dir) / "plan.json"
        incident_plan_path.write_text(
            json.dumps(incident_plan),
            encoding="utf-8",
        )
        for action in ("dry-run", "apply"):
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    incident_plan
                    if action == "dry-run"
                    else {
                        "status": "applied",
                        "readback_status": "ok",
                        "readback_changed_cells": 0,
                    }
                ),
                stderr="",
            )
            with mock.patch.object(
                hosted_runtime.subprocess,
                "run",
                return_value=completed,
            ) as run_mock:
                hosted_runtime._run_remote_vitrina_incident_rematerialization(
                    active_target,
                    action=action,
                    date_from="2026-07-25",
                    date_to="2026-07-25",
                    max_dates=14,
                    plan_path=incident_plan_path if action == "apply" else None,
                    fingerprint=(
                        "sha256:vitrina-incident-reviewed"
                        if action == "apply"
                        else ""
                    ),
                    approval_reference=(
                        "human-gate-vitrina-incident"
                        if action == "apply"
                        else ""
                    ),
                    actor="smoke" if action == "apply" else "",
                )
            if (
                run_mock.call_args.kwargs.get("timeout")
                != hosted_runtime.VITRINA_INCIDENT_REMATERIALIZATION_TIMEOUT_SECONDS
            ):
                raise AssertionError(
                    "Vitrina incident hosted runner lost its bounded timeout"
                )
            remote_command = " ".join(run_mock.call_args.args[0])
            if "apps/vitrina_incident_rematerialization.py" not in remote_command:
                raise AssertionError(
                    "Vitrina incident hosted action bypassed the repo-owned runner"
                )
            if (
                "--seller-id" not in remote_command
                or str(
                    active_target.runtime_env.get(
                        "SELLER_PORTAL_CANONICAL_SUPPLIER_ID"
                    )
                    or ""
                )
                not in remote_command
            ):
                raise AssertionError(
                    "Vitrina incident hosted action lost the target-owned seller identity"
                )
            if action == "apply":
                if (
                    "--reviewed-plan-stdin" not in remote_command
                    or run_mock.call_args.kwargs.get("input")
                    != incident_plan_path.read_text(encoding="utf-8")
                ):
                    raise AssertionError(
                        "Vitrina incident apply lost the exact reviewed stdin plan"
                    )
            elif "--stdout-plan" not in remote_command:
                raise AssertionError(
                    "Vitrina incident dry-run must stream the exact reviewed plan"
                )

    with TemporaryDirectory(prefix="finance-daily-hosted-smoke-") as finance_temp_dir:
        finance_plan = {
            "contract_name": "finance_daily_historical_recovery",
            "contract_version": 1,
            "mode": "recovery",
            "target_date": "2026-08-26",
            "expected_sku_count": 33,
            "expected_target_cells": 171,
            "apply_allowed": True,
            "fingerprint": "sha256:finance-daily-reviewed",
        }
        finance_plan_path = Path(finance_temp_dir) / "plan.json"
        finance_plan_path.write_text(json.dumps(finance_plan), encoding="utf-8")
        for action in ("parity", "plan", "apply", "readback"):
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    finance_plan
                    if action in {"parity", "plan"}
                    else {
                        "status": "complete" if action == "readback" else "applied",
                        "accepted_cells": "171/171",
                    }
                ),
                stderr="",
            )
            with mock.patch.object(hosted_runtime.subprocess, "run", return_value=completed) as run_mock:
                hosted_runtime._run_remote_finance_daily_recovery(
                    active_target,
                    action=action,
                    target_date=(
                        "2026-08-24" if action == "parity" else (
                            "" if action == "readback" else "2026-08-26"
                        )
                    ),
                    operation_id="wbc0020-finance-smoke" if action == "readback" else "",
                    plan_path=finance_plan_path if action == "apply" else None,
                    fingerprint="sha256:finance-daily-reviewed" if action == "apply" else "",
                    approval_reference="WBC0020 owner accepted" if action == "apply" else "",
                    actor="smoke" if action == "apply" else "",
                )
            if run_mock.call_args.kwargs.get("timeout") != hosted_runtime.FINANCE_DAILY_RECOVERY_TIMEOUT_SECONDS:
                raise AssertionError("Finance daily hosted runner lost its bounded timeout")
            remote_command = " ".join(run_mock.call_args.args[0])
            if "apps/finance_daily_historical_recovery.py" not in remote_command:
                raise AssertionError("Finance daily hosted action bypassed the repo-owned runner")
            if ".wb-core-runtime-sha" not in remote_command or "/opt/wb-ai/.env" not in remote_command:
                raise AssertionError("Finance daily hosted action lost exact runtime/env binding")
            if action == "apply":
                if (
                    "--reviewed-plan-stdin" not in remote_command
                    or run_mock.call_args.kwargs.get("input")
                    != finance_plan_path.read_text(encoding="utf-8")
                ):
                    raise AssertionError(
                        "Finance daily apply lost the exact reviewed stdin plan"
                    )
            elif action in {"parity", "plan"} and "--stdout-plan" not in remote_command:
                raise AssertionError(
                    "Finance daily plan/parity must stream the exact reviewed plan"
                )
    with TemporaryDirectory(prefix="finance-canonical-hosted-smoke-") as finance_temp_dir:
        finance_plan_path = Path(finance_temp_dir) / "plan.json"
        finance_plan_path.write_text(
            json.dumps(
                {
                    "fingerprint": "sha256:finance-reviewed",
                    "schema_version": "wb_finance_canonical_cost_backfill_v3",
                    "dry_run": True,
                    "apply_allowed": True,
                    "date_from": "2026-04-27",
                    "date_to": "2026-07-19",
                }
            ),
            encoding="utf-8",
        )
        for action, expected_timeout in (
            ("dry-run", hosted_runtime.FINANCE_CANONICAL_READ_TIMEOUT_SECONDS),
            ("readback", hosted_runtime.FINANCE_CANONICAL_READ_TIMEOUT_SECONDS),
            ("apply", hosted_runtime.FINANCE_CANONICAL_MUTATION_TIMEOUT_SECONDS),
        ):
            operation_id = {
                "dry-run": "a" * 24,
                "readback": "b" * 24,
                "apply": "c" * 24,
            }[action]
            deployed_sha = "1" * 40
            sha_readback = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=deployed_sha + "\n",
                stderr="",
            )
            started = subprocess.CompletedProcess(
                args=[],
                returncode=255 if action == "dry-run" else 0,
                stdout="" if action == "dry-run" else "started\n",
                stderr=(
                    "Connection reset by peer"
                    if action == "dry-run"
                    else ""
                ),
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    'complete\n0\n{"blockers":[],"weeks":[]}'
                    if action == "readback"
                    else 'complete\n0\n{"status":"dry_run"}'
                ),
                stderr="",
            )
            with mock.patch.object(
                hosted_runtime.subprocess,
                "run",
                side_effect=[sha_readback, started, completed],
            ) as run_mock:
                payload = hosted_runtime._run_remote_finance_canonical_action(
                    active_target,
                    action=action,
                    plan_path=finance_plan_path if action == "apply" else None,
                    fingerprint="sha256:finance-reviewed" if action == "apply" else "",
                    approval_reference="human-gate-123" if action == "apply" else "",
                    operation_id=operation_id,
                )
            if run_mock.call_count != 3:
                raise AssertionError(
                    f"Finance canonical {action} did not use deployed-SHA preflight, "
                    "one start and exact status readback"
                )
            sha_call, start_call, status_call = run_mock.call_args_list
            if any(
                call.kwargs.get("timeout") != 60.0
                for call in (sha_call, start_call, status_call)
            ):
                raise AssertionError("Finance canonical transport calls lost their bounded timeout")
            remote_command = " ".join(start_call.args[0])
            status_command = " ".join(status_call.args[0])
            for shell_snippet in (
                start_call.args[0][-1],
                status_call.args[0][-1],
            ):
                syntax = subprocess.run(
                    ["sh", "-n"],
                    input=shell_snippet,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if syntax.returncode != 0:
                    raise AssertionError(
                        f"Finance canonical {action} emitted invalid remote shell: "
                        f"{syntax.stderr}"
                    )
            if "canonical-cost-backfill" not in remote_command:
                raise AssertionError("Finance canonical command bypassed the repo-owned runner")
            if (
                f"timeout --signal=TERM --kill-after=30s {int(expected_timeout)}s"
                not in remote_command
            ):
                raise AssertionError(
                    f"Finance canonical {action} lost its remote process timeout"
                )
            if (
                operation_id not in remote_command
                or deployed_sha not in remote_command
                or "nohup sh -c" not in remote_command
                or ".finance-canonical-operations" not in remote_command
                or operation_id not in status_command
                or "exit_code" not in status_command
                or "request.sha256" not in status_command
                or "request-mismatch" not in status_command
            ):
                raise AssertionError(
                    f"Finance canonical {action} lost durable exact-operation recovery"
                )
            if action == "apply" and not all(
                token in remote_command
                for token in (
                    "--apply",
                    "--confirm-fingerprint",
                    "sha256:finance-reviewed",
                    "--date-from",
                    "2026-04-27",
                    "--date-to",
                    "2026-07-19",
                    "--approval-reference",
                    "human-gate-123",
                )
            ):
                raise AssertionError(
                    "Finance canonical apply lost fingerprint, fixed cutoff, or human gate"
                )
            if action != "apply" and "--apply" in remote_command:
                raise AssertionError("Finance canonical read-only command unexpectedly enables mutation")
            if action == "readback" and not payload.get("readback"):
                raise AssertionError("Finance canonical readback did not prove zero deltas")
        dry_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-canonical-dry-run",
                "--output",
                str(Path(finance_temp_dir) / "review.json"),
                "--operation-id",
                "d" * 24,
            ]
        )
        apply_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-canonical-apply",
                "--plan-file",
                str(finance_plan_path),
                "--fingerprint",
                "sha256:finance-reviewed",
                "--approval-reference",
                "human-gate-123",
            ]
        )
        if (
            dry_args.handler is not hosted_runtime.run_finance_canonical_command
            or dry_args.finance_canonical_action != "dry-run"
            or dry_args.operation_id != "d" * 24
            or apply_args.finance_canonical_action != "apply"
        ):
            raise AssertionError("hosted runner must expose bounded Finance canonical commands")
        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_canonical_action",
                return_value={"fingerprint": "sha256:finance-reviewed", "status": "dry_run"},
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_finance_canonical_command(dry_args)
        finance_evidence_path = Path(dry_args.output)
        if (
            not finance_evidence_path.is_file()
            or finance_evidence_path.stat().st_mode & 0o777 != 0o600
        ):
            raise AssertionError("Finance canonical reviewed evidence must be written with mode 0600")
        command_choices = hosted_runtime.build_arg_parser()._subparsers._group_actions[0].choices
        if any(name.startswith("finance-retro-") for name in command_choices):
            raise AssertionError("revoked hosted Finance retro commands remain executable")
        storage_plan_path = Path(finance_temp_dir) / "finance-storage-plan.json"
        storage_plan_path.write_text(
            json.dumps(
                {
                    "contract_version": "wb_core_finance_storage_split_plan_v1",
                    "mode": "dry_run",
                    "fingerprint": "sha256:storage-reviewed",
                    "apply_allowed_by_machine_preflight": True,
                }
            ),
            encoding="utf-8",
        )
        storage_payloads = {
            "dry-run": {
                "contract_version": "wb_core_finance_storage_split_plan_v1",
                "query_only_contract": {
                    "production_mutation_count": 0,
                    "destination_bytes_created": 0,
                },
                "fingerprint": "sha256:storage-reviewed",
            },
            "health": {
                "contract_version": "wb_core_storage_generation_manifest_v1",
                "canonical_source": "monolith",
            },
            "apply": {
                "contract_version": "wb_core_finance_storage_split_candidate_v1",
                "global_manifest_switched": False,
                "canonical_source": "monolith",
            },
        }
        lease_now = time.time()
        deploy_lease = {
            "contract_version": (
                "wb_core_finance_migration_deploy_lease_readback_v1"
            ),
            "policy": "finance_migration_global_deploy_hold_v1",
            "status": "active",
            "allows_finance_migration": True,
            "global_release_blocked": True,
            "observed_at": datetime.fromtimestamp(
                lease_now,
                tz=timezone.utc,
            ).isoformat().replace("+00:00", "Z"),
            "ambiguous_reasons": [],
            "lease": {
                "lease_id": "finance-split-fixture",
                "task_id": "finance-storage-smoke",
                "anchor_pr": 850,
                "head_sha": "b" * 40,
                "deployed_sha": "a" * 40,
                "window_id": "smoke-window",
                "phase": "pre-snapshot",
                "revision": 1,
                "acquired_at": datetime.fromtimestamp(
                    lease_now - 60,
                    tz=timezone.utc,
                ).isoformat().replace("+00:00", "Z"),
                "expires_at": datetime.fromtimestamp(
                    lease_now + 3600,
                    tz=timezone.utc,
                ).isoformat().replace("+00:00", "Z"),
                "baseline_invalidation_epoch": (
                    finance_lease_invalidation_epoch(
                        anchor_pr=850,
                        deployed_sha="a" * 40,
                        lease_id="finance-split-fixture",
                        revision=1,
                        task_id="finance-storage-smoke",
                    )
                ),
                "recovery_policy": "owner_bound_recovery_rebind_required_v1",
            },
        }
        deploy_lease["fingerprint"] = finance_lease_fingerprint(
            deploy_lease
        )
        deploy_lease_path = Path(finance_temp_dir) / "finance-deploy-lease.json"
        deploy_lease_path.write_text(
            json.dumps(deploy_lease),
            encoding="utf-8",
        )
        for action, expected_timeout in (
            ("dry-run", hosted_runtime.FINANCE_STORAGE_SPLIT_READ_TIMEOUT_SECONDS),
            ("health", hosted_runtime.FINANCE_STORAGE_SPLIT_READ_TIMEOUT_SECONDS),
            ("apply", hosted_runtime.FINANCE_STORAGE_SPLIT_MUTATION_TIMEOUT_SECONDS),
        ):
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(storage_payloads[action]),
                stderr="",
            )
            with mock.patch.object(
                hosted_runtime.subprocess,
                "run",
                return_value=completed,
            ) as run_mock:
                hosted_runtime._run_remote_finance_storage_split_action(
                    active_target,
                    action=action,
                    plan_path=storage_plan_path if action == "apply" else None,
                    fingerprint=(
                        "sha256:storage-reviewed" if action == "apply" else ""
                    ),
                    approval_reference=(
                        "human-gate-storage-123" if action == "apply" else ""
                    ),
                    chunk_size=100_000,
                    source_snapshot_manifest=(
                        "/opt/wb-core-runtime/state/finance-storage-split-snapshots/"
                        "fixture/snapshot_manifest.json"
                        if action in {"dry-run", "apply"}
                        else ""
                    ),
                    deploy_lease=(
                        deploy_lease if action != "health" else None
                    ),
                )
            if run_mock.call_args.kwargs.get("timeout") != expected_timeout:
                raise AssertionError(
                    f"Finance storage {action} lost its bounded timeout"
                )
            remote_command = " ".join(run_mock.call_args.args[0])
            if "apps/finance_storage_split.py" not in remote_command:
                raise AssertionError(
                    "Finance storage command bypassed the repo-owned runner"
                )
            if (
                "--generation-filesystem-contract-json"
                not in remote_command
                or "284b3362-b890-431d-a7da-7f0fcd2ee0a6"
                not in remote_command
                or "wb-finance-gen" not in remote_command
            ):
                raise AssertionError(
                    "Finance storage command lost the exact generation "
                    "filesystem identity"
                )
            if action != "health" and "--deploy-lease-json" not in remote_command:
                raise AssertionError(
                    "Finance storage command lost its global deploy-lease binding"
                )
            if action == "apply":
                for token in (
                    "--migration-plan-file",
                    "/dev/stdin",
                    "--confirm-fingerprint",
                    "sha256:storage-reviewed",
                    "--approval-reference",
                    "human-gate-storage-123",
                ):
                    if token not in remote_command:
                        raise AssertionError(
                            f"Finance storage apply lost {token}"
                        )
                if (
                    run_mock.call_args.kwargs.get("input")
                    != storage_plan_path.read_text(encoding="utf-8")
                ):
                    raise AssertionError(
                        "Finance storage apply did not stream the exact reviewed plan"
                    )
            elif "--confirm-fingerprint" in remote_command:
                raise AssertionError(
                    "Finance storage read-only command unexpectedly enables apply"
                )
        recovery_contract_payload = {
            "contract_version": (
                "wb_core_finance_storage_recovery_contract_v1"
            ),
            "status": "ready",
            "deployed_sha": "a" * 40,
            "fail_closed_default": True,
            "second_restore_job_allowed": False,
            "fingerprint": "sha256:" + ("4" * 64),
        }
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(recovery_contract_payload),
                stderr="",
            ),
        ) as recovery_contract_run:
            hosted_runtime._run_remote_finance_storage_split_action(
                active_target,
                action="recovery-contract",
                plan_path=None,
                fingerprint="",
                approval_reference="",
                chunk_size=10_000,
            )
        recovery_contract_command = " ".join(
            recovery_contract_run.call_args.args[0]
        )
        if (
            " recovery-contract " not in recovery_contract_command
            or "--deploy-lease-json" in recovery_contract_command
            or "--generation-filesystem-contract-json"
            not in recovery_contract_command
        ):
            raise AssertionError(
                "Finance recovery contract must be query-only and "
                "lease-independent"
            )
        recovery_preflight_payload = {
            "status": "ready",
            "action": "apply",
            "phase": "pre_barrier",
            "fail_closed": True,
        }
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(recovery_preflight_payload),
                stderr="",
            ),
        ) as recovery_preflight_run:
            hosted_runtime._run_remote_finance_storage_split_action(
                active_target,
                action="recovery-preflight",
                recovery_action="apply",
                plan_path=storage_plan_path,
                fingerprint="sha256:storage-reviewed",
                approval_reference="human-gate-storage-123",
                chunk_size=10_000,
                source_snapshot_manifest=(
                    "/opt/wb-core-runtime/state/finance-storage-split-"
                    "snapshots/fixture/snapshot_manifest.json"
                ),
                deploy_lease=deploy_lease,
            )
        recovery_preflight_command = " ".join(
            recovery_preflight_run.call_args.args[0]
        )
        for token in (
            " recovery-preflight ",
            "--recovery-action",
            "apply",
            "--migration-plan-file",
            "/dev/stdin",
            "--deploy-lease-json",
            "--confirm-fingerprint",
            "--approval-reference",
        ):
            if token not in recovery_preflight_command:
                raise AssertionError(
                    f"Finance recovery preflight lost {token}"
                )
        if (
            recovery_preflight_run.call_args.kwargs.get("input")
            != storage_plan_path.read_text(encoding="utf-8")
        ):
            raise AssertionError(
                "Finance recovery preflight did not stream the exact candidate plan"
            )
        recovery_contract_args = (
            hosted_runtime.build_arg_parser().parse_args(
                ["finance-storage-recovery-contract"]
            )
        )
        if (
            recovery_contract_args.handler
            is not hosted_runtime.run_finance_storage_split_command
            or recovery_contract_args.finance_storage_split_action
            != "recovery-contract"
        ):
            raise AssertionError(
                "hosted runner must expose Finance recovery contract readback"
            )
        post_manifest_args = (
            hosted_runtime.build_arg_parser().parse_args(
                [
                    "finance-storage-post-manifest-recovery-readback",
                    "--expected-retained-generation",
                    "1" * 20,
                    "--output",
                    str(
                        Path(finance_temp_dir)
                        / "post-manifest-recovery.json"
                    ),
                    "--finance-deploy-lease-evidence",
                    str(deploy_lease_path),
                ]
            )
        )
        if (
            post_manifest_args.handler
            is not hosted_runtime.run_finance_storage_split_command
            or post_manifest_args.finance_storage_split_action
            != "post-manifest-recovery-readback"
        ):
            raise AssertionError(
                "hosted runner must expose post-manifest recovery readback"
            )
        storage_dry_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-storage-split-dry-run",
                "--output",
                str(Path(finance_temp_dir) / "finance-storage-review.json"),
                "--source-snapshot-manifest",
                (
                    "/opt/wb-core-runtime/state/finance-storage-split-snapshots/"
                    "fixture/snapshot_manifest.json"
                ),
                "--finance-deploy-lease-evidence",
                str(deploy_lease_path),
            ]
        )
        storage_apply_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-storage-split-apply",
                "--plan-file",
                str(storage_plan_path),
                "--fingerprint",
                "sha256:storage-reviewed",
                "--approval-reference",
                "human-gate-storage-123",
                "--source-snapshot-manifest",
                (
                    "/opt/wb-core-runtime/state/finance-storage-split-snapshots/"
                    "fixture/snapshot_manifest.json"
                ),
                "--finance-deploy-lease-evidence",
                str(deploy_lease_path),
            ]
        )
        if (
            storage_dry_args.handler
            is not hosted_runtime.run_finance_storage_split_command
            or storage_dry_args.finance_storage_split_action != "dry-run"
            or storage_apply_args.finance_storage_split_action != "apply"
        ):
            raise AssertionError(
                "hosted runner must expose gated Finance storage commands"
            )
        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                return_value=storage_payloads["dry-run"],
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_finance_storage_split_command(storage_dry_args)
        storage_evidence_path = Path(storage_dry_args.output)
        if (
            not storage_evidence_path.is_file()
            or storage_evidence_path.stat().st_mode & 0o777 != 0o600
        ):
            raise AssertionError(
                "Finance storage reviewed evidence must be written mode 0600"
            )
        retention_fingerprint = "sha256:" + ("9" * 64)
        retention_plan_path = (
            Path(finance_temp_dir) / "snapshot-retention-plan.json"
        )
        retention_plan = {
            "contract_version": (
                "wb_core_finance_storage_snapshot_retention_plan_v1"
            ),
            "mode": "snapshot_retention_dry_run",
            "fingerprint": retention_fingerprint,
            "apply_allowed_by_machine_preflight": True,
            "blockers": [],
            "query_only_contract": {
                "business_data_mutation_count": 0,
                "snapshot_byte_mutation_count": 0,
                "archive_byte_mutation_count": 0,
            },
        }
        retention_plan_path.write_text(
            json.dumps(retention_plan),
            encoding="utf-8",
        )
        retention_payloads = {
            "snapshot-retention-plan": retention_plan,
            "snapshot-retention-apply": {
                "contract_version": (
                    "wb_core_finance_storage_snapshot_retention_result_v1"
                ),
                "status": "completed",
                "archived_snapshot_count": 3,
                "live_monolith_touched": False,
                "split_generation_touched": False,
            },
            "snapshot-retention-readback": {
                "contract_version": (
                    "wb_core_finance_storage_snapshot_retention_result_v1"
                ),
                "status": "readback_verified",
                "capacity_sufficient": True,
                "live_monolith_touched": False,
                "split_generation_touched": False,
            },
        }
        for action in (
            "snapshot-retention-plan",
            "snapshot-retention-apply",
            "snapshot-retention-readback",
        ):
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(retention_payloads[action]),
                stderr="",
            )
            with mock.patch.object(
                hosted_runtime.subprocess,
                "run",
                return_value=completed,
            ) as run_mock, mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_transport_job",
                return_value=retention_payloads[action],
            ) as transport_mock:
                hosted_runtime._run_remote_finance_storage_split_action(
                    active_target,
                    action=action,
                    plan_path=(
                        retention_plan_path
                        if action != "snapshot-retention-plan"
                        else None
                    ),
                    fingerprint=(
                        retention_fingerprint
                        if action != "snapshot-retention-plan"
                        else ""
                    ),
                    approval_reference=(
                        "canonical-finance-retention-smoke"
                        if action == "snapshot-retention-apply"
                        else ""
                    ),
                    chunk_size=10_000,
                    deploy_lease=deploy_lease,
                )
            if action == "snapshot-retention-apply":
                if transport_mock.call_count != 1:
                    raise AssertionError(
                        "Finance snapshot retention apply lost durable transport"
                    )
                remote_command = " ".join(
                    transport_mock.call_args.kwargs["runner_args"]
                )
            else:
                remote_command = " ".join(run_mock.call_args.args[0])
            for token in (
                "apps/finance_storage_split.py",
                "--deploy-lease-json",
            ):
                if token not in remote_command:
                    raise AssertionError(
                        f"Finance snapshot retention {action} lost {token}"
                    )
            if action != "snapshot-retention-plan":
                for token in (
                    "--snapshot-retention-plan-file",
                    "/dev/stdin",
                    "--confirm-fingerprint",
                    retention_fingerprint,
                ):
                    if token not in remote_command:
                        raise AssertionError(
                            "Finance snapshot retention "
                            f"{action} lost {token}"
                        )
            if action == "snapshot-retention-apply" and (
                "--approval-reference" not in remote_command
                or "canonical-finance-retention-smoke"
                not in remote_command
            ):
                raise AssertionError(
                    "Finance snapshot retention apply lost exact approval"
                )
        retention_plan_args = (
            hosted_runtime.build_arg_parser().parse_args(
                [
                    "finance-storage-snapshot-retention-plan",
                    "--output",
                    str(
                        Path(finance_temp_dir)
                        / "snapshot-retention-output.json"
                    ),
                    "--finance-deploy-lease-evidence",
                    str(deploy_lease_path),
                ]
            )
        )
        retention_apply_args = (
            hosted_runtime.build_arg_parser().parse_args(
                [
                    "finance-storage-snapshot-retention-apply",
                    "--plan-file",
                    str(retention_plan_path),
                    "--fingerprint",
                    retention_fingerprint,
                    "--approval-reference",
                    "canonical-finance-retention-smoke",
                    "--finance-deploy-lease-evidence",
                    str(deploy_lease_path),
                ]
            )
        )
        retention_readback_args = (
            hosted_runtime.build_arg_parser().parse_args(
                [
                    "finance-storage-snapshot-retention-readback",
                    "--plan-file",
                    str(retention_plan_path),
                    "--fingerprint",
                    retention_fingerprint,
                    "--output",
                    str(
                        Path(finance_temp_dir)
                        / "snapshot-retention-readback.json"
                    ),
                    "--finance-deploy-lease-evidence",
                    str(deploy_lease_path),
                ]
            )
        )
        if (
            retention_plan_args.finance_storage_split_action
            != "snapshot-retention-plan"
            or retention_apply_args.finance_storage_split_action
            != "snapshot-retention-apply"
            or retention_readback_args.finance_storage_split_action
            != "snapshot-retention-readback"
        ):
            raise AssertionError(
                "hosted runner must expose Finance snapshot retention "
                "plan/apply/readback"
            )
        stale_writer_fingerprint = "sha256:" + ("5" * 64)
        stale_writer_plan_path = (
            Path(finance_temp_dir) / "stale-writer-plan.json"
        )
        stale_writer_plan = {
            "contract_version": (
                "wb_core_finance_storage_stale_writer_recovery_plan_v1"
            ),
            "mode": "stale_writer_recovery_dry_run",
            "fingerprint": stale_writer_fingerprint,
            "stop_allowed_by_machine_preflight": True,
            "action": {
                "business_data_mutation_count": 0,
                "finance_storage_mutation_count": 0,
            },
        }
        stale_writer_plan_path.write_text(
            json.dumps(stale_writer_plan),
            encoding="utf-8",
        )
        stale_plan_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-storage-stale-writer-plan",
                "--output",
                str(
                    Path(finance_temp_dir)
                    / "stale-writer-plan-output.json"
                ),
                "--finance-deploy-lease-evidence",
                str(deploy_lease_path),
            ]
        )
        stale_stop_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-storage-stale-writer-stop",
                "--plan-file",
                str(stale_writer_plan_path),
                "--fingerprint",
                stale_writer_fingerprint,
                "--approval-reference",
                "canonical-finance-task-stale-writer-recovery",
                "--finance-deploy-lease-evidence",
                str(deploy_lease_path),
            ]
        )
        if (
            stale_plan_args.finance_storage_split_action
            != "stale-writer-plan"
            or stale_stop_args.finance_storage_split_action
            != "stale-writer-stop"
        ):
            raise AssertionError(
                "hosted runner must expose stale-writer plan/stop"
            )
        stale_payloads = {
            "stale-writer-plan": stale_writer_plan,
            "stale-writer-stop": {
                "contract_version": (
                    "wb_core_finance_storage_stale_writer_recovery_result_v1"
                ),
                "status": "stopped",
                "fingerprint": stale_writer_fingerprint,
                "stop_count": 1,
            },
        }
        for action in ("stale-writer-plan", "stale-writer-stop"):
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(stale_payloads[action]),
                stderr="",
            )
            with mock.patch.object(
                hosted_runtime.subprocess,
                "run",
                return_value=completed,
            ) as run_mock:
                hosted_runtime._run_remote_finance_storage_split_action(
                    active_target,
                    action=action,
                    plan_path=(
                        stale_writer_plan_path
                        if action == "stale-writer-stop"
                        else None
                    ),
                    fingerprint=(
                        stale_writer_fingerprint
                        if action == "stale-writer-stop"
                        else ""
                    ),
                    approval_reference=(
                        "canonical-finance-task-stale-writer-recovery"
                        if action == "stale-writer-stop"
                        else ""
                    ),
                    chunk_size=10_000,
                    deploy_lease=deploy_lease,
                )
            expected_timeout = (
                hosted_runtime.FINANCE_STORAGE_SPLIT_MUTATION_TIMEOUT_SECONDS
                if action == "stale-writer-stop"
                else hosted_runtime.FINANCE_STORAGE_SPLIT_READ_TIMEOUT_SECONDS
            )
            if run_mock.call_args.kwargs.get("timeout") != expected_timeout:
                raise AssertionError(
                    f"stale-writer {action} lost bounded timeout"
                )
            remote_command = " ".join(run_mock.call_args.args[0])
            if "--deploy-lease-json" not in remote_command:
                raise AssertionError(
                    f"stale-writer {action} lost deploy-lease binding"
                )
            if action == "stale-writer-stop":
                for token in (
                    "--stale-writer-plan-file",
                    "/dev/stdin",
                    "--confirm-fingerprint",
                    stale_writer_fingerprint,
                    "--approval-reference",
                ):
                    if token not in remote_command:
                        raise AssertionError(
                            f"stale-writer stop lost {token}"
                        )
                if run_mock.call_args.kwargs.get("input") != (
                    stale_writer_plan_path.read_text(encoding="utf-8")
                ):
                    raise AssertionError(
                        "stale-writer reviewed plan was not streamed exactly"
                    )
            elif "--confirm-fingerprint" in remote_command:
                raise AssertionError(
                    "stale-writer plan unexpectedly enabled mutation"
                )
        snapshot_plan_path = Path(finance_temp_dir) / "snapshot-plan.json"
        snapshot_fingerprint = "sha256:" + ("9" * 64)
        snapshot_plan_path.write_text(
            json.dumps(
                {
                    "contract_version": (
                        "wb_core_finance_storage_snapshot_plan_v1"
                    ),
                    "mode": "snapshot_dry_run",
                    "fingerprint": snapshot_fingerprint,
                    "snapshot_allowed_by_machine_preflight": True,
                    "target_snapshot": {
                        "window_id": "snapshot-window-smoke-001",
                    },
                }
            ),
            encoding="utf-8",
        )
        snapshot_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-storage-snapshot-apply",
                "--plan-file",
                str(snapshot_plan_path),
                "--fingerprint",
                snapshot_fingerprint,
                "--approval-reference",
                "program-authorization-smoke",
                "--finance-deploy-lease-evidence",
                str(deploy_lease_path),
            ]
        )
        maintenance_actions: list[str] = []
        continuity_fingerprint = "sha256:" + ("c" * 64)

        def maintenance_result(
            _target: object,
            *,
            action: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            maintenance_actions.append(action)
            if action == "hold":
                return {
                    "status": "held",
                    "quiet": True,
                    "auto_updates": {"revision": 19},
                }
            if action == "restore-continuity-status":
                return {
                    "status": "ready",
                    "maintenance": {
                        "status": "quiet",
                        "quiet": True,
                        "auto_updates": {"revision": 19},
                    },
                    "service_continuity": {
                        "fingerprint": continuity_fingerprint,
                    },
                }
            if action == "restore":
                return {
                    "status": "restored",
                    "exact_prior_state_restored": True,
                }
            if action == "status":
                return {
                    "status": "not_quiet",
                    "auto_updates": {
                        "master_desired": True,
                        "revision": 20,
                        "unknown_processes": [],
                        "drift_processes": [],
                    },
                    "unknown_wb_core_timers": [],
                    "writer_locks": {},
                }
            if action == "barrier-release":
                return {"status": "inactive", "active": False}
            return {"status": "active", "active": True}

        def finance_snapshot_result(
            _target: object,
            *,
            action: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            if action == "recovery-preflight":
                return {
                    "status": "ready",
                    "phase": "pre_barrier",
                    "action": "snapshot-create",
                    "fail_closed": True,
                    "boundary_classification": "fresh_acquire",
                }
            if action in {"snapshot-create", "snapshot-status"}:
                return {
                    "status": "captured_unverified",
                    "snapshot_manifest_path": (
                        "/opt/wb-core-runtime/state/finance-storage-split-"
                        "snapshots/fixture/snapshot_manifest.json"
                    ),
                }
            raise AssertionError(f"unexpected Finance action: {action}")

        def durable_restore_result(
            _target: object,
            *,
            job_action: str,
            deployed_sha: str,
            job_id: str = "",
            **_kwargs: object,
        ) -> dict[str, object]:
            if job_action == "inventory":
                return {
                    "contract_name": (
                        "business_data_maintenance_restore_inventory_v1"
                    ),
                    "status": "ready",
                    "nonterminal_job_count": 0,
                    "locks_free": True,
                    "new_restore_submit_allowed": True,
                }
            if job_action == "status":
                return {
                    "contract_name": (
                        "business_data_maintenance_restore_job_v1"
                    ),
                    "job_id": job_id,
                    "deployed_sha": deployed_sha,
                    "status": "absent",
                    "terminal": False,
                    "worker_observation": {
                        "classification": "job_absent",
                    },
                }
            if job_action == "submit":
                return {
                    "contract_name": (
                        "business_data_maintenance_restore_job_v1"
                    ),
                    "job_id": job_id,
                    "status": "succeeded",
                    "terminal": True,
                    "request": {"job_id": job_id},
                    "deployment_binding": {
                        "deployed_sha": deployed_sha,
                    },
                    "result": {
                        "status": "restored",
                        "readback": {
                            "maintenance_phase": "restored",
                            "exact_prior_state_restored": True,
                            "master_desired": True,
                            "policy_revision": 20,
                            "barrier_active": True,
                            "barrier_phase": "restoring",
                        },
                    },
                    "worker_observation": {
                        "classification": "terminal_succeeded",
                    },
                }
            raise AssertionError(
                f"unexpected durable restore action: {job_action}"
            )

        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_runner",
                side_effect=maintenance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_warehouse_functional_maintenance_action",
                return_value={"status": "held"},
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                side_effect=finance_snapshot_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_restore_job",
                side_effect=durable_restore_result,
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_finance_storage_split_command(snapshot_args)
        if maintenance_actions != [
            "barrier-acquire",
            "prepare",
            "hold",
            "barrier-confirm",
            "barrier-restoring",
            "restore-continuity-status",
            "status",
            "barrier-release",
        ]:
            raise AssertionError(
                "coherent snapshot must automatically acquire, drain, restore "
                f"and release exact controls: {maintenance_actions}"
            )
        maintenance_actions.clear()
        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_runner",
                side_effect=maintenance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                side_effect=RuntimeError(
                    "synthetic recovery preflight failure"
                ),
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            try:
                hosted_runtime.run_finance_storage_split_command(
                    snapshot_args
                )
            except RuntimeError as exc:
                if "recovery preflight" not in str(exc):
                    raise
            else:
                raise AssertionError(
                    "failed Finance recovery preflight must propagate"
                )
        if maintenance_actions:
            raise AssertionError(
                "Finance recovery preflight must run before any barrier "
                f"or writer mutation: {maintenance_actions}"
            )

        def failed_snapshot_after_preflight(
            _target: object,
            *,
            action: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            if action == "recovery-preflight":
                return {
                    "status": "ready",
                    "phase": "pre_barrier",
                    "action": "snapshot-create",
                    "fail_closed": True,
                    "boundary_classification": "fresh_acquire",
                }
            if action == "snapshot-create":
                raise RuntimeError("synthetic snapshot failure")
            raise AssertionError(f"unexpected Finance action: {action}")

        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_runner",
                side_effect=maintenance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_warehouse_functional_maintenance_action",
                return_value={"status": "held"},
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                side_effect=failed_snapshot_after_preflight,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_restore_job",
                side_effect=durable_restore_result,
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            try:
                hosted_runtime.run_finance_storage_split_command(snapshot_args)
            except RuntimeError as exc:
                if "controls were restored" not in str(exc):
                    raise
            else:
                raise AssertionError("synthetic snapshot failure must propagate")
        if maintenance_actions[-4:] != [
            "barrier-restoring",
            "restore-continuity-status",
            "status",
            "barrier-release",
        ]:
            raise AssertionError(
                "snapshot failure must still exactly restore controls before "
                f"propagating: {maintenance_actions}"
            )

        maintenance_actions.clear()
        disconnected_finance_actions: list[str] = []

        def disconnected_finance_result(
            _target: object,
            *,
            action: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            disconnected_finance_actions.append(action)
            if action == "recovery-preflight":
                return {
                    "status": "ready",
                    "phase": "pre_barrier",
                    "action": "snapshot-create",
                    "fail_closed": True,
                    "boundary_classification": "fresh_acquire",
                }
            if action == "snapshot-create":
                return {
                    "status": "captured_unverified",
                    "snapshot_manifest_path": (
                        "/opt/wb-core-runtime/state/finance-storage-split-"
                        "snapshots/fixture/snapshot_manifest.json"
                    ),
                }
            raise AssertionError(f"unexpected Finance action: {action}")

        def disconnected_durable_result(
            _target: object,
            *,
            job_action: str,
            deployed_sha: str,
            job_id: str = "",
            **_kwargs: object,
        ) -> dict[str, object]:
            if job_action in {"inventory", "status"}:
                return durable_restore_result(
                    _target,
                    job_action=job_action,
                    deployed_sha=deployed_sha,
                    job_id=job_id,
                )
            if job_action == "submit":
                raise RuntimeError(
                    "synthetic SSH disconnect after durable systemd submit"
                )
            raise AssertionError(
                f"unexpected durable restore action: {job_action}"
            )

        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_runner",
                side_effect=maintenance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_warehouse_functional_maintenance_action",
                return_value={"status": "held"},
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                side_effect=disconnected_finance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_restore_job",
                side_effect=disconnected_durable_result,
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            try:
                hosted_runtime.run_finance_storage_split_command(
                    snapshot_args
                )
            except RuntimeError as exc:
                if "disconnect after durable systemd submit" not in str(exc):
                    raise
            else:
                raise AssertionError(
                    "synthetic submitting-client disconnect must propagate"
                )
        if maintenance_actions[-2:] != [
            "barrier-restoring",
            "restore-continuity-status",
        ]:
            raise AssertionError(
                "disconnect must leave the exact restoring boundary active"
            )
        if "barrier-release" in maintenance_actions:
            raise AssertionError(
                "disconnect before durable terminal readback must not release "
                "the barrier"
            )

        maintenance_actions.clear()
        resumed_finance_actions: list[str] = []
        resumed_durable_actions: list[str] = []

        def resumed_finance_result(
            _target: object,
            *,
            action: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            resumed_finance_actions.append(action)
            if action == "recovery-preflight":
                return {
                    "status": "ready",
                    "phase": "pre_barrier",
                    "action": "snapshot-create",
                    "fail_closed": True,
                    "boundary_classification": (
                        "exact_restore_release_resume"
                    ),
                }
            if action == "snapshot-status":
                return {
                    "status": "captured_unverified",
                    "snapshot_manifest_path": (
                        "/opt/wb-core-runtime/state/finance-storage-split-"
                        "snapshots/fixture/snapshot_manifest.json"
                    ),
                }
            raise AssertionError(f"unexpected Finance action: {action}")

        def resumed_durable_result(
            _target: object,
            *,
            job_action: str,
            deployed_sha: str,
            job_id: str = "",
            **_kwargs: object,
        ) -> dict[str, object]:
            resumed_durable_actions.append(job_action)
            if job_action == "inventory":
                return durable_restore_result(
                    _target,
                    job_action=job_action,
                    deployed_sha=deployed_sha,
                    job_id=job_id,
                )
            if job_action == "status":
                terminal = durable_restore_result(
                    _target,
                    job_action="submit",
                    deployed_sha=deployed_sha,
                    job_id=job_id,
                )
                terminal["result_record"] = {
                    "result_digest": "sha256:" + ("d" * 64),
                }
                terminal["audit"] = {
                    "events": [
                        "queued",
                        "worker_started",
                        "succeeded",
                    ],
                    "sha256": "sha256:" + ("e" * 64),
                }
                return terminal
            raise AssertionError(
                "outer resume must never submit a second restore job"
            )

        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_runner",
                side_effect=maintenance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_warehouse_functional_maintenance_action",
                return_value={"status": "restored"},
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                side_effect=resumed_finance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_restore_job",
                side_effect=resumed_durable_result,
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_finance_storage_split_command(snapshot_args)
        if resumed_finance_actions != [
            "recovery-preflight",
            "snapshot-status",
        ]:
            raise AssertionError(
                "outer resume must not replay snapshot mutation: "
                f"{resumed_finance_actions}"
            )
        if resumed_durable_actions != [
            "inventory",
            "status",
            "status",
            "inventory",
        ]:
            raise AssertionError(
                "outer resume must observe one exact durable job only: "
                f"{resumed_durable_actions}"
            )
        if maintenance_actions != ["status", "barrier-release"]:
            raise AssertionError(
                "outer resume must only release after terminal restore "
                f"readback: {maintenance_actions}"
            )

        cutover_plan_path = Path(finance_temp_dir) / "cutover-plan.json"
        cutover_fingerprint = "sha256:" + ("8" * 64)
        candidate_fingerprint = "sha256:" + ("7" * 64)
        candidate_manifest_path = (
            "/opt/wb-core-runtime/state/generations/fixture/"
            "candidate_manifest.json"
        )
        cutover_plan_path.write_text(
            json.dumps(
                {
                    "contract_version": (
                        "wb_core_finance_storage_cutover_plan_v1"
                    ),
                    "mode": "cutover_dry_run",
                    "fingerprint": cutover_fingerprint,
                    "candidate_plan_fingerprint": candidate_fingerprint,
                    "apply_allowed_by_machine_preflight": True,
                }
            ),
            encoding="utf-8",
        )
        cutover_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-storage-cutover-apply",
                "--plan-file",
                str(cutover_plan_path),
                "--fingerprint",
                cutover_fingerprint,
                "--approval-reference",
                "human-cutover-smoke",
                "--candidate-manifest",
                candidate_manifest_path,
                "--candidate-plan-fingerprint",
                candidate_fingerprint,
                "--output",
                str(Path(finance_temp_dir) / "cutover-evidence.json"),
                "--finance-deploy-lease-evidence",
                str(deploy_lease_path),
            ]
        )
        shadow_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-storage-shadow-activate",
                "--candidate-manifest",
                candidate_manifest_path,
                "--fingerprint",
                candidate_fingerprint,
                "--approval-reference",
                "human-cutover-smoke",
                "--finance-deploy-lease-evidence",
                str(deploy_lease_path),
            ]
        )
        if (
            cutover_args.finance_storage_split_action != "cutover-apply"
            or shadow_args.finance_storage_split_action != "shadow-activate"
        ):
            raise AssertionError(
                "hosted runner must expose shadow and cutover lifecycle"
            )
        maintenance_actions.clear()
        warehouse_actions: list[str] = []

        def warehouse_result(
            _target: object,
            *,
            action: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            warehouse_actions.append(action)
            return {"status": "restored" if action == "restore" else "held"}

        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_runner",
                side_effect=maintenance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_warehouse_functional_maintenance_action",
                side_effect=warehouse_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                return_value={
                    "status": "cutover_complete",
                    "global_manifest_switched": True,
                    "canonical_source": "split",
                },
            ),
            mock.patch.object(
                hosted_runtime,
                "_restart_finance_cutover_http_service",
                return_value={
                    "service": "wb-core-registry-http.service",
                    "status": "active",
                },
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_finance_storage_split_command(cutover_args)
        if maintenance_actions != [
            "barrier-acquire",
            "prepare",
            "hold",
            "barrier-confirm",
            "barrier-restoring",
            "restore",
            "barrier-release",
        ]:
            raise AssertionError(
                "cutover must hold, switch, restart, exactly restore and "
                f"release the barrier: {maintenance_actions}"
            )
        if warehouse_actions != ["hold", "restore"]:
            raise AssertionError(
                "cutover must exactly restore the warehouse timer boundary"
            )
        maintenance_actions.clear()
        warehouse_actions.clear()

        def failed_cutover(
            _target: object,
            *,
            action: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            if action == "recovery-preflight":
                return {
                    "status": "ready",
                    "phase": "pre_barrier",
                    "action": "cutover-apply",
                    "fail_closed": True,
                }
            if action == "cutover-apply":
                raise RuntimeError("synthetic pre-switch cutover failure")
            if action == "health":
                return {"canonical_source": "monolith"}
            raise AssertionError(f"unexpected action: {action}")

        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_runner",
                side_effect=maintenance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_warehouse_functional_maintenance_action",
                side_effect=warehouse_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                side_effect=failed_cutover,
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            try:
                hosted_runtime.run_finance_storage_split_command(
                    cutover_args
                )
            except RuntimeError as exc:
                if "exact controls were restored" not in str(exc):
                    raise
            else:
                raise AssertionError(
                    "pre-switch cutover failure must propagate"
                )
        if maintenance_actions[-3:] != [
            "barrier-restoring",
            "restore",
            "barrier-release",
        ]:
            raise AssertionError(
                "pre-switch cutover failure must exactly restore controls"
            )
        rollback_plan_path = Path(finance_temp_dir) / "rollback-plan.json"
        rollback_fingerprint = "sha256:" + ("6" * 64)
        rollback_plan_path.write_text(
            json.dumps(
                {
                    "contract_version": (
                        "wb_core_finance_storage_rollback_plan_v1"
                    ),
                    "mode": "rollback_dry_run",
                    "fingerprint": rollback_fingerprint,
                    "prepare_allowed_by_machine_preflight": True,
                    "apply_allowed_after_candidate_readback": True,
                }
            ),
            encoding="utf-8",
        )
        rollback_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "finance-storage-rollback-apply",
                "--plan-file",
                str(rollback_plan_path),
                "--fingerprint",
                rollback_fingerprint,
                "--approval-reference",
                "human-rollback-smoke",
                "--rollback-candidate-evidence",
                (
                    "/opt/wb-core-runtime/state/generations/"
                    "rollback-smoke/rollback_candidate.json"
                ),
                "--output",
                str(Path(finance_temp_dir) / "rollback-evidence.json"),
                "--finance-deploy-lease-evidence",
                str(deploy_lease_path),
            ]
        )
        maintenance_actions.clear()
        warehouse_actions.clear()
        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_business_data_maintenance_runner",
                side_effect=maintenance_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_warehouse_functional_maintenance_action",
                side_effect=warehouse_result,
            ),
            mock.patch.object(
                hosted_runtime,
                "_run_remote_finance_storage_split_action",
                return_value={
                    "status": "rollback_complete",
                    "global_manifest_switched": True,
                    "canonical_source": "monolith",
                },
            ),
            mock.patch.object(
                hosted_runtime,
                "_restart_finance_cutover_http_service",
                return_value={
                    "service": "wb-core-registry-http.service",
                    "status": "active",
                },
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_finance_storage_split_command(rollback_args)
        if maintenance_actions != [
            "barrier-acquire",
            "prepare",
            "hold",
            "barrier-confirm",
            "barrier-restoring",
            "restore",
            "barrier-release",
        ]:
            raise AssertionError(
                "rollback must replay under hold, restart, exactly restore "
                f"and release controls: {maintenance_actions}"
            )
        if warehouse_actions != ["hold", "restore"]:
            raise AssertionError(
                "rollback must exactly restore the warehouse timer boundary"
            )
    with TemporaryDirectory(prefix="partner-ads-hosted-smoke-") as partner_temp_dir:
        partner_output = Path(partner_temp_dir) / "partner-diagnostic.json"
        partner_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "partner-finance-diagnostic",
                "--nm-id",
                "245720334",
                "--week",
                "2026-07-13",
                "--output",
                str(partner_output),
            ]
        )
        if partner_args.handler is not hosted_runtime.run_partner_finance_diagnostic_command:
            raise AssertionError("hosted runner must expose Partner/Finance diagnostic")
        partner_payload = {
            "status": "incomplete",
            "nm_id": "245720334",
            "weeks": [],
            "blockers": [{"code": "ads_date_missing"}],
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=3, stdout=json.dumps(partner_payload), stderr=""
        )
        with mock.patch.object(
            hosted_runtime.subprocess, "run", return_value=completed
        ) as run_mock:
            assert hosted_runtime._run_remote_partner_finance_diagnostic(
                active_target,
                nm_id="245720334",
                weeks=("2026-07-13",),
            ) == partner_payload
        remote_command = " ".join(run_mock.call_args.args[0])
        if not all(
            token in remote_command
            for token in (
                "partner_finance_production_diagnostic.py",
                "--server-settings",
                "--env-file",
                "/opt/wb-ai/.env",
                "--nm-id",
                "245720334",
                "--week",
                "2026-07-13",
            )
        ):
            raise AssertionError("Partner/Finance diagnostic lost exact scope")
        if run_mock.call_args.kwargs.get("timeout") != hosted_runtime.PARTNER_FINANCE_DIAGNOSTIC_TIMEOUT_SECONDS:
            raise AssertionError("Partner/Finance diagnostic lost bounded timeout")
        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_partner_finance_diagnostic",
                return_value=partner_payload,
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_partner_finance_diagnostic_command(partner_args)
        if (
            not partner_output.is_file()
            or partner_output.stat().st_mode & 0o777 != 0o600
        ):
            raise AssertionError("Partner/Finance evidence must be written with mode 0600")

        ads_dates = ("2025-12-29", "2025-12-30")
        ads_plan_path = Path(partner_temp_dir) / "ads-plan.json"
        ads_plan_path.write_text(
            json.dumps(
                {
                    "schema_version": "ads_historical_recovery_v4",
                    "dry_run": True,
                    "apply_allowed": True,
                    "fingerprint": "sha256:ads-reviewed",
                    "scope": {
                        "nm_ids": [245720334],
                        "target_dates": list(ads_dates),
                    },
                }
            ),
            encoding="utf-8",
        )
        for action in ("dry-run", "readback", "apply"):
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"status":"ready"}', stderr=""
            )
            with mock.patch.object(
                hosted_runtime.subprocess, "run", return_value=completed
            ) as run_mock:
                hosted_runtime._run_remote_ads_historical_recovery(
                    active_target,
                    action=action,
                    nm_ids=(245720334,),
                    target_dates=ads_dates,
                    plan_path=ads_plan_path if action == "apply" else None,
                    fingerprint="sha256:ads-reviewed" if action == "apply" else "",
                    approval_reference="human-gate-ads" if action == "apply" else "",
                )
            remote_command = " ".join(run_mock.call_args.args[0])
            if not all(
                token in remote_command
                for token in (
                    "ads_historical_recovery.py",
                    "--nm-id",
                    "245720334",
                    "--target-date",
                    "2025-12-29",
                    "2025-12-30",
                )
            ):
                raise AssertionError("ads historical runner lost exact scope")
            if run_mock.call_args.kwargs.get("timeout") != hosted_runtime.ADS_HISTORICAL_RECOVERY_TIMEOUT_SECONDS:
                raise AssertionError("ads historical runner lost bounded timeout")
            if action == "apply" and not all(
                token in remote_command
                for token in (
                    "--apply",
                    "--confirm-fingerprint",
                    "sha256:ads-reviewed",
                    "--approval-reference",
                    "human-gate-ads",
                    "/opt/wb-core-runtime/state/backups/ads-historical",
                    "--reviewed-plan-stdin",
                )
            ):
                raise AssertionError("ads historical apply lost fingerprint, backup, or approval")
            if action == "apply" and run_mock.call_args.kwargs.get(
                "input"
            ) != ads_plan_path.read_text(encoding="utf-8"):
                raise AssertionError("ads historical apply lost the exact reviewed plan")
            if action != "apply" and run_mock.call_args.kwargs.get("input") is not None:
                raise AssertionError("ads historical read-only command received mutation input")
            if action != "apply" and "--apply" in remote_command:
                raise AssertionError("ads historical read-only command unexpectedly enables mutation")

        blocked_readback = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status":"blocked","blockers":[{"code":"ads_date_missing"}]}',
            stderr="",
        )
        with mock.patch.object(
            hosted_runtime.subprocess, "run", return_value=blocked_readback
        ):
            try:
                hosted_runtime._run_remote_ads_historical_recovery(
                    active_target,
                    action="readback",
                    nm_ids=(245720334,),
                    target_dates=ads_dates,
                    plan_path=None,
                    fingerprint="",
                    approval_reference="",
                )
            except RuntimeError as exc:
                if "readback has blockers" not in str(exc):
                    raise
            else:
                raise AssertionError("blocked ads readback was accepted")

        ads_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "ads-historical-dry-run",
                "--nm-id",
                "245720334",
                "--target-date",
                "2025-12-29",
                "--output",
                str(Path(partner_temp_dir) / "ads-dry-run.json"),
            ]
        )
        if (
            ads_args.handler is not hosted_runtime.run_ads_historical_recovery_command
            or ads_args.ads_historical_action != "dry-run"
        ):
            raise AssertionError("hosted runner must expose ads historical commands")

        stage_sha = "a" * 40
        stage_plan_path = Path(partner_temp_dir) / "ff-stage-7a-plan.json"
        stage_plan_path.write_text(
            json.dumps(
                {
                    "contract_name": "ff_stage_7a_production_mutation_v1",
                    "contract_version": 1,
                    "mode": "dry_run",
                    "apply_allowed": True,
                    "deployed_sha": stage_sha,
                    "fingerprint": "sha256:stage-7a-reviewed",
                }
            ),
            encoding="utf-8",
        )
        stage_apply = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"status":"complete"}', stderr=""
        )
        stage_restart = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="active\n4321\n", stderr=""
        )
        stage_readback_payload = {
            "status": "ready",
            "facilities": [
                {"name": "FF Москва", "active": True},
                {"name": "FF Оренбург", "active": False},
            ],
            "collector_configuration": {"enabled": True},
            "collector_state": {
                "last_status": "success",
                "complete": True,
                "next_cursor": 0,
            },
        }
        stage_readback = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(stage_readback_payload),
            stderr="",
        )
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            side_effect=[stage_apply, stage_restart, stage_readback],
        ) as run_mock:
            stage_result = hosted_runtime._run_remote_ff_stage_7a_production(
                active_target,
                action="apply",
                deployed_sha=stage_sha,
                plan_path=stage_plan_path,
                fingerprint="sha256:stage-7a-reviewed",
                approval_reference="github-pr-123#issuecomment-456",
                actor="owner",
            )
        first_command = " ".join(run_mock.call_args_list[0].args[0])
        if not all(
            token in first_command
            for token in (
                "ff_stage_7a_production.py",
                ".wb-core-runtime-sha",
                stage_sha,
                "--reviewed-plan-stdin",
                "sha256:stage-7a-reviewed",
                "github-pr-123#issuecomment-456",
                "/opt/wb-core-runtime/state/backups/ff-stage-7a-production",
            )
        ):
            raise AssertionError("Stage 7A hosted apply lost exact SHA, plan, backup, or gate")
        if run_mock.call_args_list[0].kwargs.get("input") != stage_plan_path.read_text(encoding="utf-8"):
            raise AssertionError("Stage 7A hosted apply lost the exact reviewed plan")
        if run_mock.call_args_list[0].kwargs.get("timeout") != hosted_runtime.FF_STAGE_7A_PRODUCTION_TIMEOUT_SECONDS:
            raise AssertionError("Stage 7A hosted apply lost its bounded timeout")
        if (
            not stage_result.get("http_service_restart", {}).get("active")
            or stage_result.get("post_restart_readback") != stage_readback_payload
        ):
            raise AssertionError("Stage 7A hosted apply lacks restart/readback evidence")

        stage_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "ff-stage-7a-production-dry-run",
                "--deployed-sha",
                stage_sha,
                "--output",
                str(Path(partner_temp_dir) / "ff-stage-7a-dry-run.json"),
            ]
        )
        if (
            stage_args.handler is not hosted_runtime.run_ff_stage_7a_production_command
            or stage_args.ff_stage_7a_action != "dry-run"
        ):
            raise AssertionError("hosted runner must expose Stage 7A production commands")

        zero_sha = "d" * 40
        zero_fingerprint = "sha256:" + "e" * 64
        zero_plan_path = Path(partner_temp_dir) / "ff-pool-zero-physical-plan.json"
        zero_plan_path.write_text(
            json.dumps(
                {
                    "contract_name": hosted_runtime.FF_POOL_ZERO_PHYSICAL_CONTRACT_NAME,
                    "contract_version": hosted_runtime.FF_POOL_ZERO_PHYSICAL_CONTRACT_VERSION,
                    "mode": "dry_run",
                    "apply_allowed": True,
                    "deployed_sha": zero_sha,
                    "fingerprint": zero_fingerprint,
                    "scope": {
                        "facility_id": hosted_runtime.FF_POOL_ZERO_PHYSICAL_TARGET_FACILITY_ID,
                        "facility_name": "FF Москва",
                        "pool": "FBS",
                        "nm_ids": list(hosted_runtime.FF_POOL_ZERO_PHYSICAL_TARGET_NM_IDS),
                        "absolute_physical_target": 0,
                    },
                    "expected_effects": {
                        "balance_insert_count": len(
                            hosted_runtime.FF_POOL_ZERO_PHYSICAL_TARGET_NM_IDS
                        )
                    },
                }
            ),
            encoding="utf-8",
        )
        zero_apply_payload = {
            "status": "complete",
            "manifest_fingerprint": zero_fingerprint,
        }
        zero_readback_payload = {
            "status": "ready",
            "target_rows": [
                {"nm_id": nm_id, "state": "explicit_zero"}
                for nm_id in hosted_runtime.FF_POOL_ZERO_PHYSICAL_TARGET_NM_IDS
            ],
            "fbs_status_read_model": {
                "target_nm_ids_unblocked": True,
                "target_nm_ids_missing": [],
                "calculation_enabled": True,
            },
        }
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(zero_apply_payload),
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(zero_readback_payload),
                    stderr="",
                ),
            ],
        ) as zero_run:
            zero_result = hosted_runtime._run_remote_ff_pool_zero_physical_production(
                active_target,
                action="apply",
                deployed_sha=zero_sha,
                plan_path=zero_plan_path,
                fingerprint=zero_fingerprint,
                approval_reference="github-pr#issuecomment-apply-gate",
                actor="owner-relay",
            )
        zero_command = " ".join(zero_run.call_args_list[0].args[0])
        if not all(
            token in zero_command
            for token in (
                "ff_pool_zero_physical_production.py",
                ".wb-core-runtime-sha",
                zero_sha,
                "--reviewed-plan-stdin",
                zero_fingerprint,
                "github-pr#issuecomment-apply-gate",
                "/opt/wb-core-runtime/state/backups/ff-pool-zero-physical-production",
            )
        ):
            raise AssertionError("zero-physical hosted apply lost exact scope, SHA, or gate")
        if zero_run.call_args_list[0].kwargs.get("input") != zero_plan_path.read_text(
            encoding="utf-8"
        ):
            raise AssertionError("zero-physical hosted apply lost the reviewed plan")
        if (
            zero_run.call_args_list[0].kwargs.get("timeout")
            != hosted_runtime.FF_POOL_ZERO_PHYSICAL_PRODUCTION_TIMEOUT_SECONDS
        ):
            raise AssertionError("zero-physical hosted apply lost its bounded timeout")
        if zero_result.get("post_apply_readback") != zero_readback_payload:
            raise AssertionError("zero-physical hosted apply lacks exact readback evidence")

        zero_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "ff-pool-zero-physical-production-dry-run",
                "--deployed-sha",
                zero_sha,
                "--output",
                str(Path(partner_temp_dir) / "ff-pool-zero-physical-dry-run.json"),
            ]
        )
        if (
            zero_args.handler
            is not hosted_runtime.run_ff_pool_zero_physical_production_command
            or zero_args.ff_pool_zero_physical_action != "dry-run"
        ):
            raise AssertionError("hosted runner must expose zero-physical commands")

        overhead_sha = "9" * 40
        overhead_fingerprint = "sha256:" + "8" * 64
        overhead_plan_path = Path(partner_temp_dir) / "ff-overhead-plan.json"
        overhead_plan_path.write_text(
            json.dumps(
                {
                    "contract_name": hosted_runtime.FF_POOL_OVERHEAD_BACKFILL_CONTRACT_NAME,
                    "contract_version": hosted_runtime.FF_POOL_OVERHEAD_BACKFILL_CONTRACT_VERSION,
                    "mode": "dry_run",
                    "apply_allowed": True,
                    "blockers": [],
                    "deployed_sha": overhead_sha,
                    "fingerprint": overhead_fingerprint,
                    "pre_state": "already_current",
                    "scope": {
                        "document_ids": [f"doc-{index}" for index in range(5)],
                        "pool": "FBS",
                    },
                    "expected_effects": {
                        "selected_document_amount_rub": "175206.50",
                        "aggregate_capital_rewrite_rub": "0",
                        "capital_delta_rub": "0.00",
                        "aggregate_row_update_count": 0,
                        "quantity_delta": 0,
                        "business_document_replay_count": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        overhead_apply = {
            "status": "complete",
            "manifest_fingerprint": overhead_fingerprint,
            "readback": {
                "status": "complete",
                "quantity_unchanged": True,
                "past_fulfilled_lifecycle_unchanged": True,
                "documents_unchanged": True,
                "non_target_unchanged": True,
                "pre_change_invariants_verified": True,
            },
        }
        overhead_readback = {
            "status": "complete",
            "projection_current": True,
            "capital_conserved": True,
            "no_duplicate_submit": True,
            "queues": [{"queue_id": f"queue-{index}"} for index in range(5)],
        }
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(overhead_apply), stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(overhead_readback), stderr=""
                ),
            ],
        ) as overhead_run:
            overhead_result = hosted_runtime._run_remote_ff_pool_overhead_backfill(
                active_target,
                action="apply",
                deployed_sha=overhead_sha,
                plan_path=overhead_plan_path,
                fingerprint=overhead_fingerprint,
                approval_reference="github-pr#issuecomment-apply-gate",
                actor="owner-relay",
            )
        overhead_command = " ".join(overhead_run.call_args_list[0].args[0])
        if not all(
            token in overhead_command
            for token in (
                "ff_pool_overhead_backfill.py",
                ".wb-core-runtime-sha",
                overhead_sha,
                "--reviewed-plan-stdin",
                overhead_fingerprint,
                "github-pr#issuecomment-apply-gate",
                "/opt/wb-core-runtime/state/backups/ff-pool-overhead-backfill",
            )
        ):
            raise AssertionError("overhead hosted apply lost exact SHA, plan, or gate")
        if overhead_run.call_args_list[0].kwargs.get("input") != overhead_plan_path.read_text(
            encoding="utf-8"
        ):
            raise AssertionError("overhead hosted apply lost the reviewed plan")
        if (
            overhead_run.call_args_list[0].kwargs.get("timeout")
            != hosted_runtime.FF_POOL_OVERHEAD_BACKFILL_TIMEOUT_SECONDS
        ):
            raise AssertionError("overhead hosted apply lost bounded timeout")
        if overhead_result.get("post_apply_readback") != overhead_readback:
            raise AssertionError("overhead hosted apply lacks query-only readback")
        overhead_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "ff-pool-overhead-backfill-dry-run",
                "--deployed-sha",
                overhead_sha,
                "--output",
                str(Path(partner_temp_dir) / "ff-overhead-dry-run.json"),
            ]
        )
        if (
            overhead_args.handler is not hosted_runtime.run_ff_pool_overhead_backfill_command
            or overhead_args.ff_pool_overhead_backfill_action != "dry-run"
        ):
            raise AssertionError("hosted runner must expose overhead backfill commands")

        mapping_sha = "f" * 40
        mapping_fingerprint = "sha256:" + "1" * 64
        mapping_plan_path = Path(partner_temp_dir) / "ff-fbs-mapping-plan.json"
        mapping_plan_path.write_text(
            json.dumps(
                {
                    "contract_name": hosted_runtime.FF_FBS_MAPPING_EXTENSION_CONTRACT_NAME,
                    "contract_version": hosted_runtime.FF_FBS_MAPPING_EXTENSION_CONTRACT_VERSION,
                    "mode": "dry_run",
                    "apply_allowed": True,
                    "deployed_sha": mapping_sha,
                    "fingerprint": mapping_fingerprint,
                    "scope": {
                        "seller_warehouse_id": hosted_runtime.FF_FBS_MAPPING_EXTENSION_TARGET_WAREHOUSE_ID,
                        "official_office_id": hosted_runtime.FF_FBS_MAPPING_EXTENSION_TARGET_OFFICE_ID,
                        "facility_id": hosted_runtime.FF_FBS_MAPPING_EXTENSION_TARGET_FACILITY_ID,
                        "pool": "FBS",
                    },
                    "expected_effects": {"wb_write_count": 0},
                }
            ),
            encoding="utf-8",
        )
        mapping_apply_payload = {
            "status": "complete",
            "manifest_fingerprint": mapping_fingerprint,
        }
        mapping_readback_payload = {
            "status": "ready",
            "mapping_extension": {
                "plan_fingerprint": mapping_fingerprint,
                "deployed_sha": mapping_sha,
            },
            "mapping": [
                {
                    "seller_warehouse_id": hosted_runtime.FF_FBS_MAPPING_EXTENSION_TARGET_WAREHOUSE_ID,
                    "facility_id": hosted_runtime.FF_FBS_MAPPING_EXTENSION_TARGET_FACILITY_ID,
                }
            ],
            "backlog_partition": {"frozen_unresolved_pending_count": 0},
            "pool_aggregate_parity": {"status": "pass"},
            "wb_writes": 0,
        }
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(mapping_apply_payload),
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(mapping_readback_payload),
                    stderr="",
                ),
            ],
        ) as mapping_run:
            mapping_result = hosted_runtime._run_remote_ff_fbs_mapping_extension_production(
                active_target,
                action="apply",
                deployed_sha=mapping_sha,
                plan_path=mapping_plan_path,
                fingerprint=mapping_fingerprint,
                approval_reference="github-pr#issuecomment-orenburg-apply-gate",
                actor="owner-relay",
            )
        mapping_command = " ".join(mapping_run.call_args_list[0].args[0])
        if not all(
            token in mapping_command
            for token in (
                "ff_fbs_mapping_extension_production.py",
                ".wb-core-runtime-sha",
                mapping_sha,
                "--reviewed-plan-stdin",
                mapping_fingerprint,
                "github-pr#issuecomment-orenburg-apply-gate",
                "/opt/wb-core-runtime/state/backups/ff-fbs-mapping-extension-production",
            )
        ):
            raise AssertionError(
                "FBS mapping-extension hosted apply lost exact scope, SHA, or gate"
            )
        if mapping_run.call_args_list[0].kwargs.get(
            "input"
        ) != mapping_plan_path.read_text(encoding="utf-8"):
            raise AssertionError(
                "FBS mapping-extension hosted apply lost the reviewed plan"
            )
        if (
            mapping_run.call_args_list[0].kwargs.get("timeout")
            != hosted_runtime.FF_FBS_MAPPING_EXTENSION_PRODUCTION_TIMEOUT_SECONDS
        ):
            raise AssertionError(
                "FBS mapping-extension hosted apply lost its bounded timeout"
            )
        if mapping_result.get("post_apply_readback") != mapping_readback_payload:
            raise AssertionError(
                "FBS mapping-extension hosted apply lacks exact readback evidence"
            )
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess(
                    args=[], returncode=255, stdout="", stderr="ssh reset"
                ),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(mapping_readback_payload),
                    stderr="",
                ),
            ],
        ) as ambiguous_mapping_run:
            ambiguous_mapping_result = (
                hosted_runtime._run_remote_ff_fbs_mapping_extension_production(
                    active_target,
                    action="apply",
                    deployed_sha=mapping_sha,
                    plan_path=mapping_plan_path,
                    fingerprint=mapping_fingerprint,
                    approval_reference="github-pr#issuecomment-orenburg-apply-gate",
                    actor="owner-relay",
                )
            )
        if (
            len(ambiguous_mapping_run.call_args_list) != 2
            or ambiguous_mapping_result.get("recovered_after_transport_ambiguity")
            is not True
            or ambiguous_mapping_result.get("post_apply_readback")
            != mapping_readback_payload
        ):
            raise AssertionError(
                "ambiguous FBS mapping apply must reconcile query-only before retry"
            )
        mapping_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "ff-fbs-mapping-extension-production-dry-run",
                "--deployed-sha",
                mapping_sha,
                "--output",
                str(Path(partner_temp_dir) / "ff-fbs-mapping-dry-run.json"),
            ]
        )
        if (
            mapping_args.handler
            is not hosted_runtime.run_ff_fbs_mapping_extension_production_command
            or mapping_args.ff_fbs_mapping_extension_action != "dry-run"
        ):
            raise AssertionError(
                "hosted runner must expose FBS mapping-extension commands"
            )

        cutover_sha = "b" * 40
        cutover_gate = {
            "contract_name": hosted_runtime.FF_POOL_CUTOVER_PRODUCTION_CONTRACT_NAME,
            "contract_version": hosted_runtime.FF_POOL_CUTOVER_PRODUCTION_CONTRACT_VERSION,
            "mode": "dry_run_owner_gate",
            "deployed_sha": cutover_sha,
            "apply_allowed": True,
            "blockers": [],
            "fingerprint": "sha256:" + "c" * 64,
        }
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(cutover_gate), stderr=""
            ),
        ) as cutover_run:
            cutover_result = hosted_runtime._run_remote_ff_pool_cutover_runner(
                active_target,
                action="dry-run",
                deployed_sha=cutover_sha,
                excluded_shipment_ids=("sup_adc29a3cba934403bca4842c2add8b7d",),
                opening_facility_id="fac_moscow",
                proposed_window_minutes=15,
                reviewed_envelope=None,
                fingerprint="",
                approval_reference="",
                actor="",
            )
        cutover_command = " ".join(cutover_run.call_args.args[0])
        if not all(
            token in cutover_command
            for token in (
                "ff_pool_cutover_production.py",
                ".wb-core-runtime-sha",
                cutover_sha,
                "--excluded-shipment-id",
                "sup_adc29a3cba934403bca4842c2add8b7d",
                "--opening-facility-id",
                "fac_moscow",
            )
        ):
            raise AssertionError("Stage 7C hosted dry-run lost exact target/source binding")
        if (
            cutover_result != cutover_gate
            or cutover_run.call_args.kwargs.get("timeout")
            != hosted_runtime.FF_POOL_CUTOVER_PRODUCTION_TIMEOUT_SECONDS
        ):
            raise AssertionError("Stage 7C hosted runner lost its contract or bounded timeout")

        cutover_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "ff-pool-cutover-production-dry-run",
                "--deployed-sha",
                cutover_sha,
                "--excluded-shipment-id",
                "sup_adc29a3cba934403bca4842c2add8b7d",
                "--output",
                str(Path(partner_temp_dir) / "ff-pool-cutover-dry-run.json"),
            ]
        )
        if (
            cutover_args.handler
            is not hosted_runtime.run_ff_pool_cutover_production_command
            or cutover_args.ff_pool_cutover_action != "dry-run"
        ):
            raise AssertionError("hosted runner must expose Stage 7C production commands")

        cutover_gate_path = Path(partner_temp_dir) / "ff-pool-cutover-gate.json"
        cutover_gate_path.write_text(
            json.dumps(cutover_gate, sort_keys=True) + "\n", encoding="utf-8"
        )
        with mock.patch.object(
            hosted_runtime,
            "_read_remote_fbs_collector_timer",
            return_value={"active": False, "enabled": False},
        ):
            try:
                hosted_runtime._run_remote_ff_pool_cutover_production(
                    active_target,
                    action="apply",
                    deployed_sha=cutover_sha,
                    excluded_shipment_ids=(),
                    opening_facility_id="",
                    proposed_window_minutes=15,
                    plan_path=cutover_gate_path,
                    fingerprint=cutover_gate["fingerprint"],
                    approval_reference="github-pr-973#issuecomment-owner-gate",
                    actor="owner",
                )
            except RuntimeError as exc:
                if "five-minute FBS collector" not in str(exc):
                    raise
            else:
                raise AssertionError("Stage 7C apply must require the active collector")

        stale_cutover_gate = {**cutover_gate, "contract_version": 1}
        cutover_gate_path.write_text(
            json.dumps(stale_cutover_gate, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            hosted_runtime._run_remote_ff_pool_cutover_production(
                active_target,
                action="apply",
                deployed_sha=cutover_sha,
                excluded_shipment_ids=(),
                opening_facility_id="",
                proposed_window_minutes=15,
                plan_path=cutover_gate_path,
                fingerprint=cutover_gate["fingerprint"],
                approval_reference="github-pr-973#issuecomment-owner-gate",
                actor="owner",
            )
        except ValueError as exc:
            if "reviewed plan does not match" not in str(exc):
                raise
        else:
            raise AssertionError("Stage 7C apply accepted a stale manifest version")
    for maintenance_action, expected_timeout in (
        ("status", 300.0),
        ("hold", 1500.0),
        ("restore", 300.0),
    ):
        maintenance_payload = {
            "status": "held" if maintenance_action == "hold" else (
                "restored" if maintenance_action == "restore" else "ok"
            ),
            "units": {
                "timer": {
                    "is_enabled": "enabled",
                    "is_active": "inactive" if maintenance_action == "hold" else "active",
                },
                "service": {"is_active": "inactive", "quiescent": True},
            },
            "warehouse_lock": {"held": False},
            "finance_apply_processes": [],
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(maintenance_payload), stderr=""
        )
        with mock.patch.object(
            hosted_runtime.subprocess, "run", return_value=completed
        ) as run_mock:
            payload = hosted_runtime._run_remote_warehouse_functional_maintenance_action(
                active_target,
                action=maintenance_action,
            )
        if payload["status"] != maintenance_payload["status"]:
            raise AssertionError("hosted maintenance lost runner readback")
        if run_mock.call_args.kwargs.get("timeout") != expected_timeout:
            raise AssertionError("hosted maintenance lost its bounded timeout")
        remote_command = " ".join(run_mock.call_args.args[0])
        if not all(
            token in remote_command
            for token in (
                "apps/warehouse_functional_maintenance.py",
                maintenance_action,
                "/opt/wb-core-runtime/state",
            )
        ):
            raise AssertionError("hosted maintenance bypassed its repo-owned runner")
    for unsafe_service_state in (
        "active",
        "activating",
        "reloading",
        "deactivating",
        "unknown",
    ):
        unsafe_payload = {
            "status": "held",
            "units": {
                "timer": {"is_active": "inactive"},
                "service": {
                    "is_active": unsafe_service_state,
                    "quiescent": True,
                },
            },
            "warehouse_lock": {"held": False},
            "finance_apply_processes": [],
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(unsafe_payload), stderr=""
        )
        with mock.patch.object(
            hosted_runtime.subprocess, "run", return_value=completed
        ):
            try:
                hosted_runtime._run_remote_warehouse_functional_maintenance_action(
                    active_target,
                    action="hold",
                )
            except RuntimeError as exc:
                if "hold readback is incomplete" not in str(exc):
                    raise
            else:
                raise AssertionError(
                    f"hosted maintenance accepted unsafe service state {unsafe_service_state}"
                )
    failed_quiescent_payload = {
        "status": "held",
        "units": {
            "timer": {"is_active": "inactive"},
            "service": {"is_active": "failed", "quiescent": True},
        },
        "warehouse_lock": {"held": False},
        "finance_apply_processes": [],
    }
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(failed_quiescent_payload), stderr=""
    )
    with mock.patch.object(
        hosted_runtime.subprocess, "run", return_value=completed
    ):
        failed_quiescent = (
            hosted_runtime._run_remote_warehouse_functional_maintenance_action(
                active_target,
                action="hold",
            )
        )
    if failed_quiescent["units"]["service"]["is_active"] != "failed":
        raise AssertionError("hosted maintenance rejected terminal failed oneshot evidence")
    maintenance_args = hosted_runtime.build_arg_parser().parse_args(
        ["warehouse-functional-maintenance", "hold"]
    )
    if (
        maintenance_args.handler
        is not hosted_runtime.run_warehouse_functional_maintenance_command
        or maintenance_args.action != "hold"
    ):
        raise AssertionError("hosted runner must expose warehouse maintenance hold")
    durable_payload = {
        "status": "held",
        "units": {
            "timer": {"is_active": "inactive", "is_enabled": "disabled"},
            "service": {"is_active": "inactive", "quiescent": True},
        },
        "warehouse_lock": {"held": False},
        "finance_apply_processes": [],
    }
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(durable_payload), stderr=""
    )
    with mock.patch.object(
        hosted_runtime.subprocess, "run", return_value=completed
    ) as run_mock:
        hosted_runtime._run_remote_warehouse_functional_maintenance_action(
            active_target,
            action="hold",
            disable_timer=True,
        )
    if "--disable-timer" not in " ".join(run_mock.call_args.args[0]):
        raise AssertionError("durable warehouse hold must pass --disable-timer")
    business_args = hosted_runtime.build_arg_parser().parse_args(
        ["business-data-maintenance", "hold"]
    )
    if (
        business_args.handler is not hosted_runtime.run_business_data_maintenance_command
        or business_args.action != "hold"
    ):
        raise AssertionError("hosted runner must expose all-writer business-data hold")
    business_restore_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "business-data-maintenance",
            "restore",
            "--expected-revision",
            "7",
        ]
    )
    if (
        business_restore_args.action != "restore"
        or business_restore_args.expected_revision != 7
    ):
        raise AssertionError(
            "business-data restore must expose exact optimistic policy revision"
        )
    business_continuity_args = (
        hosted_runtime.build_arg_parser().parse_args(
            [
                "business-data-maintenance",
                "restore-continuity-status",
            ]
        )
    )
    if business_continuity_args.action != "restore-continuity-status":
        raise AssertionError(
            "hosted runner must expose read-only restore continuity preflight"
        )
    continuity_payload = {
        "status": "ready",
        "service_continuity": {
            "fingerprint": "sha256:" + "9" * 64,
            "services": [
                {
                    "unit": (
                        "wb-core-sheet-vitrina-closure-retry.service"
                    ),
                    "main_pid": 4242,
                    "started_at": "fixture",
                }
            ],
        },
    }
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(continuity_payload),
        stderr="",
    )
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=completed,
    ) as run_mock:
        captured_continuity = (
            hosted_runtime._run_remote_business_data_maintenance_runner(
                active_target,
                action="restore-continuity-status",
            )
        )
    if (
        captured_continuity != continuity_payload
        or "restore-continuity-status"
        not in " ".join(run_mock.call_args.args[0])
    ):
        raise AssertionError(
            "hosted restore continuity preflight lost read-only transport"
        )
    restore_inventory_payload = {
        "contract_name": "business_data_maintenance_restore_inventory_v1",
        "status": "ready",
        "nonterminal_job_count": 0,
        "locks_free": True,
        "new_restore_submit_allowed": True,
    }
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(restore_inventory_payload),
        stderr="",
    )
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=completed,
    ) as run_mock:
        captured_inventory = (
            hosted_runtime._run_remote_business_data_maintenance_restore_job(
                active_target,
                job_action="inventory",
                deployed_sha="1" * 40,
            )
        )
    inventory_command = " ".join(run_mock.call_args.args[0])
    if (
        captured_inventory != restore_inventory_payload
        or " inventory" not in inventory_command
        or "--expected-revision" in inventory_command
        or "--job-id" in inventory_command
    ):
        raise AssertionError(
            "query-only restore inventory must not require submit bindings"
        )
    quiet_continuity_payload = {
        "status": "ready",
        "service_continuity": {
            "boundary_kind": "quiet_confirmed_hold",
            "fingerprint": "sha256:" + "8" * 64,
            "services": [],
        },
    }
    with (
        mock.patch.object(
            hosted_runtime,
            "load_hosted_runtime_target",
            return_value=active_target,
        ),
        mock.patch.object(
            hosted_runtime,
            "_run_remote_business_data_maintenance_runner",
            return_value=quiet_continuity_payload,
        ),
        mock.patch.object(hosted_runtime, "_print_json"),
    ):
        hosted_runtime.run_business_data_maintenance_command(
            business_continuity_args
        )
    with (
        mock.patch.object(
            hosted_runtime,
            "load_hosted_runtime_target",
            return_value=active_target,
        ),
        mock.patch.object(
            hosted_runtime,
            "_run_remote_business_data_maintenance_runner",
            return_value={
                "status": "ready",
                "service_continuity": {
                    "fingerprint": "sha256:" + "7" * 64,
                    "services": [],
                },
            },
        ),
    ):
        try:
            hosted_runtime.run_business_data_maintenance_command(
                business_continuity_args
            )
        except RuntimeError as exc:
            if "continuity readback is incomplete" not in str(exc):
                raise
        else:
            raise AssertionError(
                "hosted restore continuity accepted an empty legacy boundary"
            )
    business_barrier_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "business-data-maintenance",
            "barrier-acquire",
            "--window-id",
            "snapshot-window-001",
            "--window-kind",
            "snapshot",
            "--plan-fingerprint",
            "sha256:" + ("1" * 64),
            "--approval-reference",
            "approval-comment-001",
            "--actor",
            "migration_runner",
            "--reason",
            "coherent snapshot",
        ]
    )
    if (
        business_barrier_args.action != "barrier-acquire"
        or business_barrier_args.window_id != "snapshot-window-001"
        or business_barrier_args.approval_reference != "approval-comment-001"
    ):
        raise AssertionError(
            "hosted runner must expose exact audited HTTP write barrier controls"
        )
    business_set_process_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "business-data-maintenance",
            "set-process",
            "--process-key",
            "autoanswers_worker",
            "--desired",
            "off",
            "--expected-revision",
            "13",
            "--actor",
            "incident_recovery",
            "--reason",
            "restore owner intended OFF state",
        ]
    )
    if (
        business_set_process_args.action != "set-process"
        or business_set_process_args.process_key != "autoanswers_worker"
        or business_set_process_args.desired != "off"
        or business_set_process_args.expected_revision != 13
    ):
        raise AssertionError(
            "hosted runner must expose audited exact-revision process recovery"
        )
    set_process_payload = {
        "status": "updated",
        "auto_updates": {
            "revision": 14,
            "processes": [
                {
                    "process_key": "autoanswers_worker",
                    "desired": False,
                    "actual": False,
                }
            ],
        },
    }
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(set_process_payload),
        stderr="",
    )
    with mock.patch.object(
        hosted_runtime.subprocess, "run", return_value=completed
    ) as run_mock:
        hosted_runtime._run_remote_business_data_maintenance_runner(
            active_target,
            action="set-process",
            process_key="autoanswers_worker",
            desired="off",
            expected_revision=13,
            actor="incident_recovery",
            reason="restore owner intended OFF state",
        )
    set_process_command = " ".join(run_mock.call_args.args[0])
    for expected_token in (
        "set-process",
        "--process-key",
        "autoanswers_worker",
        "--desired",
        "off",
        "--expected-revision",
        "13",
    ):
        if expected_token not in set_process_command:
            raise AssertionError(
                f"hosted set-process command lost {expected_token}: "
                f"{set_process_command}"
            )
    barrier_payload = {
        "status": "active",
        "active": True,
        "phase": "acquiring",
        "window_id": "snapshot-window-001",
    }
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(barrier_payload),
        stderr="",
    )
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=completed,
    ) as run_mock:
        hosted_runtime._run_remote_business_data_maintenance_runner(
            active_target,
            action="barrier-acquire",
            window_id="snapshot-window-001",
            window_kind="snapshot",
            plan_fingerprint="sha256:" + ("1" * 64),
            approval_reference="approval-comment-001",
            actor="migration_runner",
            reason="coherent snapshot",
        )
    barrier_command = " ".join(run_mock.call_args.args[0])
    for expected_token in (
        "barrier-acquire",
        "--window-id",
        "snapshot-window-001",
        "--plan-fingerprint",
        "sha256:" + ("1" * 64),
        "--approval-reference",
        "approval-comment-001",
    ):
        if expected_token not in barrier_command:
            raise AssertionError(
                f"hosted barrier command lost {expected_token}: "
                f"{barrier_command}"
            )
    with TemporaryDirectory(prefix="warehouse-hosted-timeout-smoke-") as opening_temp_dir:
        plan_path = Path(opening_temp_dir) / "plan.json"
        plan_path.write_text('{"plan_fingerprint":"sha256:timeout-smoke"}\n', encoding="utf-8")
        for action, expected_timeout in (("readback", 300.0), ("apply", 1800.0), ("rollback", 1800.0)):
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"ok":true}',
                stderr="",
            )
            with mock.patch.object(hosted_runtime.subprocess, "run", return_value=completed) as run_mock:
                hosted_runtime._run_remote_warehouse_opening_action(
                    active_target,
                    action=action,
                    plan_path=plan_path if action == "apply" else None,
                    fingerprint="sha256:timeout-smoke" if action in {"apply", "rollback"} else "",
                )
            actual_timeout = run_mock.call_args.kwargs.get("timeout")
            if actual_timeout != expected_timeout:
                raise AssertionError(
                    f"warehouse opening {action} subprocess timeout must be {expected_timeout}, got {actual_timeout}"
                )
        for action, expected_timeout in (
            ("readback", 300.0),
            ("backup", 1800.0),
            ("cutover-dry-run", 1800.0),
            ("emergency-dry-run", 1800.0),
            ("economics-backfill-dry-run", 1800.0),
            ("supplier-certification-dry-run", 1800.0),
            ("cutover-apply", 1800.0),
            ("sync-apply", 1800.0),
            ("supplier-certification-apply", 1800.0),
            ("supplier-certification-rollback", 1800.0),
        ):
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"ok":true}',
                stderr="",
            )
            with mock.patch.object(hosted_runtime.subprocess, "run", return_value=completed) as run_mock:
                hosted_runtime._run_remote_warehouse_functional_action(
                    active_target,
                    action=action,
                    plan_path=(
                        plan_path
                        if action
                        in {
                            "cutover-apply",
                            "sync-apply",
                            "supplier-certification-apply",
                        }
                        else None
                    ),
                    fingerprint=(
                        "sha256:timeout-smoke"
                        if action in {
                            "cutover-apply",
                            "sync-apply",
                            "supplier-certification-apply",
                            "supplier-certification-rollback",
                        }
                        else ""
                    ),
                    reason=(
                        "bounded smoke rollback"
                        if action == "supplier-certification-rollback"
                        else ""
                    ),
                )
            actual_timeout = run_mock.call_args.kwargs.get("timeout")
            if actual_timeout != expected_timeout:
                raise AssertionError(
                    f"warehouse functional {action} subprocess timeout must be "
                    f"{expected_timeout}, got {actual_timeout}"
                )
            if action in {"backup", "sync-apply"}:
                remote_command = " ".join(run_mock.call_args.args[0])
                if (
                    "/opt/wb-core-runtime/state/backups/warehouse-functional-sync"
                    not in remote_command
                    or "--backup-dir" not in remote_command
                    or "/opt/wb-core-runtime/backups/warehouse-functional-sync"
                    in remote_command
                ):
                    raise AssertionError(
                        "functional sync backup must use the mounted canonical runtime backup directory"
                    )
        archive_source = (
            "/opt/wb-core-runtime/state/backups/warehouse-functional-sync/"
            "warehouse-functional-pre-sync-20260723T184346Z.sqlite3"
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status":"ready"}',
            stderr="",
        )
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            return_value=completed,
        ) as run_mock:
            hosted_runtime._run_remote_sqlite_backup_archive(
                active_target,
                apply=True,
                source=archive_source,
                fingerprint="sha256:archive-smoke",
                reserved_free_bytes=4 * 1024 * 1024 * 1024,
            )
        archive_command = " ".join(run_mock.call_args.args[0])
        if (
            run_mock.call_args.kwargs.get("timeout") != 7200.0
            or archive_source not in archive_command
            or "--staging-directory /opt/wb-core-runtime/state"
            not in archive_command
            or "--reserved-free-bytes 4294967296" not in archive_command
            or "--apply" not in archive_command
        ):
            raise AssertionError(
                "hosted SQLite archive lost exact path/reserve/apply lifecycle"
            )
        try:
            hosted_runtime._run_remote_sqlite_backup_archive(
                active_target,
                apply=False,
                source=(
                    "/opt/wb-core-runtime/state/backups/"
                    "supplier-26gn390-recovery/foreign.sqlite3"
                ),
                fingerprint="",
                reserved_free_bytes=0,
            )
        except ValueError as exc:
            if "warehouse-functional-sync" not in str(exc):
                raise
        else:
            raise AssertionError(
                "hosted SQLite archive accepted another backup scope"
            )
        queue_plan_path = Path(opening_temp_dir) / "queue-plan.json"
        queue_plan_path.write_text(
            '{"fingerprint":"sha256:queue-smoke"}\n',
            encoding="utf-8",
        )
        with mock.patch.object(
            hosted_runtime.subprocess,
            "run",
            return_value=completed,
        ) as run_mock:
            hosted_runtime._run_remote_warehouse_cost_queue_replay(
                active_target,
                apply=True,
                invoice_numbers=["26GN582", "26GN583"],
                plan_path=queue_plan_path,
                fingerprint="sha256:queue-smoke",
            )
        queue_command = " ".join(run_mock.call_args.args[0])
        if (
            run_mock.call_args.kwargs.get("timeout") != 7200.0
            or "warehouse_cost_queue_replay.py" not in queue_command
            or queue_command.count("--invoice-no") != 2
            or "--plan-file /dev/stdin" not in queue_command
            or run_mock.call_args.kwargs.get("input")
            != '{"fingerprint":"sha256:queue-smoke"}\n'
        ):
            raise AssertionError(
                "hosted queue replay lost exact invoices/plan/fingerprint"
            )
        failed_backup_source = (
            "/opt/wb-core-runtime/backups/warehouse-functional/"
            "warehouse_functional_cutover_v1-20260719T001627Z.sqlite3"
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status":"ready"}',
            stderr="",
        )
        with mock.patch.object(hosted_runtime.subprocess, "run", return_value=completed) as run_mock:
            hosted_runtime._run_remote_warehouse_functional_failed_backup_cleanup(
                active_target,
                source=failed_backup_source,
                apply=False,
                fingerprint="",
            )
        if run_mock.call_args.kwargs.get("timeout") != 1800.0:
            raise AssertionError("failed backup SHA planning must allow the bounded mutation timeout")
        failed_emergency_backup_source = (
            "/opt/wb-core-runtime/backups/warehouse-functional-recovery/"
            "warehouse-functional-emergency-0123456789abcdef.sqlite3"
        )
        with mock.patch.object(hosted_runtime.subprocess, "run", return_value=completed) as run_mock:
            hosted_runtime._run_remote_warehouse_functional_failed_backup_cleanup(
                active_target,
                source=failed_emergency_backup_source,
                apply=False,
                fingerprint="",
            )
        emergency_cleanup_command = run_mock.call_args.args[0]
        if failed_emergency_backup_source not in " ".join(emergency_cleanup_command):
            raise AssertionError("failed emergency backup must use the repo-owned cleanup flow")
        try:
            hosted_runtime._run_remote_warehouse_functional_failed_backup_cleanup(
                active_target,
                source="/opt/wb-core-runtime/state/registry_upload_runtime.sqlite3",
                apply=False,
                fingerprint="",
            )
        except ValueError as exc:
            if "restricted" not in str(exc):
                raise AssertionError("failed backup cleanup rejected with unexpected error") from exc
        else:
            raise AssertionError("failed backup cleanup unexpectedly accepted the live database")
        try:
            hosted_runtime._run_remote_warehouse_functional_failed_backup_cleanup(
                active_target,
                source=(
                    "/opt/wb-core-runtime/backups/warehouse-functional-recovery/"
                    "unrelated.sqlite3"
                ),
                apply=False,
                fingerprint="",
            )
        except ValueError as exc:
            if "restricted" not in str(exc):
                raise AssertionError("unrelated recovery backup rejected with unexpected error") from exc
        else:
            raise AssertionError("cleanup unexpectedly accepted an unrelated recovery backup")
    ui_flow_args = hosted_runtime.build_arg_parser().parse_args(
        ["warehouse-ui-flow", "--evidence-dir", "/tmp/wb-core-warehouse-ui-smoke"]
    )
    if ui_flow_args.handler is not hosted_runtime.run_warehouse_ui_flow_command:
        raise AssertionError("hosted runner must expose canonical warehouse-ui-flow command")
    recovery_ui_flow_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "warehouse-ui-flow",
            "--evidence-dir",
            "/tmp/wb-core-warehouse-recovery-ui-smoke",
            "--acceptance-profile",
            "warehouse_recovery_policy_20260726",
        ]
    )
    if (
        recovery_ui_flow_args.acceptance_profile
        != "warehouse_recovery_policy_20260726"
    ):
        raise AssertionError("hosted runner must expose recovery-policy UI acceptance")
    recovery_canary_dry_args = hosted_runtime.build_arg_parser().parse_args(
        ["warehouse-recovery-canary-dry-run", "--deployed-sha", "a" * 40]
    )
    recovery_canary_apply_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "warehouse-recovery-canary-apply",
            "--deployed-sha",
            "a" * 40,
            "--fingerprint",
            "sha256:" + "b" * 64,
        ]
    )
    if (
        recovery_canary_dry_args.handler
        is not hosted_runtime.run_warehouse_recovery_canary_command
        or recovery_canary_dry_args.recovery_canary_apply
        or recovery_canary_apply_args.handler
        is not hosted_runtime.run_warehouse_recovery_canary_command
        or not recovery_canary_apply_args.recovery_canary_apply
    ):
        raise AssertionError("hosted runner must expose exact dry/apply recovery canary")
    retention_apply_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "warehouse-recovery-retention-apply",
            "--deployed-sha",
            "a" * 40,
            "--fingerprint",
            "sha256:" + "b" * 64,
        ]
    )
    sanitation_plan_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "storage-recovery-sanitation-plan",
            "--deployed-sha",
            "a" * 40,
            "--root",
            "backup",
            "--family",
            "supplier-26gn390-recovery",
        ]
    )
    sanitation_submit_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "--target-file",
            str(hosted_runtime.DEFAULT_TARGET_FILE),
            "storage-recovery-sanitation-submit",
            "--deployed-sha",
            "a" * 40,
            "--job-id",
            "b" * 64,
            "--operation",
            "apply",
            "--root",
            "backup",
            "--family",
            "supplier-26gn527-vtb-recovery",
            "--fingerprint",
            "sha256:" + "c" * 64,
        ]
    )
    sanitation_status_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "--target-file",
            str(hosted_runtime.DEFAULT_TARGET_FILE),
            "storage-recovery-sanitation-status",
            "--deployed-sha",
            "a" * 40,
            "--job-id",
            "b" * 64,
        ]
    )
    maintenance_restore_submit_args = (
        hosted_runtime.build_arg_parser().parse_args(
            [
                "--target-file",
                str(hosted_runtime.DEFAULT_TARGET_FILE),
                "business-data-maintenance-restore-submit",
                "--deployed-sha",
                "a" * 40,
                "--job-id",
                "d" * 64,
                "--expected-revision",
                "19",
                "--window-id",
                "snapshot-fixture",
                "--plan-fingerprint",
                "sha256:" + "e" * 64,
                "--service-continuity-fingerprint",
                "sha256:" + "f" * 64,
                "--actor",
                "fixture_replacement_task",
                "--reason",
                "restore exact prior state",
                "--allow-pre-hold-service-continuity",
            ]
        )
    )
    maintenance_restore_status_args = (
        hosted_runtime.build_arg_parser().parse_args(
            [
                "--target-file",
                str(hosted_runtime.DEFAULT_TARGET_FILE),
                "business-data-maintenance-restore-status",
                "--deployed-sha",
                "a" * 40,
                "--job-id",
                "d" * 64,
            ]
        )
    )
    maintenance_restore_resume_args = (
        hosted_runtime.build_arg_parser().parse_args(
            [
                "--target-file",
                str(hosted_runtime.DEFAULT_TARGET_FILE),
                "business-data-maintenance-restore-resume",
                "--deployed-sha",
                "a" * 40,
                "--job-id",
                "d" * 64,
                "--expected-failure-digest",
                "sha256:" + "c" * 64,
                "--service-continuity-fingerprint",
                "sha256:" + "f" * 64,
                "--actor",
                "fixture_replacement_task",
                "--reason",
                "reviewed same-job recovery deploy",
            ]
        )
    )
    promo_gc_apply_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "promo-archive-gc-apply",
            "--deployed-sha",
            "a" * 40,
            "--fingerprint",
            "sha256:" + "c" * 64,
        ]
    )
    if (
        retention_apply_args.handler
        is not hosted_runtime.run_warehouse_recovery_retention_command
        or sanitation_plan_args.handler
        is not hosted_runtime.run_storage_recovery_sanitation_command
        or sanitation_submit_args.handler
        is not hosted_runtime.run_storage_recovery_sanitation_job_command
        or sanitation_submit_args.sanitation_job_action != "submit"
        or sanitation_status_args.handler
        is not hosted_runtime.run_storage_recovery_sanitation_job_command
        or sanitation_status_args.sanitation_job_action != "status"
        or maintenance_restore_submit_args.handler
        is not (
            hosted_runtime.run_business_data_maintenance_restore_job_command
        )
        or maintenance_restore_submit_args.maintenance_restore_job_action
        != "submit"
        or maintenance_restore_status_args.handler
        is not (
            hosted_runtime.run_business_data_maintenance_restore_job_command
        )
        or maintenance_restore_status_args.maintenance_restore_job_action
        != "status"
        or maintenance_restore_resume_args.handler
        is not (
            hosted_runtime.run_business_data_maintenance_restore_job_command
        )
        or maintenance_restore_resume_args.maintenance_restore_job_action
        != "resume"
        or promo_gc_apply_args.handler
        is not hosted_runtime.run_promo_archive_gc_command
    ):
        raise AssertionError(
            "hosted runner must expose exact retention, sanitation, detached "
            "maintenance restore and Promo GC"
        )
    completed_sanitation_job = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            {
                "contract_name": "storage_recovery_sanitation_job_v1",
                "job_id": "b" * 64,
                "status": "queued",
                "terminal": False,
            }
        ),
        stderr="",
    )
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=completed_sanitation_job,
    ) as run_mock:
        hosted_runtime.run_storage_recovery_sanitation_job_command(
            sanitation_submit_args
        )
    detached_command = " ".join(run_mock.call_args.args[0])
    if (
        run_mock.call_args.kwargs.get("timeout") != 60.0
        or "apps/storage_recovery_sanitation_job.py" not in detached_command
        or " submit " not in detached_command
        or "--job-id " + "b" * 64 not in detached_command
        or "--fingerprint sha256:" + "c" * 64 not in detached_command
    ):
        raise AssertionError(
            "hosted detached sanitation lost exact job/request transport"
        )
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=completed_sanitation_job,
    ) as run_mock:
        hosted_runtime.run_storage_recovery_sanitation_job_command(
            sanitation_status_args
        )
    status_command = " ".join(run_mock.call_args.args[0])
    if (
        run_mock.call_args.kwargs.get("timeout") != 60.0
        or " status " not in status_command
        or "--fingerprint" in status_command
        or "--family" in status_command
    ):
        raise AssertionError(
            "hosted sanitation status is not bounded/read-only by exact job id"
        )
    completed_restore_job = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            {
                "contract_name": (
                    "business_data_maintenance_restore_job_v1"
                ),
                "job_id": "d" * 64,
                "status": "queued",
                "terminal": False,
            }
        ),
        stderr="",
    )
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=completed_restore_job,
    ) as run_mock:
        hosted_runtime.run_business_data_maintenance_restore_job_command(
            maintenance_restore_submit_args
        )
    restore_submit_command = " ".join(run_mock.call_args.args[0])
    if (
        run_mock.call_args.kwargs.get("timeout") != 60.0
        or "apps/business_data_maintenance_restore_job.py"
        not in restore_submit_command
        or " submit " not in restore_submit_command
        or "--job-id " + "d" * 64 not in restore_submit_command
        or "--expected-revision 19" not in restore_submit_command
        or "--window-id snapshot-fixture" not in restore_submit_command
        or "--plan-fingerprint sha256:" + "e" * 64
        not in restore_submit_command
        or "--service-continuity-fingerprint sha256:" + "f" * 64
        not in restore_submit_command
        or "--allow-pre-hold-service-continuity"
        not in restore_submit_command
    ):
        raise AssertionError(
            "hosted detached maintenance restore lost exact request transport"
        )
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=completed_restore_job,
    ) as run_mock:
        hosted_runtime.run_business_data_maintenance_restore_job_command(
            maintenance_restore_status_args
        )
    restore_status_command = " ".join(run_mock.call_args.args[0])
    if (
        run_mock.call_args.kwargs.get("timeout") != 60.0
        or " status " not in restore_status_command
        or "--expected-revision" in restore_status_command
        or "--window-id" in restore_status_command
        or "--plan-fingerprint" in restore_status_command
        or "--allow-pre-hold-service-continuity"
        in restore_status_command
    ):
        raise AssertionError(
            "hosted maintenance restore status is not exact/read-only"
        )
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=completed_restore_job,
    ) as run_mock:
        hosted_runtime.run_business_data_maintenance_restore_job_command(
            maintenance_restore_resume_args
        )
    restore_resume_command = " ".join(run_mock.call_args.args[0])
    if (
        run_mock.call_args.kwargs.get("timeout") != 60.0
        or " resume " not in restore_resume_command
        or "--expected-failure-digest sha256:" + "c" * 64
        not in restore_resume_command
        or "--service-continuity-fingerprint sha256:" + "f" * 64
        not in restore_resume_command
        or "--actor fixture_replacement_task" not in restore_resume_command
        or "reviewed same-job recovery deploy" not in restore_resume_command
        or "--expected-revision" in restore_resume_command
        or "--plan-fingerprint" in restore_resume_command
    ):
        raise AssertionError(
            "hosted same-job restore resume lost exact recovery evidence"
        )
    finance_ui_flow_args = hosted_runtime.build_arg_parser().parse_args(
        ["finance-ui-flow", "--evidence-dir", "/tmp/wb-core-finance-ui-smoke"]
    )
    if finance_ui_flow_args.handler is not hosted_runtime.run_finance_ui_flow_command:
        raise AssertionError("hosted runner must expose canonical finance-ui-flow command")
    sqlite_contention_ui_flow_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "sqlite-contention-ui-flow",
            "--evidence-dir",
            "/tmp/wb-core-sqlite-contention-ui-smoke",
            "--deployed-sha",
            "a" * 40,
        ]
    )
    if (
        sqlite_contention_ui_flow_args.handler
        is not hosted_runtime.run_sqlite_contention_ui_flow_command
    ):
        raise AssertionError(
            "hosted runner must expose canonical sqlite-contention-ui-flow command"
        )
    sqlite_contention_ui_source = (
        ROOT / "apps" / "sqlite_contention_production_ui_flow.py"
    ).read_text(encoding="utf-8")
    if (
        "?embedded=operator&shipment_id=" not in sqlite_contention_ui_source
        or "&tab=documents" not in sqlite_contention_ui_source
        or "&tab=financial" in sqlite_contention_ui_source
        or "normalized_group_text.casefold()" not in sqlite_contention_ui_source
    ):
        raise AssertionError(
            "SQLite contention UI flow must use the operator-embedded supplier "
            "documents deep-link contract and case-insensitive visual text "
            "assertions"
        )
    rollback_plan_args = hosted_runtime.build_arg_parser().parse_args(
        ["autoanswers-store-rollback-plan"]
    )
    rollback_apply_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "autoanswers-store-rollback-apply",
            "--fingerprint",
            "sha256:" + "a" * 64,
        ]
    )
    if (
        rollback_plan_args.handler
        is not hosted_runtime.run_autoanswers_store_rollback_command
        or rollback_plan_args.rollback_apply
        or rollback_apply_args.handler
        is not hosted_runtime.run_autoanswers_store_rollback_command
        or not rollback_apply_args.rollback_apply
    ):
        raise AssertionError(
            "hosted runner must expose canonical Autoanswers store rollback commands"
        )
    if finance_ui_flow.REPORTS_PATH != "/sheet-vitrina-v1/vitrina?tab=reports":
        raise AssertionError("Finance UI Flow must enter through canonical unified navigation")
    required_partner_fields = ("partner_share_pct", "invested_capital_rub")
    if not finance_ui_flow._has_complete_partner_settings(
        {
            "settings": {
                "parameters": {
                    "partner_share_pct": "40",
                    "invested_capital_rub": "1",
                }
            }
        },
        required_partner_fields,
    ):
        raise AssertionError("Finance UI Flow must recognize complete existing Partner settings")
    if finance_ui_flow._has_complete_partner_settings(
        {"settings": {"parameters": {"partner_share_pct": "40"}}},
        required_partner_fields,
    ):
        raise AssertionError("Finance UI Flow must not preview incomplete Partner settings")
    period_vitrina_url = warehouse_ui_flow._period_vitrina_url(
        "https://api.selleros.pro/",
        date_to="2026-07-19",
    )
    if period_vitrina_url != (
        "https://api.selleros.pro/sheet-vitrina-v1/vitrina"
        "?tab=vitrina&history_mode=explicit&date_from=2026-07-01&date_to=2026-07-19"
    ):
        raise AssertionError("warehouse UI Flow must ignore persisted tabs for period acceptance")
    failed_backup_source = (
        "/opt/wb-core-runtime/backups/warehouse-functional/"
        "warehouse_functional_cutover_v1-20260719T001627Z.sqlite3"
    )
    cleanup_dry_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "warehouse-functional-failed-backup-cleanup-dry-run",
            "--source",
            failed_backup_source,
        ]
    )
    if (
        cleanup_dry_args.handler
        is not hosted_runtime.run_warehouse_functional_failed_backup_cleanup_command
        or cleanup_dry_args.cleanup_apply is not False
    ):
        raise AssertionError("hosted runner must expose read-only failed-backup cleanup planning")
    cleanup_apply_args = hosted_runtime.build_arg_parser().parse_args(
        [
            "warehouse-functional-failed-backup-cleanup-apply",
            "--source",
            failed_backup_source,
            "--fingerprint",
            "sha256:cleanup-smoke",
        ]
    )
    if cleanup_apply_args.cleanup_apply is not True:
        raise AssertionError("hosted runner must explicitly distinguish cleanup apply")
    with TemporaryDirectory(prefix="ff-inventory-hosted-smoke-") as inventory_temp_dir:
        inventory_source = Path(inventory_temp_dir) / "manager.xlsx"
        inventory_source.write_bytes(b"fixture-xlsx")
        inventory_args = hosted_runtime.build_arg_parser().parse_args(
            [
                "ff-inventory-reconciliation-apply",
                "--source-file",
                str(inventory_source),
                "--business-date",
                "2026-07-31",
                "--return-supply-id",
                "41132380",
                "--fingerprint",
                "sha256:inventory-fixture",
                "--approval-reference",
                "github-comment:fixture",
            ]
        )
        if (
            inventory_args.handler is not hosted_runtime.run_ff_inventory_reconciliation_command
            or inventory_args.ff_inventory_action != "apply"
        ):
            raise AssertionError("hosted runner must expose the gated FF inventory apply command")
        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_ff_inventory_reconciliation",
                return_value={"status": "applied", "fingerprint": "sha256:inventory-fixture"},
            ) as inventory_remote,
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_ff_inventory_reconciliation_command(inventory_args)
        inventory_call = inventory_remote.call_args.kwargs
        if (
            inventory_call.get("business_date") != "2026-07-31"
            or inventory_call.get("return_supply_ids") != ("41132380",)
            or inventory_call.get("fingerprint") != "sha256:inventory-fixture"
            or inventory_call.get("approval_reference") != "github-comment:fixture"
        ):
            raise AssertionError("hosted FF inventory apply lost its exact manifest gate inputs")
    opening_args = hosted_runtime.build_arg_parser().parse_args(["warehouse-opening-readback"])
    if opening_args.handler is not hosted_runtime.run_warehouse_opening_command:
        raise AssertionError("hosted runner must expose canonical warehouse opening commands")
    functional_backup_args = hosted_runtime.build_arg_parser().parse_args(
        ["warehouse-functional-backup"]
    )
    if (
        functional_backup_args.handler is not hosted_runtime.run_warehouse_functional_command
        or functional_backup_args.warehouse_functional_action != "backup"
    ):
        raise AssertionError("hosted runner must expose coherent warehouse functional backup")
    with TemporaryDirectory(prefix="warehouse-functional-reviewed-plan-smoke-") as plan_temp_dir:
        reviewed_plan_path = Path(plan_temp_dir) / "functional-plan.json"
        functional_args = hosted_runtime.build_arg_parser().parse_args(
            ["warehouse-functional-dry-run", "--output", str(reviewed_plan_path)]
        )
        remote_payload = {
            "kind": "functional_cutover",
            "plan_fingerprint": "sha256:reviewed-plan-smoke",
            "calculation_digest": "sha256:calculation-smoke",
            "preflight_supply_refresh": {"production_source_mutation": False},
        }
        with (
            mock.patch.object(
                hosted_runtime,
                "_run_remote_warehouse_functional_action",
                return_value=remote_payload,
            ),
            mock.patch.object(hosted_runtime, "_print_json"),
        ):
            hosted_runtime.run_warehouse_functional_command(functional_args)
        reviewed_plan = json.loads(reviewed_plan_path.read_text(encoding="utf-8"))
        if "preflight_supply_refresh" in reviewed_plan:
            raise AssertionError("diagnostic refresh evidence must not alter the exact reviewed plan")
        if reviewed_plan.get("plan_fingerprint") != "sha256:reviewed-plan-smoke":
            raise AssertionError("hosted dry-run must preserve the exact signed plan fingerprint")
    diagnostic_args = hosted_runtime.build_arg_parser().parse_args(
        ["warehouse-opening-diagnostic", "--nm-id", "180330785"]
    )
    if (
        diagnostic_args.handler is not hosted_runtime.run_warehouse_opening_command
        or diagnostic_args.warehouse_action != "diagnose-discrepancy"
        or diagnostic_args.nm_id != [180330785]
    ):
        raise AssertionError("hosted runner must expose bounded warehouse discrepancy diagnostics")
    with TemporaryDirectory(prefix="warehouse-env-loader-smoke-") as env_temp_dir:
        env_path = Path(env_temp_dir) / "runtime.env"
        env_path.write_text(
            "WAREHOUSE_OPENING_ENV_SMOKE_NAME=Влад Сагитов\n"
            "WAREHOUSE_OPENING_ENV_SMOKE_COMPANY='Acme Technology' # comment\n"
            "WAREHOUSE_OPENING_ENV_SMOKE_LITERAL=$(must_not_execute)\n",
            encoding="utf-8",
        )
        previous_name = os.environ.get("WAREHOUSE_OPENING_ENV_SMOKE_NAME")
        previous_company = os.environ.get("WAREHOUSE_OPENING_ENV_SMOKE_COMPANY")
        previous_literal = os.environ.get("WAREHOUSE_OPENING_ENV_SMOKE_LITERAL")
        try:
            warehouse_opening_snapshot._load_env_file(env_path)
            if os.environ.get("WAREHOUSE_OPENING_ENV_SMOKE_NAME") != "Влад Сагитов":
                raise AssertionError("warehouse env loader must preserve unquoted spaces as data")
            if os.environ.get("WAREHOUSE_OPENING_ENV_SMOKE_COMPANY") != "Acme Technology":
                raise AssertionError("warehouse env loader must parse quoted values without shell evaluation")
            if os.environ.get("WAREHOUSE_OPENING_ENV_SMOKE_LITERAL") != "$(must_not_execute)":
                raise AssertionError("warehouse env loader must keep shell syntax as literal data")
        finally:
            for key, previous in (
                ("WAREHOUSE_OPENING_ENV_SMOKE_NAME", previous_name),
                ("WAREHOUSE_OPENING_ENV_SMOKE_COMPANY", previous_company),
                ("WAREHOUSE_OPENING_ENV_SMOKE_LITERAL", previous_literal),
            ):
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous
    complete_payload = json.dumps({"rows": ["x" * 256] * 4096}, separators=(",", ":")).encode("utf-8")
    body, truncated, bytes_read = hosted_runtime._read_probe_response_body(
        _ShortReadResponse(complete_payload, chunk_size=64 * 1024)
    )
    if truncated is not True or bytes_read != hosted_runtime.PROBE_BODY_LIMIT_BYTES:
        raise AssertionError("probe reader must keep reading short socket chunks through the bounded limit")
    if not body.startswith('{"rows":['):
        raise AssertionError("probe reader must retain the bounded JSON prefix")

    short_payload = json.dumps({"rows": ["ok"] * 1000}, separators=(",", ":")).encode("utf-8")
    body, truncated, bytes_read = hosted_runtime._read_probe_response_body(
        _ShortReadResponse(short_payload, chunk_size=1024)
    )
    if truncated is not False or bytes_read != len(short_payload) or json.loads(body)["rows"][-1] != "ok":
        raise AssertionError("probe reader must assemble all short reads before declaring EOF")

    truncated_warehouse_prefix = json.dumps(
        {
            "contract_name": "sheet_vitrina_v1_warehouse_functional",
            "contract_version": "v2",
            "status": "ready",
            "probe_shape": {
                "warehouse_key": "ff",
                "required_collections": ["balances", "documents"],
            },
            "warehouse": {"warehouse_key": "ff", "warehouse_name": "Склад FF"},
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )[:-1] + ',"balances":[{"oversized":"'
    warehouse_result = {
        "route": "warehouse_ff",
        "method": "GET",
        "url": "http://127.0.0.1:8765/v1/sheet-vitrina-v1/warehouses/ff",
        "http_status": 200,
        "content_type": "application/json; charset=utf-8",
        "body_excerpt": truncated_warehouse_prefix,
        "body_truncated": True,
        "body_bytes_read": hosted_runtime.PROBE_BODY_LIMIT_BYTES,
        "json_body": None,
        "network_error": None,
    }
    warehouse_evaluation = hosted_runtime._evaluate_route_result(
        warehouse_result,
        route_paths=active_target.route_paths,
    )
    if warehouse_evaluation["ok"] is not True:
        raise AssertionError(
            "truncated canonical FF warehouse detail must remain verifiable: "
            + str(warehouse_evaluation)
        )
    for invalid_warehouse in (True, {"warehouse_key": "wb"}):
        invalid_payload = {
            "contract_name": "sheet_vitrina_v1_warehouse_functional",
            "contract_version": "v2",
            "status": "ready",
            "probe_shape": {
                "warehouse_key": "ff",
                "required_collections": ["balances", "documents"],
            },
            "warehouse": invalid_warehouse,
        }
        invalid_result = {
            **warehouse_result,
            "body_excerpt": json.dumps(
                invalid_payload,
                separators=(",", ":"),
                ensure_ascii=False,
            )[:-1]
            + ',"balances":[{"oversized":"',
        }
        invalid_evaluation = hosted_runtime._evaluate_route_result(
            invalid_result,
            route_paths=active_target.route_paths,
        )
        if invalid_evaluation["ok"] is not False:
            raise AssertionError(
                "invalid truncated FF warehouse detail must fail closed: "
                + str(invalid_evaluation)
            )
    missing_shape_result = {
        **warehouse_result,
        "body_excerpt": json.dumps(
            {
                "contract_name": "sheet_vitrina_v1_warehouse_functional",
                "contract_version": "v2",
                "status": "ready",
                "warehouse": {"warehouse_key": "ff"},
            },
            separators=(",", ":"),
        )[:-1]
        + ',"balances":[{"oversized":"',
    }
    missing_shape_evaluation = hosted_runtime._evaluate_route_result(
        missing_shape_result,
        route_paths=active_target.route_paths,
    )
    if missing_shape_evaluation["ok"] is not False:
        raise AssertionError(
            "truncated FF warehouse detail without bounded shape evidence must fail closed"
        )
    wrong_shape_result = {
        **warehouse_result,
        "body_excerpt": truncated_warehouse_prefix.replace(
            '"required_collections":["balances","documents"]',
            '"required_collections":["balances"]',
        ),
    }
    wrong_shape_evaluation = hosted_runtime._evaluate_route_result(
        wrong_shape_result,
        route_paths=active_target.route_paths,
    )
    if wrong_shape_evaluation["ok"] is not False:
        raise AssertionError(
            "truncated FF warehouse detail with incomplete bounded shape must fail closed"
        )

    with TemporaryDirectory(prefix="hosted-runtime-contract-smoke-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        target_file = Path(tmp) / "target.json"
        deploy_target_file = Path(tmp) / "deploy_target.json"
        archived_target_file = Path(tmp) / "archived_target.json"
        port = _reserve_free_port()
        base_url = f"http://127.0.0.1:{port}"
        base_target_payload = {
            "target_status": "local_test",
            "target_id": "local_smoke_target",
            "public_base_url": base_url,
            "loopback_base_url": base_url,
            "ssh_destination": "",
            "target_dir": "/srv/wb-core",
            "service_name": "wb-core-registry-upload",
            "restart_command": "sudo systemctl restart wb-core-registry-upload",
            "status_command": "sudo systemctl status --no-pager wb-core-registry-upload",
            "environment_file": "/etc/wb-core/registry-upload.env",
            "systemd_unit_directory": "/etc/systemd/system",
            "systemd_units_source_dir": "artifacts/registry_upload_http_entrypoint/systemd",
            "nginx_public_routes": {
                "server_config_path": "/etc/nginx/sites-enabled/wb-ai",
                "backup_dir": "/etc/nginx/sites-enabled",
                "test_command": "nginx -t",
                "reload_command": "systemctl reload nginx",
                "manifest_path": "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json",
                "managed_block_label": "WB-CORE MANAGED PUBLIC ROUTES",
            },
            "managed_systemd_units": [
                {
                    "name": "wb-core-sheet-vitrina-refresh.service",
                    "enable": False,
                    "restart": False,
                },
                {
                    "name": "wb-core-sheet-vitrina-refresh.timer",
                    "enable": True,
                    "restart": True,
                },
            ],
            "retired_systemd_units": [
                "wb-core-spp-tester-schedule-tick.timer",
                "wb-core-spp-tester-schedule-tick.service",
            ],
            "runtime_env": {
                "REGISTRY_UPLOAD_HTTP_HOST": "127.0.0.1",
                "REGISTRY_UPLOAD_HTTP_PORT": str(port),
                "REGISTRY_UPLOAD_RUNTIME_DIR": str(runtime_dir),
                "REGISTRY_UPLOAD_HTTP_PATH": "/v1/registry-upload/bundle",
                "COST_PRICE_UPLOAD_HTTP_PATH": "/v1/cost-price/upload",
                "SHEET_VITRINA_HTTP_PATH": "/v1/sheet-vitrina-v1/plan",
                "SHEET_VITRINA_REFRESH_HTTP_PATH": "/v1/sheet-vitrina-v1/refresh",
                "SHEET_VITRINA_STATUS_HTTP_PATH": "/v1/sheet-vitrina-v1/status",
                "SHEET_VITRINA_OPERATOR_UI_PATH": "/sheet-vitrina-v1/operator",
            },
        }
        target_file.write_text(
            json.dumps(base_target_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        deploy_target_payload = dict(base_target_payload)
        deploy_target_payload.update(
            {
                "target_status": "active",
                "public_base_url": "https://api.selleros.pro",
                "ssh_destination": "wb-core-eu-root",
                "target_dir": "/opt/wb-core-runtime/app",
                "service_name": "wb-core-registry-http.service",
                "restart_command": "systemctl restart wb-core-registry-http.service",
                "status_command": "systemctl status --no-pager --full wb-core-registry-http.service",
                "root_storage_policy_file": "artifacts/registry_upload_http_entrypoint/root_storage_policy_v1.json",
            }
        )
        deploy_target_payload["runtime_env"] = {
            **base_target_payload["runtime_env"],
            "REGISTRY_UPLOAD_RUNTIME_DIR": "/opt/wb-core-runtime/state",
        }
        deploy_target_payload["managed_systemd_units"] = [
            *base_target_payload["managed_systemd_units"],
            {
                "name": "wb-core-root-storage-policy.service",
                "enable": False,
                "restart": True,
            },
            {
                "name": "wb-core-root-storage-policy.timer",
                "enable": True,
                "restart": True,
            },
        ]
        deploy_target_file.write_text(
            json.dumps(deploy_target_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        archived_target_payload = dict(deploy_target_payload)
        archived_target_payload.update(
            {
                "target_status": "archived",
                "target_role": "rollback_only",
                "target_lifecycle": "deprecated_live_target",
                "mutation_policy": "do_not_deploy_without_emergency_rollback_override",
                "provider_side_label_recommendation": "ROLLBACK-ONLY_DO-NOT-DEPLOY_wb-core-old-selleros",
                "target_id": "archived_selleros_target",
                "public_base_url": "https://api.selleros.pro",
                "ssh_destination": "selleros-root",
            }
        )
        archived_target_file.write_text(
            json.dumps(archived_target_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.update(
            {
                "REGISTRY_UPLOAD_HTTP_PORT": str(port),
                "REGISTRY_UPLOAD_RUNTIME_DIR": str(runtime_dir),
            }
        )
        process = subprocess.Popen(
            [sys.executable, str(LIVE_RUNNER)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_until_ready(f"{base_url}/sheet-vitrina-v1/operator")
            _post_bundle(f"{base_url}/v1/registry-upload/bundle")
            _seed_ready_snapshot(runtime_dir)

            print_plan = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(target_file),
                    "print-plan",
                ]
            )
            if print_plan["deploy_plan"]["applicable_to_current_checkout_without_merge"] is not True:
                raise AssertionError("print-plan must confirm applicability to current checkout")
            if "WB_API_TOKEN" not in print_plan["required_secret_contract"]:
                raise AssertionError("print-plan must expose canonical secret contract")
            if "PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH" not in print_plan["optional_runtime_contract"]:
                raise AssertionError("print-plan must expose promo collector storage-state override contract")
            if "SELLER_PORTAL_CANONICAL_SUPPLIER_ID" not in print_plan["optional_runtime_contract"]:
                raise AssertionError("print-plan must expose canonical seller supplier id contract")
            if "SELLER_PORTAL_RELOGIN_SSH_DESTINATION" not in print_plan["optional_runtime_contract"]:
                raise AssertionError("print-plan must expose seller recovery SSH destination contract")
            if len(print_plan["deploy_plan"]["managed_systemd_units"]) != 2:
                raise AssertionError("print-plan must expose managed systemd units when configured")
            if print_plan["deploy_plan"]["retired_systemd_units"] != [
                "wb-core-spp-tester-schedule-tick.timer",
                "wb-core-spp-tester-schedule-tick.service",
            ]:
                raise AssertionError("print-plan must expose retired SPP schedule units in order")
            deploy_sequence = print_plan["deploy_plan"]["deploy_sequence"]
            if not (
                deploy_sequence.index(
                    "disable, stop and remove explicitly retired systemd units before runtime sync"
                )
                < deploy_sequence.index("sync current checked-out worktree to target_dir via rsync")
            ):
                raise AssertionError("retired SPP schedule units must be stopped before runtime sync")
            corrective_deploy_sequence = hosted_runtime.build_deploy_plan(
                hosted_runtime.load_hosted_runtime_target(deploy_target_file)
            )["deploy_sequence"]
            corrective_step = (
                "remove the exact block-003 journald drop-in and submit one corrective journald restart"
            )
            if not (
                corrective_deploy_sequence.index("restart hosted runtime via restart_command")
                < corrective_deploy_sequence.index(corrective_step)
                < corrective_deploy_sequence.index("probe loopback/runtime contour")
            ):
                raise AssertionError(
                    "corrective journald submit must follow all ordinary deploy mutations"
                )
            nginx_routes = print_plan["deploy_plan"].get("nginx_public_routes") or {}
            if nginx_routes.get("route_count", 0) < 20:
                raise AssertionError("print-plan must expose nginx public route allowlist")
            if "/v1/sheet-vitrina-v1/feedbacks" not in {item["path"] for item in nginx_routes.get("routes", [])}:
                raise AssertionError("nginx public route allowlist must include feedbacks route")
            if (
                "/v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-options"
                not in {item["path"] for item in nginx_routes.get("routes", [])}
            ):
                raise AssertionError(
                    "nginx public route allowlist must include WB warehouse exclusion options"
                )
            exclusion_settings_routes = {
                item["path"]: item
                for item in nginx_routes.get("routes", [])
                if item["path"]
                == "/v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-settings"
            }
            if not exclusion_settings_routes:
                raise AssertionError(
                    "nginx public route allowlist must include WB warehouse exclusion settings"
                )
            if exclusion_settings_routes[
                "/v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-settings"
            ].get("methods") != ["GET", "POST"]:
                raise AssertionError(
                    "WB warehouse exclusion settings route must publish GET and POST"
                )
            if (
                "/v1/sheet-vitrina-v1/settings/auto-updates"
                not in {item["path"] for item in nginx_routes.get("routes", [])}
            ):
                raise AssertionError(
                    "nginx public route allowlist must include Settings auto-updates control plane"
                )

            deploy_dry_run = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(deploy_target_file),
                    "deploy",
                    "--dry-run",
                    "--allow-dirty",
                ]
            )
            if deploy_dry_run["dry_run"] is not True:
                raise AssertionError("deploy --dry-run must stay dry-run")
            if "rsync" not in " ".join(deploy_dry_run["commands"]["rsync"]):
                raise AssertionError("deploy --dry-run must expose rsync command")
            if "chown -R root:root /opt/wb-core-runtime/app" not in " ".join(
                deploy_dry_run["commands"]["chown_target_dir"]
            ):
                raise AssertionError("deploy --dry-run must expose target_dir ownership normalization")
            root_status_command = " ".join(deploy_dry_run["commands"]["root_storage_status"])
            journald_operation_command = " ".join(
                deploy_dry_run["commands"]["journald_operation"]
            )
            journald_readback_command = " ".join(
                deploy_dry_run["commands"]["journald_operation_readback"]
            )
            if " status " not in root_status_command or "--fail-on-unregistered" not in root_status_command:
                raise AssertionError("deploy must publish status and reject unregistered large root producers")
            if "/var/lib/wb-core-root-storage-policy/status.json" not in root_status_command:
                raise AssertionError("deploy must atomically publish the server-owned root status artifact")
            root_status_readback_command = " ".join(
                deploy_dry_run["commands"]["root_storage_status_artifact_readback"]
            )
            if "status-readback" not in root_status_readback_command:
                raise AssertionError("deploy must validate the fresh server-owned root status artifact")
            if deploy_dry_run["commands"]["journald_operation_name"] != "corrective_remove":
                raise AssertionError("deploy must select the corrective journald operation")
            if "journald-corrective-remove" not in journald_operation_command:
                raise AssertionError("deploy must expose one canonical journald correction")
            if "journald-corrective-readback" not in journald_readback_command:
                raise AssertionError("deploy must expose query-only corrective reconciliation")
            preparing_metadata = " ".join(
                deploy_dry_run["commands"]["deploy_metadata"]
            )
            completed_metadata = " ".join(
                deploy_dry_run["commands"]["deploy_completion_metadata"]
            )
            if (
                '"deployment_complete":false' not in preparing_metadata
                or '"deployment_complete":true' not in completed_metadata
                or "wb_core_deploy_metadata_v2" not in preparing_metadata
                or "wb_core_deploy_metadata_v2" not in completed_metadata
            ):
                raise AssertionError(
                    "deploy plan must distinguish early SHA visibility from final completion proof"
                )
            seller_os_command = " ".join(deploy_dry_run["commands"]["seller_portal_recovery_os_dependencies"])
            if "python3-venv" not in seller_os_command or "xvfb" not in seller_os_command:
                raise AssertionError("deploy --dry-run must expose seller recovery OS dependency install")
            if "websockify" not in seller_os_command or "/usr/share/novnc" not in seller_os_command:
                raise AssertionError("deploy --dry-run must expose noVNC/websockify dependency checks")
            owner_os_command = " ".join(deploy_dry_run["commands"]["seller_portal_owner_runtime_os_dependencies"])
            if "postgresql" not in owner_os_command or "systemctl enable --now postgresql" not in owner_os_command:
                raise AssertionError("deploy --dry-run must expose owner runtime PostgreSQL dependency install")
            if "openpyxl==3.1.5" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must expose runtime pip install command for openpyxl")
            if "xlrd==2.0.1" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must expose runtime pip install command for xlrd")
            if "playwright==1.58.0" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must expose runtime pip install command for playwright")
            if "pypdf==6.4.1" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must expose runtime pip install command for pypdf")
            if "reportlab==4.4.5" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must expose runtime pip install command for reportlab")
            if "import openpyxl, xlrd, playwright, pypdf, reportlab" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must guard on openpyxl, xlrd, playwright, pypdf and reportlab imports")
            seller_venv_command = " ".join(deploy_dry_run["commands"]["seller_portal_recovery_venv"])
            if "python3 -m venv /opt/wb-web-bot/venv" not in seller_venv_command:
                raise AssertionError("deploy --dry-run must create or repair /opt/wb-web-bot/venv")
            if "playwright==1.58.0" not in seller_venv_command:
                raise AssertionError("deploy --dry-run must install Playwright into wb-web-bot venv")
            if "psycopg2-binary==2.9.11" not in seller_venv_command:
                raise AssertionError("deploy --dry-run must install psycopg2 into wb-web-bot venv")
            owner_venv_command = " ".join(deploy_dry_run["commands"]["seller_portal_owner_runtime_venv"])
            if "python3 -m venv --clear /opt/wb-ai/venv" not in owner_venv_command:
                raise AssertionError("deploy --dry-run must repair /opt/wb-ai/venv when owner imports fail")
            if "fastapi==0.129.1" not in owner_venv_command or "uvicorn==0.41.0" not in owner_venv_command:
                raise AssertionError("deploy --dry-run must install wb-ai API packages")
            owner_contract_command = " ".join(deploy_dry_run["commands"]["seller_portal_owner_runtime_contract"])
            if "/opt/wb-web-bot/bot/runner_day.py" not in owner_contract_command:
                raise AssertionError("deploy --dry-run must verify wb-web-bot owner code")
            if "/opt/wb-ai/run_web_source_handoff.py" not in owner_contract_command:
                raise AssertionError("deploy --dry-run must verify wb-ai handoff code")
            seller_browser_command = " ".join(deploy_dry_run["commands"]["seller_portal_recovery_playwright_browser"])
            if "playwright install --with-deps chromium" not in seller_browser_command:
                raise AssertionError("deploy --dry-run must expose Playwright Chromium dependency install")
            if "/opt/wb-web-bot/venv/bin/python -m playwright install chromium" not in seller_browser_command:
                raise AssertionError("deploy --dry-run must expose wb-web-bot venv browser install")
            if "install" not in " ".join(deploy_dry_run["commands"]["systemd_install"]):
                raise AssertionError("deploy --dry-run must expose systemd install command")
            retirement_command = " ".join(deploy_dry_run["commands"]["systemd_retire"])
            if (
                "systemctl disable --now wb-core-spp-tester-schedule-tick.timer"
                not in retirement_command
                or "systemctl disable --now wb-core-spp-tester-schedule-tick.service"
                not in retirement_command
                or "rm -f /etc/systemd/system/wb-core-spp-tester-schedule-tick.timer"
                not in retirement_command
                or "rm -f /etc/systemd/system/wb-core-spp-tester-schedule-tick.service"
                not in retirement_command
            ):
                raise AssertionError(
                    "deploy --dry-run must disable, stop and remove both retired SPP schedule units"
                )
            if "daemon-reload" not in " ".join(deploy_dry_run["commands"]["systemd_daemon_reload"]):
                raise AssertionError("deploy --dry-run must expose daemon-reload command")
            if "enable" not in " ".join(deploy_dry_run["commands"]["systemd_enable"]):
                raise AssertionError("deploy --dry-run must expose systemd enable command")
            if "restart" not in " ".join(deploy_dry_run["commands"]["systemd_restart"]):
                raise AssertionError("deploy --dry-run must expose systemd restart command")
            systemd_install = " ".join(deploy_dry_run["commands"]["systemd_install"])
            systemd_enable = " ".join(deploy_dry_run["commands"]["systemd_enable"])
            systemd_restart = " ".join(deploy_dry_run["commands"]["systemd_restart"])
            if (
                "wb-core-root-storage-policy.service" not in systemd_install
                or "wb-core-root-storage-policy.timer" not in systemd_install
                or "wb-core-root-storage-policy.timer" not in systemd_enable
                or "wb-core-root-storage-policy.service" not in systemd_restart
                or "wb-core-root-storage-policy.timer" not in systemd_restart
            ):
                raise AssertionError("deploy must install and activate the root-storage monitor timer")
            command_choices = hosted_runtime.build_arg_parser()._subparsers._group_actions[0].choices
            for required_command in (
                "root-storage-status",
                "root-storage-readback",
                "root-storage-admission",
                "journald-retention-readback",
                "journald-corrective-readback",
            ):
                if required_command not in command_choices:
                    raise AssertionError(f"hosted adapter must expose {required_command}")
            transport_summary: dict[str, object] = {}
            with mock.patch.object(
                hosted_runtime,
                "_run_command",
                side_effect=subprocess.CalledProcessError(255, ["ssh", "activate"]),
            ) as activation_submit, mock.patch.object(
                hosted_runtime.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["ssh", "readback"], 0, '{"ok":true}', ""
                ),
            ) as activation_readback:
                hosted_runtime._run_journald_operation_once(
                    operation_command=["ssh", "correct"],
                    readback_command=["ssh", "readback"],
                    summary=transport_summary,
                )
            if activation_submit.call_count != 1 or activation_readback.call_count != 1:
                raise AssertionError("ambiguous journald correction must submit once then read back once")
            if transport_summary["journald_transport_reconciliation"]["operation_retried"] is not False:
                raise AssertionError("ambiguous journald operation must never retry")
            refresh_unit = (
                ROOT
                / "artifacts"
                / "registry_upload_http_entrypoint"
                / "systemd"
                / "wb-core-sheet-vitrina-refresh.service"
            ).read_text(encoding="utf-8")
            if "--runtime-dir /opt/wb-core-runtime/state" not in refresh_unit:
                raise AssertionError("refresh tick systemd unit must pin production runtime state dir")
            functional_service = (
                ROOT
                / "artifacts"
                / "registry_upload_http_entrypoint"
                / "systemd"
                / "wb-core-warehouse-functional-sync.service"
            ).read_text(encoding="utf-8")
            functional_timer = (
                ROOT
                / "artifacts"
                / "registry_upload_http_entrypoint"
                / "systemd"
                / "wb-core-warehouse-functional-sync.timer"
            ).read_text(encoding="utf-8")
            if (
                "apps/warehouse_functional_runner.py" not in functional_service
                or "hourly-sync" not in functional_service
                or "sheet-vitrina-refresh" in functional_service
            ):
                raise AssertionError("functional scheduler must run only the bounded warehouse runner")
            if "TimeoutStartSec=3h" not in functional_service:
                raise AssertionError(
                    "functional scheduler must outlive a complete backup, publication and archive cycle"
                )
            if "OnCalendar=*-*-* *:17:00 Europe/Moscow" not in functional_timer:
                raise AssertionError("functional scheduler must run hourly in the explicit business timezone")
            for removed_spp_unit in (
                "wb-core-spp-tester-schedule-tick.service",
                "wb-core-spp-tester-schedule-tick.timer",
            ):
                if (
                    ROOT
                    / "artifacts"
                    / "registry_upload_http_entrypoint"
                    / "systemd"
                    / removed_spp_unit
                ).exists():
                    raise AssertionError(f"removed SPP scheduler artifact still exists: {removed_spp_unit}")
            if "apply-nginx-routes" not in " ".join(deploy_dry_run["commands"]["nginx_public_routes_update"]):
                raise AssertionError("deploy --dry-run must expose nginx public route update command")

            archived_print_plan = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "print-plan",
                ]
            )
            archived_plan = archived_print_plan["deploy_plan"]
            if archived_plan["target_role"] != "rollback_only":
                raise AssertionError("archived selleros print-plan must expose rollback_only target_role")
            if archived_plan["target_lifecycle"] != "deprecated_live_target":
                raise AssertionError("archived selleros print-plan must expose deprecated lifecycle")
            mutation_guard = archived_plan["target_mutation_guard"]
            if mutation_guard["mutating_actions_require_override"] is not True:
                raise AssertionError("archived selleros target must require explicit mutation override")
            if (
                mutation_guard["override_env"]
                != hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV
            ):
                raise AssertionError("archived selleros target must expose rollback override env")

            archived_deploy_dry_run = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "deploy",
                    "--dry-run",
                    "--allow-dirty",
                ]
            )
            if archived_deploy_dry_run["dry_run"] is not True:
                raise AssertionError("archived selleros deploy --dry-run must stay allowed and dry")
            if "selleros-root" not in " ".join(archived_deploy_dry_run["commands"]["rsync"]):
                raise AssertionError("archived selleros deploy --dry-run must expose, not execute, selleros command plan")

            archived_deploy = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "deploy",
                    "--allow-dirty",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if archived_deploy.returncode == 0:
                raise AssertionError("archived selleros real deploy target must fail fast")
            archived_output = archived_deploy.stdout + archived_deploy.stderr
            if "rollback-only after EU migration" not in archived_output:
                raise AssertionError("archived deploy failure must name EU migration rollback-only guard")
            if "hosted_runtime_target__europe_api.json" not in archived_output:
                raise AssertionError("archived deploy failure must point operators to the EU target file")
            if hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV not in archived_output:
                raise AssertionError("archived deploy failure must name explicit emergency rollback override")

            archived_nginx_apply = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "apply-nginx-routes",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if archived_nginx_apply.returncode == 0:
                raise AssertionError("archived selleros apply-nginx-routes must fail fast")
            archived_nginx_output = archived_nginx_apply.stdout + archived_nginx_apply.stderr
            if "rollback-only after EU migration" not in archived_nginx_output:
                raise AssertionError("archived nginx apply failure must name rollback-only guard")

            archived_deploy_and_verify = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "deploy-and-verify",
                    "--allow-dirty",
                    "--skip-refresh",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if archived_deploy_and_verify.returncode == 0:
                raise AssertionError("archived selleros deploy-and-verify must fail fast")
            archived_deploy_and_verify_output = (
                archived_deploy_and_verify.stdout + archived_deploy_and_verify.stderr
            )
            if "rollback-only after EU migration" not in archived_deploy_and_verify_output:
                raise AssertionError("archived deploy-and-verify failure must name rollback-only guard")

            archived_target = hosted_runtime.load_hosted_runtime_target(archived_target_file)
            previous_override = os.environ.get(hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV)
            try:
                os.environ[hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV] = (
                    hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_VALUE
                )
                hosted_runtime._ensure_target_allows_mutation(
                    archived_target,
                    action="deploy",
                    dry_run=False,
                )
            finally:
                if previous_override is None:
                    os.environ.pop(hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV, None)
                else:
                    os.environ[hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV] = previous_override

            public_probe = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(target_file),
                    "public-probe",
                    "--as-of-date",
                    "2026-04-12",
                ]
            )
            if public_probe["ok"] is not True:
                raise AssertionError("public probe must succeed against local live runner")
            if public_probe["include_refresh"] is not False:
                raise AssertionError("canonical public probe must skip heavy refresh by default")
            route_map = {item["route"]: item for item in public_probe["routes"]}
            if "refresh" in route_map:
                raise AssertionError("canonical public probe must not call refresh unless explicitly requested")
            if route_map["operator_reports"]["http_status"] != 200:
                raise AssertionError("operator reports embedded panel must be publicly readable")
            if route_map["load_route"]["http_status"] != 404:
                raise AssertionError("GET load-route probe must reach app-level 404")
            if route_map["job"]["http_status"] != 404:
                raise AssertionError("job-route probe must reach app-level 404 for fake job id")
            if route_map["status"]["http_status"] != 200:
                raise AssertionError("status after seeded snapshot must be publicly readable")
            if route_map["web_vitrina_page"]["http_status"] != 200:
                raise AssertionError("web-vitrina page route must be publicly readable")
            if route_map["instructions_page"]["http_status"] != 200:
                raise AssertionError("instructions page route must be readable for the authorized probe")
            if route_map["web_vitrina_read"]["http_status"] != 200:
                raise AssertionError("web-vitrina read route with seeded snapshot must be publicly readable")
            if route_map["web_vitrina_page_composition"]["http_status"] != 200:
                raise AssertionError("web-vitrina page composition surface must be publicly readable")
            if route_map["web_vitrina_business_projection_status"]["http_status"] != 200:
                raise AssertionError(
                    "business projection status route must be publicly readable"
                )
            if route_map["daily_report"]["http_status"] != 200:
                raise AssertionError("daily-report route must be publicly readable")
            if route_map["stock_report"]["http_status"] != 200:
                raise AssertionError("stock-report route must be publicly readable")
            if route_map["plan_report"]["http_status"] != 200:
                raise AssertionError("plan-report route must be publicly readable")
            if route_map["plan_report_baseline_status"]["http_status"] != 200:
                raise AssertionError("plan-report baseline status route must be publicly readable")
            if route_map["plan_report_baseline_template"]["http_status"] != 200:
                raise AssertionError("plan-report baseline template route must be publicly readable")
            if route_map["plan"]["http_status"] != 200:
                raise AssertionError("plan with seeded snapshot must be publicly readable")
            if route_map["own_product_capital_status"]["http_status"] != 200:
                raise AssertionError("own-product-capital status route must be publicly readable")
            if route_map["factory_order_status"]["http_status"] != 200:
                raise AssertionError("factory-order status route must be publicly readable")
            if route_map["fbs_fulfillment_order_status"]["http_status"] != 200:
                raise AssertionError("FBS fulfillment-order status route must be publicly readable")
            if route_map["factory_order_template_stock_ff"]["http_status"] != 200:
                raise AssertionError("stock_ff template route must be publicly readable")
            if route_map["factory_order_template_inbound_factory"]["http_status"] != 200:
                raise AssertionError("inbound_factory template route must be publicly readable")
            if route_map["factory_order_template_inbound_ff_to_wb"]["http_status"] != 200:
                raise AssertionError("inbound_ff_to_wb template route must be publicly readable")
            if route_map["factory_order_recommendation"]["http_status"] != 422:
                raise AssertionError("recommendation route without calculation must stay truthful 422")
            if route_map["fbs_fulfillment_order_recommendation"]["http_status"] != 422:
                raise AssertionError("FBS recommendation route without calculation must stay truthful 422")
            if route_map["wb_regional_status"]["http_status"] != 200:
                raise AssertionError("wb-regional status route must be publicly readable")
            if "wb_warehouse_exclusion_options" in route_map:
                raise AssertionError(
                    "local probe must not require the production WB-token-backed exclusion payload"
                )
            if "auto_updates_status" in route_map:
                raise AssertionError(
                    "local probe must not require the production systemd auto-updates readback"
                )
            if route_map["wb_regional_district_central"]["http_status"] != 422:
                raise AssertionError("district route without calculation must stay truthful 422")
            loopback_probe = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(target_file),
                    "loopback-probe",
                    "--as-of-date",
                    "2026-04-12",
                ]
            )
            if loopback_probe["ok"] is not True:
                raise AssertionError("loopback probe must succeed against local loopback target")
            if loopback_probe["include_refresh"] is not False:
                raise AssertionError("canonical loopback probe must skip heavy refresh by default")
            loopback_routes = {item["route"]: item for item in loopback_probe["routes"]}
            if "refresh" in loopback_routes:
                raise AssertionError("canonical loopback probe must not call refresh unless explicitly requested")
            if loopback_routes["status"]["http_status"] != 200:
                raise AssertionError("status with seeded snapshot must stay 200")
            if loopback_routes["own_product_capital_status"]["http_status"] != 200:
                raise AssertionError("own-product-capital status loopback route must stay 200")
            if loopback_routes["web_vitrina_read"]["http_status"] != 200:
                raise AssertionError("web-vitrina read route with seeded snapshot must stay 200")
            if loopback_routes["instructions_page"]["http_status"] != 200:
                raise AssertionError("instructions page route must stay 200 for the authorized loopback probe")
            if loopback_routes["operator_reports"]["http_status"] != 200:
                raise AssertionError("operator reports embedded panel must stay 200")
            if loopback_routes["web_vitrina_page_composition"]["http_status"] != 200:
                raise AssertionError("web-vitrina page composition surface must stay 200")
            if (
                loopback_routes["web_vitrina_business_projection_status"][
                    "http_status"
                ]
                != 200
            ):
                raise AssertionError(
                    "business projection status loopback route must stay 200"
                )
            if loopback_routes["daily_report"]["http_status"] != 200:
                raise AssertionError("daily-report route must stay 200")
            if loopback_routes["stock_report"]["http_status"] != 200:
                raise AssertionError("stock-report route must stay 200")
            if loopback_routes["plan_report"]["http_status"] != 200:
                raise AssertionError("plan-report route must stay 200")
            if loopback_routes["plan_report_baseline_status"]["http_status"] != 200:
                raise AssertionError("plan-report baseline status route must stay 200")
            if loopback_routes["plan_report_baseline_template"]["http_status"] != 200:
                raise AssertionError("plan-report baseline template route must stay 200")
            if loopback_routes["plan"]["http_status"] != 200:
                raise AssertionError("plan with seeded snapshot must stay 200")
            if loopback_routes["fbs_fulfillment_order_status"]["http_status"] != 200:
                raise AssertionError("FBS fulfillment-order status loopback route must stay 200")
            if loopback_routes["fbs_fulfillment_order_recommendation"]["http_status"] != 422:
                raise AssertionError("FBS recommendation loopback route without calculation must stay 422")
            if hosted_runtime._probe_include_refresh(
                type("Args", (), {"include_refresh": True, "skip_refresh": False})()
            ) is not True:
                raise AssertionError("--include-refresh must opt into the deep refresh probe")
            if hosted_runtime._probe_include_refresh(
                type("Args", (), {"include_refresh": True, "skip_refresh": True})()
            ) is not False:
                raise AssertionError("--skip-refresh must force-disable the deep refresh probe")
            if hosted_runtime._probe_auth_summary("wb_core_web_session=masked") != {
                "mode": "app_session_cookie",
                "cookie_configured": True,
            }:
                raise AssertionError("probe auth summary must stay sanitized")

            print(f"print_plan: ok -> {print_plan['deploy_plan']['target_id']}")
            print(f"deploy_dry_run: ok -> {deploy_dry_run['commands']['restart'][-1]}")
            print(f"deploy_dry_run_runtime_pip: ok -> {deploy_dry_run['commands']['runtime_pip_install'][-1]}")
            print("deploy_dry_run_seller_recovery_dependencies: ok -> OS, venv and browser commands exposed")
            print(
                "deploy_dry_run_systemd: ok -> "
                f"{deploy_dry_run['commands']['systemd_restart'][-1]}"
            )
            print(
                "deploy_dry_run_nginx_routes: ok -> "
                f"{deploy_dry_run['commands']['nginx_public_routes_update'][-1]}"
            )
            print("archived_target_print_plan: ok -> rollback_only metadata exposed")
            print("archived_target_deploy_dry_run: ok -> command plan only")
            print("archived_target_guard: ok -> real deploy/apply-nginx refused")
            print("archived_target_override_guard: ok -> explicit env override recognized without remote mutation")
            print(f"public_probe_web_vitrina_page: ok -> {route_map['web_vitrina_page']['http_status']}")
            print(f"public_probe_instructions_page: ok -> {route_map['instructions_page']['http_status']}")
            print(f"public_probe_operator_reports: ok -> {route_map['operator_reports']['http_status']}")
            print(f"public_probe_web_vitrina_read: ok -> {route_map['web_vitrina_read']['http_status']}")
            print(
                "public_probe_web_vitrina_page_composition: ok -> "
                f"{route_map['web_vitrina_page_composition']['http_status']}"
            )
            print(f"public_probe_stock_report: ok -> {route_map['stock_report']['http_status']}")
            print(f"public_probe_plan_report: ok -> {route_map['plan_report']['http_status']}")
            print(f"public_probe_plan_baseline: ok -> {route_map['plan_report_baseline_status']['http_status']}/{route_map['plan_report_baseline_template']['http_status']}")
            print(f"factory_order_status: ok -> {route_map['factory_order_status']['http_status']}")
            print(f"fbs_fulfillment_order_status: ok -> {route_map['fbs_fulfillment_order_status']['http_status']}")
            print(f"wb_regional_status: ok -> {route_map['wb_regional_status']['http_status']}")
            print(f"loopback_probe_status: ok -> {loopback_routes['status']['http_status']}")
            print("canonical_probe_refresh_policy: ok -> refresh skipped by default")
            print("probe_auth_summary: ok -> sanitized")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _post_bundle(url: str) -> None:
    payload = INPUT_BUNDLE.read_bytes()
    request = urllib_request.Request(
        url,
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib_request.urlopen(request, timeout=10) as response:
        if response.getcode() != 200:
            raise AssertionError(f"bundle upload must return 200, got {response.getcode()}")


def _seed_ready_snapshot(runtime_dir: Path) -> None:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    current_state = runtime.load_current_state()
    enabled = [item for item in current_state.config_v2 if item.enabled]
    runtime.save_sheet_vitrina_ready_snapshot(
        current_state=current_state,
        refreshed_at="2026-04-20T09:05:00Z",
        plan=_build_plan(
            first_nm_id=enabled[0].nm_id,
            second_nm_id=enabled[1].nm_id,
            first_group=enabled[0].group,
        ),
    )


def _build_plan(
    *,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="hosted-runtime-web-vitrina-fixture",
        as_of_date="2026-04-12",
        date_columns=["2026-04-12", "2026-04-20"],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date="2026-04-12",
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today current",
                column_date="2026-04-20",
            ),
        ],
        source_temporal_policies={
            "seller_funnel_snapshot": "dual_day_capable",
            "prices_snapshot": "accepted_current_rollover",
        },
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D5",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-12", "2026-04-20"],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100, 140],
                    [f"Группа {first_group}: Показы в воронке", f"GROUP:{first_group}|view_count", 40, 55],
                    [f"SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 20, 30],
                    [f"SKU B: Заказы, шт.", f"SKU:{second_nm_id}|orderSum", 5, 7],
                ],
                row_count=4,
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "seller_funnel_snapshot",
                        "success",
                        "fresh",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        2,
                        2,
                        "",
                        "",
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _wait_until_ready(url: str) -> None:
    # The live runner imports the full operator surface, including PDF parsing.
    # Shared CI runners can need more than ten seconds to finish that cold start.
    deadline = time.time() + 30
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib_request.urlopen(url, timeout=1.5) as response:
                if response.getcode() == 200:
                    return
        except Exception as exc:  # pragma: no cover - bounded smoke retry
            last_error = str(exc)
            time.sleep(0.1)
    raise AssertionError(f"local live runner did not become ready: {last_error}")


def _reserve_free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_json(command: list[str]) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            "command failed: "
            + " ".join(command)
            + f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("runner must emit a JSON object")
    return payload


if __name__ == "__main__":
    main()
