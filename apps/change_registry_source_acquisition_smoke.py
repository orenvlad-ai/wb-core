"""Deterministic smoke for strict read-only Prices + Ads acquisition."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_prices_management import WbPricesApiError  # noqa: E402
from packages.adapters.wb_promotion import WbPromotionApiError  # noqa: E402
from packages.application.change_registry_source_acquisition import (  # noqa: E402
    ChangeRegistrySourceAcquirer,
    SourceAcquisitionConfig,
)


FIXED_TIME = "2026-08-29T06:00:00+00:00"


class FakePricesSource:
    def __init__(
        self,
        pages: Mapping[int, Sequence[Mapping[str, Any]]],
        *,
        failures: Sequence[Exception] = (),
    ) -> None:
        self.pages = {int(key): [deepcopy(item) for item in value] for key, value in pages.items()}
        self.failures = list(failures)
        self.calls: list[dict[str, Any]] = []
        self.write_calls = 0

    def fetch_goods(self, *, limit: int, offset: int, filter_nm_id: int | None = None):
        self.calls.append(
            {"limit": limit, "offset": offset, "filter_nm_id": filter_nm_id}
        )
        if self.failures:
            raise self.failures.pop(0)
        return {
            "data": {"listGoods": deepcopy(self.pages.get(offset, []))},
            "error": False,
            "errorText": "",
        }

    def fetch_goods_by_nm_ids(self, nm_ids):
        raise AssertionError("targeted POST cannot prove Prices completeness")

    def upload_task(self, goods):
        self.write_calls += 1
        raise AssertionError("Prices write is forbidden in acquisition")

    def fetch_upload_status(self, upload_id):
        raise AssertionError("upload status is outside acquisition")

    def fetch_upload_goods(self, *, upload_id, limit, offset):
        raise AssertionError("upload details are outside acquisition")

    def fetch_quarantine_goods(self, *, limit, offset):
        raise AssertionError("quarantine is outside acquisition")


class AlwaysRateLimitedPricesSource(FakePricesSource):
    def fetch_goods(self, *, limit: int, offset: int, filter_nm_id: int | None = None):
        self.calls.append(
            {"limit": limit, "offset": offset, "filter_nm_id": filter_nm_id}
        )
        raise WbPricesApiError(
            method="GET",
            url="https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000",
            http_status=429,
            headers={"Retry-After": "2"},
            retry_after_seconds=2.0,
        )


class FakeAdsSource:
    def __init__(
        self,
        count_payload: Mapping[str, Any],
        details: Mapping[int, Mapping[str, Any]],
        *,
        detail_failures: Sequence[Exception] = (),
    ) -> None:
        self.count_payload = deepcopy(count_payload)
        self.details = {int(key): deepcopy(value) for key, value in details.items()}
        self.detail_failures = list(detail_failures)
        self.detail_calls: list[list[int]] = []
        self.write_calls = 0

    def fetch_campaign_count(self):
        return deepcopy(self.count_payload)

    def fetch_adverts(self, advert_ids, *, statuses=None, payment_type=""):
        ids = [int(value) for value in advert_ids]
        self.detail_calls.append(ids)
        if self.detail_failures:
            raise self.detail_failures.pop(0)
        return {
            "adverts": [deepcopy(self.details[value]) for value in ids if value in self.details]
        }

    def fetch_min_bids(self, **kwargs):
        raise AssertionError("minimum bids are outside baseline acquisition")

    def fetch_recommendations(self, **kwargs):
        raise AssertionError("recommendations are outside baseline acquisition")

    def fetch_fullstats(self, *args, **kwargs):
        raise AssertionError("statistics are outside baseline acquisition")

    def patch_bids(self, payload):
        self.write_calls += 1
        raise AssertionError("Ads write is forbidden in acquisition")


def _empty_ads() -> FakeAdsSource:
    return FakeAdsSource({"adverts": [], "all": 0}, {})


def _count_payload(current_ids: Sequence[int], *, legacy_ids: Sequence[int] = ()) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    if current_ids:
        groups.append(
            {
                "type": 8,
                "status": 9,
                "count": len(current_ids),
                "advert_list": [
                    {
                        "advertId": int(advert_id),
                        "changeTime": "2026-08-29T08:00:00+05:00",
                    }
                    for advert_id in current_ids
                ],
            }
        )
    if legacy_ids:
        groups.append(
            {
                "type": 6,
                "status": 7,
                "count": len(legacy_ids),
                "advert_list": [
                    {
                        "advertId": int(advert_id),
                        "changeTime": "2025-01-01T00:00:00+03:00",
                    }
                    for advert_id in legacy_ids
                ],
            }
        )
    return {"adverts": groups, "all": len(current_ids) + len(legacy_ids)}


def _detail(advert_id: int, nm_ids: Sequence[int], *, created: Any = "2026-08-01T09:00:00+03:00"):
    timestamps: dict[str, Any] = {}
    if created is not _MISSING:
        timestamps["created"] = created
    return {
        "id": int(advert_id),
        "status": 9,
        "bid_type": "manual",
        "settings": {
            "name": f"Campaign {advert_id}",
            "payment_type": "cpc" if advert_id % 2 else "cpm",
            "placements": {"search": True, "recommendations": True},
        },
        "timestamps": timestamps,
        "nm_settings": [
            {
                "nm_id": int(nm_id),
                "bids_kopecks": {"search": 0, "recommendations": 1200},
            }
            for nm_id in nm_ids
        ],
    }


class _Missing:
    pass


_MISSING = _Missing()


def _acquire(
    prices,
    ads,
    sleeps: list[float] | None = None,
    *,
    now: Any = FIXED_TIME,
):
    observed_sleeps = sleeps if sleeps is not None else []
    return ChangeRegistrySourceAcquirer(
        seller_id="seller-primary",
        account_scope="account-primary",
        prices_source=prices,
        ads_source=ads,
        now_fn=lambda: now,
        sleep_fn=lambda seconds: observed_sleeps.append(float(seconds)),
    ).acquire()


def _assert_prices_and_identity_contract() -> None:
    uniform_good = {
        "nmID": 101,
        "vendorCode": "uniform",
        "discount": 10,
        "currencyIsoCode4217": "RUB",
        "editableSizePrice": False,
        "sizes": [
            {
                "sizeID": 1,
                "techSizeName": "S",
                "price": 1000,
                "discountedPrice": 900,
                "clubDiscountedPrice": 0,
            },
            {
                "sizeID": 2,
                "techSizeName": "M",
                "price": 1000,
                "discountedPrice": 900,
                "clubDiscountedPrice": None,
            },
            {
                "sizeID": 3,
                "techSizeName": "L",
                "price": 1000,
                "discountedPrice": 900,
            },
        ],
    }
    nonuniform_good = {
        "nmID": 102,
        "vendorCode": "sizes",
        "discount": 20,
        "currencyIsoCode4217": "RUB",
        "editableSizePrice": True,
        "sizes": [
            {"sizeID": 10, "techSizeName": "S", "price": 800, "discountedPrice": 640},
            {"sizeID": 11, "techSizeName": "M", "price": 900, "discountedPrice": 720},
        ],
    }
    final_good = {
        "nmID": 103,
        "vendorCode": "last-page",
        "discount": 0,
        "currencyIsoCode4217": "RUB",
        "editableSizePrice": False,
        "sizes": [
            {"sizeID": 20, "techSizeName": "ONE", "price": 500, "discountedPrice": 500}
        ],
    }
    prices = FakePricesSource({0: [uniform_good, nonuniform_good], 1000: [final_good], 2000: []})
    details = {
        201: _detail(201, [101], created=None),
        202: _detail(202, [], created=_MISSING),
        203: _detail(203, [103, 102]),
    }
    ads = FakeAdsSource(_count_payload([201, 202, 203], legacy_ids=[204]), details)
    result = _acquire(prices, ads)

    if not result["joint_complete"] or result["completeness_status"] != "complete":
        raise AssertionError("complete Prices + Ads sources did not produce joint complete")
    offsets = [int(call["offset"]) for call in prices.calls]
    if offsets != [0, 1000, 2000] or any(call["limit"] != 1000 for call in prices.calls):
        raise AssertionError(f"Prices GET pagination is not exhaustive: {prices.calls!r}")
    prices_manifest = result["sources"]["prices"]
    if not prices_manifest["pagination"]["terminal_empty_page"]:
        raise AssertionError("Prices completeness lacks terminal empty-page proof")
    goods = {int(item["nm_id"]): item for item in prices_manifest["goods"]}
    if goods[101]["representation"] != "sku_uniform" or len(goods[101]["sizes"]) != 3:
        raise AssertionError("uniform source size tuples were not preserved")
    if goods[102]["representation"] != "size_level":
        raise AssertionError("nonuniform sizes were collapsed into a SKU scalar")
    if goods[102]["sku_values"]["seller_price_minor"]["status"] != "inapplicable":
        raise AssertionError("size-level representation is not explicit")
    club_values = {
        int(item["size_id"]["value"]["integer_value"]): item["club_price_minor"]
        for item in goods[101]["sizes"]
    }
    if club_values[1]["status"] != "exact_zero":
        raise AssertionError("integer zero was lost")
    if club_values[2]["value"]["kind"] != "null":
        raise AssertionError("explicit null was not preserved")
    if club_values[3]["status"] != "missing":
        raise AssertionError("missing field was not preserved")

    ads_manifest = result["sources"]["ads"]
    campaigns = {int(item["advert_id"]): item for item in ads_manifest["campaigns"]}
    if campaigns[204]["detail_status"]["status"] != "inapplicable":
        raise AssertionError("legacy type6/status7 detail is not explicit inapplicable")
    if campaigns[204]["payment_model"]["status"] != "inapplicable":
        raise AssertionError("legacy payment evidence is not explicit inapplicable")
    if campaigns[201]["mapping"]["status"] != "exact":
        raise AssertionError("exact-one campaign mapping was not admitted")
    if campaigns[202]["mapping"]["candidate_count"] != 0:
        raise AssertionError("zero-cardinality mapping was not retained")
    if campaigns[203]["mapping"]["candidate_nm_ids"] != [102, 103]:
        raise AssertionError("many-cardinality mapping is not sorted/deterministic")
    if campaigns[202]["campaign_target_actionable"] or campaigns[203]["campaign_target_actionable"]:
        raise AssertionError("identity incidents remained actionable")
    incidents = ads_manifest["identity_incidents"]
    if len(incidents) != 2 or any(item["persistence_status"] != "not_persisted" for item in incidents):
        raise AssertionError("identity incident output is missing or persisted")
    if campaigns[201]["created_at"]["value"]["kind"] != "null":
        raise AssertionError("campaign explicit null was not preserved")
    if campaigns[202]["created_at"]["status"] != "missing":
        raise AssertionError("campaign missing timestamp was not preserved")
    zero_bids = [
        item
        for item in campaigns[201]["bids"]
        if item["placement"] == "search" and item["nm_id"] == 101
    ]
    if len(zero_bids) != 1 or zero_bids[0]["bid_minor"]["status"] != "exact_zero":
        raise AssertionError("zero bid was lost through truthiness fallback")
    if zero_bids[0]["payment_unit"]["value"]["text_value"] != "per_click":
        raise AssertionError("payment unit is not explicit")
    if result["persistence"] != {
        "registry_rows_written": 0,
        "checkpoints_written": 0,
        "facts_written": 0,
        "identity_incidents_written": 0,
    }:
        raise AssertionError("acquisition claimed a persistence side effect")
    if result["wb_mutation_calls"] != {"post": 0, "patch": 0}:
        raise AssertionError("acquisition claimed a WB mutation call")
    if prices.write_calls or ads.write_calls:
        raise AssertionError("a WB writer was called")

    reordered_uniform = deepcopy(uniform_good)
    reordered_uniform["sizes"] = list(reversed(reordered_uniform["sizes"]))
    reordered_details = deepcopy(details)
    reordered_details[203]["nm_settings"] = list(
        reversed(reordered_details[203]["nm_settings"])
    )
    replay = _acquire(
        FakePricesSource(
            {0: [nonuniform_good, reordered_uniform], 1000: [final_good], 2000: []}
        ),
        FakeAdsSource(
            _count_payload([203, 202, 201], legacy_ids=[204]), reordered_details
        ),
    )
    if replay != result or replay["manifest_digest"] != result["manifest_digest"]:
        raise AssertionError("canonical acquisition bytes/digest are not idempotent")


def _assert_ads_batching_and_partial_mismatch() -> None:
    ids = list(range(1001, 1052))
    details = {advert_id: _detail(advert_id, [900000 + advert_id]) for advert_id in ids}
    sleeps: list[float] = []
    ads = FakeAdsSource(_count_payload(ids), details)
    result = _acquire(FakePricesSource({0: []}), ads, sleeps)
    if not result["joint_complete"]:
        raise AssertionError("complete 51-campaign manifest became partial")
    if [len(batch) for batch in ads.detail_calls] != [50, 1]:
        raise AssertionError(f"Ads detail batching exceeded 50: {ads.detail_calls!r}")
    if 3.0 not in sleeps:
        raise AssertionError("Ads detail interval 3 seconds was not honored")

    partial_ads = FakeAdsSource(
        _count_payload([301, 302]),
        {301: _detail(301, [101])},
    )
    partial = _acquire(FakePricesSource({0: []}), partial_ads)
    if partial["joint_complete"] or partial["sources"]["ads"]["completeness_status"] != "partial":
        raise AssertionError("count/detail mismatch failed open")
    codes = {item["error_code"] for item in partial["sources"]["ads"]["issues"]}
    if "count_detail_mismatch" not in codes or "joint_detail_coverage_incomplete" not in codes:
        raise AssertionError(f"Ads mismatch evidence is incomplete: {codes!r}")


def _assert_retry_after_and_bounded_failure() -> None:
    rate_limited_once = WbPricesApiError(
        method="GET",
        url="https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000",
        http_status=429,
        headers={"Retry-After": "2"},
        retry_after_seconds=2.0,
    )
    sleeps: list[float] = []
    recovered_prices = FakePricesSource({0: []}, failures=[rate_limited_once])
    recovered = _acquire(recovered_prices, _empty_ads(), sleeps)
    if not recovered["joint_complete"] or 2.0 not in sleeps:
        raise AssertionError("Retry-After recovery did not complete safely")
    if recovered["sources"]["prices"]["counts"]["rate_limit_retries"] != 1:
        raise AssertionError("Retry-After evidence count drifted")

    exhausted_sleeps: list[float] = []
    exhausted_prices = AlwaysRateLimitedPricesSource({})
    exhausted = _acquire(exhausted_prices, _empty_ads(), exhausted_sleeps)
    if exhausted["joint_complete"]:
        raise AssertionError("bounded 429 exhaustion was misclassified as empty/complete")
    if len(exhausted_prices.calls) != 3:
        raise AssertionError("bounded retry budget did not stop after initial + two retries")
    codes = {item["error_code"] for item in exhausted["sources"]["prices"]["issues"]}
    if "rate_limit_retry_exhausted" not in codes:
        raise AssertionError("terminal rate-limit exhaustion lacks typed evidence")

    over_bound = WbPricesApiError(
        method="GET",
        url="https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000",
        http_status=429,
        headers={"Retry-After": "301"},
        retry_after_seconds=301.0,
    )
    over_bound_source = FakePricesSource({0: []}, failures=[over_bound])
    over_bound_result = _acquire(over_bound_source, _empty_ads(), [])
    over_bound_codes = {
        item["error_code"]
        for item in over_bound_result["sources"]["prices"]["issues"]
    }
    if len(over_bound_source.calls) != 1 or "retry_after_exceeds_bound" not in over_bound_codes:
        raise AssertionError("Retry-After beyond the bounded window was retried early")

    promotion_error = WbPromotionApiError(
        method="GET",
        url="https://advert-api.wildberries.ru/api/advert/v2/adverts?ids=401",
        http_status=429,
        headers={"X-Ratelimit-Retry": "1.5"},
        retry_after_seconds=1.5,
    )
    ads_sleeps: list[float] = []
    recovered_ads = FakeAdsSource(
        _count_payload([401]),
        {401: _detail(401, [101])},
        detail_failures=[promotion_error],
    )
    ads_result = _acquire(FakePricesSource({0: []}), recovered_ads, ads_sleeps)
    if not ads_result["joint_complete"] or 1.5 not in ads_sleeps:
        raise AssertionError("official Promotion rate-limit hint was not honored")


def _assert_utc_z_digest_identity_and_production_shape() -> None:
    good = {
        "nmID": 101,
        "vendorCode": "canonical-time",
        "discount": 0,
        "currencyIsoCode4217": "RUB",
        "editableSizePrice": False,
        "sizes": [
            {
                "sizeID": 1,
                "techSizeName": "ONE",
                "price": 100,
                "discountedPrice": 100,
            }
        ],
    }
    variants = (
        (datetime(2026, 8, 29, 6, tzinfo=timezone.utc), "2026-08-29T03:00:00Z", "2026-08-29T06:00:00Z"),
        ("2026-08-29T06:00:00+00:00", "2026-08-29T03:00:00+00:00", "2026-08-29T06:00:00+00:00"),
        ("2026-08-29T11:00:00+05:00", "2026-08-29T08:00:00+05:00", "2026-08-29T11:00:00+05:00"),
    )
    acquisitions = []
    for now, change_time, created in variants:
        count = _count_payload([201])
        count["adverts"][0]["advert_list"][0]["changeTime"] = change_time
        acquisitions.append(
            _acquire(
                FakePricesSource({0: [good], 1000: []}),
                FakeAdsSource(count, {201: _detail(201, [101], created=created)}),
                now=now,
            )
        )
    if acquisitions[0] != acquisitions[1] or acquisitions[1] != acquisitions[2]:
        raise AssertionError("equivalent aware instants changed canonical bytes/digests")
    canonical = acquisitions[0]
    if canonical["interval"]["completed_at"] != "2026-08-29T06:00:00Z":
        raise AssertionError("default aware UTC acquisition did not render canonical Z")
    campaign = canonical["sources"]["ads"]["campaigns"][0]
    if campaign["created_at"]["value"]["text_value"] != "2026-08-29T06:00:00Z":
        raise AssertionError("detail timestamp escaped the UTC-Z boundary")

    price_goods = [
        {
            **deepcopy(good),
            "nmID": 100_000 + index,
            "vendorCode": f"fixture-{index}",
        }
        for index in range(92)
    ]
    current_ids = list(range(200_000, 200_179))
    legacy_ids = list(range(300_000, 300_010))
    details = {
        advert_id: _detail(advert_id, [400_000 + index])
        for index, advert_id in enumerate(current_ids)
    }
    shaped = _acquire(
        FakePricesSource({0: price_goods, 1000: []}),
        FakeAdsSource(_count_payload(current_ids, legacy_ids=legacy_ids), details),
    )
    counts = shaped["sources"]["ads"]["counts"]
    if shaped["sources"]["prices"]["counts"]["goods"] != 92:
        raise AssertionError("production-shaped Prices fixture did not keep 92 goods")
    if shaped["sources"]["prices"]["counts"]["pages"] != 2:
        raise AssertionError("production-shaped Prices fixture did not prove two pages")
    if (
        counts["manifest_campaigns"],
        counts["detail_campaigns"],
        counts["legacy_count_only_campaigns"],
        counts["bids"],
    ) != (189, 179, 10, 537):
        raise AssertionError(f"production-shaped Ads cardinality drifted: {counts!r}")


def main() -> None:
    _assert_prices_and_identity_contract()
    _assert_ads_batching_and_partial_mismatch()
    _assert_retry_after_and_bounded_failure()
    _assert_utc_z_digest_identity_and_production_shape()
    print("change_registry_source_acquisition_smoke: ok")


if __name__ == "__main__":
    main()
