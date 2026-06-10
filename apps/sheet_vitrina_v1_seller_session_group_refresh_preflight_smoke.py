"""Targeted smoke for Seller Portal session preflight before bot group refresh."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys
from tempfile import TemporaryDirectory
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_group_refresh_smoke import (  # noqa: E402
    BUNDLE_FIXTURE,
    NEW_REFRESHED_AT,
    OLD_REFRESHED_AT,
    _build_partial_seller_error_plan,
    _build_previous_plan,
    _wait_job,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
    _build_seller_portal_session_check_payload,
)


class _FakeSellerPortalRecovery:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.check_calls = 0

    def check_session(self, *, launcher_download_path: str) -> dict[str, object]:
        self.check_calls += 1
        result = dict(self.payload)
        result.setdefault("launcher_download_path", launcher_download_path)
        return result

    def read_status(self, **_: object) -> dict[str, object]:
        return dict(self.payload)


def main() -> None:
    _assert_session_probe_payload_normalization()
    _assert_invalid_session_blocks_heavy_group_refresh()
    _assert_valid_session_continues_with_same_contract()
    print("seller_session_probe_payload_normalization: ok -> valid/invalid/missing/probe/wrong supplier")
    print("seller_group_refresh_preflight_invalid: ok -> no heavy source fetch")
    print("seller_group_refresh_preflight_valid: ok -> source fetch continues after same session contract")
    print("smoke-check passed")


def _assert_session_probe_payload_normalization() -> None:
    config = SimpleNamespace(
        canonical_supplier_id="canonical-supplier-id",
        canonical_supplier_label="ИП Сагитов В. Р.",
        storage_state_path=Path("/opt/wb-web-bot/storage_state.json"),
    )

    cases = [
        (
            "session_valid_canonical",
            {
                "current_storage_probe": {
                    "ok": True,
                    "status": "ok",
                    "supplier_context": {
                        "current_supplier_id": "canonical-supplier-id",
                        "analytics_supplier_id": "canonical-supplier-id",
                    },
                }
            },
        ),
        (
            "session_invalid",
            {"current_storage_probe": {"ok": False, "status": "seller_portal_session_invalid"}},
        ),
        (
            "session_missing",
            {"current_storage_probe": {"ok": False, "status": "seller_portal_session_missing"}},
        ),
        (
            "session_probe_error",
            {"current_storage_probe": {"ok": False, "status": "seller_portal_session_probe_failed"}},
        ),
        (
            "session_valid_wrong_org",
            {
                "current_storage_probe": {
                    "ok": True,
                    "status": "ok",
                    "supplier_context": {
                        "current_supplier_id": "wrong-supplier-id",
                        "analytics_supplier_id": "wrong-supplier-id",
                    },
                }
            },
        ),
    ]
    for expected_status, raw in cases:
        payload = _build_seller_portal_session_check_payload(
            raw,
            config=config,
            launcher_download_path="/v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip",
        )
        if payload.get("status") != expected_status:
            raise AssertionError(f"expected {expected_status}, got {payload}")
        if payload.get("storage_state_path") != "/opt/wb-web-bot/storage_state.json":
            raise AssertionError(f"session payload must expose sanitized storage path, got {payload}")


def _assert_invalid_session_blocks_heavy_group_refresh() -> None:
    invalid = _FakeSellerPortalRecovery(
        {
            "status": "session_invalid",
            "status_label": "Нужен вход",
            "status_tone": "error",
            "summary": "Сохранённая seller-сессия больше не действует.",
            "instruction": "Нажмите «Восстановить сессию».",
            "storage_state_path": "/opt/wb-web-bot/storage_state.json",
        }
    )
    entrypoint, _runtime, _nm_id = _build_entrypoint(invalid)

    def forbidden_build_plan(**_: object):
        raise AssertionError("heavy source fetch must not start when seller session preflight is invalid")

    entrypoint.sheet_plan_block.build_plan = forbidden_build_plan  # type: ignore[method-assign]
    job = entrypoint.start_sheet_source_group_refresh_job(
        source_group_id="seller_portal_bot",
        as_of_date="2026-04-21",
    )
    snapshot = _wait_job(entrypoint, str(job["job_id"]))
    if snapshot.get("status") != "success":
        raise AssertionError(f"action-required preflight must return a completed job result, got {snapshot}")
    result = snapshot.get("result") or {}
    if result.get("status") != "action_required" or result.get("failed_stage") != "session_preflight":
        raise AssertionError(f"invalid session must return action_required preflight result, got {result}")
    if result.get("source_group_id") != "seller_portal_bot" or result.get("source_group_label") != "Seller Portal / бот":
        raise AssertionError(f"action_required result must identify the group, got {result}")
    if result.get("session_status") != "session_invalid" or not result.get("action_required"):
        raise AssertionError(f"action_required result must carry session status/action, got {result}")
    if invalid.check_calls != 1:
        raise AssertionError(f"seller session preflight must run exactly once, got {invalid.check_calls}")


def _assert_valid_session_continues_with_same_contract() -> None:
    valid = _FakeSellerPortalRecovery(
        {
            "status": "session_valid_canonical",
            "status_label": "Сессия активна",
            "status_tone": "success",
            "summary": "Сохранённая seller-сессия активна, нужный кабинет подтверждён.",
            "storage_state_path": "/opt/wb-web-bot/storage_state.json",
        }
    )
    entrypoint, _runtime, nm_id = _build_entrypoint(valid)
    captured: dict[str, object] = {}

    def build_partial_plan(**kwargs: object):
        captured["source_keys"] = list(kwargs.get("source_keys") or [])
        return _build_partial_seller_error_plan(nm_id=nm_id)

    entrypoint.sheet_plan_block.build_plan = build_partial_plan  # type: ignore[method-assign]
    job = entrypoint.start_sheet_source_group_refresh_job(
        source_group_id="seller_portal_bot",
        as_of_date="2026-04-21",
    )
    snapshot = _wait_job(entrypoint, str(job["job_id"]))
    if snapshot.get("status") != "success":
        raise AssertionError(f"valid preflight must continue to source fetch, got {snapshot}")
    result = snapshot.get("result") or {}
    preflight = result.get("session_preflight") or {}
    if preflight.get("status") != "session_valid_canonical":
        raise AssertionError(f"valid group refresh must include session preflight result, got {result}")
    if preflight.get("storage_state_path") != "/opt/wb-web-bot/storage_state.json":
        raise AssertionError(f"group refresh must use the recovery/session storage path contract, got {preflight}")
    if captured.get("source_keys") != ["seller_funnel_snapshot", "web_source_snapshot", "promo_by_price"]:
        raise AssertionError(f"valid seller group refresh must continue to selected source fetch, got {captured}")
    if valid.check_calls != 1:
        raise AssertionError(f"seller session preflight must run exactly once, got {valid.check_calls}")


def _build_entrypoint(
    recovery: _FakeSellerPortalRecovery,
) -> tuple[RegistryUploadHttpEntrypoint, RegistryUploadDbBackedRuntime, int]:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    runtime_dir = TemporaryDirectory(prefix="seller-session-group-preflight-")
    tmp_path = Path(runtime_dir.name)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=tmp_path)
    accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-20T09:00:00Z")
    if accepted.status != "accepted":
        raise AssertionError(f"fixture bundle must be accepted, got {accepted}")
    current_state = runtime.load_current_state()
    nm_id = next(item.nm_id for item in current_state.config_v2 if item.enabled)
    runtime.save_sheet_vitrina_ready_snapshot(
        current_state=current_state,
        refreshed_at=OLD_REFRESHED_AT,
        plan=_build_previous_plan(nm_id=nm_id),
    )
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=tmp_path,
        runtime=runtime,
        seller_portal_recovery_controller=recovery,  # type: ignore[arg-type]
        activated_at_factory=lambda: NEW_REFRESHED_AT,
        refreshed_at_factory=lambda: NEW_REFRESHED_AT,
        now_factory=lambda: datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc),
    )
    entrypoint._preflight_tempdir = runtime_dir  # type: ignore[attr-defined]
    return entrypoint, runtime, nm_id


if __name__ == "__main__":
    main()
