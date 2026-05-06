"""Targeted smoke for SPP source correctness semantics."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.spp_block import (  # noqa: E402
    HttpBackedSppSource,
    SellerPortalDiscountOnSiteSppSource,
    _discount_on_site_goods_to_spp_items,
    _normalize_discount_on_site,
)
from packages.application.spp_block import SppBlock  # noqa: E402
from packages.contracts.spp_block import SppRequest  # noqa: E402


def _assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def _assert_close(actual: float, expected: float, label: str) -> None:
    if abs(actual - expected) > 0.000001:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def test_discount_on_site_normalization() -> None:
    cases = [
        (23, 0.23),
        (23.0, 0.23),
        (0.23, 0.23),
        (30, 0.30),
    ]
    for raw, expected in cases:
        _assert_close(_normalize_discount_on_site(raw) or -1, expected, f"discountOnSite {raw!r}")

    _assert_equal(_normalize_discount_on_site(None), None, "missing discountOnSite")
    _assert_equal(_normalize_discount_on_site("23"), None, "string discountOnSite")
    _assert_equal(_normalize_discount_on_site(-1), None, "negative discountOnSite")


def test_goods_to_items() -> None:
    goods = [
        {"nmID": 210183919, "discountOnSite": 23},
        {"nmID": 210184534, "discountOnSite": 30},
        {"nmID": 111, "discountOnSite": 99},
        {"nmID": 222, "discountOnSite": None},
    ]
    items = _discount_on_site_goods_to_spp_items(goods, [210184534, 210183919, 222])
    _assert_equal(
        items,
        [
            {"nmId": 210183919, "spp_avg": 0.23, "spp_count": 1, "source_field": "discountOnSite"},
            {"nmId": 210184534, "spp_avg": 0.3, "spp_count": 1, "source_field": "discountOnSite"},
        ],
        "discountOnSite goods mapping",
    )


def test_current_only_source() -> None:
    calls: list[list[int]] = []

    def fetcher(nm_ids: list[int]) -> list[dict[str, object]]:
        calls.append(nm_ids)
        return [{"nmID": 210183919, "discountOnSite": 23}]

    source = SellerPortalDiscountOnSiteSppSource(
        goods_fetcher=fetcher,
        business_date_factory=lambda: "2026-05-06",
    )
    app = SppBlock(source)
    current = app.execute(
        SppRequest(snapshot_type="spp", snapshot_date="2026-05-06", nm_ids=[210183919])
    ).result
    _assert_equal(current.kind, "success", "current source result kind")
    _assert_equal(current.count, 1, "current source count")
    _assert_close(current.items[0].spp, 0.23, "current source spp")
    _assert_equal(calls, [[210183919]], "current source must call fetcher")

    historical = app.execute(
        SppRequest(snapshot_type="spp", snapshot_date="2026-05-05", nm_ids=[210183919])
    ).result
    _assert_equal(historical.kind, "empty", "historical current-only result kind")
    _assert_equal(historical.count, 0, "historical current-only count")
    _assert_equal(calls, [[210183919]], "historical date must not call current fetcher")


def test_http_source_routes_to_configured_seller_portal_source() -> None:
    source = SellerPortalDiscountOnSiteSppSource(
        goods_fetcher=lambda nm_ids: [{"nmID": nm_ids[0], "discountOnSite": 23}],
        business_date_factory=lambda: "2026-05-06",
    )
    routed = HttpBackedSppSource(
        source_mode="seller_portal_discount_on_site",
        seller_portal_source=source,
    )
    result = SppBlock(routed).execute(
        SppRequest(snapshot_type="spp", snapshot_date="2026-05-06", nm_ids=[210183919])
    ).result
    _assert_equal(result.kind, "success", "routed source result kind")
    _assert_close(result.items[0].spp, 0.23, "routed source spp")


def main() -> None:
    test_discount_on_site_normalization()
    test_goods_to_items()
    test_current_only_source()
    test_http_source_routes_to_configured_seller_portal_source()
    print("spp_source_correctness: ok")


if __name__ == "__main__":
    main()
