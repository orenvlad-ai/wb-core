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
from typing import Any, Callable, Iterator, Mapping
from urllib import parse as urllib_parse


UTC = timezone.utc
DEFAULT_BUYER_SESSION_DIR = Path("/opt/wb-core-runtime/state/wb_buyer_session")
DEFAULT_BUYER_STORAGE_STATE_PATH = DEFAULT_BUYER_SESSION_DIR / "storage_state.json"
DEFAULT_BUYER_URL = "https://www.wildberries.ru/lk"
DEFAULT_PRODUCT_URL = "https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
FINGERPRINT_VERSION = "wb-buyer-account-hmac-sha256-v1"
MAX_NETWORK_JSON_BYTES = 2_000_000
AUTH_NAME_RE = re.compile(r"(?:auth|token|session|user|profile|account|login|validation|wbx)", re.IGNORECASE)
LOGIN_URL_RE = re.compile(r"/(?:login|security|authorize|auth)(?:[/?#]|$)", re.IGNORECASE)
CHALLENGE_MARKERS = (
    "__wbaas/challenges/antibot",
    "data-site-key",
    "подтвердите, что вы не робот",
    "почти готово",
)


@dataclass(frozen=True)
class WbBuyerSessionConfig:
    state_dir: Path = DEFAULT_BUYER_SESSION_DIR
    storage_state_path: Path = DEFAULT_BUYER_STORAGE_STATE_PATH
    buyer_url: str = DEFAULT_BUYER_URL
    product_url_template: str = DEFAULT_PRODUCT_URL
    navigation_timeout_ms: int = 45_000
    settle_timeout_ms: int = 8_000

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "buyer_session.lock"

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
    )


class WbBuyerSessionAdapter:
    """Owns buyer-session probing, fingerprint validation and network price reads."""

    def __init__(
        self,
        *,
        config: WbBuyerSessionConfig | None = None,
        now_factory: Callable[[], datetime] | None = None,
        browser_probe: Callable[[Path], Mapping[str, Any]] | None = None,
        price_probe: Callable[[Path, int], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config or load_wb_buyer_session_config_from_env()
        self.now_factory = now_factory or (lambda: datetime.now(UTC))
        self.browser_probe = browser_probe
        self.price_probe = price_probe

    def check_session(
        self,
        *,
        storage_state_path: Path | None = None,
        persist_fingerprint: bool = True,
        acquire_lock: bool = True,
    ) -> dict[str, Any]:
        path = storage_state_path or self.config.storage_state_path
        self._ensure_runtime_permissions()
        if not path.exists():
            result = self._session_result("missing", reason="buyer_storage_state_missing")
            if path == self.config.storage_state_path:
                self._write_probe_metadata(result)
            return result
        self._enforce_storage_state_permissions(path)
        try:
            if acquire_lock:
                with self.session_lock(blocking=False):
                    result = self._probe_and_validate(path, persist_fingerprint=persist_fingerprint)
            else:
                result = self._probe_and_validate(path, persist_fingerprint=persist_fingerprint)
        except BlockingIOError:
            result = self._session_result("recovery_running", reason="buyer_session_lock_busy")
        except Exception:
            result = self._session_result("probe_error", reason="buyer_session_probe_failed")
        if path == self.config.storage_state_path:
            self._write_probe_metadata(result)
        return result

    def fetch_authenticated_buyer_price(self, nm_id: int) -> dict[str, Any]:
        normalized_nm_id = int(nm_id)
        if normalized_nm_id <= 0:
            return self._price_error(normalized_nm_id, "probe_error", "invalid_nm_id")
        self._ensure_runtime_permissions()
        path = self.config.storage_state_path
        if not path.exists():
            return self._price_error(normalized_nm_id, "session_missing", "buyer_storage_state_missing")
        try:
            with self.session_lock(blocking=False):
                session = self._probe_and_validate(path, persist_fingerprint=True)
                if session.get("status") != "valid":
                    return self._price_error(
                        normalized_nm_id,
                        f"session_{session.get('status') or 'invalid'}",
                        str(session.get("reason") or "buyer_session_invalid"),
                        session=session,
                    )
                raw = dict(
                    self.price_probe(path, normalized_nm_id)
                    if self.price_probe is not None
                    else self._run_playwright_price_probe(path, normalized_nm_id)
                )
        except BlockingIOError:
            return self._price_error(normalized_nm_id, "session_recovery_running", "buyer_session_lock_busy")
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
            "session_fingerprint": str(session.get("session_fingerprint") or ""),
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
    def session_lock(self, *, blocking: bool) -> Iterator[None]:
        self._ensure_runtime_permissions()
        handle = self.config.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.config.lock_path, 0o600)
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            handle.close()
            raise
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def persist_storage_state_atomically(self, candidate_path: Path) -> None:
        self._ensure_runtime_permissions()
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("buyer storage state must be a JSON object")
        staged = self.config.state_dir / f"storage_state.staged.{secrets.token_hex(8)}.json"
        staged.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.chmod(staged, 0o600)
        staged.replace(self.config.storage_state_path)
        os.chmod(self.config.storage_state_path, 0o600)

    def stored_fingerprint(self) -> str:
        try:
            payload = json.loads(self.config.fingerprint_record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(payload.get("fingerprint") or "") if isinstance(payload, Mapping) else ""

    def _probe_and_validate(self, path: Path, *, persist_fingerprint: bool) -> dict[str, Any]:
        raw = dict(self.browser_probe(path) if self.browser_probe is not None else self._run_playwright_session_probe(path))
        status = str(raw.get("status") or "probe_error")
        if status != "valid":
            return self._session_result(status, reason=str(raw.get("reason") or f"buyer_session_{status}"))
        identity_material = raw.pop("identity_material", None)
        if not identity_material:
            identity_material = _derive_identity_material(path)
        if not identity_material:
            return self._session_result("probe_error", reason="account_fingerprint_source_missing")
        fingerprint = self._fingerprint(identity_material)
        expected = self.stored_fingerprint()
        if expected and not hmac.compare_digest(expected, fingerprint):
            return self._session_result(
                "wrong_account",
                reason="buyer_account_fingerprint_mismatch",
                session_fingerprint=fingerprint,
            )
        if not expected and persist_fingerprint:
            self._write_fingerprint_record(fingerprint)
        return self._session_result(
            "valid",
            reason="buyer_session_valid",
            session_fingerprint=fingerprint,
            account_confirmed=True,
        )

    def _run_playwright_session_probe(self, path: Path) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(storage_state=str(path), locale="ru-RU")
                page = context.new_page()
                page.set_default_timeout(self.config.navigation_timeout_ms)
                identity_candidates: list[Mapping[str, Any]] = []

                def capture_identity(response: Any) -> None:
                    try:
                        url = str(response.url or "")
                        lowered_url = url.lower()
                        if response.status != 200 or not any(marker in lowered_url for marker in ("profile", "account", "user", "/lk")):
                            return
                        if "json" not in str(response.headers.get("content-type") or "").lower():
                            return
                        identity = _extract_account_identity(response.json())
                        if identity:
                            identity_candidates.append(identity)
                    except Exception:
                        return

                page.on("response", capture_identity)
                page.goto(self.config.buyer_url, wait_until="domcontentloaded")
                page.wait_for_timeout(min(self.config.settle_timeout_ms, 8_000))
                url = str(page.url or "")
                body = _safe_page_text(page)
                if LOGIN_URL_RE.search(urllib_parse.urlparse(url).path):
                    return {"status": "login_redirect", "reason": "buyer_login_redirect"}
                lowered = body.lower()
                if any(marker in lowered for marker in CHALLENGE_MARKERS):
                    return {"status": "security_challenge", "reason": "buyer_security_challenge"}
                if _looks_logged_out(lowered):
                    return {"status": "expired", "reason": "buyer_login_required"}
                identity = (identity_candidates[-1] if identity_candidates else None) or _derive_context_identity_material(context) or _derive_identity_material(path)
                if not identity:
                    return {"status": "probe_error", "reason": "buyer_account_context_missing"}
                return {"status": "valid", "identity_material": identity}
            finally:
                browser.close()

    def _run_playwright_price_probe(self, path: Path, nm_id: int) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        candidates: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(storage_state=str(path), locale="ru-RU")
                page = context.new_page()
                page.set_default_timeout(self.config.navigation_timeout_ms)

                def capture(response: Any) -> None:
                    try:
                        content_type = str(response.headers.get("content-type") or "").lower()
                        content_length = _int_or_none(response.headers.get("content-length"))
                        if response.status != 200 or "json" not in content_type:
                            return
                        if content_length is not None and content_length > MAX_NETWORK_JSON_BYTES:
                            return
                        parsed = urllib_parse.urlparse(str(response.url or ""))
                        if not _is_wb_host(parsed.hostname):
                            return
                        extracted = extract_authenticated_price_from_network_payload(
                            response.json(),
                            nm_id=nm_id,
                            response_url=response.url,
                        )
                        if extracted.get("status") == "ok":
                            candidates.append(extracted)
                    except Exception:
                        return

                page.on("response", capture)
                page.goto(
                    self.config.product_url_template.format(nm_id=nm_id),
                    wait_until="domcontentloaded",
                )
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
                dom = _extract_dom_price(page)
                if dom is not None:
                    return {
                        "status": "ok",
                        "authenticated_buyer_price": dom,
                        "normal_price": dom,
                        "wallet_price": None,
                        "card_price": None,
                        "club_price": None,
                        "payment_context": "unknown/mixed",
                        "destination_context": {},
                        "source_method": "authenticated_browser_dom_fallback",
                        "source_endpoint": urllib_parse.urlunparse((urllib_parse.urlparse(url).scheme, urllib_parse.urlparse(url).netloc, urllib_parse.urlparse(url).path, "", "", "")),
                        "http_status": 200,
                        "measured_at": self._now_text(),
                        "diagnostics": {"network_primary_missing": True, "dom_fallback": True},
                    }
                return {"status": "price_missing", "reason": "authenticated_network_price_missing"}
            finally:
                browser.close()

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

    def _write_fingerprint_record(self, fingerprint: str) -> None:
        payload = {
            "version": FINGERPRINT_VERSION,
            "fingerprint": fingerprint,
            "created_at": self._now_text(),
        }
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

    @staticmethod
    def _enforce_storage_state_permissions(path: Path) -> None:
        os.chmod(path, 0o600)

    def _session_result(
        self,
        status: str,
        *,
        reason: str,
        session_fingerprint: str = "",
        account_confirmed: bool = False,
    ) -> dict[str, Any]:
        return {
            "contract_name": "wb_buyer_session_status_v1",
            "status": status,
            "reason": reason,
            "valid": status == "valid",
            "checked_at": self._now_text(),
            "session_fingerprint": session_fingerprint,
            "account_confirmed": account_confirmed,
        }

    def _price_error(
        self,
        nm_id: int,
        status: str,
        reason: str,
        *,
        session: Mapping[str, Any] | None = None,
        diagnostics: Mapping[str, Any] | None = None,
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
            "session_fingerprint": str((session or {}).get("session_fingerprint") or ""),
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


def _derive_identity_material(path: Path) -> Mapping[str, Any] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, Mapping):
        return None
    material: dict[str, Any] = {"cookies": [], "storage": [], "account_fields": []}
    for cookie in state.get("cookies", []) if isinstance(state.get("cookies"), list) else []:
        if not isinstance(cookie, Mapping):
            continue
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if domain.endswith("wildberries.ru") and value and AUTH_NAME_RE.search(name):
            material["cookies"].append([domain, name, value])
    for origin in state.get("origins", []) if isinstance(state.get("origins"), list) else []:
        if not isinstance(origin, Mapping) or "wildberries.ru" not in str(origin.get("origin") or "").lower():
            continue
        for item in origin.get("localStorage", []) if isinstance(origin.get("localStorage"), list) else []:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if value and AUTH_NAME_RE.search(name):
                material["storage"].append([str(origin.get("origin") or ""), name, value])
                parsed_value = _json_or_text(value)
                identity = _extract_account_identity(parsed_value)
                if identity:
                    material["account_fields"].append(identity)
    if material["account_fields"]:
        return {"account_fields": sorted(material["account_fields"], key=lambda item: json.dumps(item, sort_keys=True))}
    stable_cookies = [item for item in material["cookies"] if re.search(r"(?:user|profile|account|uid)", item[1], re.IGNORECASE)]
    if stable_cookies:
        return {"cookies": sorted(stable_cookies)}
    material["cookies"].sort()
    material["storage"].sort()
    return material if material["cookies"] or material["storage"] else None


def _derive_context_identity_material(context: Any) -> Mapping[str, Any] | None:
    try:
        state = context.storage_state()
    except Exception:
        return None
    staged = {"cookies": [], "origins": state.get("origins", []) if isinstance(state, Mapping) else []}
    for cookie in state.get("cookies", []) if isinstance(state, Mapping) and isinstance(state.get("cookies"), list) else []:
        if isinstance(cookie, Mapping):
            staged["cookies"].append(dict(cookie))
    material: dict[str, Any] = {"cookies": [], "storage": [], "account_fields": []}
    for cookie in staged["cookies"]:
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if domain.endswith("wildberries.ru") and value and AUTH_NAME_RE.search(name):
            material["cookies"].append([domain, name, value])
    for origin in staged["origins"]:
        if not isinstance(origin, Mapping) or "wildberries.ru" not in str(origin.get("origin") or "").lower():
            continue
        for item in origin.get("localStorage", []) if isinstance(origin.get("localStorage"), list) else []:
            if isinstance(item, Mapping) and AUTH_NAME_RE.search(str(item.get("name") or "")) and str(item.get("value") or ""):
                value = str(item.get("value") or "")
                material["storage"].append([str(origin.get("origin") or ""), str(item.get("name") or ""), value])
                identity = _extract_account_identity(_json_or_text(value))
                if identity:
                    material["account_fields"].append(identity)
    if material["account_fields"]:
        return {"account_fields": sorted(material["account_fields"], key=lambda item: json.dumps(item, sort_keys=True))}
    stable_cookies = [item for item in material["cookies"] if re.search(r"(?:user|profile|account|uid)", item[1], re.IGNORECASE)]
    if stable_cookies:
        return {"cookies": sorted(stable_cookies)}
    material["cookies"].sort()
    material["storage"].sort()
    return material if material["cookies"] or material["storage"] else None


def _extract_account_identity(value: Any, *, depth: int = 0) -> Mapping[str, Any] | None:
    if depth > 6:
        return None
    identity_keys = {
        "userid",
        "user_id",
        "profileid",
        "profile_id",
        "accountid",
        "account_id",
        "phone",
        "phone_number",
        "login",
    }
    if isinstance(value, Mapping):
        found: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").lower()
            if key in identity_keys and raw_value is not None and raw_value != "" and isinstance(raw_value, (str, int)):
                found[key] = str(raw_value)
        if found:
            return found
        for child in value.values():
            nested = _extract_account_identity(child, depth=depth + 1)
            if nested:
                return nested
    elif isinstance(value, list):
        for child in value[:50]:
            nested = _extract_account_identity(child, depth=depth + 1)
            if nested:
                return nested
    return None


def _json_or_text(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _looks_logged_out(lowered_text: str) -> bool:
    markers = ("войти или зарегистрироваться", "введите номер телефона", "получить код")
    return any(marker in lowered_text for marker in markers)


def _safe_page_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=5_000) or "")[:20_000]
    except Exception:
        return ""


def _extract_dom_price(page: Any) -> float | None:
    selectors = (
        "[data-link*='price'] ins",
        ".price-block__final-price",
        ".product-page__price",
        "ins.price-block__wallet-price",
    )
    for selector in selectors:
        try:
            text = str(page.locator(selector).first.inner_text(timeout=1_000) or "")
        except Exception:
            continue
        match = re.search(r"([0-9][0-9\s]*)", text.replace("\u00a0", " "))
        if match:
            try:
                return round(float(match.group(1).replace(" ", "")), 2)
            except ValueError:
                continue
    return None


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
        "network_primary_missing",
        "dom_fallback",
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
