"""Isolated Playwright-backed WB buyer-session and authenticated price adapter."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable, Iterator, Mapping
from urllib import parse as urllib_parse


UTC = timezone.utc
DEFAULT_BUYER_SESSION_DIR = Path("/opt/wb-core-runtime/state/wb_buyer_session")
DEFAULT_BUYER_STORAGE_STATE_PATH = DEFAULT_BUYER_SESSION_DIR / "storage_state.json"
DEFAULT_BUYER_URL = "https://www.wildberries.ru/lk"
DEFAULT_PRODUCT_URL = "https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
FINGERPRINT_VERSION = "wb-buyer-account-hmac-sha256-v2-stable-identity"
DEFAULT_VALIDATION_NM_ID = 497416931
MAX_NETWORK_JSON_BYTES = 2_000_000
LOGIN_URL_RE = re.compile(r"/(?:login|security|authorize|auth)(?:[/?#]|$)", re.IGNORECASE)
CHALLENGE_MARKERS = (
    "__wbaas/challenges/antibot",
    "data-site-key",
    "подтвердите, что вы не робот",
    "почти готово",
    "подозрительная активность",
    "captcha-support@rwb.ru",
)
RECOVERY_PROBE_BLOCKING_STATUSES = {
    "starting",
    "checking_session",
    "automatic_login",
    "awaiting_human",
    "validating_session",
}


class WbBuyerSessionLockTimeout(TimeoutError):
    """The recovery supervisor could not acquire its automation lock in time."""


@dataclass(frozen=True)
class WbBuyerSessionConfig:
    state_dir: Path = DEFAULT_BUYER_SESSION_DIR
    storage_state_path: Path = DEFAULT_BUYER_STORAGE_STATE_PATH
    buyer_url: str = DEFAULT_BUYER_URL
    product_url_template: str = DEFAULT_PRODUCT_URL
    navigation_timeout_ms: int = 45_000
    settle_timeout_ms: int = 8_000
    validation_nm_id: int = DEFAULT_VALIDATION_NM_ID

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "buyer_session_automation.lock"

    @property
    def lock_owner_path(self) -> Path:
        return self.state_dir / "buyer_session_automation_owner.json"

    @property
    def persistent_profile_dir(self) -> Path:
        return self.state_dir / "chromium_user_data"

    @property
    def legacy_migration_path(self) -> Path:
        return self.state_dir / "legacy_storage_state_migration.json"

    @property
    def fingerprint_key_path(self) -> Path:
        return self.state_dir / "fingerprint.key"

    @property
    def fingerprint_record_path(self) -> Path:
        return self.state_dir / "account_fingerprint.json"

    @property
    def probe_metadata_path(self) -> Path:
        return self.state_dir / "session_probe.json"


def load_wb_buyer_session_config_from_env() -> WbBuyerSessionConfig:
    state_dir = Path(str(os.environ.get("WB_BUYER_SESSION_STATE_DIR") or DEFAULT_BUYER_SESSION_DIR)).expanduser()
    storage_path = Path(
        str(os.environ.get("WB_BUYER_SESSION_STORAGE_STATE_PATH") or (state_dir / "storage_state.json"))
    ).expanduser()
    validation_nm_id = _env_int("WB_BUYER_SESSION_VALIDATION_NM_ID", DEFAULT_VALIDATION_NM_ID)
    if validation_nm_id <= 0:
        validation_nm_id = DEFAULT_VALIDATION_NM_ID
    return WbBuyerSessionConfig(
        state_dir=state_dir,
        storage_state_path=storage_path,
        buyer_url=str(os.environ.get("WB_BUYER_SESSION_URL") or DEFAULT_BUYER_URL).strip() or DEFAULT_BUYER_URL,
        product_url_template=(
            str(os.environ.get("WB_BUYER_PRODUCT_URL_TEMPLATE") or DEFAULT_PRODUCT_URL).strip()
            or DEFAULT_PRODUCT_URL
        ),
        navigation_timeout_ms=_env_int("WB_BUYER_NAVIGATION_TIMEOUT_MS", 45_000),
        settle_timeout_ms=_env_int("WB_BUYER_SETTLE_TIMEOUT_MS", 8_000),
        validation_nm_id=validation_nm_id,
    )


class WbBuyerSessionAdapter:
    """Owns buyer-session probing, fingerprint validation and network price reads."""

    def __init__(
        self,
        *,
        config: WbBuyerSessionConfig | None = None,
        now_factory: Callable[[], datetime] | None = None,
        operation_probe: Callable[[Path, int | None, bool], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config or load_wb_buyer_session_config_from_env()
        self.now_factory = now_factory or (lambda: datetime.now(UTC))
        self.operation_probe = operation_probe

    def check_session(
        self,
        *,
        persist_fingerprint: bool = True,
        acquire_lock: bool = True,
    ) -> dict[str, Any]:
        self._ensure_runtime_permissions()
        joined = self._active_recovery()
        if acquire_lock and joined:
            result = self._joined_session_result(joined)
            self._write_probe_metadata(result)
            return result
        try:
            if acquire_lock:
                with self.session_lock(
                    blocking=False,
                    owner_run_id=f"buyer-check-{secrets.token_hex(6)}",
                    owner_operation="session_check",
                ):
                    joined = self._active_recovery()
                    if joined:
                        result = self._joined_session_result(joined)
                    else:
                        operation = self._run_persistent_operation(nm_id=None, headless=True)
                        result = self._validate_authenticated_session(
                            operation.get("session") if isinstance(operation.get("session"), Mapping) else {},
                            persist_fingerprint=persist_fingerprint,
                        )
            else:
                operation = self._run_persistent_operation(nm_id=None, headless=True)
                result = self._validate_authenticated_session(
                    operation.get("session") if isinstance(operation.get("session"), Mapping) else {},
                    persist_fingerprint=persist_fingerprint,
                )
        except BlockingIOError:
            result = self._joined_session_result(self._active_recovery() or self._lock_owner())
        except Exception:
            result = self._session_result("probe_error", reason="buyer_session_probe_failed")
        self._write_probe_metadata(result)
        return result

    def fetch_authenticated_buyer_price(self, nm_id: int) -> dict[str, Any]:
        normalized_nm_id = int(nm_id)
        if normalized_nm_id <= 0:
            return self._price_error(normalized_nm_id, "probe_error", "invalid_nm_id")
        self._ensure_runtime_permissions()
        joined = self._active_recovery()
        if joined:
            return self._price_error(
                normalized_nm_id,
                "session_recovery_running",
                "buyer_recovery_in_progress",
                joined_run_id=str(joined.get("run_id") or ""),
            )
        try:
            with self.session_lock(
                blocking=False,
                owner_run_id=f"buyer-price-{secrets.token_hex(6)}",
                owner_operation="authenticated_price_read",
            ):
                joined = self._active_recovery()
                if joined:
                    return self._price_error(
                        normalized_nm_id,
                        "session_recovery_running",
                        "buyer_recovery_in_progress",
                        joined_run_id=str(joined.get("run_id") or ""),
                    )
                operation = self._run_persistent_operation(nm_id=normalized_nm_id, headless=True)
                session = self._validate_authenticated_session(
                    operation.get("session") if isinstance(operation.get("session"), Mapping) else {},
                    persist_fingerprint=True,
                )
                if session.get("status") != "valid":
                    return self._price_error(
                        normalized_nm_id,
                        f"session_{session.get('status') or 'invalid'}",
                        str(session.get("reason") or "buyer_session_invalid"),
                        session=session,
                    )
                raw = dict(operation.get("price") or {}) if isinstance(operation.get("price"), Mapping) else {}
        except BlockingIOError:
            joined = self._active_recovery() or self._lock_owner()
            return self._price_error(
                normalized_nm_id,
                "session_recovery_running",
                "buyer_session_automation_busy",
                joined_run_id=str(joined.get("run_id") or ""),
            )
        except Exception:
            return self._price_error(normalized_nm_id, "probe_error", "authenticated_price_probe_failed")

        status = str(raw.get("status") or "probe_error")
        if status != "ok":
            return self._price_error(
                normalized_nm_id,
                status,
                str(raw.get("reason") or "authenticated_price_unavailable"),
                session=session,
                diagnostics=raw.get("diagnostics") if isinstance(raw.get("diagnostics"), Mapping) else {},
            )
        result = {
            "status": "ok",
            "nm_id": normalized_nm_id,
            "authenticated_buyer_price": _money_or_none(raw.get("authenticated_buyer_price")),
            "normal_price": _money_or_none(raw.get("normal_price")),
            "wallet_price": _money_or_none(raw.get("wallet_price")),
            "card_price": _money_or_none(raw.get("card_price")),
            "club_price": _money_or_none(raw.get("club_price")),
            "payment_context": str(raw.get("payment_context") or "unknown/mixed"),
            "destination_context": _safe_destination_context(raw.get("destination_context")),
            "measured_at": str(raw.get("measured_at") or self._now_text()),
            "source_method": str(raw.get("source_method") or "authenticated_browser_network_json"),
            "source_endpoint": _safe_endpoint(raw.get("source_endpoint")),
            "session_status": str(session.get("status") or "valid"),
            "session_reason": str(session.get("reason") or "buyer_session_valid"),
            "session_checked_at": str(session.get("checked_at") or self._now_text()),
            "session_fingerprint": str(session.get("session_fingerprint") or ""),
            "account_fingerprint_available": bool(session.get("account_confirmed")),
            "authenticated_session_proof": True,
            "persistent_profile": True,
            "freshness": {
                "live_read": True,
                "http_status": _int_or_none(raw.get("http_status")),
                "stability": "single_read",
            },
            "diagnostics": _safe_diagnostics(raw.get("diagnostics")),
        }
        if result["authenticated_buyer_price"] is None:
            return self._price_error(
                normalized_nm_id,
                "price_missing",
                "authenticated_primary_price_missing",
                session=session,
                diagnostics=result["diagnostics"],
            )
        return result

    @contextmanager
    def session_lock(
        self,
        *,
        blocking: bool,
        timeout_seconds: float | None = None,
        poll_seconds: float = 0.1,
        owner_run_id: str = "",
        owner_operation: str = "buyer_automation",
    ) -> Iterator[None]:
        self._ensure_runtime_permissions()
        handle = self.config.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.config.lock_path, 0o600)
        try:
            if blocking and timeout_seconds is not None:
                deadline = time.monotonic() + max(0.0, float(timeout_seconds))
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise WbBuyerSessionLockTimeout("buyer session automation lock wait timed out") from exc
                        time.sleep(min(max(0.01, float(poll_seconds)), remaining))
            else:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), flags)
        except Exception:
            handle.close()
            raise
        owner = {
            "run_id": str(owner_run_id or ""),
            "operation": str(owner_operation or "buyer_automation")[:120],
            "pid": os.getpid(),
            "acquired_at": self._now_text(),
        }
        _atomic_write_json(self.config.lock_owner_path, owner, mode=0o600)
        try:
            yield
        finally:
            current_owner = self._lock_owner()
            if int(current_owner.get("pid") or 0) == os.getpid() and str(current_owner.get("run_id") or "") == owner["run_id"]:
                self.config.lock_owner_path.unlink(missing_ok=True)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _active_recovery(self) -> dict[str, Any]:
        try:
            payload = json.loads((self.config.state_dir / "recovery_status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(payload, Mapping) and str(payload.get("status") or "") in RECOVERY_PROBE_BLOCKING_STATUSES:
            return dict(payload)
        return {}

    def _lock_owner(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.config.lock_owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _joined_session_result(self, run: Mapping[str, Any]) -> dict[str, Any]:
        is_recovery = str(run.get("status") or "") in RECOVERY_PROBE_BLOCKING_STATUSES or str(run.get("operation") or "") == "buyer_recovery_supervisor"
        return self._session_result(
            "recovery_running",
            reason="buyer_recovery_in_progress" if is_recovery else "buyer_session_automation_busy",
            recovery_run_id=str(run.get("run_id") or ""),
        )

    def stored_fingerprint(self) -> str:
        return str(self._fingerprint_record().get("fingerprint") or "")

    def _fingerprint_record(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.config.fingerprint_record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _validate_authenticated_session(
        self,
        raw: Mapping[str, Any],
        *,
        persist_fingerprint: bool,
    ) -> dict[str, Any]:
        status = str(raw.get("status") or "probe_error")
        if status != "valid":
            return self._session_result(status, reason=str(raw.get("reason") or f"buyer_session_{status}"))
        identity_material = _canonical_account_identity(raw.get("identity_material"))
        if not identity_material:
            return self._session_result(
                "valid",
                reason="buyer_account_context_missing",
                account_confirmed=False,
            )
        fingerprint = self._fingerprint(identity_material)
        record = self._fingerprint_record()
        expected = str(record.get("fingerprint") or "") if str(record.get("version") or "") == FINGERPRINT_VERSION else ""
        if expected and not hmac.compare_digest(expected, fingerprint):
            return self._session_result(
                "wrong_account",
                reason="buyer_account_fingerprint_mismatch",
                session_fingerprint=fingerprint,
            )
        if not expected and persist_fingerprint:
            self._write_fingerprint_record(
                fingerprint,
                migration_state="bound_from_authenticated_response"
                if record
                else "",
            )
        return self._session_result(
            "valid",
            reason="buyer_session_valid",
            session_fingerprint=fingerprint,
            account_confirmed=True,
        )

    def _run_persistent_operation(self, *, nm_id: int | None, headless: bool) -> dict[str, Any]:
        profile_dir = self.config.persistent_profile_dir
        if self.operation_probe is not None:
            return dict(self.operation_probe(profile_dir, nm_id, headless))
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                locale="ru-RU",
                viewport={"width": 1500, "height": 820},
            )
            try:
                self.migrate_legacy_storage_state(context)
                return self.probe_persistent_context(context, nm_id=nm_id)
            finally:
                context.close()
                os.chmod(profile_dir, 0o700)

    def migrate_legacy_storage_state(self, context: Any) -> dict[str, Any]:
        """Best-effort one-time import; the legacy JSON never becomes canonical again."""

        marker = self.config.legacy_migration_path
        if marker.exists():
            return {"status": "already_attempted"}
        payload = _read_legacy_storage_state(self.config.storage_state_path)
        migrated_cookies = 0
        migrated_origins = 0
        status = "legacy_state_absent"
        if payload:
            status = "attempted"
            cookies = _legacy_wb_cookies(payload)
            if cookies:
                try:
                    context.add_cookies(cookies)
                    migrated_cookies = len(cookies)
                except Exception:
                    pass
            for origin, items in _legacy_wb_local_storage(payload):
                try:
                    page = context.new_page()
                    page.goto(origin, wait_until="domcontentloaded", timeout=self.config.navigation_timeout_ms)
                    page.evaluate(
                        "items => { for (const item of items) localStorage.setItem(item.name, item.value); }",
                        items,
                    )
                    page.close()
                    migrated_origins += 1
                except Exception:
                    continue
        result = {
            "status": status,
            "attempted_at": self._now_text(),
            "cookies_imported": migrated_cookies,
            "origins_imported": migrated_origins,
            "canonical_session": "persistent_chromium_profile",
        }
        _atomic_write_json(marker, result, mode=0o600)
        return result

    def probe_persistent_context(
        self,
        context: Any,
        *,
        nm_id: int | None,
        page: Any | None = None,
    ) -> dict[str, Any]:
        active_page = page or (context.pages[0] if getattr(context, "pages", None) else context.new_page())
        active_page.set_default_timeout(self.config.navigation_timeout_ms)
        active_page.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        session = self._probe_session_in_context(active_page)
        result: dict[str, Any] = {"session": session, "price": {}}
        if str(session.get("status") or "") == "valid" and nm_id is not None:
            result["price"] = self._probe_price_in_context(active_page, int(nm_id))
        return result

    def validate_persistent_proof(
        self,
        operation: Mapping[str, Any],
        *,
        require_price: bool,
    ) -> dict[str, Any]:
        session = self._validate_authenticated_session(
            operation.get("session") if isinstance(operation.get("session"), Mapping) else {},
            persist_fingerprint=True,
        )
        price = dict(operation.get("price") or {}) if isinstance(operation.get("price"), Mapping) else {}
        valid = session.get("status") == "valid" and (not require_price or price.get("status") == "ok")
        return {
            "valid": bool(valid),
            "session": session,
            "price": price,
            "reason": (
                str(session.get("reason") or "buyer_session_invalid")
                if session.get("status") != "valid"
                else str(price.get("reason") or "authenticated_price_unavailable")
                if require_price and price.get("status") != "ok"
                else "buyer_persistent_profile_authenticated"
            ),
        }

    def _probe_session_in_context(self, page: Any) -> dict[str, Any]:
        identity_candidates: list[Mapping[str, Any]] = []

        def capture_identity(response: Any) -> None:
            try:
                parsed = urllib_parse.urlparse(str(response.url or ""))
                lowered_url = str(response.url or "").lower()
                if response.status != 200 or not _is_wb_host(parsed.hostname):
                    return
                if not any(marker in lowered_url for marker in ("profile", "account", "user", "/lk")):
                    return
                if "json" not in str(response.headers.get("content-type") or "").lower():
                    return
                identity = _extract_account_identity(response.json())
                if identity:
                    identity_candidates.append(identity)
            except Exception:
                return

        page.on("response", capture_identity)
        response = page.goto(self.config.buyer_url, wait_until="domcontentloaded")
        page.wait_for_timeout(min(self.config.settle_timeout_ms, 8_000))
        url = str(page.url or "")
        body = _safe_page_text(page)
        parsed = urllib_parse.urlparse(url)
        if LOGIN_URL_RE.search(parsed.path):
            return {"status": "login_redirect", "reason": "buyer_login_redirect"}
        lowered = body.lower()
        if any(marker in lowered for marker in CHALLENGE_MARKERS):
            return {"status": "security_challenge", "reason": "buyer_security_challenge"}
        if _looks_logged_out(lowered):
            return {"status": "expired", "reason": "buyer_login_required"}
        normalized_path = parsed.path.rstrip("/").lower()
        if not _is_wb_host(parsed.hostname) or not (normalized_path == "/lk" or normalized_path.startswith("/lk/")):
            return {"status": "login_redirect", "reason": "buyer_login_redirect"}
        if response is None or int(getattr(response, "status", 0) or 0) >= 400:
            return {"status": "probe_error", "reason": "buyer_session_probe_failed"}
        return {
            "status": "valid",
            "reason": "buyer_session_valid",
            "identity_material": _select_stable_account_identity(list(reversed(identity_candidates))),
            "authenticated_response_proof": True,
        }

    def _probe_price_in_context(self, page: Any, nm_id: int) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []

        def capture(response: Any) -> None:
            try:
                content_type = str(response.headers.get("content-type") or "").lower()
                content_length = _int_or_none(response.headers.get("content-length"))
                parsed = urllib_parse.urlparse(str(response.url or ""))
                if response.status != 200 or "json" not in content_type or not _is_wb_host(parsed.hostname):
                    return
                if content_length is not None and content_length > MAX_NETWORK_JSON_BYTES:
                    return
                extracted = extract_authenticated_price_from_network_payload(response.json(), nm_id=nm_id, response_url=response.url)
                if extracted.get("status") == "ok":
                    candidates.append(extracted)
            except Exception:
                return

        page.on("response", capture)
        page.goto(self.config.product_url_template.format(nm_id=nm_id), wait_until="domcontentloaded")
        page.wait_for_timeout(self.config.settle_timeout_ms)
        url = str(page.url or "")
        body = _safe_page_text(page)
        if LOGIN_URL_RE.search(urllib_parse.urlparse(url).path) or _looks_logged_out(body.lower()):
            return {"status": "session_expired", "reason": "buyer_login_required"}
        if any(marker in body.lower() for marker in CHALLENGE_MARKERS):
            return {"status": "security_challenge", "reason": "buyer_security_challenge"}
        if candidates:
            candidates.sort(key=lambda row: int(row.get("source_score") or 0), reverse=True)
            result = dict(candidates[0])
            result.pop("source_score", None)
            result["measured_at"] = self._now_text()
            return result
        return {"status": "price_missing", "reason": "authenticated_network_price_missing"}

    def _fingerprint(self, identity_material: Any) -> str:
        key = self._fingerprint_key()
        payload = json.dumps(identity_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    def _fingerprint_key(self) -> bytes:
        self._ensure_runtime_permissions()
        path = self.config.fingerprint_key_path
        if path.exists():
            key = path.read_bytes()
            os.chmod(path, 0o600)
            if len(key) >= 32:
                return key
        key = secrets.token_bytes(32)
        staged = self.config.state_dir / f"fingerprint.key.{secrets.token_hex(6)}.tmp"
        staged.write_bytes(key)
        os.chmod(staged, 0o600)
        staged.replace(path)
        os.chmod(path, 0o600)
        return key

    def _write_fingerprint_record(
        self,
        fingerprint: str,
        *,
        created_at: str = "",
        migration_state: str = "",
    ) -> None:
        payload = {
            "version": FINGERPRINT_VERSION,
            "fingerprint": fingerprint,
            "created_at": created_at or self._now_text(),
        }
        if migration_state:
            payload["migrated_at"] = self._now_text()
            payload["migration_state"] = migration_state
        _atomic_write_json(self.config.fingerprint_record_path, payload, mode=0o600)

    def _write_probe_metadata(self, result: Mapping[str, Any]) -> None:
        payload = {
            "status": str(result.get("status") or "probe_error"),
            "reason": str(result.get("reason") or ""),
            "checked_at": str(result.get("checked_at") or self._now_text()),
            "session_fingerprint": str(result.get("session_fingerprint") or ""),
            "account_confirmed": bool(result.get("account_confirmed")),
        }
        _atomic_write_json(self.config.probe_metadata_path, payload, mode=0o600)

    def _ensure_runtime_permissions(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.state_dir, 0o700)
        self.config.persistent_profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.persistent_profile_dir, 0o700)
        if self.config.storage_state_path.exists():
            os.chmod(self.config.storage_state_path, 0o600)

    def _session_result(
        self,
        status: str,
        *,
        reason: str,
        session_fingerprint: str = "",
        account_confirmed: bool = False,
        recovery_run_id: str = "",
    ) -> dict[str, Any]:
        return {
            "contract_name": "wb_buyer_session_status_v1",
            "status": status,
            "reason": reason,
            "valid": status == "valid",
            "checked_at": self._now_text(),
            "session_fingerprint": session_fingerprint,
            "account_confirmed": account_confirmed,
            "authenticated_session_proof": status == "valid",
            "persistent_profile": True,
            "recovery_run_id": recovery_run_id,
        }

    def _price_error(
        self,
        nm_id: int,
        status: str,
        reason: str,
        *,
        session: Mapping[str, Any] | None = None,
        diagnostics: Mapping[str, Any] | None = None,
        joined_run_id: str = "",
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "nm_id": nm_id,
            "authenticated_buyer_price": None,
            "normal_price": None,
            "wallet_price": None,
            "card_price": None,
            "club_price": None,
            "payment_context": "unknown/mixed",
            "destination_context": {},
            "measured_at": self._now_text(),
            "source_method": "authenticated_browser_network_json",
            "source_endpoint": "",
            "session_status": str((session or {}).get("status") or ""),
            "session_reason": str((session or {}).get("reason") or ""),
            "session_checked_at": str((session or {}).get("checked_at") or ""),
            "session_fingerprint": str((session or {}).get("session_fingerprint") or ""),
            "account_fingerprint_available": bool((session or {}).get("account_confirmed")),
            "authenticated_session_proof": False,
            "persistent_profile": True,
            "recovery_run_id": joined_run_id,
            "freshness": {"live_read": False, "stability": "unavailable"},
            "diagnostics": _safe_diagnostics(diagnostics or {}),
        }

    def _now_text(self) -> str:
        return self.now_factory().astimezone(UTC).isoformat()


def extract_authenticated_price_from_network_payload(
    payload: Any,
    *,
    nm_id: int,
    response_url: str,
) -> dict[str, Any]:
    product = _find_product(payload, nm_id=nm_id)
    if product is None:
        return {"status": "missing", "reason": "product_payload_missing"}
    price_block, block_path = _find_price_block(product)
    normal, normal_field = _first_price(
        product,
        price_block,
        ("product", "total", "salePriceU", "salePrice", "finalPriceU", "finalPrice", "clientPriceU", "clientPrice"),
    )
    wallet, wallet_field = _first_price(product, price_block, ("wallet", "walletPrice", "walletPriceU"))
    card, card_field = _first_price(product, price_block, ("card", "cardPrice", "cardPriceU"))
    club, club_field = _first_price(product, price_block, ("club", "clubPrice", "clubPriceU"))
    if normal is None:
        return {"status": "missing", "reason": "network_primary_price_missing"}
    parsed = urllib_parse.urlparse(response_url)
    query = urllib_parse.parse_qs(parsed.query)
    contexts = [name for name, value in (("wallet", wallet), ("card", card), ("club", club)) if value is not None]
    payment_context = "account_default"
    if contexts:
        payment_context = "account_default_with_" + "_and_".join(contexts) + "_option"
    field_path = ".".join(part for part in (block_path, normal_field) if part)
    return {
        "status": "ok",
        "authenticated_buyer_price": normal,
        "normal_price": normal,
        "wallet_price": wallet,
        "card_price": card,
        "club_price": club,
        "payment_context": payment_context,
        "destination_context": {
            "dest": _first_query(query, "dest"),
            "regions": _first_query(query, "regions"),
            "currency": _first_query(query, "curr", "currency") or "rub",
            "locale": _first_query(query, "locale"),
        },
        "source_method": f"authenticated_browser_network_json:{field_path or 'product_price'}",
        "source_endpoint": urllib_parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", "")),
        "source_score": 100 if "card" in parsed.netloc or "detail" in parsed.path else 50,
        "http_status": 200,
        "diagnostics": {
            "normal_field": normal_field,
            "wallet_field": wallet_field,
            "card_field": card_field,
            "club_field": club_field,
            "network_primary": True,
        },
    }


def _find_product(value: Any, *, nm_id: int) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        candidate_id = _int_or_none(value.get("id") or value.get("nmId") or value.get("nmID") or value.get("nm_id"))
        if candidate_id == nm_id:
            return value
        for child in value.values():
            found = _find_product(child, nm_id=nm_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value[:100]:
            found = _find_product(child, nm_id=nm_id)
            if found is not None:
                return found
    return None


def _find_price_block(product: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    sizes = product.get("sizes")
    if isinstance(sizes, list):
        for index, size in enumerate(sizes[:10]):
            if isinstance(size, Mapping) and isinstance(size.get("price"), Mapping):
                return size["price"], f"sizes.{index}.price"
    for key in ("price", "prices", "priceInfo"):
        if isinstance(product.get(key), Mapping):
            return product[key], key
    return product, ""


def _first_price(
    product: Mapping[str, Any],
    block: Mapping[str, Any],
    fields: tuple[str, ...],
) -> tuple[float | None, str]:
    for field in fields:
        for container, prefix in ((block, ""), (product, "product.")):
            if field not in container:
                continue
            normalized = _normalize_price(container.get(field), field=field)
            if normalized is not None:
                return normalized, f"{prefix}{field}"
    return None, ""


def _normalize_price(value: Any, *, field: str) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    if field.lower().endswith("u") or numeric >= 10_000:
        numeric /= 100.0
    return round(numeric, 2)


def _read_legacy_storage_state(path: Path) -> Mapping[str, Any] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, Mapping) else None


def _legacy_wb_cookies(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for cookie in state.get("cookies", []) if isinstance(state.get("cookies"), list) else []:
        if not isinstance(cookie, Mapping):
            continue
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or not value or not _is_wb_host(domain.lstrip(".")):
            continue
        migrated: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(cookie.get("path") or "/"),
            "httpOnly": bool(cookie.get("httpOnly")),
            "secure": bool(cookie.get("secure")),
        }
        if isinstance(cookie.get("expires"), (int, float)):
            migrated["expires"] = cookie.get("expires")
        same_site = str(cookie.get("sameSite") or "")
        if same_site in {"Strict", "Lax", "None"}:
            migrated["sameSite"] = same_site
        result.append(migrated)
    return result


def _legacy_wb_local_storage(state: Mapping[str, Any]) -> list[tuple[str, list[dict[str, str]]]]:
    result: list[tuple[str, list[dict[str, str]]]] = []
    for origin in state.get("origins", []) if isinstance(state.get("origins"), list) else []:
        if not isinstance(origin, Mapping):
            continue
        origin_url = str(origin.get("origin") or "")
        if not _is_wb_host(urllib_parse.urlparse(origin_url).hostname):
            continue
        items: list[dict[str, str]] = []
        for item in origin.get("localStorage", []) if isinstance(origin.get("localStorage"), list) else []:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if name:
                items.append({"name": name, "value": value})
        if items:
            result.append((origin_url, items))
    return result


IDENTITY_KEY_ALIASES = {
    "userid": "user_id",
    "user_id": "user_id",
    "profileid": "profile_id",
    "profile_id": "profile_id",
    "accountid": "account_id",
    "account_id": "account_id",
}
IDENTITY_KEY_PRIORITY = ("user_id", "account_id", "profile_id")


def _select_stable_account_identity(*values: Any) -> Mapping[str, Any] | None:
    claims: dict[str, set[str]] = {key: set() for key in IDENTITY_KEY_PRIORITY}
    for value in values:
        _collect_account_identity_claims(value, claims=claims)
    for key in IDENTITY_KEY_PRIORITY:
        candidates = claims[key]
        if len(candidates) == 1:
            return {key: next(iter(candidates))}
        if len(candidates) > 1:
            return None
    return None


def _canonical_account_identity(value: Any) -> Mapping[str, Any] | None:
    return _select_stable_account_identity(value)


def _collect_account_identity_claims(
    value: Any,
    *,
    claims: dict[str, set[str]],
    depth: int = 0,
) -> None:
    if depth > 8:
        return
    if isinstance(value, Mapping):
        for raw_key, raw_value in value.items():
            key = IDENTITY_KEY_ALIASES.get(str(raw_key or "").lower())
            if key and isinstance(raw_value, (str, int)) and str(raw_value).strip():
                claims[key].add(str(raw_value).strip())
            else:
                _collect_account_identity_claims(raw_value, claims=claims, depth=depth + 1)
    elif isinstance(value, list):
        for child in value[:100]:
            _collect_account_identity_claims(child, claims=claims, depth=depth + 1)


def _extract_account_identity(value: Any, *, depth: int = 0) -> Mapping[str, Any] | None:
    del depth
    return _canonical_account_identity(value)


def _looks_logged_out(lowered_text: str) -> bool:
    markers = (
        "войти или зарегистрироваться",
        "введите номер телефона",
        "получить код",
        "войти под этим аккаунтом",
        "продолжить как",
        "войти как",
    )
    return any(marker in lowered_text for marker in markers)


def _safe_page_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=5_000) or "")[:20_000]
    except Exception:
        return ""


def _safe_endpoint(value: Any) -> str:
    try:
        parsed = urllib_parse.urlparse(str(value or ""))
    except Exception:
        return ""
    if not _is_wb_host(parsed.hostname):
        return ""
    return urllib_parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _safe_destination_context(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: str(value.get(key) or "")[:160]
        for key in ("dest", "regions", "currency", "locale")
        if value.get(key) not in {None, ""}
    }


def _is_wb_host(hostname: str | None) -> bool:
    normalized = str(hostname or "").lower().rstrip(".")
    return normalized == "wb.ru" or normalized.endswith(".wb.ru") or normalized == "wildberries.ru" or normalized.endswith(".wildberries.ru")


def _safe_diagnostics(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "normal_field",
        "wallet_field",
        "card_field",
        "club_field",
        "network_primary",
        "region_mismatch",
        "currency_mismatch",
    }
    return {str(key): item for key, item in value.items() if str(key) in allowed and isinstance(item, (str, int, float, bool, type(None)))}


def _first_query(query: Mapping[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = query.get(key)
        if values:
            return str(values[0])[:160]
    return ""


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    staged = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    staged.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.chmod(staged, mode)
    staged.replace(path)
    os.chmod(path, mode)


def _money_or_none(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return round(numeric, 2) if numeric > 0 else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or default))
    except ValueError:
        return default
