"""Application service for WB prices management in the operator shell."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from packages.adapters.wb_prices_management import HttpBackedWbPricesManagementSource, WbPricesManagementSource
from packages.contracts.wb_prices_management import (
    MAX_PRICE_CHANGES_PER_UPLOAD,
    PRICE_UPLOAD_FINAL_STATUSES,
    PRICE_UPLOAD_STATUS_LABELS,
    WbPriceChange,
    WbPriceGood,
    WbPriceSize,
)


class WbPricesManagementError(ValueError):
    """Expected validation/safety error for the WB prices operator block."""

    def __init__(self, message: str, *, http_status: int = 400, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.http_status = int(http_status)
        self.payload = dict(payload or {})


@dataclass(frozen=True)
class WbPricesSafetyConfig:
    write_enabled: bool
    preview_ttl_seconds: int


class WbPricesManagementBlock:
    """Builds prices table views and guarded WB price upload operations."""

    def __init__(
        self,
        *,
        runtime: Any,
        runtime_dir: Path,
        source: WbPricesManagementSource | None = None,
        now_factory: Callable[[], datetime] | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        safety_config: WbPricesSafetyConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self.runtime_dir = runtime_dir
        self.source = source or HttpBackedWbPricesManagementSource()
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.timestamp_factory = timestamp_factory or (lambda: datetime.now(timezone.utc).isoformat())
        self.safety = safety_config or _load_safety_config()
        self._state_dir = self.runtime_dir / "sheet_vitrina_v1_prices"
        self._preview_dir = self._state_dir / "previews"
        self._audit_path = self._state_dir / "upload_audit.jsonl"

    def build_goods_table(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        limit = _parse_limit(params.get("limit"), default=1000)
        offset = _parse_offset(params.get("offset"))
        filter_nm_id = _optional_positive_int(_single_param(params.get("filterNmID") or params.get("nm_id")))
        active_nm_ids = [filter_nm_id] if filter_nm_id is not None else self._load_active_nm_ids()
        requested_nm_ids = _dedupe_ints(active_nm_ids)[:MAX_PRICE_CHANGES_PER_UPLOAD]
        if requested_nm_ids:
            payload = self.source.fetch_goods_by_nm_ids(requested_nm_ids)
            source_mode = "active_registry_nm_list"
        else:
            payload = self.source.fetch_goods(limit=limit, offset=offset, filter_nm_id=filter_nm_id)
            source_mode = "wb_list_goods_filter"
        goods = normalize_goods_payload(payload)
        enrichment = self._load_nomenclature_enrichment()
        rows = [self._build_row(good, enrichment.get(good.nm_id, {})) for good in goods]
        requested_set = set(requested_nm_ids)
        returned_set = {row["nmID"] for row in rows}
        missing_nm_ids = sorted(requested_set - returned_set)
        return {
            "contract_name": "sheet_vitrina_v1_prices_goods",
            "generated_at": self.timestamp_factory(),
            "write_enabled": self.safety.write_enabled,
            "rows": rows,
            "meta": {
                "source": "WB Prices and Discounts API",
                "source_mode": source_mode,
                "requested_count": len(requested_nm_ids),
                "returned_count": len(rows),
                "missing_nm_ids": missing_nm_ids,
                "limit": limit,
                "offset": offset,
                "filterNmID": filter_nm_id,
                "write_guard_env": "WB_PRICES_WRITE_ENABLED",
            },
        }

    def preview_changes(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        changes = _parse_changes(payload)
        nm_ids = [change.nm_id for change in changes]
        current_payload = self.source.fetch_goods_by_nm_ids(nm_ids)
        current_by_nm = {good.nm_id: good for good in normalize_goods_payload(current_payload)}
        enrichment = self._load_nomenclature_enrichment()
        rows: list[dict[str, Any]] = []
        valid_changes: list[dict[str, Any]] = []
        for change in changes:
            good = current_by_nm.get(change.nm_id)
            errors: list[str] = []
            warnings: list[str] = []
            if good is None:
                rows.append(
                    {
                        "nmID": change.nm_id,
                        "vendorCode": "",
                        "valid": False,
                        "errors": ["current price was not found in WB response"],
                        "warnings": [],
                        "requested": change.to_upload_dict(),
                    }
                )
                continue
            if good.editable_size_price and change.price is not None:
                errors.append("size-based price item: ordinary item price upload is blocked in MVP")
            old_price = _number_to_decimal(good.price)
            old_discount = Decimal(good.discount or 0)
            old_discounted = _number_to_decimal(good.discounted_price)
            new_price = Decimal(change.price) if change.price is not None else old_price
            new_discount = Decimal(change.discount) if change.discount is not None else old_discount
            new_discounted = _discounted_price(new_price, new_discount)
            if old_discounted > 0 and new_discounted * Decimal("3") <= old_discounted:
                warnings.append("quarantine_risk_new_discounted_price_at_least_3x_lower")
            valid = not errors
            row = {
                "nmID": good.nm_id,
                "vendorCode": good.vendor_code,
                "title": _display_title(enrichment.get(good.nm_id, {})),
                "editableSizePrice": good.editable_size_price,
                "valid": valid,
                "errors": errors,
                "warnings": warnings,
                "requested": change.to_upload_dict(),
                "current": {
                    "price": _decimal_to_json(old_price),
                    "discount": int(old_discount),
                    "discountedPrice": _decimal_to_json(old_discounted),
                },
                "new": {
                    "price": _decimal_to_json(new_price),
                    "discount": int(new_discount),
                    "discountedPrice": _decimal_to_json(new_discounted),
                },
                "diff": {
                    "price": _decimal_to_json(new_price - old_price),
                    "discount": int(new_discount - old_discount),
                    "discountedPrice": _decimal_to_json(new_discounted - old_discounted),
                },
            }
            rows.append(row)
            if valid:
                valid_changes.append(change.to_upload_dict())

        preview_id = uuid4().hex
        confirmation_token = uuid4().hex
        operation_id = uuid4().hex
        preview = {
            "preview_id": preview_id,
            "confirmation_token": confirmation_token,
            "operation_id": operation_id,
            "created_at": self.timestamp_factory(),
            "expires_at_epoch": int(time.time()) + self.safety.preview_ttl_seconds,
            "changes": valid_changes,
            "rows": rows,
        }
        self._save_preview(preview)
        return {
            "contract_name": "sheet_vitrina_v1_prices_preview",
            "status": "ready" if valid_changes else "blocked",
            "write_enabled": self.safety.write_enabled,
            "preview": {
                "preview_id": preview_id,
                "confirmation_token": confirmation_token,
                "operation_id": operation_id,
                "expires_at_epoch": preview["expires_at_epoch"],
                "rows": rows,
                "summary": {
                    "requested": len(changes),
                    "valid": len(valid_changes),
                    "blocked": len(changes) - len(valid_changes),
                    "warnings": sum(1 for row in rows if row.get("warnings")),
                },
            },
            "confirmation_payload": {
                "preview_id": preview_id,
                "confirmation_token": confirmation_token,
                "confirm": True,
            },
        }

    def upload_task(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        if not self.safety.write_enabled:
            raise WbPricesManagementError(
                "WB price writes are disabled; set WB_PRICES_WRITE_ENABLED=true for controlled live commit",
                http_status=403,
            )
        preview_id = str(payload.get("preview_id") or "").strip()
        confirmation_token = str(payload.get("confirmation_token") or "").strip()
        if not _coerce_bool(payload.get("confirm")):
            raise WbPricesManagementError("confirm=true is required for live price upload", http_status=400)
        preview = self._load_preview(preview_id)
        if confirmation_token != str(preview.get("confirmation_token") or ""):
            raise WbPricesManagementError("confirmation_token does not match preview", http_status=409)
        if int(preview.get("expires_at_epoch") or 0) < int(time.time()):
            raise WbPricesManagementError("price preview is stale; run preview again", http_status=409)
        changes = preview.get("changes") if isinstance(preview.get("changes"), list) else []
        if not changes:
            raise WbPricesManagementError("preview has no valid changes to upload", http_status=422)
        response_payload = self.source.upload_task([dict(item) for item in changes if isinstance(item, Mapping)])
        data = response_payload.get("data") if isinstance(response_payload.get("data"), Mapping) else {}
        upload_id = _optional_int(data.get("id") or data.get("uploadID") or data.get("upload_id"))
        already_exists = bool(data.get("alreadyExists"))
        status = "upload_already_exists" if already_exists else "upload_task_created"
        event = {
            "event_type": "sheet_vitrina_v1_prices_upload_task",
            "operation_id": str(preview.get("operation_id") or ""),
            "actor": actor,
            "timestamp": self.timestamp_factory(),
            "preview_id": preview_id,
            "upload_id": upload_id,
            "already_exists": already_exists,
            "request": {"data": changes},
            "wb_response": response_payload,
        }
        self._append_audit_event(event)
        return {
            "contract_name": "sheet_vitrina_v1_prices_upload_task",
            "status": status,
            "uploadID": upload_id,
            "alreadyExists": already_exists,
            "operation_id": event["operation_id"],
            "message": "WB accepted the upload task; final price application must be checked via upload status.",
            "wb_response": response_payload,
        }

    def get_upload_task(self, upload_id: int) -> dict[str, Any]:
        upload_id = _as_positive_int(upload_id, "upload_id")
        payload = self.source.fetch_upload_status(upload_id)
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        status_code = _optional_int(data.get("status"))
        status_label = map_upload_status(status_code)
        result = {
            "contract_name": "sheet_vitrina_v1_prices_upload_task_status",
            "generated_at": self.timestamp_factory(),
            "uploadID": upload_id,
            "status_code": status_code,
            "status": status_label,
            "is_final": status_code in PRICE_UPLOAD_FINAL_STATUSES,
            "uploadDate": str(data.get("uploadDate") or ""),
            "activationDate": str(data.get("activationDate") or ""),
            "overAllGoodsNumber": _optional_int(data.get("overAllGoodsNumber")),
            "successGoodsNumber": _optional_int(data.get("successGoodsNumber")),
            "wb_response": payload,
            "goods_errors": [],
        }
        if status_code in {5, 6}:
            try:
                details = self.get_upload_task_goods(upload_id, limit=MAX_PRICE_CHANGES_PER_UPLOAD, offset=0)
                result["goods_errors"] = [row for row in details["rows"] if row.get("errorText")]
            except Exception as exc:
                result["goods_errors_error"] = str(exc)
        return result

    def get_upload_task_goods(self, upload_id: int, *, limit: int, offset: int) -> dict[str, Any]:
        upload_id = _as_positive_int(upload_id, "upload_id")
        limit = _parse_limit(limit, default=1000)
        offset = _parse_offset(offset)
        payload = self.source.fetch_upload_goods(upload_id=upload_id, limit=limit, offset=offset)
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        raw_rows = data.get("historyGoods")
        if not isinstance(raw_rows, list):
            raw_rows = data.get("bufferGoods") if isinstance(data.get("bufferGoods"), list) else []
        rows = [normalize_upload_good(item) for item in raw_rows if isinstance(item, Mapping)]
        return {
            "contract_name": "sheet_vitrina_v1_prices_upload_task_goods",
            "generated_at": self.timestamp_factory(),
            "uploadID": upload_id,
            "limit": limit,
            "offset": offset,
            "rows": rows,
            "wb_response": payload,
        }

    def get_quarantine_goods(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        limit = _parse_limit(params.get("limit"), default=1000)
        offset = _parse_offset(params.get("offset"))
        payload = self.source.fetch_quarantine_goods(limit=limit, offset=offset)
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        raw_rows = data.get("quarantineGoods") if isinstance(data.get("quarantineGoods"), list) else []
        rows = [normalize_quarantine_good(item) for item in raw_rows if isinstance(item, Mapping)]
        return {
            "contract_name": "sheet_vitrina_v1_prices_quarantine",
            "generated_at": self.timestamp_factory(),
            "limit": limit,
            "offset": offset,
            "rows": rows,
            "wb_response": payload,
        }

    def _build_row(self, good: WbPriceGood, enrichment: Mapping[str, Any]) -> dict[str, Any]:
        title = _display_title(enrichment)
        return {
            **good.to_dict(),
            "title": title,
            "displayName": title,
            "ourSku": str(enrichment.get("our_sku") or ""),
            "barcode": str(enrichment.get("barcode") or enrichment.get("primary_barcode") or ""),
            "photoUrl": str(enrichment.get("photo_url") or ""),
            "lastUploadStatus": "",
            "wbErrorText": "",
        }

    def _load_active_nm_ids(self) -> list[int]:
        try:
            current_state = self.runtime.load_current_state()
        except Exception:
            return []
        nm_ids: list[int] = []
        for item in current_state.config_v2:
            if not bool(getattr(item, "enabled", False)):
                continue
            nm_ids.append(int(getattr(item, "nm_id")))
        return _dedupe_ints(nm_ids)

    def _load_nomenclature_enrichment(self) -> dict[int, Mapping[str, Any]]:
        try:
            items = self.runtime.list_nomenclature_items(active_only=True)
        except Exception:
            return {}
        result: dict[int, Mapping[str, Any]] = {}
        for item in items:
            nm_id = _optional_int(item.get("nm_id"))
            if nm_id is not None:
                result[nm_id] = item
        return result

    def _save_preview(self, preview: Mapping[str, Any]) -> None:
        self._preview_dir.mkdir(parents=True, exist_ok=True)
        path = self._preview_dir / f"{preview['preview_id']}.json"
        path.write_text(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _load_preview(self, preview_id: str) -> dict[str, Any]:
        normalized = str(preview_id or "").strip()
        if not normalized or "/" in normalized or "." in normalized:
            raise WbPricesManagementError("invalid preview_id", http_status=400)
        path = self._preview_dir / f"{normalized}.json"
        if not path.exists():
            raise WbPricesManagementError("preview_id not found", http_status=404)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise WbPricesManagementError("stored preview is invalid", http_status=500)
        return payload

    def _append_audit_event(self, event: Mapping[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def normalize_goods_payload(payload: Mapping[str, Any]) -> list[WbPriceGood]:
    if bool(payload.get("error")):
        detail = str(payload.get("errorText") or "unknown WB prices API error")
        raise WbPricesManagementError(f"WB prices API returned error payload: {detail}", http_status=502)
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    raw_goods = data.get("listGoods") if isinstance(data.get("listGoods"), list) else []
    return [normalize_good(item) for item in raw_goods if isinstance(item, Mapping)]


def normalize_good(good: Mapping[str, Any]) -> WbPriceGood:
    sizes = [normalize_size(size) for size in good.get("sizes", []) if isinstance(size, Mapping)]
    prices = [_number_or_none(size.price) for size in sizes if size.price is not None]
    discounted = [_number_or_none(size.discounted_price) for size in sizes if size.discounted_price is not None]
    club_discounted = [
        _number_or_none(size.club_discounted_price) for size in sizes if size.club_discounted_price is not None
    ]
    return WbPriceGood(
        nm_id=_as_positive_int(good.get("nmID"), "nmID"),
        vendor_code=str(good.get("vendorCode") or ""),
        sizes=sizes,
        price=min(prices) if prices else None,
        discounted_price=min(discounted) if discounted else None,
        club_discounted_price=min(club_discounted) if club_discounted else None,
        discount=_optional_int(good.get("discount")),
        club_discount=_optional_int(good.get("clubDiscount")),
        currency_iso_code_4217=str(good.get("currencyIsoCode4217") or ""),
        editable_size_price=bool(good.get("editableSizePrice")),
        wholesale_discount_threshold=[
            dict(item) for item in good.get("wholesaleDiscountThreshold", []) if isinstance(item, Mapping)
        ],
        is_bad_turnover=bool(good.get("isBadTurnover")) if good.get("isBadTurnover") is not None else None,
    )


def normalize_size(size: Mapping[str, Any]) -> WbPriceSize:
    return WbPriceSize(
        size_id=_optional_int(size.get("sizeID")),
        tech_size_name=str(size.get("techSizeName") or ""),
        price=_number_or_none(size.get("price")),
        discounted_price=_number_or_none(size.get("discountedPrice")),
        club_discounted_price=_number_or_none(size.get("clubDiscountedPrice")),
    )


def normalize_upload_good(row: Mapping[str, Any]) -> dict[str, Any]:
    status_code = _optional_int(row.get("status"))
    return {
        "nmID": _optional_int(row.get("nmID")),
        "vendorCode": str(row.get("vendorCode") or ""),
        "sizeID": _optional_int(row.get("sizeID")),
        "techSizeName": str(row.get("techSizeName") or ""),
        "price": _number_or_none(row.get("price")),
        "currencyIsoCode4217": str(row.get("currencyIsoCode4217") or ""),
        "discount": _optional_int(row.get("discount")),
        "clubDiscount": _optional_int(row.get("clubDiscount")),
        "status_code": status_code,
        "status": map_upload_status(status_code),
        "errorText": str(row.get("errorText") or ""),
    }


def normalize_quarantine_good(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "nmID": _optional_int(row.get("nmID")),
        "sizeID": _optional_int(row.get("sizeID")),
        "techSizeName": str(row.get("techSizeName") or ""),
        "currencyIsoCode4217": str(row.get("currencyIsoCode4217") or ""),
        "newPrice": _number_or_none(row.get("newPrice")),
        "oldPrice": _number_or_none(row.get("oldPrice")),
        "newDiscount": _optional_int(row.get("newDiscount")),
        "oldDiscount": _optional_int(row.get("oldDiscount")),
        "priceDiff": _number_or_none(row.get("priceDiff")),
    }


def map_upload_status(status_code: int | None) -> str:
    if status_code is None:
        return "unknown"
    return PRICE_UPLOAD_STATUS_LABELS.get(int(status_code), "unknown")


def _parse_changes(payload: Mapping[str, Any]) -> list[WbPriceChange]:
    raw_changes = payload.get("changes") if isinstance(payload.get("changes"), list) else payload.get("data")
    if not isinstance(raw_changes, list):
        raise WbPricesManagementError("changes must be a list", http_status=400)
    if not raw_changes:
        raise WbPricesManagementError("changes must not be empty", http_status=400)
    if len(raw_changes) > MAX_PRICE_CHANGES_PER_UPLOAD:
        raise WbPricesManagementError("changes must contain no more than 1000 goods", http_status=422)
    seen: set[int] = set()
    changes: list[WbPriceChange] = []
    for raw in raw_changes:
        if not isinstance(raw, Mapping):
            raise WbPricesManagementError("each change must be an object", http_status=400)
        nm_id = _as_positive_int(raw.get("nmID") or raw.get("nm_id"), "nmID")
        if nm_id in seen:
            raise WbPricesManagementError(f"duplicate nmID in changes: {nm_id}", http_status=422)
        seen.add(nm_id)
        has_price = "price" in raw and raw.get("price") is not None and str(raw.get("price")).strip() != ""
        has_discount = "discount" in raw and raw.get("discount") is not None and str(raw.get("discount")).strip() != ""
        if not has_price and not has_discount:
            raise WbPricesManagementError("each change must include price or discount", http_status=422)
        price = _parse_price(raw.get("price")) if has_price else None
        discount = _parse_discount(raw.get("discount")) if has_discount else None
        changes.append(WbPriceChange(nm_id=nm_id, price=price, discount=discount))
    return changes


def _load_safety_config() -> WbPricesSafetyConfig:
    return WbPricesSafetyConfig(
        write_enabled=os.environ.get("WB_PRICES_WRITE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"},
        preview_ttl_seconds=int(os.environ.get("WB_PRICES_PREVIEW_TTL_SECONDS", "300") or "300"),
    )


def _discounted_price(price: Decimal, discount: Decimal) -> Decimal:
    return (price * (Decimal("100") - discount) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_price(value: Any) -> int:
    amount = _parse_decimal(value, field_name="price")
    if amount <= 0:
        raise WbPricesManagementError("price must be > 0", http_status=422)
    if amount != amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP):
        raise WbPricesManagementError("price must be an integer", http_status=422)
    return int(amount)


def _parse_discount(value: Any) -> int:
    amount = _parse_decimal(value, field_name="discount")
    if amount < 0 or amount > 99:
        raise WbPricesManagementError("discount must be between 0 and 99", http_status=422)
    if amount != amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP):
        raise WbPricesManagementError("discount must be an integer percent", http_status=422)
    return int(amount)


def _parse_decimal(value: Any, *, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise WbPricesManagementError(f"{field_name} must be numeric", http_status=400)
    raw = str(value).strip().replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise WbPricesManagementError(f"{field_name} must be numeric", http_status=400) from exc


def _display_title(enrichment: Mapping[str, Any]) -> str:
    return str(
        enrichment.get("nomenclature_name")
        or enrichment.get("wb_title")
        or enrichment.get("display_name")
        or ""
    )


def _dedupe_ints(values: Sequence[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number in seen:
            continue
        seen.add(number)
        result.append(number)
    return result


def _parse_limit(value: Any, *, default: int) -> int:
    try:
        limit = int(_single_param(value) or default)
    except (TypeError, ValueError) as exc:
        raise WbPricesManagementError("limit must be integer", http_status=400) from exc
    if limit <= 0 or limit > 1000:
        raise WbPricesManagementError("limit must be between 1 and 1000", http_status=422)
    return limit


def _parse_offset(value: Any) -> int:
    try:
        offset = int(_single_param(value) or 0)
    except (TypeError, ValueError) as exc:
        raise WbPricesManagementError("offset must be integer", http_status=400) from exc
    if offset < 0:
        raise WbPricesManagementError("offset must be >= 0", http_status=422)
    return offset


def _single_param(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _optional_positive_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _as_positive_int(value, "nmID")


def _as_positive_int(value: Any, field_name: str) -> int:
    number = _optional_int(value)
    if number is None or number <= 0:
        raise WbPricesManagementError(f"{field_name} must be a positive integer", http_status=400)
    return number


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return _decimal_to_json(number)


def _number_to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def _decimal_to_json(value: Decimal) -> int | float:
    normalized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if normalized == normalized.to_integral_value():
        return int(normalized)
    return float(normalized)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False
