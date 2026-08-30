"""SKU-first operator ads block for WB Promotion API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from packages.adapters.wb_promotion import (
    HttpBackedWbPromotionSource,
    WbPromotionApiError,
    WbPromotionSource,
    extract_advert_ids_from_count,
)
from packages.application.change_registry_writer import (
    InternalWriterRegistry,
    InternalWriterRegistryError,
)


SUPPORTED_BID_STATUSES = {4, 9, 11}
SUPPORTED_PLACEMENTS = {"combined", "search", "recommendations"}
MIN_BID_PLACEMENT = {
    "combined": "combined",
    "search": "search",
    "recommendations": "recommendation",
}


class SheetVitrinaV1AdsError(ValueError):
    """Expected validation/safety error for the ads operator block."""

    def __init__(self, message: str, *, http_status: int = 400, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.http_status = int(http_status)
        self.payload = dict(payload or {})


@dataclass(frozen=True)
class AdsSafetyConfig:
    write_enabled: bool
    absolute_max_bid_kopecks: int
    max_percent_increase: Decimal
    max_absolute_increase_kopecks: int
    preview_ttl_seconds: int


class AdsBidSafetyThresholdPolicy(str, Enum):
    """Controls only seller-defined bid thresholds, never WB/API guards."""

    STRICT = "strict"
    OWNER_CONFIRMED_BALANCE = "owner_confirmed_balance"


class SheetVitrinaV1AdsBlock:
    """Builds SKU-first Promotion API views and guarded bid-change operations."""

    def __init__(
        self,
        *,
        runtime: Any,
        runtime_dir: Path,
        source: WbPromotionSource | None = None,
        now_factory: Callable[[], datetime] | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        cache_ttl_seconds: int = 120,
        safety_config: AdsSafetyConfig | None = None,
        writer_registry: InternalWriterRegistry | None = None,
        registry_source_surface: str = "ads_bid_change",
    ) -> None:
        self.runtime = runtime
        self.runtime_dir = runtime_dir
        self.source = source or HttpBackedWbPromotionSource()
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.timestamp_factory = timestamp_factory or (lambda: datetime.now(timezone.utc).isoformat())
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.safety = safety_config or _load_safety_config()
        self.writer_registry = writer_registry
        self.registry_source_surface = registry_source_surface
        self._campaign_cache: dict[str, Any] | None = None
        self._state_dir = self.runtime_dir / "sheet_vitrina_v1_ads"
        self._preview_dir = self._state_dir / "previews"
        self._audit_path = self._state_dir / "bid_audit.jsonl"
        self._last_stats_error = ""

    def build_sku_table(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        date_from, date_to = self._resolve_period(params or {})
        timestamp = self.timestamp_factory()
        sku_rows = self._load_sku_universe()
        campaigns_payload = self._load_campaigns()
        reverse_index = _build_reverse_index(campaigns_payload["campaigns"])
        advert_ids = [int(campaign["advert_id"]) for campaign in campaigns_payload["campaigns"]]
        stats_by_nm = self._safe_fetch_stats(advert_ids, date_from=date_from, date_to=date_to)
        stats_status = "error" if self._last_stats_error else "ok"

        rows: list[dict[str, Any]] = []
        known_nm_ids = {int(row["nm_id"]) for row in sku_rows}
        for sku in sku_rows:
            nm_id = int(sku["nm_id"])
            stats = stats_by_nm.get((0, nm_id), _empty_stats())
            campaigns = reverse_index.get(nm_id, [])
            rows.append(
                {
                    **sku,
                    "campaign_count": len({int(item["advert_id"]) for item in campaigns}),
                    "placement_count": len(campaigns),
                    "views": stats["views"],
                    "clicks": stats["clicks"],
                    "ctr": _ratio(stats["clicks"], stats["views"]),
                    "orders": stats["orders"],
                    "sum": stats["sum"],
                    "spend_rub": stats["sum"],
                    "status": "linked" if campaigns else "no_campaigns",
                    "stats_status": stats_status,
                    "error": self._last_stats_error,
                }
            )

        external_rows = []
        for nm_id, campaign_rows in sorted(reverse_index.items()):
            if nm_id in known_nm_ids:
                continue
            stats = stats_by_nm.get((0, nm_id), _empty_stats())
            external_rows.append(
                {
                    "nm_id": nm_id,
                    "display_name": "",
                    "our_sku": "",
                    "barcode": "",
                    "source": "wb_campaign_only",
                    "campaign_count": len({int(item["advert_id"]) for item in campaign_rows}),
                    "placement_count": len(campaign_rows),
                    "views": stats["views"],
                    "clicks": stats["clicks"],
                    "ctr": _ratio(stats["clicks"], stats["views"]),
                    "orders": stats["orders"],
                    "sum": stats["sum"],
                    "spend_rub": stats["sum"],
                    "status": "missing_in_registry",
                    "stats_status": stats_status,
                    "error": self._last_stats_error
                    or "nm_id is present in WB campaigns but absent from registry_upload_config_v2",
                }
            )

        return {
            "contract_name": "sheet_vitrina_v1_ads_skus",
            "generated_at": timestamp,
            "last_refreshed_at": campaigns_payload["fetched_at"],
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "period": {"date_from": date_from, "date_to": date_to},
            "rows": rows + external_rows,
            "meta": {
                "sku_source": "registry_upload_config_v2",
                "enrichment_source": "sheet_vitrina_v1_nomenclature_items",
                "campaign_count": len(campaigns_payload["campaigns"]),
                "external_nm_count": len(external_rows),
                "stats_scope": "campaign_sku_aggregate",
            },
        }

    def build_sku_detail(
        self,
        nm_id: int,
        params: Mapping[str, Any] | None = None,
        *,
        bypass_cache: bool = False,
    ) -> dict[str, Any]:
        date_from, date_to = self._resolve_period(params or {})
        nm_id = _as_positive_int(nm_id, "nm_id")
        sku = self._sku_by_nm_id().get(nm_id) or {
            "nm_id": nm_id,
            "display_name": "",
            "our_sku": "",
            "barcode": "",
            "source": "wb_campaign_only",
        }
        campaigns_payload = self._load_campaigns(bypass_cache=bypass_cache)
        reverse_index = _build_reverse_index(campaigns_payload["campaigns"])
        placement_rows = reverse_index.get(nm_id, [])
        advert_ids = sorted({int(row["advert_id"]) for row in placement_rows})
        stats = self._safe_fetch_stats(advert_ids, date_from=date_from, date_to=date_to)
        rows = [self._enrich_placement_row(row, stats.get((int(row["advert_id"]), nm_id), _empty_stats())) for row in placement_rows]
        return {
            "contract_name": "sheet_vitrina_v1_ads_sku",
            "generated_at": self.timestamp_factory(),
            "last_refreshed_at": campaigns_payload["fetched_at"],
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "period": {"date_from": date_from, "date_to": date_to},
            "sku": sku,
            "rows": rows,
            "meta": {
                "campaign_count": len(advert_ids),
                "placement_count": len(rows),
                "stats_scope": "campaign_sku_aggregate",
                "stats_status": "error" if self._last_stats_error else "ok",
                "stats_error": self._last_stats_error,
                "recommended_cpc_status": "not_available",
            },
        }

    def build_placement_index(self, *, bypass_cache: bool = False) -> dict[int, list[dict[str, Any]]]:
        """Return current campaign/placement identity without per-row min/recommendation calls."""

        return self.build_placement_index_read(
            bypass_cache=bypass_cache
        )["index"]

    def build_placement_index_read(
        self,
        *,
        bypass_cache: bool = False,
    ) -> dict[str, Any]:
        """Return placement identity plus sanitized cache/network call diagnostics."""

        campaigns_payload = self._load_campaigns(bypass_cache=bypass_cache)
        reverse_index = _build_reverse_index(campaigns_payload["campaigns"])
        return {
            "index": {
                int(nm_id): [
                    {
                        **dict(row),
                        "current_bid_rub": _kopecks_to_rub(
                            _optional_int(row.get("current_bid_kopecks"))
                        ),
                        "campaign_fetched_at": campaigns_payload["fetched_at"],
                    }
                    for row in rows
                ]
                for nm_id, rows in reverse_index.items()
            },
            "diagnostics": {
                "source_mode": str(
                    campaigns_payload.get("_read_source_mode") or "unknown"
                ),
                "remote_call_counts": dict(
                    campaigns_payload.get("_remote_call_counts") or {}
                ),
            },
        }

    def read_exact_bid(
        self,
        *,
        nm_id: int,
        advert_id: int,
        placement: str,
    ) -> dict[str, Any]:
        """Read one mutation tuple without stats, minimum or recommendation fanout."""

        normalized_nm_id = _as_positive_int(nm_id, "nm_id")
        normalized_advert_id = _as_positive_int(advert_id, "advert_id")
        normalized_placement = normalize_placement(placement)
        row = self._find_current_row(
            nm_id=normalized_nm_id,
            advert_id=normalized_advert_id,
            placement=normalized_placement,
            bypass_cache=True,
        )
        return {
            "contract_name": "sheet_vitrina_v1_ads_exact_bid_readback",
            "fetched_at": self.timestamp_factory(),
            "nm_id": normalized_nm_id,
            "advert_id": normalized_advert_id,
            "campaign_name": str(row.get("campaign_name") or ""),
            "placement": normalized_placement,
            "payment_type": str(row.get("payment_type") or ""),
            "bid_type": str(row.get("bid_type") or ""),
            "status": _as_int(row.get("status"), default=0),
            "current_bid_kopecks": _optional_int(row.get("current_bid_kopecks")),
            "current_bid_rub": _kopecks_to_rub(
                _optional_int(row.get("current_bid_kopecks"))
            ),
            "read_scope": "exact_advert_placement_without_stats_min_recommendations",
        }

    def preflight_bid_targets(
        self,
        targets: Sequence[Mapping[str, Any]],
        *,
        min_bid_interval_seconds: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
        safety_threshold_policy: AdsBidSafetyThresholdPolicy = AdsBidSafetyThresholdPolicy.STRICT,
    ) -> list[dict[str, Any]]:
        """Fresh, fail-closed bulk guard for Balance-owned bid targets.

        Campaign details are fetched in documented batches. Minimum bids remain
        advert-scoped in WB, so the first allowed burst is used and later calls
        are paced at the documented three-second interval.
        """

        try:
            threshold_policy = AdsBidSafetyThresholdPolicy(safety_threshold_policy)
        except ValueError as exc:
            raise SheetVitrinaV1AdsError(
                "unsupported bid safety threshold policy", http_status=422
            ) from exc
        normalized = [_normalize_bulk_bid_target(item) for item in targets]
        advert_ids = sorted({item["advert_id"] for item in normalized})
        payload: Mapping[str, Any] = {}
        for attempt in range(2):
            try:
                payload = self.source.fetch_adverts(
                    advert_ids,
                    statuses=sorted(SUPPORTED_BID_STATUSES),
                )
                break
            except WbPromotionApiError as exc:
                if exc.http_status != 429 or attempt > 0:
                    raise
                delay = max(float(exc.retry_after_seconds or 0.2), 0.0)
                if delay:
                    sleep(delay)
        raw_adverts = payload.get("adverts") if isinstance(payload, Mapping) else []
        campaigns = {
            int(campaign["advert_id"]): campaign
            for campaign in (
                _parse_campaign(item)
                for item in (raw_adverts or [])
                if isinstance(item, Mapping)
            )
            if int(campaign["advert_id"]) > 0
        }
        grouped: dict[int, list[dict[str, Any]]] = {}
        for item in normalized:
            grouped.setdefault(item["advert_id"], []).append(item)

        results: dict[str, dict[str, Any]] = {}
        eligible_groups: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = []
        for advert_id, group in sorted(grouped.items()):
            campaign = campaigns.get(advert_id)
            if campaign is None:
                for item in group:
                    results[item["target_key"]] = _bulk_preflight_error(
                        item, "campaign_not_found", "Кампания не найдена в WB."
                    )
                continue
            candidate_nm_ids = sorted(
                {
                    int(value)
                    for value in (
                        _optional_int(row.get("nm_id"))
                        for row in campaign.get("nm_settings", [])
                        if isinstance(row, Mapping)
                    )
                    if value is not None
                }
            )
            expected_nm_ids = sorted({item["nm_id"] for item in group})
            if len(candidate_nm_ids) != 1 or candidate_nm_ids != expected_nm_ids:
                for item in group:
                    results[item["target_key"]] = _bulk_preflight_error(
                        item,
                        "campaign_identity_incident",
                        "Кампания должна однозначно принадлежать ровно одному SKU.",
                        candidate_nm_ids=candidate_nm_ids,
                    )
                continue
            status = _as_int(campaign.get("status"), default=0)
            payment_type = str(campaign.get("payment_type") or "").strip().lower()
            bid_type = str(campaign.get("bid_type") or "").strip().lower()
            if status not in SUPPORTED_BID_STATUSES:
                message = "Статус кампании не допускает изменение ставки."
            elif payment_type not in {"cpm", "cpc"}:
                message = "Тип оплаты кампании не поддерживается."
            elif bid_type not in {"manual", "unified"}:
                message = "Тип ставки кампании не поддерживается."
            else:
                message = ""
            if message:
                for item in group:
                    results[item["target_key"]] = _bulk_preflight_error(
                        item, "campaign_not_actionable", message
                    )
                continue
            rows = {
                (int(row["nm_id"]), str(row["placement"])): row
                for row in _campaign_placement_rows(campaign)
            }
            valid: list[dict[str, Any]] = []
            for item in group:
                row = rows.get((item["nm_id"], item["placement"]))
                if row is None:
                    results[item["target_key"]] = _bulk_preflight_error(
                        item,
                        "placement_not_found",
                        "Размещение кампании больше не найдено в WB.",
                    )
                    continue
                current = _optional_int(row.get("current_bid_kopecks"))
                if current is None or current != item["current_bid_minor"]:
                    results[item["target_key"]] = _bulk_preflight_error(
                        item,
                        "stale_current_bid",
                        "Текущая ставка изменилась после расчёта.",
                        observed_bid_minor=current,
                    )
                    continue
                safety_warnings = self.bid_safety_threshold_warnings(
                    old_bid_kopecks=current,
                    new_bid_kopecks=item["requested_bid_minor"],
                )
                if (
                    safety_warnings
                    and threshold_policy is AdsBidSafetyThresholdPolicy.STRICT
                ):
                    results[item["target_key"]] = _bulk_preflight_error(
                        item,
                        "safety_guard",
                        str(safety_warnings[0]["message"]),
                        safety_warnings=safety_warnings,
                    )
                    continue
                valid.append(
                    {
                        **item,
                        "payment_type": payment_type,
                        "safety_threshold_policy": threshold_policy.value,
                        "safety_warnings": safety_warnings,
                    }
                )
            if valid:
                eligible_groups.append((advert_id, campaign, valid))

        minimum_call_index = 0
        burst = 5
        for advert_id, _campaign, group in eligible_groups:
            if minimum_call_index >= burst and min_bid_interval_seconds > 0:
                sleep(float(min_bid_interval_seconds))
            minimum_call_index += 1
            placement_types = sorted(
                {MIN_BID_PLACEMENT[item["placement"]] for item in group}
            )
            try:
                minimum_payload: Mapping[str, Any] = {}
                for attempt in range(2):
                    try:
                        minimum_payload = self.source.fetch_min_bids(
                            advert_id=advert_id,
                            nm_ids=[group[0]["nm_id"]],
                            payment_type=group[0]["payment_type"],
                            placement_types=placement_types,
                        )
                        break
                    except WbPromotionApiError as exc:
                        if exc.http_status != 429 or attempt > 0:
                            raise
                        delay = max(
                            float(
                                exc.retry_after_seconds
                                if exc.retry_after_seconds is not None
                                else min_bid_interval_seconds
                            ),
                            0.0,
                        )
                        if delay:
                            sleep(delay)
            except Exception as exc:
                for item in group:
                    results[item["target_key"]] = _bulk_preflight_error(
                        item,
                        "minimum_bid_unavailable",
                        "Не удалось подтвердить минимальную ставку WB.",
                        detail=str(exc),
                    )
                continue
            for item in group:
                minimum = _extract_min_bid(
                    minimum_payload,
                    nm_id=item["nm_id"],
                    placement=MIN_BID_PLACEMENT[item["placement"]],
                )
                if minimum is None:
                    results[item["target_key"]] = _bulk_preflight_error(
                        item,
                        "minimum_bid_unavailable",
                        "WB не вернул минимальную ставку для размещения.",
                    )
                elif item["requested_bid_minor"] < minimum:
                    results[item["target_key"]] = _bulk_preflight_error(
                        item,
                        "below_minimum_bid",
                        "Рекомендованная ставка ниже текущего минимума WB.",
                        minimum_bid_minor=minimum,
                    )
                else:
                    results[item["target_key"]] = {
                        **item,
                        "ok": True,
                        "payment_type": group[0]["payment_type"],
                        "minimum_bid_minor": minimum,
                        "observed_bid_minor": item["current_bid_minor"],
                    }
        return [results[item["target_key"]] for item in normalized]

    def submit_bid_targets(
        self, targets: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        """Submit one documented WB PATCH with at most fifty atomic targets."""

        normalized = [_normalize_bulk_bid_target(item) for item in targets]
        if not normalized or len(normalized) > 50:
            raise SheetVitrinaV1AdsError(
                "bulk bid PATCH requires 1..50 exact targets", http_status=422
            )
        grouped: dict[int, list[dict[str, int | str]]] = {}
        for item in normalized:
            grouped.setdefault(item["advert_id"], []).append(
                {
                    "nm_id": item["nm_id"],
                    "bid_kopecks": item["requested_bid_minor"],
                    "placement": item["placement"],
                }
            )
        payload = {
            "bids": [
                {"advert_id": advert_id, "nm_bids": grouped[advert_id]}
                for advert_id in sorted(grouped)
            ]
        }
        return self.source.patch_bids(payload)

    def read_bid_targets(
        self, targets: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """One query-only batched readback projected to each exact target."""

        normalized = [_normalize_bulk_bid_target(item) for item in targets]
        advert_ids = sorted({item["advert_id"] for item in normalized})
        payload = self.source.fetch_adverts(
            advert_ids,
            statuses=sorted(SUPPORTED_BID_STATUSES),
        )
        raw_adverts = payload.get("adverts") if isinstance(payload, Mapping) else []
        campaigns = {
            int(campaign["advert_id"]): campaign
            for campaign in (
                _parse_campaign(item)
                for item in (raw_adverts or [])
                if isinstance(item, Mapping)
            )
            if int(campaign["advert_id"]) > 0
        }
        results: list[dict[str, Any]] = []
        for item in normalized:
            campaign = campaigns.get(item["advert_id"])
            candidate_nm_ids = sorted(
                {
                    int(value)
                    for value in (
                        _optional_int(row.get("nm_id"))
                        for row in (campaign or {}).get("nm_settings", [])
                        if isinstance(row, Mapping)
                    )
                    if value is not None
                }
            )
            if candidate_nm_ids != [item["nm_id"]]:
                results.append(
                    _bulk_preflight_error(
                        item,
                        "campaign_identity_incident",
                        "Кампания потеряла однозначную связь с SKU.",
                        candidate_nm_ids=candidate_nm_ids,
                    )
                )
                continue
            row = next(
                (
                    row
                    for row in _campaign_placement_rows(campaign or {})
                    if int(row["nm_id"]) == item["nm_id"]
                    and str(row["placement"]) == item["placement"]
                ),
                None,
            )
            results.append(
                {
                    **item,
                    "ok": row is not None
                    and _optional_int(row.get("current_bid_kopecks")) is not None,
                    "observed_bid_minor": (
                        _optional_int(row.get("current_bid_kopecks"))
                        if row is not None
                        else None
                    ),
                    "error_code": "" if row is not None else "placement_not_found",
                    "message": "" if row is not None else "Размещение не найдено при проверке.",
                }
            )
        return results

    def preview_bid_change(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        nm_id = _as_positive_int(payload.get("nm_id"), "nm_id")
        advert_id = _as_positive_int(payload.get("advert_id"), "advert_id")
        placement = normalize_placement(payload.get("placement"))
        new_bid_kopecks = _parse_bid_rub_to_kopecks(payload.get("requested_bid_rub"))
        row = self._find_current_row(nm_id=nm_id, advert_id=advert_id, placement=placement, bypass_cache=True)
        old_bid_kopecks = _as_nonnegative_int(row.get("current_bid_kopecks"), "current_bid_kopecks")
        warnings: list[str] = []

        status = _as_int(row.get("status"), default=0)
        if status not in SUPPORTED_BID_STATUSES:
            raise SheetVitrinaV1AdsError(f"unsupported campaign status for bid change: {status}", http_status=422)
        payment_type = str(row.get("payment_type") or "").strip().lower()
        if payment_type not in {"cpm", "cpc"}:
            raise SheetVitrinaV1AdsError("unsupported or missing payment_type for bid change", http_status=422)
        bid_type = str(row.get("bid_type") or "").strip().lower()
        if bid_type not in {"manual", "unified"}:
            raise SheetVitrinaV1AdsError("unsupported or missing bid_type for bid change", http_status=422)

        min_bid_kopecks, min_status = self._fetch_min_bid_kopecks(
            advert_id=advert_id,
            nm_id=nm_id,
            payment_type=payment_type,
            placement=placement,
        )
        if min_bid_kopecks is not None and new_bid_kopecks < min_bid_kopecks:
            raise SheetVitrinaV1AdsError(
                "requested_bid_rub is below WB minimum bid",
                http_status=422,
                payload={"min_bid_kopecks": min_bid_kopecks},
            )
        if min_bid_kopecks is None:
            warnings.append("min_bid_unavailable")

        self._validate_safety_thresholds(
            old_bid_kopecks=old_bid_kopecks,
            new_bid_kopecks=new_bid_kopecks,
        )

        preview_id = uuid4().hex
        operation_id = uuid4().hex
        preview = {
            "preview_id": preview_id,
            "operation_id": operation_id,
            "created_at": self.timestamp_factory(),
            "expires_at_epoch": int(time.time()) + self.safety.preview_ttl_seconds,
            "nm_id": nm_id,
            "advert_id": advert_id,
            "campaign_name": row.get("campaign_name") or "",
            "status": status,
            "payment_type": payment_type,
            "bid_type": bid_type,
            "placement": placement,
            "old_bid_kopecks": old_bid_kopecks,
            "old_bid_rub": _kopecks_to_rub(old_bid_kopecks),
            "new_bid_kopecks": new_bid_kopecks,
            "new_bid_rub": _kopecks_to_rub(new_bid_kopecks),
            "delta_kopecks": new_bid_kopecks - old_bid_kopecks,
            "delta_rub": _kopecks_to_rub(new_bid_kopecks - old_bid_kopecks),
            "min_bid_kopecks": min_bid_kopecks,
            "min_bid_rub": _kopecks_to_rub(min_bid_kopecks) if min_bid_kopecks is not None else None,
            "min_bid_status": min_status,
            "warnings": warnings,
            "wb_request_preview": _build_patch_payload(
                advert_id=advert_id,
                nm_id=nm_id,
                placement=placement,
                bid_kopecks=new_bid_kopecks,
            ),
        }
        self._save_preview(preview)
        return {
            "contract_name": "sheet_vitrina_v1_ads_bid_change_preview",
            "status": "ready",
            "preview": preview,
            "confirmation_payload": {
                "preview_id": preview_id,
                "operation_id": operation_id,
                "nm_id": nm_id,
                "advert_id": advert_id,
                "placement": placement,
            },
            "warning": "This changes live WB advertising spend after commit.",
        }

    def commit_bid_change(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        preview_id = _extract_preview_id(payload)
        if not self.safety.write_enabled:
            raise SheetVitrinaV1AdsError(
                "ads bid writes are disabled; set SHEET_VITRINA_ADS_WRITE_ENABLED=1 for controlled live commit",
                http_status=403,
            )
        preview = self._load_preview(preview_id)
        if int(preview.get("expires_at_epoch") or 0) < int(time.time()):
            raise SheetVitrinaV1AdsError("bid-change preview is stale; run preview again", http_status=409)

        current = self._find_current_row(
            nm_id=int(preview["nm_id"]),
            advert_id=int(preview["advert_id"]),
            placement=str(preview["placement"]),
            bypass_cache=True,
        )
        current_bid = _as_nonnegative_int(current.get("current_bid_kopecks"), "current_bid_kopecks")
        if current_bid != int(preview["old_bid_kopecks"]):
            raise SheetVitrinaV1AdsError(
                "current WB bid differs from preview old_bid; run preview again",
                http_status=409,
                payload={"current_bid_kopecks": current_bid},
            )
        min_bid_kopecks, min_status = self._fetch_min_bid_kopecks(
            advert_id=int(preview["advert_id"]),
            nm_id=int(preview["nm_id"]),
            payment_type=str(preview.get("payment_type") or ""),
            placement=str(preview["placement"]),
        )
        if min_bid_kopecks is None:
            raise SheetVitrinaV1AdsError(
                "current WB minimum bid is unavailable; run preview again",
                http_status=409,
                payload={"min_bid_status": min_status},
            )
        if int(preview["new_bid_kopecks"]) < min_bid_kopecks:
            raise SheetVitrinaV1AdsError(
                "requested bid is now below the current WB minimum; run preview again",
                http_status=409,
                payload={"min_bid_kopecks": min_bid_kopecks},
            )
        self._validate_safety_thresholds(
            old_bid_kopecks=current_bid,
            new_bid_kopecks=int(preview["new_bid_kopecks"]),
        )
        request_payload = _build_patch_payload(
            advert_id=int(preview["advert_id"]),
            nm_id=int(preview["nm_id"]),
            placement=str(preview["placement"]),
            bid_kopecks=int(preview["new_bid_kopecks"]),
        )
        prepared = None
        registry_receipt = f"wb-ads-bid:{preview['operation_id']}"
        if self.writer_registry is not None:
            try:
                prepared = self.writer_registry.prepare_bid(
                    source_surface=self.registry_source_surface,
                    actor=actor,
                    native_operation_id=str(preview["operation_id"]),
                    nm_id=int(preview["nm_id"]),
                    advert_id=int(preview["advert_id"]),
                    placement=str(preview["placement"]),
                    before_bid_minor=current_bid,
                    requested_bid_minor=int(preview["new_bid_kopecks"]),
                    requested_at=str(preview.get("created_at") or self.timestamp_factory()),
                    correlation_id=preview_id,
                    native_audit_reference=(
                        "sheet_vitrina_v1_ads/bid_audit.jsonl"
                        f"#operation={preview['operation_id']}"
                    ),
                )
            except InternalWriterRegistryError as exc:
                raise SheetVitrinaV1AdsError(
                    "change registry preparation failed; WB bid patch was not called",
                    http_status=503,
                    payload={"reason": "registry_fail_closed", "detail": str(exc)},
                ) from exc
        try:
            response_payload = self.source.patch_bids(request_payload)
        except WbPromotionApiError as exc:
            if prepared is not None:
                if exc.http_status is None:
                    self.writer_registry.ambiguous(
                        prepared,
                        error_code="wb_submit_transport_unknown",
                        error_message=str(exc),
                        receipt_reference=registry_receipt,
                    )
                else:
                    self.writer_registry.fail_before_submit(
                        prepared,
                        rejected=True,
                        error_code=f"wb_http_{exc.http_status}",
                        error_message=str(exc),
                    )
            raise
        except Exception as exc:
            if prepared is not None:
                self.writer_registry.ambiguous(
                    prepared,
                    error_code="wb_submit_transport_unknown",
                    error_message=str(exc),
                    receipt_reference=registry_receipt,
                )
            raise
        if prepared is not None:
            try:
                self.writer_registry.submitted(
                    prepared,
                    receipt_reference=registry_receipt,
                    receipt_basis={
                        "operation_id": str(preview["operation_id"]),
                        "nm_id": int(preview["nm_id"]),
                        "advert_id": int(preview["advert_id"]),
                        "placement": str(preview["placement"]),
                    },
                )
            except InternalWriterRegistryError as exc:
                try:
                    self.writer_registry.ambiguous(
                        prepared,
                        error_code="registry_post_submit_failure",
                        error_message=str(exc),
                        receipt_reference=registry_receipt,
                    )
                except InternalWriterRegistryError:
                    pass
                raise SheetVitrinaV1AdsError(
                    "WB bid response was received but registry lifecycle is ambiguous",
                    http_status=503,
                    payload={"reason": "registry_post_submit_ambiguous"},
                ) from exc
        audit_event = {
            "event_type": "sheet_vitrina_v1_ads_bid_change_commit",
            "operation_id": str(preview["operation_id"]),
            "actor": actor,
            "timestamp": self.timestamp_factory(),
            "nm_id": int(preview["nm_id"]),
            "advert_id": int(preview["advert_id"]),
            "placement": str(preview["placement"]),
            "payment_type": preview.get("payment_type") or "",
            "bid_type": preview.get("bid_type") or "",
            "old_bid_kopecks": int(preview["old_bid_kopecks"]),
            "old_bid_rub": preview.get("old_bid_rub"),
            "new_bid_kopecks": int(preview["new_bid_kopecks"]),
            "new_bid_rub": preview.get("new_bid_rub"),
            "delta_kopecks": int(preview["delta_kopecks"]),
            "delta_rub": preview.get("delta_rub"),
            "preview_facts": preview,
            "wb_request": request_payload,
            "wb_response": response_payload,
        }
        self._append_audit_event(audit_event)
        self._campaign_cache = None
        registry_readback_status = "not_instrumented"
        if prepared is not None:
            try:
                exact_readback = self.read_exact_bid(
                    nm_id=int(preview["nm_id"]),
                    advert_id=int(preview["advert_id"]),
                    placement=str(preview["placement"]),
                )
                if exact_readback.get("current_bid_kopecks") == int(
                    preview["new_bid_kopecks"]
                ):
                    self.writer_registry.confirm_bid(
                        prepared,
                        confirmed_bid_minor=int(preview["new_bid_kopecks"]),
                        readback_basis={
                            "nm_id": int(preview["nm_id"]),
                            "advert_id": int(preview["advert_id"]),
                            "placement": str(preview["placement"]),
                            "bid_minor": int(preview["new_bid_kopecks"]),
                        },
                        receipt_reference=registry_receipt,
                        native_audit_references=(
                            "sheet_vitrina_v1_ads/bid_audit.jsonl"
                            f"#operation={preview['operation_id']}",
                        ),
                    )
                    registry_readback_status = "confirmed"
                else:
                    self.writer_registry.ambiguous(
                        prepared,
                        error_code="wb_readback_mismatch",
                        error_message="exact bid readback did not match requested value",
                        receipt_reference=registry_receipt,
                    )
                    registry_readback_status = "ambiguous"
            except Exception as exc:
                try:
                    self.writer_registry.ambiguous(
                        prepared,
                        error_code="wb_readback_unavailable",
                        error_message=str(exc),
                        receipt_reference=registry_receipt,
                    )
                except InternalWriterRegistryError:
                    pass
                registry_readback_status = "ambiguous"
        return {
            "contract_name": "sheet_vitrina_v1_ads_bid_change_commit",
            "status": "pending_refresh",
            "operation_id": str(preview["operation_id"]),
            "registry_operation_id": prepared.operation_id if prepared is not None else "",
            "registry_receipt_reference": registry_receipt if prepared is not None else "",
            "registry_readback_status": registry_readback_status,
            "audit_event": audit_event,
            "delayed_refresh_after_seconds": 30,
            "wb_response": response_payload,
        }

    def reconcile_registry_bid(
        self,
        *,
        receipt_reference: str,
        exact_readback: Mapping[str, Any],
    ) -> str:
        """Late-link one exact SKU readback to the already submitted operation."""

        if self.writer_registry is None:
            return "not_instrumented"
        prepared = self.writer_registry.find_by_receipt(receipt_reference)
        stored = self.writer_registry.read_by_receipt(receipt_reference)
        if prepared is None or stored is None or len(stored["items"]) != 1:
            raise SheetVitrinaV1AdsError(
                "registry bid operation is unavailable for reconciliation",
                http_status=503,
            )
        item = stored["items"][0]
        expected = int(item["requested_value_integer"])
        observed = _optional_int(exact_readback.get("current_bid_kopecks"))
        if observed != expected:
            self.writer_registry.ambiguous(
                prepared,
                error_code="wb_readback_mismatch",
                error_message="late exact bid readback did not match requested value",
                receipt_reference=receipt_reference,
            )
            return "ambiguous"
        self.writer_registry.confirm_bid(
            prepared,
            confirmed_bid_minor=observed,
            readback_basis={
                "nm_id": int(item["nm_id"]),
                "advert_id": int(item["advert_id"]),
                "placement": str(item["placement"]),
                "bid_minor": observed,
            },
            receipt_reference=receipt_reference,
            native_audit_references=(
                "sheet_vitrina_v1_ads/bid_audit.jsonl"
                f"#operation={prepared.native_operation_id}",
            ),
        )
        return "confirmed"

    def _load_campaigns(self, *, bypass_cache: bool = False) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        if not bypass_cache and self._campaign_cache:
            age = now_monotonic - float(self._campaign_cache.get("cached_at_monotonic") or 0)
            if age <= self.cache_ttl_seconds:
                cached = dict(self._campaign_cache["payload"])
                cached["_read_source_mode"] = "cache"
                cached["_remote_call_counts"] = {
                    "campaign_count": 0,
                    "adverts_batch": 0,
                }
                return cached
        count_payload = self.source.fetch_campaign_count()
        advert_ids = extract_advert_ids_from_count(count_payload)
        adverts_payload = self.source.fetch_adverts(advert_ids, statuses=sorted(SUPPORTED_BID_STATUSES))
        raw_adverts = adverts_payload.get("adverts") if isinstance(adverts_payload, Mapping) else []
        campaigns = [_parse_campaign(advert) for advert in raw_adverts if isinstance(advert, Mapping)]
        campaigns = [campaign for campaign in campaigns if campaign["advert_id"] > 0]
        payload = {
            "fetched_at": self.timestamp_factory(),
            "advert_ids": advert_ids,
            "campaigns": campaigns,
            "_read_source_mode": "network",
            "_remote_call_counts": {
                "campaign_count": 1,
                "adverts_batch": 1,
            },
        }
        self._campaign_cache = {"cached_at_monotonic": now_monotonic, "payload": payload}
        return payload

    def _find_current_row(self, *, nm_id: int, advert_id: int, placement: str, bypass_cache: bool) -> dict[str, Any]:
        campaigns: list[dict[str, Any]] = []
        if not bypass_cache and self._campaign_cache:
            cached_payload = self._campaign_cache.get("payload")
            if isinstance(cached_payload, Mapping):
                campaigns = [
                    dict(item)
                    for item in cached_payload.get("campaigns", [])
                    if isinstance(item, Mapping)
                    and int(item.get("advert_id") or 0) == advert_id
                ]
        if not campaigns:
            adverts_payload = self.source.fetch_adverts(
                [advert_id],
                statuses=sorted(SUPPORTED_BID_STATUSES),
            )
            raw_adverts = (
                adverts_payload.get("adverts")
                if isinstance(adverts_payload, Mapping)
                else []
            )
            campaigns = [
                _parse_campaign(advert)
                for advert in raw_adverts or []
                if isinstance(advert, Mapping)
            ]
        for campaign in campaigns:
            if int(campaign["advert_id"]) != advert_id:
                continue
            for row in _campaign_placement_rows(campaign):
                if int(row["nm_id"]) == nm_id and str(row["placement"]) == placement:
                    return row
            raise SheetVitrinaV1AdsError("nm_id or placement is not present in advert_id", http_status=422)
        raise SheetVitrinaV1AdsError("advert_id not found in WB campaigns", http_status=404)

    def _enrich_placement_row(self, row: Mapping[str, Any], stats: Mapping[str, Any]) -> dict[str, Any]:
        payment_type = str(row.get("payment_type") or "").strip().lower()
        min_bid_kopecks, min_status = self._fetch_min_bid_kopecks(
            advert_id=int(row["advert_id"]),
            nm_id=int(row["nm_id"]),
            payment_type=payment_type,
            placement=str(row["placement"]),
        )
        recommended = self._fetch_recommended_bid(
            advert_id=int(row["advert_id"]),
            nm_id=int(row["nm_id"]),
            payment_type=payment_type,
        )
        return {
            **dict(row),
            "current_bid_rub": _kopecks_to_rub(_optional_int(row.get("current_bid_kopecks"))),
            "min_bid_kopecks": min_bid_kopecks,
            "min_bid_rub": _kopecks_to_rub(min_bid_kopecks) if min_bid_kopecks is not None else None,
            "min_bid_status": min_status,
            "recommended_bid_kopecks": recommended["bid_kopecks"],
            "recommended_bid_rub": _kopecks_to_rub(recommended["bid_kopecks"])
            if recommended["bid_kopecks"] is not None
            else None,
            "recommended_bid_status": recommended["status"],
            "views": stats["views"],
            "clicks": stats["clicks"],
            "ctr": _ratio(stats["clicks"], stats["views"]),
            "cpc": _ratio(stats["sum"], stats["clicks"]),
            "cpm": (stats["sum"] / stats["views"] * 1000.0) if stats["views"] else 0.0,
            "cr": _ratio(stats["orders"], stats["clicks"]),
            "orders": stats["orders"],
            "sum": stats["sum"],
            "spend_rub": stats["sum"],
            "stats_scope": "campaign_sku_aggregate",
        }

    def _fetch_min_bid_kopecks(
        self,
        *,
        advert_id: int,
        nm_id: int,
        payment_type: str,
        placement: str,
    ) -> tuple[int | None, str]:
        placement_type = MIN_BID_PLACEMENT.get(placement)
        if not placement_type:
            return None, "unsupported_placement"
        try:
            payload = self.source.fetch_min_bids(
                advert_id=advert_id,
                nm_ids=[nm_id],
                payment_type=payment_type,
                placement_types=[placement_type],
            )
        except Exception as exc:  # pragma: no cover - network fallback
            return None, f"error: {exc}"
        bid = _extract_min_bid(payload, nm_id=nm_id, placement=placement_type)
        return (bid, "ok") if bid is not None else (None, "not_available")

    def _fetch_recommended_bid(self, *, advert_id: int, nm_id: int, payment_type: str) -> dict[str, Any]:
        if payment_type != "cpm":
            return {"bid_kopecks": None, "status": "not_available"}
        try:
            payload = self.source.fetch_recommendations(advert_id=advert_id, nm_id=nm_id)
        except Exception as exc:  # pragma: no cover - network fallback
            return {"bid_kopecks": None, "status": f"error: {exc}"}
        bid = _extract_recommended_bid(payload)
        return {"bid_kopecks": bid, "status": "ok" if bid is not None else "not_available"}

    def _safe_fetch_stats(
        self,
        advert_ids: Sequence[int],
        *,
        date_from: str,
        date_to: str,
    ) -> dict[tuple[int, int], dict[str, float]]:
        if not advert_ids:
            self._last_stats_error = ""
            return {}
        try:
            payload = self.source.fetch_fullstats(advert_ids, begin_date=date_from, end_date=date_to)
        except Exception as exc:
            self._last_stats_error = str(exc)
            return {}
        self._last_stats_error = ""
        return _parse_fullstats(payload)

    def _load_sku_universe(self) -> list[dict[str, Any]]:
        try:
            current_state = self.runtime.load_current_state()
        except Exception:
            return []
        enrich_by_nm: dict[int, Mapping[str, Any]] = {}
        try:
            for item in self.runtime.list_nomenclature_items(active_only=True):
                nm_id = _optional_int(item.get("nm_id"))
                if nm_id is not None:
                    enrich_by_nm[nm_id] = item
        except Exception:
            enrich_by_nm = {}
        rows: list[dict[str, Any]] = []
        for item in current_state.config_v2:
            if not bool(getattr(item, "enabled", False)):
                continue
            nm_id = int(getattr(item, "nm_id"))
            enrichment = enrich_by_nm.get(nm_id, {})
            display_name = str(getattr(item, "display_name", "") or "").strip()
            rows.append(
                {
                    "nm_id": nm_id,
                    "display_name": display_name or str(enrichment.get("nomenclature_name") or ""),
                    "our_sku": str(enrichment.get("our_sku") or ""),
                    "barcode": str(enrichment.get("barcode") or ""),
                    "source": "registry_upload_config_v2",
                }
            )
        return rows

    def _sku_by_nm_id(self) -> dict[int, dict[str, Any]]:
        return {int(row["nm_id"]): row for row in self._load_sku_universe()}

    def _resolve_period(self, params: Mapping[str, Any]) -> tuple[str, str]:
        date_to = _single_param(params.get("date_to")) or self.now_factory().date().isoformat()
        date_from = _single_param(params.get("date_from")) or (
            _parse_date(date_to) - timedelta(days=6)
        ).isoformat()
        _parse_date(date_from)
        _parse_date(date_to)
        if date_from > date_to:
            raise SheetVitrinaV1AdsError("date_from must be <= date_to", http_status=422)
        return date_from, date_to

    def _save_preview(self, preview: Mapping[str, Any]) -> None:
        self._preview_dir.mkdir(parents=True, exist_ok=True)
        path = self._preview_dir / f"{preview['preview_id']}.json"
        path.write_text(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _load_preview(self, preview_id: str) -> dict[str, Any]:
        normalized = str(preview_id or "").strip()
        if not normalized or "/" in normalized or "." in normalized:
            raise SheetVitrinaV1AdsError("invalid preview_id", http_status=400)
        path = self._preview_dir / f"{normalized}.json"
        if not path.exists():
            raise SheetVitrinaV1AdsError("preview_id not found", http_status=404)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SheetVitrinaV1AdsError("stored preview is invalid", http_status=500)
        return payload

    def _append_audit_event(self, event: Mapping[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def bid_safety_threshold_warnings(
        self, *, old_bid_kopecks: int, new_bid_kopecks: int
    ) -> list[dict[str, Any]]:
        """Return seller-threshold warnings without weakening WB/API validation."""

        old_bid = _as_nonnegative_int(old_bid_kopecks, "old_bid_kopecks")
        new_bid = _as_positive_int(new_bid_kopecks, "new_bid_kopecks")
        warnings: list[dict[str, Any]] = []
        if new_bid > self.safety.absolute_max_bid_kopecks:
            warnings.append(
                {
                    "code": "absolute_max_bid",
                    "message": "requested_bid_rub exceeds absolute safety threshold",
                    "current_bid_minor": old_bid,
                    "requested_bid_minor": new_bid,
                    "threshold_minor": self.safety.absolute_max_bid_kopecks,
                }
            )
        increase = new_bid - old_bid
        if increase <= 0:
            return warnings
        if increase > self.safety.max_absolute_increase_kopecks:
            warnings.append(
                {
                    "code": "max_absolute_increase",
                    "message": "requested_bid_rub exceeds absolute increase threshold",
                    "current_bid_minor": old_bid,
                    "requested_bid_minor": new_bid,
                    "delta_minor": increase,
                    "threshold_minor": self.safety.max_absolute_increase_kopecks,
                }
            )
        if old_bid > 0:
            pct = Decimal(increase * 100) / Decimal(old_bid)
            if pct > self.safety.max_percent_increase:
                warnings.append(
                    {
                        "code": "max_percent_increase",
                        "message": "requested_bid_rub exceeds percent increase threshold",
                        "current_bid_minor": old_bid,
                        "requested_bid_minor": new_bid,
                        "delta_percent": float(pct),
                        "threshold_percent": float(self.safety.max_percent_increase),
                    }
                )
        return warnings

    def _validate_safety_thresholds(self, *, old_bid_kopecks: int, new_bid_kopecks: int) -> None:
        warnings = self.bid_safety_threshold_warnings(
            old_bid_kopecks=old_bid_kopecks,
            new_bid_kopecks=new_bid_kopecks,
        )
        if warnings:
            raise SheetVitrinaV1AdsError(
                str(warnings[0]["message"]), http_status=422
            )


def normalize_placement(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "combined": "combined",
        "search": "search",
        "recommendation": "recommendations",
        "recommendations": "recommendations",
        "reco": "recommendations",
    }
    placement = aliases.get(raw, "")
    if not placement:
        raise SheetVitrinaV1AdsError("unsupported placement", http_status=422)
    return placement


def _load_safety_config() -> AdsSafetyConfig:
    return AdsSafetyConfig(
        write_enabled=os.environ.get("SHEET_VITRINA_ADS_WRITE_ENABLED", "").strip().lower() in {"1", "true", "yes"},
        absolute_max_bid_kopecks=_parse_env_rub("SHEET_VITRINA_ADS_MAX_BID_RUB", "1000"),
        max_percent_increase=Decimal(os.environ.get("SHEET_VITRINA_ADS_MAX_PERCENT_INCREASE", "50").strip() or "50"),
        max_absolute_increase_kopecks=_parse_env_rub("SHEET_VITRINA_ADS_MAX_ABSOLUTE_INCREASE_RUB", "100"),
        preview_ttl_seconds=int(os.environ.get("SHEET_VITRINA_ADS_PREVIEW_TTL_SECONDS", "180") or "180"),
    )


def _parse_campaign(advert: Mapping[str, Any]) -> dict[str, Any]:
    settings = advert.get("settings") if isinstance(advert.get("settings"), Mapping) else {}
    advert_id = _optional_int(advert.get("id")) or _optional_int(advert.get("advertId")) or 0
    payment_type = str(settings.get("payment_type") or advert.get("payment_type") or "").strip().lower()
    placements = settings.get("placements") if isinstance(settings.get("placements"), Mapping) else {}
    return {
        "advert_id": advert_id,
        "campaign_name": str(settings.get("name") or advert.get("name") or ""),
        "status": _as_int(advert.get("status"), default=0),
        "payment_type": payment_type,
        "bid_type": str(advert.get("bid_type") or advert.get("bidType") or "").strip().lower(),
        "placements": _normalize_placements_map(placements),
        "nm_settings": [item for item in advert.get("nm_settings", []) if isinstance(item, Mapping)]
        if isinstance(advert.get("nm_settings"), list)
        else [],
        "raw_timestamps": advert.get("timestamps") if isinstance(advert.get("timestamps"), Mapping) else {},
    }


def _build_reverse_index(campaigns: Sequence[Mapping[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    index: dict[int, list[dict[str, Any]]] = {}
    for campaign in campaigns:
        for row in _campaign_placement_rows(campaign):
            index.setdefault(int(row["nm_id"]), []).append(row)
    return index


def _campaign_placement_rows(campaign: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    placements = campaign.get("placements") if isinstance(campaign.get("placements"), Mapping) else {}
    allowed = {placement for placement, enabled in placements.items() if enabled and placement in SUPPORTED_PLACEMENTS}
    if not allowed:
        allowed = {"combined", "search", "recommendations"}
    for nm_setting in campaign.get("nm_settings", []):
        if not isinstance(nm_setting, Mapping):
            continue
        nm_id = _optional_int(nm_setting.get("nm_id"))
        if nm_id is None:
            continue
        bids_kopecks = nm_setting.get("bids_kopecks")
        if not isinstance(bids_kopecks, Mapping):
            bids = nm_setting.get("bids")
            if isinstance(bids, Mapping):
                bids_kopecks = {key: _rub_to_kopecks_float(value) for key, value in bids.items()}
            else:
                bids_kopecks = {}
        normalized_bids = _normalize_bid_map(bids_kopecks)
        for placement in sorted(allowed):
            bid_kopecks = normalized_bids.get(placement)
            if bid_kopecks is None and placement == "combined":
                bid_kopecks = normalized_bids.get("search") or normalized_bids.get("recommendations")
            if bid_kopecks is None:
                continue
            rows.append(
                {
                    "nm_id": nm_id,
                    "advert_id": int(campaign["advert_id"]),
                    "campaign_name": campaign.get("campaign_name") or "",
                    "status": campaign.get("status"),
                    "payment_type": campaign.get("payment_type") or "",
                    "bid_type": campaign.get("bid_type") or "",
                    "placement": placement,
                    "current_bid_kopecks": int(bid_kopecks),
                }
            )
    return rows


def _normalize_placements_map(placements: Mapping[str, Any]) -> dict[str, bool]:
    normalized: dict[str, bool] = {}
    for key, value in placements.items():
        try:
            placement = normalize_placement(key)
        except SheetVitrinaV1AdsError:
            continue
        normalized[placement] = bool(value)
    return normalized


def _normalize_bid_map(bids: Mapping[str, Any]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for key, value in bids.items():
        try:
            placement = normalize_placement(key)
        except SheetVitrinaV1AdsError:
            continue
        bid = _optional_int(value)
        if bid is not None:
            normalized[placement] = bid
    return normalized


def _parse_fullstats(payload: Any) -> dict[tuple[int, int], dict[str, float]]:
    result: dict[tuple[int, int], dict[str, float]] = {}
    items = payload if isinstance(payload, list) else []
    for advert in items:
        if not isinstance(advert, Mapping):
            continue
        advert_id = _optional_int(advert.get("advertId")) or _optional_int(advert.get("advert_id")) or _optional_int(advert.get("id")) or 0
        for day in advert.get("days", []) if isinstance(advert.get("days"), list) else []:
            if not isinstance(day, Mapping):
                continue
            for app in day.get("apps", []) if isinstance(day.get("apps"), list) else []:
                if not isinstance(app, Mapping):
                    continue
                for nm in app.get("nms", []) if isinstance(app.get("nms"), list) else []:
                    if not isinstance(nm, Mapping):
                        continue
                    nm_id = _optional_int(nm.get("nmId")) or _optional_int(nm.get("nm_id"))
                    if nm_id is None:
                        continue
                    for key in ((advert_id, nm_id), (0, nm_id)):
                        stats = result.setdefault(key, _empty_stats())
                        stats["views"] += _float(nm.get("views"))
                        stats["clicks"] += _float(nm.get("clicks"))
                        stats["orders"] += _float(nm.get("orders"))
                        stats["sum"] += _float(nm.get("sum"))
    return result


def _extract_min_bid(payload: Mapping[str, Any], *, nm_id: int, placement: str) -> int | None:
    bids = payload.get("bids") if isinstance(payload, Mapping) else None
    if not isinstance(bids, list):
        return None
    wanted = normalize_placement(placement)
    for item in bids:
        if not isinstance(item, Mapping):
            continue
        if _optional_int(item.get("nm_id")) != nm_id:
            continue
        for bid in item.get("bids", []) if isinstance(item.get("bids"), list) else []:
            if not isinstance(bid, Mapping):
                continue
            try:
                bid_placement = normalize_placement(bid.get("type"))
            except SheetVitrinaV1AdsError:
                continue
            if bid_placement == wanted:
                return (
                    _optional_int(bid.get("value"))
                    or _optional_int(bid.get("bid_kopecks"))
                    or _optional_int(bid.get("bidKopecks"))
                    or _optional_int(bid.get("min_bid_kopecks"))
                )
    return None


def _extract_recommended_bid(payload: Mapping[str, Any]) -> int | None:
    base = payload.get("base") if isinstance(payload.get("base"), Mapping) else {}
    for key in ("competitiveBid", "leadersBid", "top2"):
        node = base.get(key)
        if isinstance(node, Mapping):
            bid = _optional_int(node.get("bidKopecks") or node.get("bid_kopecks"))
            if bid and bid > 0:
                return bid
    return None


def _build_patch_payload(*, advert_id: int, nm_id: int, placement: str, bid_kopecks: int) -> dict[str, Any]:
    return {
        "bids": [
            {
                "advert_id": int(advert_id),
                "nm_bids": [
                    {
                        "nm_id": int(nm_id),
                        "bid_kopecks": int(bid_kopecks),
                        "placement": normalize_placement(placement),
                    }
                ],
            }
        ]
    }


def _normalize_bulk_bid_target(value: Mapping[str, Any]) -> dict[str, Any]:
    target_key = str(value.get("target_key") or "").strip()
    if not target_key:
        raise SheetVitrinaV1AdsError("bulk bid target_key is required", http_status=422)
    return {
        **dict(value),
        "target_key": target_key,
        "nm_id": _as_positive_int(value.get("nm_id"), "nm_id"),
        "advert_id": _as_positive_int(value.get("advert_id"), "advert_id"),
        "placement": normalize_placement(value.get("placement")),
        "current_bid_minor": _as_nonnegative_int(
            value.get("current_bid_minor"), "current_bid_minor"
        ),
        "requested_bid_minor": _as_positive_int(
            value.get("requested_bid_minor"), "requested_bid_minor"
        ),
    }


def _bulk_preflight_error(
    item: Mapping[str, Any],
    error_code: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        **dict(item),
        "ok": False,
        "error_code": str(error_code),
        "message": str(message),
        **details,
    }


def _extract_preview_id(payload: Mapping[str, Any]) -> str:
    if isinstance(payload.get("confirmation_payload"), Mapping):
        value = payload["confirmation_payload"].get("preview_id")
    else:
        value = payload.get("preview_id")
    preview_id = str(value or "").strip()
    if not preview_id:
        raise SheetVitrinaV1AdsError("preview_id is required", http_status=400)
    return preview_id


def _parse_bid_rub_to_kopecks(value: Any) -> int:
    if value is None or isinstance(value, bool):
        raise SheetVitrinaV1AdsError("requested_bid_rub is required", http_status=400)
    raw = str(value).strip().replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise SheetVitrinaV1AdsError("requested_bid_rub must be numeric", http_status=400) from exc
    if amount < 0:
        raise SheetVitrinaV1AdsError("requested_bid_rub must be >= 0", http_status=400)
    if amount != amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
        raise SheetVitrinaV1AdsError("requested_bid_rub must have at most 2 decimal places", http_status=400)
    kopecks = int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return kopecks


def _parse_env_rub(name: str, default_value: str) -> int:
    raw = os.environ.get(name, default_value).strip() or default_value
    return _parse_bid_rub_to_kopecks(raw)


def _parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SheetVitrinaV1AdsError(f"invalid date: {value}", http_status=422) from exc


def _single_param(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def _as_positive_int(value: Any, field: str) -> int:
    result = _optional_int(value)
    if result is None or result <= 0:
        raise SheetVitrinaV1AdsError(f"{field} must be a positive integer", http_status=400)
    return result


def _as_nonnegative_int(value: Any, field: str) -> int:
    result = _optional_int(value)
    if result is None or result < 0:
        raise SheetVitrinaV1AdsError(f"{field} must be a non-negative integer", http_status=422)
    return result


def _as_int(value: Any, *, default: int) -> int:
    result = _optional_int(value)
    return result if result is not None else default


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rub_to_kopecks_float(value: Any) -> int | None:
    try:
        return int((Decimal(str(value)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _kopecks_to_rub(value: int | None) -> float | None:
    if value is None:
        return None
    return float((Decimal(int(value)) / Decimal("100")).quantize(Decimal("0.01")))


def _empty_stats() -> dict[str, float]:
    return {"views": 0.0, "clicks": 0.0, "orders": 0.0, "sum": 0.0}


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0
