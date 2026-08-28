"""Strict read-only Prices + Ads acquisition for a future registry baseline.

The module does not persist checkpoints, observations, facts or incidents.  It
only returns deterministic, sanitized manifests built from official seller
read endpoints.  Existing Prices and Ads writer paths are not called or
instrumented here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import re
import time
from typing import Any, Callable, Mapping, Sequence

from packages.adapters.wb_prices_management import (
    HttpBackedWbPricesManagementSource,
    WbPricesManagementSource,
)
from packages.adapters.wb_promotion import (
    HttpBackedWbPromotionSource,
    WbPromotionSource,
)
from packages.application.change_registry import MAPPING_VERSION, canonical_digest, canonical_json


CONTRACT_NAME = "wb_change_registry_source_acquisition"
CONTRACT_VERSION = 1
PRICES_SOURCE = "wb_prices_goods_filter_get"
ADS_COUNT_SOURCE = "wb_promotion_count"
ADS_DETAIL_SOURCE = "wb_promotion_adverts_v2"
PRICES_ENDPOINT = "/api/v2/list/goods/filter"
ADS_COUNT_ENDPOINT = "/adv/v1/promotion/count"
ADS_DETAIL_ENDPOINT = "/api/advert/v2/adverts"
PRICE_PAGE_LIMIT = 1000
ADS_DETAIL_BATCH_LIMIT = 50
PRICES_MIN_INTERVAL_SECONDS = 0.6
ADS_DETAIL_MIN_INTERVAL_SECONDS = 3.0
LEGACY_COUNT_ONLY_TYPE = 6
LEGACY_COUNT_ONLY_STATUS = 7
PLACEMENTS = ("combined", "recommendations", "search")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,119}")


class SourceAcquisitionError(ValueError):
    """The requested seller scope or acquisition configuration is invalid."""


class _SourceFailure(RuntimeError):
    def __init__(self, evidence: Mapping[str, Any]) -> None:
        super().__init__("official source acquisition failed")
        self.evidence = dict(evidence)


@dataclass(frozen=True)
class SourceAcquisitionConfig:
    price_page_limit: int = PRICE_PAGE_LIMIT
    max_price_pages: int = 10_000
    ads_detail_batch_limit: int = ADS_DETAIL_BATCH_LIMIT
    prices_min_interval_seconds: float = PRICES_MIN_INTERVAL_SECONDS
    ads_detail_min_interval_seconds: float = ADS_DETAIL_MIN_INTERVAL_SECONDS
    max_rate_limit_retries: int = 2
    max_retry_after_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.price_page_limit != PRICE_PAGE_LIMIT:
            raise SourceAcquisitionError("Prices completeness requires limit=1000")
        if self.max_price_pages < 1 or self.max_price_pages > 100_000:
            raise SourceAcquisitionError("max_price_pages is outside the bounded range")
        if not 1 <= self.ads_detail_batch_limit <= ADS_DETAIL_BATCH_LIMIT:
            raise SourceAcquisitionError("Ads detail batches must contain at most 50 IDs")
        if self.prices_min_interval_seconds < PRICES_MIN_INTERVAL_SECONDS:
            raise SourceAcquisitionError("Prices pacing cannot be faster than 600 ms")
        if self.ads_detail_min_interval_seconds < ADS_DETAIL_MIN_INTERVAL_SECONDS:
            raise SourceAcquisitionError("Ads detail pacing cannot be faster than 3 seconds")
        if not 0 <= self.max_rate_limit_retries <= 5:
            raise SourceAcquisitionError("rate-limit retry budget is invalid")
        if not 0 <= self.max_retry_after_seconds <= 900:
            raise SourceAcquisitionError("Retry-After bound is invalid")


class _Pacer:
    """Conservative deterministic interval pacing for one endpoint family."""

    def __init__(self, interval_seconds: float, sleep_fn: Callable[[float], None]) -> None:
        self.interval_seconds = float(interval_seconds)
        self.sleep_fn = sleep_fn
        self.calls = 0

    def before_call(self) -> None:
        if self.calls:
            self.sleep_fn(self.interval_seconds)
        self.calls += 1


class ChangeRegistrySourceAcquirer:
    """Acquire one joint seller/account Prices + Ads read-only manifest."""

    def __init__(
        self,
        *,
        seller_id: str,
        account_scope: str,
        prices_source: WbPricesManagementSource | None = None,
        ads_source: WbPromotionSource | None = None,
        now_fn: Callable[[], datetime | str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        config: SourceAcquisitionConfig | None = None,
    ) -> None:
        self.seller_id = _identity(seller_id, "seller_id")
        self.account_scope = _identity(account_scope, "account_scope")
        self.prices_source = prices_source or HttpBackedWbPricesManagementSource()
        self.ads_source = ads_source or HttpBackedWbPromotionSource()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.sleep_fn = sleep_fn or time.sleep
        self.config = config or SourceAcquisitionConfig()

    def acquire(self) -> dict[str, Any]:
        started_at = self._now()
        prices = self._acquire_prices()
        ads = self._acquire_ads()
        completed_at = self._now()
        complete = (
            prices["completeness_status"] == "complete"
            and ads["completeness_status"] == "complete"
        )
        payload = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mapping_version": MAPPING_VERSION,
            "seller": {
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
            },
            "interval": {"started_at": started_at, "completed_at": completed_at},
            "completeness_status": "complete" if complete else "partial",
            "joint_complete": complete,
            "sources": {"prices": prices, "ads": ads},
            "counts": {
                "price_goods": int(prices["counts"]["goods"]),
                "ads_manifest_campaigns": int(ads["counts"]["manifest_campaigns"]),
                "ads_detail_campaigns": int(ads["counts"]["detail_campaigns"]),
                "identity_incidents": int(ads["counts"]["identity_incidents"]),
            },
            "persistence": {
                "registry_rows_written": 0,
                "checkpoints_written": 0,
                "facts_written": 0,
                "identity_incidents_written": 0,
            },
            "wb_mutation_calls": {"post": 0, "patch": 0},
        }
        return _with_digest(payload)

    def _acquire_prices(self) -> dict[str, Any]:
        started_at = self._now()
        pages: list[dict[str, Any]] = []
        goods: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        retry_events: list[dict[str, Any]] = []
        terminal_empty_page = False
        offset = 0
        pacer = _Pacer(self.config.prices_min_interval_seconds, self.sleep_fn)

        for page_number in range(1, self.config.max_price_pages + 1):
            try:
                payload, retries = self._call_with_rate_limit_retry(
                    lambda current_offset=offset: self.prices_source.fetch_goods(
                        limit=self.config.price_page_limit,
                        offset=current_offset,
                        filter_nm_id=None,
                    ),
                    endpoint=PRICES_ENDPOINT,
                    pacer=pacer,
                )
            except _SourceFailure as exc:
                issues.append(dict(exc.evidence))
                break
            retry_events.extend(retries)
            if not isinstance(payload, Mapping):
                issues.append(_schema_issue(PRICES_SOURCE, "response_not_object"))
                break
            if bool(payload.get("error")):
                issues.append(
                    _schema_issue(
                        PRICES_SOURCE,
                        "upstream_error_payload",
                        evidence_digest=_digest_any(payload),
                    )
                )
                break
            data = payload.get("data")
            raw_goods = data.get("listGoods") if isinstance(data, Mapping) else None
            if not isinstance(raw_goods, list):
                issues.append(
                    _schema_issue(
                        PRICES_SOURCE,
                        "list_goods_not_array",
                        evidence_digest=_digest_any(payload),
                    )
                )
                break
            page_evidence = {
                "page": page_number,
                "method": "GET",
                "endpoint": PRICES_ENDPOINT,
                "limit": self.config.price_page_limit,
                "offset": offset,
                "row_count": len(raw_goods),
                "response_digest": _digest_any(payload),
                "rate_limit_retry_count": len(retries),
            }
            pages.append(page_evidence)
            if not raw_goods:
                terminal_empty_page = True
                break
            for raw_good in raw_goods:
                if not isinstance(raw_good, Mapping):
                    issues.append(_schema_issue(PRICES_SOURCE, "good_not_object"))
                    continue
                record, record_issues = _normalize_price_good(raw_good)
                issues.extend(record_issues)
                if record is not None:
                    goods.append(record)
            offset += self.config.price_page_limit
        else:
            issues.append(_schema_issue(PRICES_SOURCE, "pagination_bound_exhausted"))

        goods.sort(key=lambda item: (int(item["nm_id"]), canonical_json(item)))
        nm_ids = [int(item["nm_id"]) for item in goods]
        duplicates = sorted(
            value for value, count in Counter(nm_ids).items() if count > 1
        )
        if duplicates:
            issues.append(
                _schema_issue(
                    PRICES_SOURCE,
                    "duplicate_nm_id",
                    identities=duplicates,
                )
            )
        if not terminal_empty_page:
            issues.append(_schema_issue(PRICES_SOURCE, "terminal_empty_page_not_proven"))

        completed_at = self._now()
        payload = {
            "contract_name": "wb_change_registry_prices_acquisition",
            "contract_version": 1,
            "seller_id": self.seller_id,
            "account_scope": self.account_scope,
            "source": {
                "source_id": PRICES_SOURCE,
                "method": "GET",
                "endpoint": PRICES_ENDPOINT,
                "official_limit": {
                    "period_seconds": 6,
                    "requests": 10,
                    "interval_ms": 600,
                    "burst": 5,
                },
            },
            "interval": {"started_at": started_at, "completed_at": completed_at},
            "completeness_status": "complete" if not issues else "partial",
            "pagination": {
                "limit": self.config.price_page_limit,
                "terminal_empty_page": terminal_empty_page,
                "pages": pages,
            },
            "goods": goods,
            "counts": {
                "goods": len(goods),
                "pages": len(pages),
                "issues": len(issues),
                "rate_limit_retries": len(retry_events),
            },
            "retry_evidence": retry_events,
            "issues": sorted(issues, key=canonical_json),
        }
        return _with_digest(payload)

    def _acquire_ads(self) -> dict[str, Any]:
        started_at = self._now()
        issues: list[dict[str, Any]] = []
        retry_events: list[dict[str, Any]] = []
        count_groups: list[dict[str, Any]] = []
        count_entries: dict[int, dict[str, Any]] = {}
        expected_all: int | None = None

        try:
            count_payload, retries = self._call_with_rate_limit_retry(
                self.ads_source.fetch_campaign_count,
                endpoint=ADS_COUNT_ENDPOINT,
                pacer=None,
            )
            retry_events.extend(retries)
        except _SourceFailure as exc:
            issues.append(dict(exc.evidence))
            count_payload = None

        if isinstance(count_payload, Mapping):
            parsed = _parse_ads_count_manifest(count_payload)
            count_groups = parsed["groups"]
            count_entries = parsed["entries"]
            expected_all = parsed["expected_all"]
            issues.extend(parsed["issues"])
            count_response_digest = _digest_any(count_payload)
        else:
            if count_payload is not None:
                issues.append(_schema_issue(ADS_COUNT_SOURCE, "response_not_object"))
            count_response_digest = _digest_any(count_payload)

        detail_ids = sorted(
            advert_id
            for advert_id, entry in count_entries.items()
            if not bool(entry["legacy_count_only"])
        )
        legacy_ids = sorted(
            advert_id
            for advert_id, entry in count_entries.items()
            if bool(entry["legacy_count_only"])
        )
        detail_batches: list[dict[str, Any]] = []
        details: dict[int, Mapping[str, Any]] = {}
        pacer = _Pacer(self.config.ads_detail_min_interval_seconds, self.sleep_fn)

        for batch_number, batch in enumerate(
            _chunks(detail_ids, self.config.ads_detail_batch_limit), start=1
        ):
            try:
                payload, retries = self._call_with_rate_limit_retry(
                    lambda current_batch=tuple(batch): self.ads_source.fetch_adverts(
                        current_batch,
                        statuses=None,
                        payment_type="",
                    ),
                    endpoint=ADS_DETAIL_ENDPOINT,
                    pacer=pacer,
                )
            except _SourceFailure as exc:
                issues.append(dict(exc.evidence))
                break
            retry_events.extend(retries)
            raw_adverts = payload.get("adverts") if isinstance(payload, Mapping) else None
            if not isinstance(raw_adverts, list):
                issues.append(
                    _schema_issue(
                        ADS_DETAIL_SOURCE,
                        "adverts_not_array",
                        identities=batch,
                        evidence_digest=_digest_any(payload),
                    )
                )
                break
            observed_ids: list[int] = []
            for raw_advert in raw_adverts:
                if not isinstance(raw_advert, Mapping):
                    issues.append(_schema_issue(ADS_DETAIL_SOURCE, "advert_not_object"))
                    continue
                advert_id = _positive_int_field(raw_advert, ("id", "advertId"))
                if advert_id is None:
                    issues.append(
                        _schema_issue(
                            ADS_DETAIL_SOURCE,
                            "advert_id_missing_or_invalid",
                            evidence_digest=_digest_any(raw_advert),
                        )
                    )
                    continue
                observed_ids.append(advert_id)
                if advert_id in details:
                    issues.append(
                        _schema_issue(
                            ADS_DETAIL_SOURCE,
                            "duplicate_detail_advert_id",
                            identities=[advert_id],
                        )
                    )
                details[advert_id] = raw_advert
            batch_evidence = {
                "batch": batch_number,
                "method": "GET",
                "endpoint": ADS_DETAIL_ENDPOINT,
                "requested_ids": list(batch),
                "observed_ids": sorted(observed_ids),
                "requested_count": len(batch),
                "observed_count": len(observed_ids),
                "response_digest": _digest_any(payload),
                "rate_limit_retry_count": len(retries),
            }
            detail_batches.append(batch_evidence)
            if sorted(observed_ids) != sorted(batch):
                issues.append(
                    _schema_issue(
                        ADS_DETAIL_SOURCE,
                        "count_detail_mismatch",
                        identities=sorted(set(batch) ^ set(observed_ids)),
                        evidence_digest=canonical_digest(batch_evidence),
                    )
                )

        campaigns: list[dict[str, Any]] = []
        incidents: list[dict[str, Any]] = []
        for advert_id in legacy_ids:
            campaigns.append(_legacy_campaign(count_entries[advert_id]))
        for advert_id in sorted(details):
            entry = count_entries.get(advert_id)
            if entry is None:
                issues.append(
                    _schema_issue(
                        ADS_DETAIL_SOURCE,
                        "detail_id_absent_from_count_manifest",
                        identities=[advert_id],
                    )
                )
                continue
            campaign, incident, campaign_issues = _normalize_campaign_detail(
                details[advert_id],
                count_entry=entry,
                seller_id=self.seller_id,
                account_scope=self.account_scope,
                observed_at=self._now(),
            )
            campaigns.append(campaign)
            if incident is not None:
                incidents.append(incident)
            issues.extend(campaign_issues)

        observed_detail_ids = sorted(details)
        if observed_detail_ids != detail_ids:
            issues.append(
                _schema_issue(
                    ADS_DETAIL_SOURCE,
                    "joint_detail_coverage_incomplete",
                    identities=sorted(set(detail_ids) ^ set(observed_detail_ids)),
                )
            )
        campaigns.sort(key=lambda item: (int(item["advert_id"]), canonical_json(item)))
        incidents.sort(key=canonical_json)
        completed_at = self._now()
        payload = {
            "contract_name": "wb_change_registry_ads_acquisition",
            "contract_version": 1,
            "seller_id": self.seller_id,
            "account_scope": self.account_scope,
            "sources": {
                "count": {
                    "source_id": ADS_COUNT_SOURCE,
                    "method": "GET",
                    "endpoint": ADS_COUNT_ENDPOINT,
                    "response_digest": count_response_digest,
                },
                "detail": {
                    "source_id": ADS_DETAIL_SOURCE,
                    "method": "GET",
                    "endpoint": ADS_DETAIL_ENDPOINT,
                    "max_ids_per_request": ADS_DETAIL_BATCH_LIMIT,
                    "official_limit": {
                        "period_seconds": 60,
                        "requests": 20,
                        "interval_ms": 3000,
                        "burst": 5,
                    },
                },
            },
            "interval": {"started_at": started_at, "completed_at": completed_at},
            "completeness_status": "complete" if not issues else "partial",
            "count_manifest": {
                "expected_all": expected_all,
                "groups": count_groups,
                "advert_ids": sorted(count_entries),
                "detail_expected_ids": detail_ids,
                "legacy_count_only_ids": legacy_ids,
            },
            "detail_batches": detail_batches,
            "campaigns": campaigns,
            "identity_incidents": incidents,
            "counts": {
                "manifest_campaigns": len(count_entries),
                "detail_campaigns": len(details),
                "legacy_count_only_campaigns": len(legacy_ids),
                "bids": sum(len(item.get("bids", [])) for item in campaigns),
                "identity_incidents": len(incidents),
                "issues": len(issues),
                "rate_limit_retries": len(retry_events),
            },
            "retry_evidence": retry_events,
            "issues": sorted(issues, key=canonical_json),
        }
        return _with_digest(payload)

    def _call_with_rate_limit_retry(
        self,
        call: Callable[[], Any],
        *,
        endpoint: str,
        pacer: _Pacer | None,
    ) -> tuple[Any, list[dict[str, Any]]]:
        retry_events: list[dict[str, Any]] = []
        for attempt in range(1, self.config.max_rate_limit_retries + 2):
            if pacer is not None:
                pacer.before_call()
            try:
                return call(), retry_events
            except Exception as exc:
                status = _exception_status(exc)
                if status != 429:
                    raise _SourceFailure(_source_error_evidence(exc, endpoint, attempt)) from exc
                retry_after = _exception_retry_after(exc)
                bounded_delay = retry_after if retry_after is not None else 0.0
                event = {
                    "endpoint": endpoint,
                    "failed_attempt": attempt,
                    "http_status": 429,
                    "retry_after_seconds": bounded_delay,
                    "hint_present": retry_after is not None,
                    "error_digest": _exception_digest(exc, endpoint),
                }
                retry_events.append(event)
                if bounded_delay > self.config.max_retry_after_seconds:
                    evidence = _source_error_evidence(exc, endpoint, attempt)
                    evidence.update(
                        {
                            "error_code": "retry_after_exceeds_bound",
                            "retry_count": len(retry_events) - 1,
                            "last_retry_after_seconds": bounded_delay,
                        }
                    )
                    raise _SourceFailure(evidence) from exc
                if attempt > self.config.max_rate_limit_retries:
                    evidence = _source_error_evidence(exc, endpoint, attempt)
                    evidence.update(
                        {
                            "error_code": "rate_limit_retry_exhausted",
                            "retry_count": len(retry_events) - 1,
                            "last_retry_after_seconds": bounded_delay,
                        }
                    )
                    raise _SourceFailure(evidence) from exc
                if bounded_delay > 0:
                    self.sleep_fn(bounded_delay)
        raise AssertionError("unreachable retry loop")

    def _now(self) -> str:
        value = self.now_fn()
        return canonical_utc_timestamp(value, allow_naive=True)


def _normalize_price_good(
    raw_good: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    nm_id = _positive_int_field(raw_good, ("nmID",))
    if nm_id is None:
        return None, [
            _schema_issue(
                PRICES_SOURCE,
                "nm_id_missing_or_invalid",
                evidence_digest=_digest_any(raw_good),
            )
        ]
    raw_sizes = raw_good.get("sizes")
    if not isinstance(raw_sizes, list):
        raw_sizes = []
        issues.append(
            _schema_issue(PRICES_SOURCE, "sizes_not_array", identities=[nm_id])
        )
    sizes: list[dict[str, Any]] = []
    for raw_size in raw_sizes:
        if not isinstance(raw_size, Mapping):
            issues.append(
                _schema_issue(PRICES_SOURCE, "size_not_object", identities=[nm_id])
            )
            continue
        size = {
            "size_id": _field_observation(raw_size, "sizeID", _non_negative_integer),
            "tech_size_name": _field_observation(raw_size, "techSizeName", _text_value),
            "original_price_minor": _field_observation(raw_size, "price", _money_minor),
            "seller_price_minor": _field_observation(
                raw_size, "discountedPrice", _money_minor
            ),
            "club_price_minor": _field_observation(
                raw_size, "clubDiscountedPrice", _money_minor
            ),
        }
        if any(value["status"] == "error" for value in size.values()):
            issues.append(
                _schema_issue(
                    PRICES_SOURCE,
                    "invalid_size_scalar",
                    identities=[nm_id],
                    evidence_digest=_digest_any(raw_size),
                )
            )
        size["tuple_digest"] = canonical_digest(size)
        sizes.append(size)
    sizes.sort(key=canonical_json)

    discount = _field_observation(raw_good, "discount", _discount_bps)
    currency = _field_observation(raw_good, "currencyIsoCode4217", _token_value)
    editable_size_price = _field_observation(raw_good, "editableSizePrice", _boolean_value)
    vendor_code = _field_observation(raw_good, "vendorCode", _text_value)
    if any(
        item["status"] == "error"
        for item in (discount, currency, editable_size_price, vendor_code)
    ):
        issues.append(
            _schema_issue(
                PRICES_SOURCE,
                "invalid_good_scalar",
                identities=[nm_id],
                evidence_digest=_digest_any(raw_good),
            )
        )
    size_tuples = [
        {
            "original_price_minor": item["original_price_minor"],
            "seller_price_minor": item["seller_price_minor"],
            "discount_bps": discount,
        }
        for item in sizes
    ]
    uniform = bool(size_tuples) and all(
        canonical_json(item) == canonical_json(size_tuples[0])
        for item in size_tuples[1:]
    )
    sku_values = (
        size_tuples[0]
        if uniform
        else {
            "original_price_minor": _inapplicable("size_level_representation_required"),
            "seller_price_minor": _inapplicable("size_level_representation_required"),
            "discount_bps": _inapplicable("size_level_representation_required"),
        }
    )
    sku_actionable = uniform and all(
        _is_exact_integer(sku_values[field])
        for field in ("original_price_minor", "seller_price_minor", "discount_bps")
    )
    record = {
        "nm_id": nm_id,
        "vendor_code": vendor_code,
        "currency": currency,
        "editable_size_price": editable_size_price,
        "representation": "sku_uniform" if uniform else "size_level",
        "sku_values": sku_values,
        "sizes": sizes,
        "target_actionable": bool(sku_actionable),
        "source_evidence_digest": _digest_any(raw_good),
    }
    record["record_digest"] = canonical_digest(record)
    return record, issues


def _parse_ads_count_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    groups_payload = payload.get("adverts")
    expected_all = _strict_non_negative_integer(payload.get("all"))
    if expected_all is None:
        issues.append(_schema_issue(ADS_COUNT_SOURCE, "all_count_missing_or_invalid"))
    if not isinstance(groups_payload, list):
        return {
            "groups": [],
            "entries": {},
            "expected_all": expected_all,
            "issues": issues + [_schema_issue(ADS_COUNT_SOURCE, "groups_not_array")],
        }
    groups: list[dict[str, Any]] = []
    entries: dict[int, dict[str, Any]] = {}
    declared_count_sum = 0
    for raw_group in groups_payload:
        if not isinstance(raw_group, Mapping):
            issues.append(_schema_issue(ADS_COUNT_SOURCE, "group_not_object"))
            continue
        group_type = _strict_integer(raw_group.get("type"))
        group_status = _strict_integer(raw_group.get("status"))
        group_count = _strict_non_negative_integer(raw_group.get("count"))
        raw_list = (
            raw_group.get("advert_list")
            if "advert_list" in raw_group
            else raw_group.get("advertList")
        )
        if group_type is None or group_status is None or group_count is None:
            issues.append(
                _schema_issue(
                    ADS_COUNT_SOURCE,
                    "group_identity_or_count_invalid",
                    evidence_digest=_digest_any(raw_group),
                )
            )
            continue
        if not isinstance(raw_list, list):
            issues.append(
                _schema_issue(
                    ADS_COUNT_SOURCE,
                    "advert_list_not_array",
                    evidence_digest=_digest_any(raw_group),
                )
            )
            raw_list = []
        declared_count_sum += group_count
        adverts: list[dict[str, Any]] = []
        for raw_entry in raw_list:
            if not isinstance(raw_entry, Mapping):
                issues.append(_schema_issue(ADS_COUNT_SOURCE, "manifest_advert_not_object"))
                continue
            advert_id = _positive_int_field(raw_entry, ("advertId", "advert_id", "id"))
            if advert_id is None:
                issues.append(
                    _schema_issue(
                        ADS_COUNT_SOURCE,
                        "manifest_advert_id_invalid",
                        evidence_digest=_digest_any(raw_entry),
                    )
                )
                continue
            entry = {
                "advert_id": advert_id,
                "campaign_type": group_type,
                "campaign_status": group_status,
                "change_time": _field_observation_alias(
                    raw_entry, ("changeTime", "change_time"), _timestamp_value
                ),
                "legacy_count_only": bool(
                    group_type == LEGACY_COUNT_ONLY_TYPE
                    and group_status == LEGACY_COUNT_ONLY_STATUS
                ),
                "source_evidence_digest": _digest_any(raw_entry),
            }
            if entry["change_time"]["status"] == "error":
                issues.append(
                    _schema_issue(
                        ADS_COUNT_SOURCE,
                        "invalid_change_time",
                        identities=[advert_id],
                        evidence_digest=_digest_any(raw_entry),
                    )
                )
            if advert_id in entries:
                issues.append(
                    _schema_issue(
                        ADS_COUNT_SOURCE,
                        "duplicate_manifest_advert_id",
                        identities=[advert_id],
                    )
                )
            entries[advert_id] = entry
            adverts.append(entry)
        adverts.sort(key=lambda item: int(item["advert_id"]))
        if group_count != len(adverts):
            issues.append(
                _schema_issue(
                    ADS_COUNT_SOURCE,
                    "group_count_mismatch",
                    identities=[item["advert_id"] for item in adverts],
                    evidence_digest=_digest_any(raw_group),
                )
            )
        groups.append(
            {
                "campaign_type": group_type,
                "campaign_status": group_status,
                "declared_count": group_count,
                "observed_count": len(adverts),
                "legacy_count_only": bool(
                    group_type == LEGACY_COUNT_ONLY_TYPE
                    and group_status == LEGACY_COUNT_ONLY_STATUS
                ),
                "adverts": adverts,
            }
        )
    groups.sort(key=canonical_json)
    if expected_all is not None and (
        expected_all != declared_count_sum or expected_all != len(entries)
    ):
        issues.append(
            _schema_issue(
                ADS_COUNT_SOURCE,
                "top_level_count_mismatch",
                identities=sorted(entries),
                evidence_digest=_digest_any(payload),
            )
        )
    return {
        "groups": groups,
        "entries": entries,
        "expected_all": expected_all,
        "issues": issues,
    }


def _normalize_campaign_detail(
    raw_advert: Mapping[str, Any],
    *,
    count_entry: Mapping[str, Any],
    seller_id: str,
    account_scope: str,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    advert_id = int(count_entry["advert_id"])
    settings = raw_advert.get("settings")
    if not isinstance(settings, Mapping):
        settings = {}
    timestamps = raw_advert.get("timestamps")
    if not isinstance(timestamps, Mapping):
        timestamps = {}
    raw_nm_settings = raw_advert.get("nm_settings")
    if not isinstance(raw_nm_settings, list):
        if "nm_settings" not in raw_advert:
            issues.append(
                _schema_issue(
                    ADS_DETAIL_SOURCE,
                    "nm_settings_missing",
                    identities=[advert_id],
                    evidence_digest=_digest_any(raw_advert),
                )
            )
        elif raw_nm_settings is not None:
            issues.append(
                _schema_issue(
                    ADS_DETAIL_SOURCE,
                    "nm_settings_not_array",
                    identities=[advert_id],
                    evidence_digest=_digest_any(raw_advert),
                )
            )
        raw_nm_settings = []
    candidates: list[int] = []
    valid_nm_rows: list[tuple[int, Mapping[str, Any]]] = []
    for raw_nm in raw_nm_settings:
        if not isinstance(raw_nm, Mapping):
            issues.append(
                _schema_issue(ADS_DETAIL_SOURCE, "nm_setting_not_object", identities=[advert_id])
            )
            continue
        nm_id = _positive_int_field(raw_nm, ("nm_id", "nmId"))
        if nm_id is None:
            issues.append(
                _schema_issue(
                    ADS_DETAIL_SOURCE,
                    "nm_setting_identity_invalid",
                    identities=[advert_id],
                    evidence_digest=_digest_any(raw_nm),
                )
            )
            continue
        candidates.append(nm_id)
        valid_nm_rows.append((nm_id, raw_nm))
    unique_candidates = sorted(set(candidates))
    mapping_exact = len(unique_candidates) == 1
    incident = None
    if not mapping_exact:
        incident_basis = {
            "contract_name": "wb_change_registry_identity_incident_shape",
            "contract_version": 1,
            "seller_id": seller_id,
            "account_scope": account_scope,
            "incident_kind": "campaign_nm_mapping_cardinality",
            "target_kind": "campaign",
            "advert_id": advert_id,
            "candidate_nm_ids": unique_candidates,
            "candidate_count": len(unique_candidates),
            "source_surface": ADS_DETAIL_SOURCE,
            "observed_at": observed_at,
            "evidence_digest": _digest_any(raw_advert),
            "persistence_status": "not_persisted",
        }
        incident = {
            "incident_id": "crii_"
            + hashlib.sha256(canonical_json(incident_basis).encode("utf-8")).hexdigest()[:32],
            **incident_basis,
        }
        incident["incident_digest"] = canonical_digest(incident)

    raw_status = _field_observation(raw_advert, "status", _integer_value)
    campaign_state = _campaign_state_observation(raw_status)
    raw_status_value = raw_status.get("value")
    detail_status = (
        int(raw_status_value["integer_value"])
        if raw_status.get("status") in {"exact", "exact_zero"}
        and isinstance(raw_status_value, Mapping)
        and raw_status_value.get("kind") == "integer"
        else None
    )
    if detail_status is None:
        issues.append(
            _schema_issue(
                ADS_DETAIL_SOURCE,
                "count_detail_status_unknown",
                identities=[advert_id],
                evidence_digest=_digest_any(raw_advert),
            )
        )
    elif detail_status != int(count_entry["campaign_status"]):
        issues.append(
            _schema_issue(
                ADS_DETAIL_SOURCE,
                "count_detail_status_mismatch",
                identities=[advert_id],
                evidence_digest=_digest_any(raw_advert),
            )
        )
    payment_model = _field_observation_alias(
        settings,
        ("payment_type", "paymentType"),
        _payment_model_value,
        fallback=(raw_advert, ("payment_type", "paymentType")),
    )
    payment_unit = _payment_unit_observation(payment_model)
    placement_flags, invalid_placement_flags = _normalize_placement_flags(
        settings.get("placements")
    )
    if "placements" in settings and settings.get("placements") is not None and not isinstance(
        settings.get("placements"), Mapping
    ):
        issues.append(
            _schema_issue(
                ADS_DETAIL_SOURCE,
                "placements_not_object",
                identities=[advert_id],
                evidence_digest=_digest_any(settings.get("placements")),
            )
        )
    if invalid_placement_flags:
        issues.append(
            _schema_issue(
                ADS_DETAIL_SOURCE,
                "invalid_placement_flag",
                identities=[advert_id],
                evidence_digest=_digest_any(settings.get("placements")),
            )
        )
    bids: list[dict[str, Any]] = []
    duplicate_nm_ids = sorted(
        value for value, count in Counter(candidates).items() if count > 1
    )
    for nm_id, raw_nm in valid_nm_rows:
        raw_bid_map = raw_nm.get("bids_kopecks")
        bid_map, invalid_bid_keys = _normalize_bid_map(raw_bid_map)
        if "bids_kopecks" in raw_nm and raw_bid_map is not None and not isinstance(
            raw_bid_map, Mapping
        ):
            issues.append(
                _schema_issue(
                    ADS_DETAIL_SOURCE,
                    "bids_not_object",
                    identities=[advert_id, nm_id],
                    evidence_digest=_digest_any(raw_bid_map),
                )
            )
        if invalid_bid_keys:
            issues.append(
                _schema_issue(
                    ADS_DETAIL_SOURCE,
                    "unsupported_bid_placement",
                    identities=[advert_id, nm_id],
                    evidence_digest=_digest_any(raw_bid_map),
                )
            )
        for placement in PLACEMENTS:
            if placement in bid_map:
                bid_observation = bid_map[placement]
            elif placement in placement_flags and placement_flags[placement] is False:
                bid_observation = _inapplicable("placement_disabled_by_source")
            else:
                bid_observation = _missing("bid_field_absent")
            if bid_observation["status"] == "error":
                issues.append(
                    _schema_issue(
                        ADS_DETAIL_SOURCE,
                        "invalid_bid_scalar",
                        identities=[advert_id, nm_id],
                        evidence_digest=_digest_any(raw_nm),
                    )
                )
            actionable = (
                mapping_exact
                and nm_id == unique_candidates[0]
                and nm_id not in duplicate_nm_ids
                and placement_flags.get(placement) is not False
                and _is_exact_integer(bid_observation)
                and _is_exact_text(payment_model)
                and _is_exact_text(payment_unit)
            )
            bid = {
                "nm_id": nm_id,
                "advert_id": advert_id,
                "placement": placement,
                "bid_minor": bid_observation,
                "payment_model": payment_model,
                "payment_unit": payment_unit,
                "target_actionable": bool(actionable),
                "source_entry_digest": _digest_any(raw_nm),
            }
            bid["target_digest"] = canonical_digest(bid)
            bids.append(bid)
    bids.sort(key=canonical_json)
    if duplicate_nm_ids:
        issues.append(
            _schema_issue(
                ADS_DETAIL_SOURCE,
                "duplicate_nm_setting",
                identities=[advert_id, *duplicate_nm_ids],
                evidence_digest=_digest_any(raw_advert),
            )
        )
    created_at = _field_observation(timestamps, "created", _timestamp_value)
    if any(
        item["status"] == "error"
        for item in (
            raw_status,
            campaign_state,
            payment_model,
            payment_unit,
            created_at,
            count_entry["change_time"],
        )
    ):
        issues.append(
            _schema_issue(
                ADS_DETAIL_SOURCE,
                "invalid_campaign_scalar",
                identities=[advert_id],
                evidence_digest=_digest_any(raw_advert),
            )
        )
    campaign_actionable = (
        mapping_exact
        and not duplicate_nm_ids
        and _is_exact_text(campaign_state)
        and _is_exact_text(payment_model)
        and _is_exact_text(payment_unit)
    )
    campaign = {
        "advert_id": advert_id,
        "source_support": "current_detail",
        "campaign_type": int(count_entry["campaign_type"]),
        "manifest_status": int(count_entry["campaign_status"]),
        "detail_status": _exact_text("available"),
        "created_at": created_at,
        "discovered_change_at": count_entry["change_time"],
        "raw_status": raw_status,
        "campaign_state": campaign_state,
        "payment_model": payment_model,
        "payment_unit": payment_unit,
        "mapping": {
            "status": "exact" if mapping_exact else "error",
            "candidate_nm_ids": unique_candidates,
            "candidate_count": len(unique_candidates),
            "exact_nm_id": unique_candidates[0] if mapping_exact else None,
        },
        "campaign_target_actionable": bool(campaign_actionable),
        "bids": bids,
        "source_evidence_digest": _digest_any(raw_advert),
    }
    campaign["record_digest"] = canonical_digest(campaign)
    return campaign, incident, issues


def _legacy_campaign(count_entry: Mapping[str, Any]) -> dict[str, Any]:
    advert_id = int(count_entry["advert_id"])
    campaign = {
        "advert_id": advert_id,
        "source_support": "legacy_count_only_type6_status7",
        "campaign_type": LEGACY_COUNT_ONLY_TYPE,
        "manifest_status": LEGACY_COUNT_ONLY_STATUS,
        "detail_status": _inapplicable("legacy_count_only"),
        "created_at": _inapplicable("legacy_count_only"),
        "discovered_change_at": count_entry["change_time"],
        "raw_status": _exact_integer(LEGACY_COUNT_ONLY_STATUS),
        "campaign_state": _exact_text("completed"),
        "payment_model": _inapplicable("legacy_count_only"),
        "payment_unit": _inapplicable("legacy_count_only"),
        "mapping": {
            "status": "inapplicable",
            "candidate_nm_ids": [],
            "candidate_count": 0,
            "exact_nm_id": None,
            "reason": "legacy_count_only",
        },
        "campaign_target_actionable": False,
        "bids": [],
        "bid_evidence_status": _inapplicable("legacy_count_only"),
        "source_evidence_digest": str(count_entry["source_evidence_digest"]),
    }
    campaign["record_digest"] = canonical_digest(campaign)
    return campaign


def _normalize_placement_flags(value: Any) -> tuple[dict[str, bool], bool]:
    if not isinstance(value, Mapping):
        return {}, False
    result: dict[str, bool] = {}
    invalid = False
    for key, raw_flag in value.items():
        placement = _placement_name(key)
        if placement is None or not isinstance(raw_flag, bool):
            invalid = True
            continue
        result[placement] = raw_flag
    return result, invalid


def _normalize_bid_map(value: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    if not isinstance(value, Mapping):
        return {}, False
    result: dict[str, dict[str, Any]] = {}
    invalid = False
    for key, raw_bid in value.items():
        placement = _placement_name(key)
        if placement is None:
            invalid = True
            continue
        result[placement] = _scalar_observation(raw_bid, _non_negative_integer)
    return result, invalid


def _placement_name(value: Any) -> str | None:
    token = str(value or "").strip().casefold().replace("-", "_")
    return {
        "combined": "combined",
        "search": "search",
        "recommendation": "recommendations",
        "recommendations": "recommendations",
        "reco": "recommendations",
    }.get(token)


def _campaign_state_observation(raw_status: Mapping[str, Any]) -> dict[str, Any]:
    value = raw_status.get("value") if isinstance(raw_status, Mapping) else None
    if raw_status.get("status") != "exact" or not isinstance(value, Mapping):
        return dict(raw_status)
    if value.get("kind") == "null":
        return _exact_null()
    if value.get("kind") != "integer":
        return _error("campaign_status_not_integer")
    status = int(value["integer_value"])
    token = {
        -1: "deleted",
        4: "ready",
        7: "completed",
        8: "cancelled",
        9: "active",
        11: "paused",
    }.get(status)
    return _exact_text(token) if token is not None else _error("unsupported_campaign_status")


def _payment_unit_observation(payment_model: Mapping[str, Any]) -> dict[str, Any]:
    value = payment_model.get("value") if isinstance(payment_model, Mapping) else None
    if payment_model.get("status") != "exact" or not isinstance(value, Mapping):
        return dict(payment_model)
    if value.get("kind") == "null":
        return _exact_null()
    model = str(value.get("text_value") or "")
    if model == "cpm":
        return _exact_text("per_thousand_impressions")
    if model == "cpc":
        return _exact_text("per_click")
    return _error("unsupported_payment_model")


def _field_observation(
    payload: Mapping[str, Any],
    key: str,
    converter: Callable[[Any], tuple[str, Any]],
) -> dict[str, Any]:
    if key not in payload:
        return _missing("field_absent")
    return _scalar_observation(payload[key], converter)


def _field_observation_alias(
    payload: Mapping[str, Any],
    keys: Sequence[str],
    converter: Callable[[Any], tuple[str, Any]],
    *,
    fallback: tuple[Mapping[str, Any], Sequence[str]] | None = None,
) -> dict[str, Any]:
    for key in keys:
        if key in payload:
            return _scalar_observation(payload[key], converter)
    if fallback is not None:
        fallback_payload, fallback_keys = fallback
        for key in fallback_keys:
            if key in fallback_payload:
                return _scalar_observation(fallback_payload[key], converter)
    return _missing("field_absent")


def _scalar_observation(
    raw_value: Any,
    converter: Callable[[Any], tuple[str, Any]],
) -> dict[str, Any]:
    if raw_value is None:
        return _exact_null()
    try:
        kind, value = converter(raw_value)
    except (SourceAcquisitionError, ValueError, TypeError, InvalidOperation):
        return _error("invalid_scalar")
    if kind == "integer":
        return _exact_integer(int(value))
    if kind == "text":
        return _exact_text(str(value))
    if kind == "boolean":
        return _exact_boolean(bool(value))
    return _error("unsupported_scalar_kind")


def _exact_integer(value: int) -> dict[str, Any]:
    return {
        "status": "exact_zero" if value == 0 else "exact",
        "value": {"kind": "integer", "integer_value": int(value), "text_value": None},
    }


def _exact_text(value: str) -> dict[str, Any]:
    return {
        "status": "exact",
        "value": {"kind": "text", "integer_value": None, "text_value": str(value)},
    }


def _exact_boolean(value: bool) -> dict[str, Any]:
    return {
        "status": "exact",
        "value": {"kind": "boolean", "integer_value": int(value), "text_value": None},
    }


def _exact_null() -> dict[str, Any]:
    return {
        "status": "exact",
        "value": {"kind": "null", "integer_value": None, "text_value": None},
    }


def _missing(reason: str) -> dict[str, Any]:
    return {
        "status": "missing",
        "value": {"kind": "missing", "integer_value": None, "text_value": None},
        "reason": reason,
    }


def _inapplicable(reason: str) -> dict[str, Any]:
    return {
        "status": "inapplicable",
        "value": {"kind": "missing", "integer_value": None, "text_value": None},
        "reason": reason,
    }


def _error(reason: str) -> dict[str, Any]:
    return {
        "status": "error",
        "value": {"kind": "missing", "integer_value": None, "text_value": None},
        "reason": reason,
    }


def _is_exact_integer(observation: Mapping[str, Any]) -> bool:
    value = observation.get("value") if isinstance(observation, Mapping) else None
    return (
        observation.get("status") in {"exact", "exact_zero"}
        and isinstance(value, Mapping)
        and value.get("kind") == "integer"
    )


def _is_exact_text(observation: Mapping[str, Any]) -> bool:
    value = observation.get("value") if isinstance(observation, Mapping) else None
    return (
        observation.get("status") == "exact"
        and isinstance(value, Mapping)
        and value.get("kind") == "text"
        and bool(str(value.get("text_value") or ""))
    )


def _money_minor(value: Any) -> tuple[str, int]:
    amount = _decimal(value)
    minor = amount * Decimal(100)
    if amount < 0 or minor != minor.to_integral_value():
        raise SourceAcquisitionError("money must be non-negative exact minor units")
    return "integer", int(minor)


def _discount_bps(value: Any) -> tuple[str, int]:
    discount = _decimal(value)
    basis_points = discount * Decimal(100)
    if discount < 0 or discount > 100 or basis_points != basis_points.to_integral_value():
        raise SourceAcquisitionError("discount is outside exact basis-point range")
    return "integer", int(basis_points)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise SourceAcquisitionError("boolean is not numeric")
    number = Decimal(str(value).strip())
    if not number.is_finite():
        raise SourceAcquisitionError("non-finite numeric value")
    return number


def _integer_value(value: Any) -> tuple[str, int]:
    integer = _strict_integer(value)
    if integer is None:
        raise SourceAcquisitionError("integer required")
    return "integer", integer


def _non_negative_integer(value: Any) -> tuple[str, int]:
    integer = _strict_non_negative_integer(value)
    if integer is None:
        raise SourceAcquisitionError("non-negative integer required")
    return "integer", integer


def _boolean_value(value: Any) -> tuple[str, bool]:
    if not isinstance(value, bool):
        raise SourceAcquisitionError("boolean required")
    return "boolean", value


def _text_value(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or "\x00" in value or len(value) > 512:
        raise SourceAcquisitionError("bounded text required")
    return "text", value


def _token_value(value: Any) -> tuple[str, str]:
    kind, text = _text_value(value)
    token = text.strip().casefold()
    if not token or len(token) > 120:
        raise SourceAcquisitionError("non-empty token required")
    return kind, token


def _payment_model_value(value: Any) -> tuple[str, str]:
    kind, token = _token_value(value)
    return kind, token


def _timestamp_value(value: Any) -> tuple[str, str]:
    kind, text = _text_value(value)
    return kind, canonical_utc_timestamp(text)


def canonical_utc_timestamp(
    value: datetime | str | Any,
    *,
    allow_naive: bool = False,
) -> str:
    """Render one instant in the only digest/persistence timestamp form: UTC ``Z``."""

    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            raise SourceAcquisitionError("timestamp is empty")
        try:
            moment = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            )
        except ValueError as exc:
            raise SourceAcquisitionError("timestamp must be valid ISO-8601") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        if not allow_naive:
            raise SourceAcquisitionError("timestamp must have timezone")
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonicalize_acquisition_timestamps(value: Any) -> Any:
    """Canonicalize every explicit aware ISO timestamp before any digest boundary."""

    if isinstance(value, Mapping):
        return {
            str(key): canonicalize_acquisition_timestamps(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [canonicalize_acquisition_timestamps(child) for child in value]
    if isinstance(value, datetime):
        return canonical_utc_timestamp(value)
    if isinstance(value, str) and "T" in value:
        try:
            return canonical_utc_timestamp(value)
        except SourceAcquisitionError:
            return value
    return value


def _strict_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not -(2**63) <= value <= 2**63 - 1:
        return None
    return value


def _strict_non_negative_integer(value: Any) -> int | None:
    integer = _strict_integer(value)
    return integer if integer is not None and integer >= 0 else None


def _positive_int_field(payload: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        if key not in payload:
            continue
        integer = _strict_integer(payload[key])
        if integer is not None and integer > 0:
            return integer
        return None
    return None


def _chunks(values: Sequence[int], size: int) -> list[list[int]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _with_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    canonical = canonicalize_acquisition_timestamps(payload)
    if not isinstance(canonical, Mapping):
        raise SourceAcquisitionError("manifest payload must be an object")
    result = dict(canonical)
    result["manifest_digest"] = canonical_digest(result)
    return result


def _digest_any(value: Any) -> str:
    try:
        return canonical_digest(
            _normalized_digest_value(canonicalize_acquisition_timestamps(value))
        )
    except Exception:
        safe = {"type": type(value).__name__}
        return canonical_digest(safe)


def _normalized_digest_value(value: Any) -> Any:
    """Normalize semantically unordered source arrays before hashing."""

    if isinstance(value, Mapping):
        return {
            str(key): _normalized_digest_value(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        normalized = [_normalized_digest_value(child) for child in value]
        return sorted(normalized, key=canonical_json)
    return value


def _schema_issue(
    source: str,
    error_code: str,
    *,
    identities: Sequence[int] | None = None,
    evidence_digest: str = "",
) -> dict[str, Any]:
    basis = {
        "source": source,
        "error_code": error_code,
        "identities": sorted({int(value) for value in identities or []}),
        "evidence_digest": evidence_digest or canonical_digest(
            {"source": source, "error_code": error_code}
        ),
    }
    return basis


def _exception_status(exc: Exception) -> int | None:
    value = getattr(exc, "http_status", None)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _exception_retry_after(exc: Exception) -> float | None:
    direct = getattr(exc, "retry_after_seconds", None)
    try:
        parsed = float(direct)
    except (TypeError, ValueError):
        parsed = -1.0
    if parsed >= 0:
        return parsed
    headers = getattr(exc, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    normalized = {
        str(key).strip().casefold(): str(value).strip()
        for key, value in headers.items()
    }
    for name in (
        "retry-after",
        "x-ratelimit-retry",
        "x-rate-limit-retry",
        "x-ratelimit-reset",
        "x-rate-limit-reset",
    ):
        raw_value = normalized.get(name, "")
        if not raw_value:
            continue
        try:
            parsed = float(raw_value)
        except ValueError:
            if name != "retry-after":
                continue
            try:
                retry_at = parsedate_to_datetime(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if retry_at.tzinfo is None or retry_at.utcoffset() is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        if parsed >= 0:
            return parsed
    return None


def _exception_digest(exc: Exception, endpoint: str) -> str:
    return canonical_digest(
        {
            "endpoint": endpoint,
            "error_type": type(exc).__name__,
            "http_status": _exception_status(exc),
            "retry_after_seconds": _exception_retry_after(exc),
        }
    )


def _source_error_evidence(exc: Exception, endpoint: str, attempt: int) -> dict[str, Any]:
    safe_endpoint = endpoint
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
        except Exception:
            payload = None
        if isinstance(payload, Mapping):
            candidate = str(payload.get("endpoint") or "").strip()
            if candidate.startswith("/"):
                safe_endpoint = candidate.split("?", 1)[0]
    return {
        "source": "official_api",
        "error_code": "source_request_failed",
        "endpoint": safe_endpoint,
        "attempt": int(attempt),
        "http_status": _exception_status(exc),
        "retry_after_seconds": _exception_retry_after(exc),
        "error_type": type(exc).__name__,
        "error_digest": _exception_digest(exc, safe_endpoint),
    }


def _identity(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not _IDENTITY.fullmatch(text):
        raise SourceAcquisitionError(f"{name} is invalid")
    return text


__all__ = [
    "ADS_DETAIL_BATCH_LIMIT",
    "ADS_DETAIL_MIN_INTERVAL_SECONDS",
    "ChangeRegistrySourceAcquirer",
    "canonical_utc_timestamp",
    "canonicalize_acquisition_timestamps",
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "PRICE_PAGE_LIMIT",
    "PRICES_MIN_INTERVAL_SECONDS",
    "SourceAcquisitionConfig",
    "SourceAcquisitionError",
]
