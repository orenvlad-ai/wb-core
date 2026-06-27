"""Seller Portal browser/network-json source for WB transit supply costs.

This adapter is intentionally not part of the official WB Developer API
boundary. It drives an authenticated Seller Portal browser session in read-only
mode and stores only normalized facts returned by the table's network JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
import time
from typing import Any, Mapping
from urllib import parse as urllib_parse


SELLER_PORTAL_TRANSIT_COST_SOURCE = "seller_portal_browser"
SELLER_PORTAL_TRANSIT_COST_EVIDENCE_TYPE = "network_json"
SELLER_PORTAL_SUPPLIES_URL = "https://seller.wildberries.ru/supplies-management/all-supplies"
SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH = "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost"
SELLER_PORTAL_LIST_SUPPLIES_ENDPOINT_PATH = "/ns/sm-supply/supply-manager/api/v1/supply/listSupplies"


class SellerPortalTransitCostSourceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "failed") -> None:
        self.code = str(code or "failed")
        super().__init__(_safe_text(message, 500))


@dataclass(frozen=True)
class SellerPortalTransitCostCandidate:
    supply_id: str


class SellerPortalTransitCostNetworkJsonSource:
    """Read-only browser worker for Seller Portal supply cost network JSON."""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 20_000,
        search_wait_ms: int = 7_000,
    ) -> None:
        self.headless = bool(headless)
        self.timeout_ms = max(5_000, int(timeout_ms or 20_000))
        self.search_wait_ms = max(2_000, int(search_wait_ms or 7_000))

    def fetch_costs(
        self,
        candidates: list[Mapping[str, Any]],
        *,
        run_id: str,
        runtime_dir: Path,
        fetched_at: str,
    ) -> list[dict[str, Any]]:
        normalized_candidates = [
            SellerPortalTransitCostCandidate(supply_id=str(item.get("supply_id") or "").strip())
            for item in candidates
            if str(item.get("supply_id") or "").strip()
        ]
        if not normalized_candidates:
            return []
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - depends on host runtime.
            raise SellerPortalTransitCostSourceError(
                f"Playwright is unavailable for Seller Portal enrichment: {exc.__class__.__name__}",
                code="playwright_unavailable",
            ) from exc
        try:
            from apps.seller_portal_automation_guard import (
                SellerPortalAutomationBusy,
                SellerPortalStorageStatePolicyError,
                seller_portal_automation_lock,
                seller_portal_storage_state_path,
                validate_storage_state_path_for_runtime,
            )
        except Exception as exc:  # pragma: no cover - import should be stable in repo runtime.
            raise SellerPortalTransitCostSourceError(
                f"Seller Portal automation guard is unavailable: {exc.__class__.__name__}",
                code="guard_unavailable",
            ) from exc

        storage_state_path = seller_portal_storage_state_path()
        try:
            validate_storage_state_path_for_runtime(storage_state_path, runtime_dir)
        except SellerPortalStorageStatePolicyError as exc:
            raise SellerPortalTransitCostSourceError(str(exc), code="storage_state_policy") from exc
        if not storage_state_path.exists():
            raise SellerPortalTransitCostSourceError(
                "Seller Portal storage state is absent",
                code="storage_state_absent",
            )

        try:
            with seller_portal_automation_lock(
                runtime_dir=runtime_dir,
                owner="wb_supply_transit_cost_enrichment",
                purpose="transit_cost_enrichment",
                run_id=run_id,
                expected_max_seconds=max(180, int(len(normalized_candidates) * self.search_wait_ms / 1000) + 120),
                wait_seconds=0,
            ) as lock:
                results: list[dict[str, Any]] = []
                captured_cost_payloads: list[Mapping[str, Any]] = []
                captured_list_payloads: list[Mapping[str, Any]] = []

                def on_response(response: Any) -> None:
                    try:
                        split = urllib_parse.urlsplit(str(response.url))
                        if split.netloc != "seller-supply.wildberries.ru":
                            return
                        if split.path not in {SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH, SELLER_PORTAL_LIST_SUPPLIES_ENDPOINT_PATH}:
                            return
                        payload = response.json()
                        if split.path == SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH and isinstance(payload, Mapping):
                            captured_cost_payloads.append(payload)
                        elif split.path == SELLER_PORTAL_LIST_SUPPLIES_ENDPOINT_PATH and isinstance(payload, Mapping):
                            captured_list_payloads.append(payload)
                    except Exception:
                        return

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        headless=self.headless,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                    context = browser.new_context(
                        storage_state=str(storage_state_path),
                        locale="ru-RU",
                        timezone_id="Europe/Moscow",
                        viewport={"width": 1600, "height": 1100},
                        accept_downloads=False,
                    )
                    page = context.new_page()
                    page.set_default_timeout(self.timeout_ms)
                    page.on("response", on_response)
                    try:
                        page.goto(SELLER_PORTAL_SUPPLIES_URL, wait_until="domcontentloaded", timeout=self.timeout_ms * 2)
                        try:
                            page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
                        except PlaywrightTimeoutError:
                            pass
                        page.wait_for_timeout(2500)
                        if _looks_like_login(page):
                            return [
                                _status_result(candidate.supply_id, status="session_expired", error="Seller Portal session expired")
                                for candidate in normalized_candidates
                            ]
                        search_input = page.locator('input[placeholder="Номер поставки"]').first
                        if not search_input.count():
                            raise SellerPortalTransitCostSourceError(
                                "Seller Portal supply search input was not found",
                                code="selector_not_found",
                            )

                        for candidate in normalized_candidates:
                            supply_id = candidate.supply_id
                            existing = _find_cost_result(captured_cost_payloads, supply_id, fetched_at=fetched_at)
                            if existing:
                                results.append(existing)
                                continue
                            before_cost_count = len(captured_cost_payloads)
                            before_list_count = len(captured_list_payloads)
                            try:
                                search_input.fill(supply_id, timeout=6000)
                                page.keyboard.press("Enter")
                            except Exception as exc:
                                results.append(
                                    _status_result(
                                        supply_id,
                                        status="failed",
                                        error=f"Seller Portal search failed: {exc.__class__.__name__}",
                                    )
                                )
                                continue
                            deadline = time.monotonic() + (self.search_wait_ms / 1000)
                            found: dict[str, Any] | None = None
                            while time.monotonic() < deadline:
                                found = _find_cost_result(
                                    captured_cost_payloads[before_cost_count:],
                                    supply_id,
                                    fetched_at=fetched_at,
                                )
                                if found:
                                    break
                                page.wait_for_timeout(250)
                            if found:
                                results.append(found)
                                continue
                            if _list_response_has_empty_target(captured_list_payloads[before_list_count:], supply_id):
                                results.append(_status_result(supply_id, status="not_found", error="target row not found"))
                            else:
                                results.append(_status_result(supply_id, status="failed", error="supply/cost response missing target key"))
                        lock.heartbeat()
                    finally:
                        context.close()
                        browser.close()
                return results
        except SellerPortalAutomationBusy as exc:
            raise SellerPortalTransitCostSourceError(str(exc), code="lock_busy") from exc


def parse_supply_cost_payload(payload: Mapping[str, Any], *, fetched_at: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return []
    results: list[dict[str, Any]] = []
    for supply_id, entry in data.items():
        normalized = _normalize_cost_entry(str(supply_id), entry, fetched_at=fetched_at)
        if normalized:
            results.append(normalized)
    return results


def _find_cost_result(payloads: list[Mapping[str, Any]], supply_id: str, *, fetched_at: str) -> dict[str, Any] | None:
    for payload in reversed(payloads):
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping) or str(supply_id) not in data:
            continue
        normalized = _normalize_cost_entry(str(supply_id), data.get(str(supply_id)), fetched_at=fetched_at)
        if normalized:
            return normalized
        return _status_result(str(supply_id), status="failed", error="supply/cost entry has no usable amount")
    return None


def _normalize_cost_entry(supply_id: str, entry: Any, *, fetched_at: str) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    supplier_currency = entry.get("costInSupplierCurrency") if isinstance(entry.get("costInSupplierCurrency"), Mapping) else {}
    amount = _optional_number(supplier_currency.get("amountWithVat"))
    source_field = "costInSupplierCurrency.amountWithVat"
    if amount is None:
        amount = _optional_number(entry.get("cost"))
        source_field = "cost"
    if amount is None:
        return None
    return {
        "supply_id": str(supply_id),
        "amount": amount,
        "currency": "RUB",
        "amount_label": _format_rub(amount),
        "is_transit": True,
        "source": SELLER_PORTAL_TRANSIT_COST_SOURCE,
        "evidence_type": SELLER_PORTAL_TRANSIT_COST_EVIDENCE_TYPE,
        "confidence": "high" if source_field == "costInSupplierCurrency.amountWithVat" else "medium",
        "fetched_at": fetched_at,
        "status": "success",
        "error": "",
        "source_endpoint_path": SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH,
        "source_field": source_field,
        "tariff_id": entry.get("tariffID"),
        "box_amount": entry.get("boxAmount"),
        "number_of_pallets": entry.get("numberOfPallets"),
    }


def _status_result(supply_id: str, *, status: str, error: str) -> dict[str, Any]:
    return {
        "supply_id": str(supply_id),
        "amount": None,
        "currency": "RUB",
        "amount_label": "",
        "is_transit": True,
        "source": SELLER_PORTAL_TRANSIT_COST_SOURCE,
        "evidence_type": SELLER_PORTAL_TRANSIT_COST_EVIDENCE_TYPE,
        "confidence": "none",
        "fetched_at": "",
        "status": status,
        "error": _safe_text(error, 500),
        "source_endpoint_path": SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH,
    }


def _list_response_has_empty_target(payloads: list[Mapping[str, Any]], supply_id: str) -> bool:
    for payload in reversed(payloads):
        result = payload.get("result") if isinstance(payload, Mapping) else None
        data = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(data, list):
            continue
        if not data:
            return True
        for row in data:
            if isinstance(row, Mapping) and str(row.get("supplyId") or row.get("supplyID") or "") == str(supply_id):
                return False
    return False


def _looks_like_login(page: Any) -> bool:
    url = str(getattr(page, "url", "") or "").lower()
    if "seller-auth" in url or "passport" in url or "login" in url:
        return True
    try:
        text = page.locator("body").inner_text(timeout=1500).lower()
    except Exception:
        return False
    return "войти" in text and ("телефон" in text or "пароль" in text or "qr" in text)


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
        return amount if amount == amount else None
    text = str(value).strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        amount = float(text)
    except ValueError:
        return None
    return amount if amount == amount else None


def _format_rub(amount: float) -> str:
    if abs(amount - round(amount)) < 0.005:
        return f"{int(round(amount)):,}".replace(",", " ") + " ₽"
    integer, fractional = f"{amount:.2f}".split(".")
    return f"{int(integer):,}".replace(",", " ") + f",{fractional} ₽"


def _safe_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    blocked = ("token", "cookie", "secret", "password", "authorization", "storage_state", "header")
    lowered = text.lower()
    if any(marker in lowered for marker in blocked):
        return "[redacted]"
    return text[: max(0, int(limit))]
