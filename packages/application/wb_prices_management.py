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
from packages.contracts.wb_price_quarantine import (
    WB_QUARANTINE_WARNING_CODE,
    evaluate_wb_price_quarantine_transition,
)
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
        provided_payload = params.get("_current_goods_payload")
        if isinstance(provided_payload, Mapping):
            payload = provided_payload
            source_mode = "provided_current_goods_payload"
        elif requested_nm_ids:
            payload = self.source.fetch_goods_by_nm_ids(requested_nm_ids)
            source_mode = "active_registry_nm_list"
        else:
            payload = self.source.fetch_goods(limit=limit, offset=offset, filter_nm_id=filter_nm_id)
            source_mode = "wb_list_goods_filter"
        goods = normalize_goods_payload(payload)
        enrichment = self._load_nomenclature_enrichment()
        read_side = self._load_read_side_enrichment([good.nm_id for good in goods])
        rows = [
            self._build_row(
                good,
                enrichment.get(good.nm_id, {}),
                read_side.get(good.nm_id, {}),
            )
            for good in goods
        ]
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

    def preview_changes(
        self,
        payload: Mapping[str, Any],
        *,
        current_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        changes = _parse_changes(payload)
        nm_ids = [change.nm_id for change in changes]
        current_goods_payload = (
            current_payload
            if isinstance(current_payload, Mapping)
            else self.source.fetch_goods_by_nm_ids(nm_ids)
        )
        current_by_nm = {
            good.nm_id: good
            for good in normalize_goods_payload(current_goods_payload)
        }
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
            quarantine_transition = None
            if old_discounted > 0 and new_discounted > 0:
                quarantine_transition = evaluate_wb_price_quarantine_transition(
                    old_discounted,
                    new_discounted,
                )
                if quarantine_transition.risky:
                    warnings.append(WB_QUARANTINE_WARNING_CODE)
            valid = not errors
            row = {
                "nmID": good.nm_id,
                "vendorCode": good.vendor_code,
                "title": _display_title(enrichment.get(good.nm_id, {})),
                "editableSizePrice": good.editable_size_price,
                "valid": valid,
                "errors": errors,
                "warnings": warnings,
                "quarantine_transition": quarantine_transition.to_dict() if quarantine_transition else None,
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

    def _build_row(
        self,
        good: WbPriceGood,
        enrichment: Mapping[str, Any],
        read_side: Mapping[str, Any],
    ) -> dict[str, Any]:
        title = _display_title(enrichment)
        return {
            **good.to_dict(),
            "title": title,
            "displayName": title,
            "ourSku": str(enrichment.get("our_sku") or ""),
            "barcode": str(enrichment.get("barcode") or enrichment.get("primary_barcode") or ""),
            "photoUrl": str(enrichment.get("photo_url") or ""),
            "sppProxy": read_side.get("sppProxy"),
            "sppProxyLabel": str(read_side.get("sppProxyLabel") or "н/д"),
            "sppProxyReason": str(read_side.get("sppProxyReason") or ""),
            "promoEligibleCount": read_side.get("promoEligibleCount"),
            "promoCandidateCount": read_side.get("promoCandidateCount"),
            "promoCurrentCount": read_side.get("promoCurrentCount"),
            "promoLabel": str(read_side.get("promoLabel") or "н/д"),
            "promoReason": str(read_side.get("promoReason") or ""),
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

    def _load_read_side_enrichment(self, nm_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        requested = _dedupe_ints([int(value) for value in nm_ids])
        result = {
            nm_id: {
                "sppProxy": None,
                "sppProxyLabel": "н/д",
                "sppProxyReason": "spp_proxy source is unavailable",
                "promoEligibleCount": None,
                "promoCandidateCount": None,
                "promoCurrentCount": None,
                "promoLabel": "н/д",
                "promoReason": "promo source is unavailable",
            }
            for nm_id in requested
        }
        if not requested:
            return result

        try:
            snapshot = self.runtime.load_sheet_vitrina_ready_snapshot()
        except Exception as exc:
            reason = f"ready snapshot unavailable: {exc}"
            for row in result.values():
                row["sppProxyReason"] = reason
                row["promoReason"] = reason
            return result

        data_sheet = next((sheet for sheet in getattr(snapshot, "sheets", []) if sheet.sheet_name == "DATA_VITRINA"), None)
        if data_sheet is None:
            for row in result.values():
                row["sppProxyReason"] = "DATA_VITRINA sheet is unavailable"
                row["promoReason"] = "DATA_VITRINA sheet is unavailable"
            return result

        date_columns = [str(item) for item in getattr(snapshot, "date_columns", []) if str(item)]
        row_by_id = {
            str(row[1]): list(row)
            for row in getattr(data_sheet, "rows", [])
            if isinstance(row, list) and len(row) >= 2
        }
        promo_context_cache: dict[str, tuple[float | None, str, dict[int, float], set[int], str, str | None]] = {}

        for nm_id in requested:
            spp_value, spp_date = _latest_metric_value(
                row_by_id.get(f"SKU:{nm_id}|spp_proxy"),
                date_columns=date_columns,
            )
            if spp_value is None:
                result[nm_id]["sppProxyReason"] = "spp_proxy value is unavailable in latest ready snapshot"
            else:
                result[nm_id]["sppProxy"] = spp_value
                result[nm_id]["sppProxyLabel"] = _format_percent_label(spp_value)
                result[nm_id]["sppProxyReason"] = f"source=DATA_VITRINA metric=spp_proxy date={spp_date}"

            promo_eligible, promo_date = _latest_metric_value(
                row_by_id.get(f"SKU:{nm_id}|promo_count_by_price"),
                date_columns=date_columns,
            )
            if promo_eligible is None or not promo_date:
                result[nm_id]["promoReason"] = "promo_count_by_price value is unavailable in latest ready snapshot"
                continue

            current_count, current_count_source, candidate_count, candidate_reason = self._load_promo_current_count(
                nm_id=nm_id,
                snapshot_date=promo_date,
                cache=promo_context_cache,
            )
            result[nm_id]["promoEligibleCount"] = promo_eligible
            result[nm_id]["promoCandidateCount"] = candidate_count
            result[nm_id]["promoCurrentCount"] = current_count
            if current_count is None:
                result[nm_id]["promoLabel"] = "н/д"
                result[nm_id]["promoReason"] = candidate_reason
            else:
                result[nm_id]["promoLabel"] = f"{_format_count_label(promo_eligible)} / {_format_count_label(current_count)}"
                candidate_part = (
                    f"; candidate campaigns for SKU={_format_count_label(candidate_count)}"
                    if candidate_count is not None
                    else ""
                )
                result[nm_id]["promoReason"] = (
                    f"eligible by price={_format_count_label(promo_eligible)}; "
                    f"total current promo campaigns={_format_count_label(current_count)}; "
                    f"denominator_source={current_count_source}; "
                    f"source=promo_by_price date={promo_date}{candidate_part}"
                )
        return result

    def _load_promo_current_count(
        self,
        *,
        nm_id: int,
        snapshot_date: str,
        cache: dict[str, tuple[float | None, str, dict[int, float], set[int], str, str | None]],
    ) -> tuple[float | None, str, float | None, str]:
        if snapshot_date not in cache:
            cache[snapshot_date] = self._load_promo_context(snapshot_date=snapshot_date)
        current_count, current_count_source, candidate_counts, missing_count_nm_ids, reason, captured_at = cache[snapshot_date]
        if current_count is None:
            return None, "", None, reason
        candidate_count = candidate_counts.get(nm_id)
        captured_part = f"; captured_at={captured_at}" if captured_at else ""
        if nm_id in missing_count_nm_ids:
            return current_count, current_count_source, None, (
                f"promo source payload for {snapshot_date} has no candidate count; "
                f"using global current promo denominator{captured_part}"
            )
        if candidate_count is None:
            return current_count, current_count_source, None, (
                f"promo source payload has no item for nmID {nm_id} on {snapshot_date}; "
                f"using global current promo denominator{captured_part}"
            )
        return current_count, current_count_source, candidate_count, f"source=promo_by_price date={snapshot_date}{captured_part}"

    def _load_promo_context(
        self,
        *,
        snapshot_date: str,
    ) -> tuple[float | None, str, dict[int, float], set[int], str, str | None]:
        try:
            payload, captured_at = self.runtime.load_temporal_source_snapshot(
                source_key="promo_by_price",
                snapshot_date=snapshot_date,
            )
        except Exception as exc:
            return None, "", {}, set(), f"promo source payload is unavailable for {snapshot_date}: {exc}", None
        if payload is None:
            return None, "", {}, set(), f"promo source payload is unavailable for {snapshot_date}", None
        result_payload = _promo_result_payload(payload)
        current_count, current_count_source = _promo_current_count(result_payload)
        if current_count is None:
            return None, "", {}, set(), (
                f"promo source payload for {snapshot_date} has no current promo denominator; "
                "run a fresh promo source refresh/backfill"
            ), captured_at
        items = _promo_payload_field(result_payload, "items")
        if not isinstance(items, list):
            return current_count, current_count_source, {}, set(), "", captured_at
        counts: dict[int, float] = {}
        missing_count_nm_ids: set[int] = set()
        for item in items:
            item_nm_id = _optional_int(_promo_payload_field(item, "nm_id"))
            if item_nm_id is None:
                continue
            count = _number_or_none(_promo_payload_field(item, "promo_candidate_count"))
            if count is None:
                missing_count_nm_ids.add(item_nm_id)
                continue
            counts[item_nm_id] = float(count)
        return current_count, current_count_source, counts, missing_count_nm_ids, "", captured_at

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


def _promo_result_payload(payload: Any) -> Any:
    result = _promo_payload_field(payload, "result")
    if result is not None:
        return result
    return payload


def _promo_current_count(payload: Any) -> tuple[float | None, str]:
    for field_name in ("current_promos", "current_promo_count", "covering_campaigns"):
        value = _number_or_none(_promo_payload_field(payload, field_name))
        if value is not None:
            return float(value), field_name

    diagnostics = _promo_payload_field(payload, "diagnostics")
    for field_name in ("current_promo_count", "covering_campaigns", "materializable_campaigns", "usable_campaigns"):
        value = _number_or_none(_promo_payload_field(diagnostics, field_name))
        if value is not None:
            return float(value), f"diagnostics.{field_name}"

    counters = _promo_payload_field(diagnostics, "counters")
    for field_name in ("current_promo_count", "covering_campaigns", "materializable_campaigns", "usable_campaigns"):
        value = _number_or_none(_promo_payload_field(counters, field_name))
        if value is not None:
            return float(value), f"diagnostics.counters.{field_name}"

    return None, ""


def _promo_payload_field(payload: Any, field_name: str) -> Any:
    if payload is None:
        return None
    if isinstance(payload, Mapping):
        return payload.get(field_name)
    return getattr(payload, field_name, None)


def _latest_metric_value(row: list[Any] | None, *, date_columns: Sequence[str]) -> tuple[float | None, str]:
    if not row or not date_columns:
        return None, ""
    for index in range(len(date_columns) - 1, -1, -1):
        value_index = index + 2
        if value_index >= len(row):
            continue
        value = _number_or_none(row[value_index])
        if value is not None:
            return float(value), str(date_columns[index])
    return None, ""


def _format_percent_label(value: float) -> str:
    number = float(value)
    percent = number * 100 if abs(number) <= 1 else number
    return f"{_format_decimal_label(percent, max_digits=1)}%"


def _format_count_label(value: float) -> str:
    return _format_decimal_label(float(value), max_digits=1)


def _format_decimal_label(value: float, *, max_digits: int) -> str:
    quant = Decimal("1") if max_digits <= 0 else Decimal("0." + ("0" * (max_digits - 1)) + "1")
    number = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


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
