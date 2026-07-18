"""Application boundary for the isolated WB buyer-session contour."""

from __future__ import annotations

import importlib
import time
from typing import Any, Callable, Mapping

from packages.adapters.wb_buyer_session import WbBuyerSessionAdapter


BUYER_STATUS_LABELS = {
    "valid": "Действует",
    "missing": "Не установлена",
    "expired": "Истекла",
    "wrong_account": "Другой аккаунт",
    "login_redirect": "Требуется вход",
    "security_challenge": "Требуется проверка WB",
    "probe_error": "Ошибка проверки",
    "recovery_running": "Восстановление выполняется",
    "starting": "Запускается",
    "checking_session": "Проверяем сессию",
    "automatic_login": "Выполняем автоматический вход",
    "awaiting_human": "Ждём действие человека",
    "validating_session": "Проверяем сохранённую сессию",
    "completed": "Установлена",
    "stopped": "Остановлена",
    "timeout": "Время истекло",
    "error": "Ошибка",
    "idle": "Не запускалась",
}

SAFE_BUYER_REASONS = {
    "",
    "account_fingerprint_source_missing",
    "authenticated_network_price_missing",
    "authenticated_price_probe_failed",
    "authenticated_price_unavailable",
    "authenticated_primary_price_missing",
    "buyer_account_context_missing",
    "buyer_account_fingerprint_mismatch",
    "buyer_account_selection_required",
    "buyer_captcha_required",
    "buyer_fingerprint_migrated",
    "buyer_fingerprint_migration_unproven",
    "buyer_fingerprint_ready",
    "buyer_human_action_required",
    "buyer_login_redirect",
    "buyer_login_required",
    "buyer_login_timeout",
    "buyer_login_window_ready",
    "buyer_phone_required",
    "buyer_recovery_checking_session",
    "buyer_recovery_in_progress",
    "buyer_recovery_process_identity_mismatch",
    "buyer_recovery_run_not_current",
    "buyer_recovery_runtime_error",
    "buyer_recovery_starting",
    "buyer_recovery_stopped",
    "buyer_recovery_unexpected_exit",
    "buyer_security_challenge",
    "buyer_security_confirmation_required",
    "buyer_session_stabilizing",
    "buyer_sms_required",
    "buyer_saved_account_available",
    "buyer_saved_account_login_not_completed",
    "buyer_saved_account_login_started",
    "buyer_saved_account_login_unavailable",
    "buyer_session_already_valid",
    "buyer_session_expired",
    "buyer_session_lock_busy",
    "buyer_session_lock_wait_timeout",
    "buyer_session_automation_busy",
    "buyer_session_missing",
    "buyer_session_probe_failed",
    "buyer_session_saved_and_validated",
    "buyer_session_saving",
    "buyer_session_valid",
    "buyer_session_valid_without_account_fingerprint",
    "buyer_session_validating",
    "buyer_persistent_profile_authenticated",
    "buyer_persistent_profile_proof_failed",
    "buyer_persistent_profile_restart_validation_failed",
    "buyer_persistent_profile_restart_validated",
    "buyer_persistent_profile_restarting",
    "buyer_persistent_profile_validated",
    "buyer_login_surface_unrecognized",
    "buyer_storage_state_missing",
    "buyer_visual_session_starting",
    "invalid_nm_id",
    "network_primary_price_missing",
    "product_payload_missing",
}


class WbBuyerSessionBlock:
    """Safe public/application surface used by HTTP and the SPP tester."""

    def __init__(self, *, adapter: WbBuyerSessionAdapter | None = None) -> None:
        self.adapter = adapter or WbBuyerSessionAdapter()

    def check_session(self) -> dict[str, Any]:
        return _public_session_payload(self.adapter.check_session())

    def ensure_session(
        self,
        *,
        auto_recover: bool = True,
        wait_seconds: float = 90.0,
        poll_seconds: float = 1.0,
    ) -> dict[str, Any]:
        session = self.check_session()
        if session.get("valid") or not auto_recover:
            return session
        controller = WbBuyerSessionRecoveryController()
        launcher_path = "/v1/sheet-vitrina-v1/prices/spp-test/buyer-session/recovery/launcher.zip"
        recovery = controller.start(replace=False, launcher_download_path=launcher_path)
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while recovery.get("running") and recovery.get("status") != "awaiting_human":
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.1, float(poll_seconds)))
            recovery = controller.read_status(
                launcher_download_path=launcher_path,
                run_id=str(recovery.get("run_id") or "") or None,
                with_probe=False,
            )
        if recovery.get("status") == "completed":
            return self.check_session()
        if recovery.get("status") == "awaiting_human":
            return {
                **session,
                "status": "action_required",
                "status_label": "Нужно действие человека",
                "valid": False,
                "reason": _safe_reason(recovery.get("reason")),
                "action": "Откройте защищённое окно и завершите вход в Wildberries",
                "recovery": recovery,
            }
        return {
            **session,
            "status": "recovery_pending" if recovery.get("running") else str(recovery.get("status") or session.get("status") or "probe_error"),
            "valid": False,
            "reason": _safe_reason(recovery.get("reason") or session.get("reason")),
            "recovery": recovery,
        }

    def fetch_authenticated_buyer_price(self, nm_id: int) -> dict[str, Any]:
        return _public_price_payload(self.adapter.fetch_authenticated_buyer_price(int(nm_id)))


class WbBuyerSessionRecoveryController:
    """Thin wrapper around the repo-owned buyer recovery/noVNC tool."""

    def __init__(
        self,
        *,
        config_factory: Callable[[], Any] | None = None,
        start_runner: Callable[[Any, bool], Mapping[str, Any]] | None = None,
        status_reader: Callable[..., Mapping[str, Any]] | None = None,
        stop_runner: Callable[[Any], Mapping[str, Any]] | None = None,
        launcher_builder: Callable[[Any, str, str], tuple[bytes, str]] | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._start_runner = start_runner
        self._status_reader = status_reader
        self._stop_runner = stop_runner
        self._launcher_builder = launcher_builder

    @staticmethod
    def _tool() -> Any:
        return importlib.import_module("apps.wb_buyer_session_recovery")

    def _config(self) -> Any:
        return self._config_factory() if self._config_factory is not None else self._tool().load_recovery_config_from_env()

    def read_status(self, *, launcher_download_path: str, run_id: str | None = None, with_probe: bool = True) -> dict[str, Any]:
        config = self._config()
        raw = dict(
            self._status_reader(config, with_probe, requested_run_id=run_id)
            if self._status_reader is not None
            else self._tool().read_recovery_status(config, with_probe=with_probe, requested_run_id=run_id)
        )
        return _public_recovery_payload(raw, launcher_download_path=launcher_download_path)

    def start(self, *, replace: bool, launcher_download_path: str) -> dict[str, Any]:
        config = self._config()
        raw = dict(
            self._start_runner(config, replace)
            if self._start_runner is not None
            else self._tool().start_recovery(config, replace=replace)
        )
        return _public_recovery_payload(raw, launcher_download_path=launcher_download_path)

    def stop(self, *, launcher_download_path: str, run_id: str | None = None) -> dict[str, Any]:
        config = self._config()
        raw = dict(
            self._stop_runner(config)
            if self._stop_runner is not None
            else self._tool().stop_recovery(config, requested_run_id=run_id)
        )
        return _public_recovery_payload(raw, launcher_download_path=launcher_download_path)

    def build_launcher_archive(self, *, public_status_url: str, public_operator_url: str) -> tuple[bytes, str]:
        config = self._config()
        if self._launcher_builder is not None:
            return self._launcher_builder(config, public_status_url, public_operator_url)
        return self._tool().build_macos_launcher_archive(
            config,
            public_status_url=public_status_url,
            public_operator_url=public_operator_url,
        )


def _public_session_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    status = str(raw.get("status") or "probe_error")
    return {
        "contract_name": "wb_buyer_session_status_v1",
        "status": status,
        "status_label": BUYER_STATUS_LABELS.get(status, "Неизвестно"),
        "status_tone": "success" if status == "valid" else ("warning" if status in {"recovery_running", "security_challenge"} else "danger"),
        "valid": status == "valid",
        "reason": _safe_reason(raw.get("reason")),
        "checked_at": str(raw.get("checked_at") or ""),
        "session_fingerprint": _safe_fingerprint(raw.get("session_fingerprint")),
        "account_confirmed": bool(raw.get("account_confirmed")),
        "authenticated_session_proof": bool(raw.get("authenticated_session_proof")),
        "persistent_profile": bool(raw.get("persistent_profile")),
        "recovery_run_id": str(raw.get("recovery_run_id") or "")[:160],
        "action": "" if status == "valid" else "Установить сессию",
    }


def _public_price_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": str(raw.get("status") or "probe_error"),
        "reason": _safe_reason(raw.get("reason")),
        "nm_id": _int_or_none(raw.get("nm_id")),
        "authenticated_buyer_price": _number_or_none(raw.get("authenticated_buyer_price")),
        "normal_price": _number_or_none(raw.get("normal_price")),
        "wallet_price": _number_or_none(raw.get("wallet_price")),
        "card_price": _number_or_none(raw.get("card_price")),
        "club_price": _number_or_none(raw.get("club_price")),
        "payment_context": str(raw.get("payment_context") or "unknown/mixed")[:120],
        "destination_context": dict(raw.get("destination_context") or {}) if isinstance(raw.get("destination_context"), Mapping) else {},
        "measured_at": str(raw.get("measured_at") or ""),
        "source_method": str(raw.get("source_method") or "")[:240],
        "source_endpoint": str(raw.get("source_endpoint") or "")[:500],
        "session_fingerprint": _safe_fingerprint(raw.get("session_fingerprint")),
        "account_fingerprint_available": bool(raw.get("account_fingerprint_available")),
        "authenticated_session_proof": bool(raw.get("authenticated_session_proof")),
        "persistent_profile": bool(raw.get("persistent_profile")),
        "recovery_run_id": str(raw.get("recovery_run_id") or "")[:160],
        "freshness": dict(raw.get("freshness") or {}) if isinstance(raw.get("freshness"), Mapping) else {},
        "diagnostics": dict(raw.get("diagnostics") or {}) if isinstance(raw.get("diagnostics"), Mapping) else {},
    }


def _public_recovery_payload(raw: Mapping[str, Any], *, launcher_download_path: str) -> dict[str, Any]:
    status = str(raw.get("status") or "idle")
    running = bool(raw.get("running"))
    launcher_ready = running and status == "awaiting_human"
    session = raw.get("session") if isinstance(raw.get("session"), Mapping) else {}
    session_status = str(session.get("status") or ("valid" if status == "completed" else "missing"))
    return {
        "contract_name": "wb_buyer_session_recovery_v1",
        "run_id": str(raw.get("run_id") or "")[:160],
        "status": status,
        "status_label": BUYER_STATUS_LABELS.get(status, "Неизвестно"),
        "status_tone": "success" if status in {"completed"} else ("warning" if running else "danger" if status in {"error", "timeout"} else "neutral"),
        "running": running,
        "run_is_final": status in {"completed", "stopped", "timeout", "error"},
        "started_at": str(raw.get("started_at") or ""),
        "finished_at": str(raw.get("finished_at") or ""),
        "deadline_at": str(raw.get("deadline_at") or ""),
        "reason": _safe_reason(raw.get("reason") or raw.get("message")),
        "stage": status,
        "human_action_required": status == "awaiting_human",
        "human_action": _human_action(_safe_reason(raw.get("reason") or raw.get("message"))) if status == "awaiting_human" else "",
        "launcher_ready": launcher_ready,
        "can_download_launcher": launcher_ready,
        "launcher_download_path": launcher_download_path if launcher_ready else "",
        "session": {
            "status": session_status,
            "status_label": BUYER_STATUS_LABELS.get(session_status, "Неизвестно"),
            "valid": session_status == "valid",
            "checked_at": str(session.get("checked_at") or ""),
            "session_fingerprint": _safe_fingerprint(session.get("session_fingerprint")),
            "account_confirmed": bool(session.get("account_confirmed")),
        },
    }


def _human_action(reason: str) -> str:
    return {
        "buyer_sms_required": "Введите SMS-код в защищённом окне Wildberries.",
        "buyer_phone_required": "Введите номер телефона в защищённом окне Wildberries.",
        "buyer_captcha_required": "Пройдите проверку Wildberries в защищённом окне.",
        "buyer_account_selection_required": "Выберите разрешённый покупательский аккаунт.",
        "buyer_account_fingerprint_mismatch": "Открыт другой аккаунт. Переключитесь на ранее привязанный аккаунт.",
        "buyer_security_confirmation_required": "Подтвердите вход в защищённом окне Wildberries.",
    }.get(reason, "Завершите требуемое действие в защищённом окне Wildberries.")


def _safe_reason(value: Any) -> str:
    text = str(value or "")
    return text if text in SAFE_BUYER_REASONS else "buyer_session_detail_sanitized"


def _safe_fingerprint(value: Any) -> str:
    text = str(value or "").lower()
    return text if len(text) == 64 and all(char in "0123456789abcdef" for char in text) else ""


def _number_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
