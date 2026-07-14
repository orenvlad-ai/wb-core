"""Targeted smoke for SPP proxy source/extraction semantics."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.spp_proxy_block import (  # noqa: E402
    ArtifactBackedPublicWbCardBuyerPriceSource,
    HttpBackedPublicWbCardBuyerPriceSource,
    extract_public_buyer_price_from_public_card_json,
    extract_public_buyer_price_from_wb_card_html,
)
from packages.application.spp_proxy_block import SppProxyBlock, calculate_spp_proxy  # noqa: E402
from packages.contracts.spp_proxy_block import SppProxyRequest  # noqa: E402


ARTIFACTS = ROOT / "artifacts" / "spp_proxy_block"
HTML_FIXTURE = ARTIFACTS / "public_card" / "card_hydrated__fixture.html"
JSON_FIXTURE = ARTIFACTS / "public_card" / "card_api__fixture.json"


def _assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def _assert_close(actual: float, expected: float, label: str) -> None:
    if abs(actual - expected) > 0.000001:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> None:
    _assert_fixture_extractors()
    _assert_formula_and_percent_normalization()
    _assert_missing_inputs_stay_blank()
    _assert_current_only_source_does_not_fetch_historical()
    _assert_public_api_v4_fallback_after_card_antibot()
    _assert_isolated_destination_override()
    print("spp_proxy_source: ok")


def _assert_fixture_extractors() -> None:
    html = HTML_FIXTURE.read_text(encoding="utf-8")
    html_extracted = extract_public_buyer_price_from_wb_card_html(html, nm_id=210183919)
    _assert_equal(html_extracted.price, 770.0, "hydrated HTML salePriceU extraction")
    if not html_extracted.method.startswith("html_script_json"):
        raise AssertionError(f"HTML extractor must prefer hydrated JSON, got {html_extracted}")

    payload = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    api_extracted = extract_public_buyer_price_from_public_card_json(payload, nm_id=210183919)
    _assert_equal(api_extracted.price, 880.0, "public card API price extraction")
    if "sizes.0.price.total" not in api_extracted.method:
        raise AssertionError(f"API extractor must report stable path, got {api_extracted}")


def _assert_formula_and_percent_normalization() -> None:
    spp_proxy, rub_delta, reason = calculate_spp_proxy(
        price_seller_discounted=1000,
        public_buyer_price=770,
    )
    _assert_equal(reason, "", "formula valid reason")
    _assert_close(spp_proxy or -1, 0.23, "formula ratio")
    _assert_close(rub_delta or -1, 230.0, "formula rub delta")

    result = SppProxyBlock(ArtifactBackedPublicWbCardBuyerPriceSource(ARTIFACTS)).execute(
        SppProxyRequest(
            snapshot_type="spp_proxy",
            snapshot_date="2026-05-06",
            nm_ids=[210183919, 210184534],
            price_seller_discounted_by_nm_id={
                210183919: 1000,
                210184534: 2000,
            },
        )
    ).result
    _assert_equal(result.kind, "success", "SPP proxy result kind")
    values = {item.nm_id: item.spp_proxy for item in result.items}
    _assert_close(values[210183919], 0.23, "0.23 means 23 percent")
    _assert_close(values[210184534], 0.2, "second SPP proxy ratio")


def _assert_missing_inputs_stay_blank() -> None:
    empty_result = SppProxyBlock(ArtifactBackedPublicWbCardBuyerPriceSource(ARTIFACTS)).execute(
        SppProxyRequest(
            snapshot_type="spp_proxy",
            snapshot_date="2026-05-06",
            nm_ids=[210183919],
            price_seller_discounted_by_nm_id={210183919: 1000},
            scenario="empty",
        )
    ).result
    _assert_equal(empty_result.kind, "empty", "missing public price result kind")
    _assert_equal(empty_result.items, [], "missing public price must not fake zero")
    if "missing_public_buyer_price" not in empty_result.detail:
        raise AssertionError(f"missing public price detail mismatch, got {empty_result.detail}")

    missing_seller = SppProxyBlock(ArtifactBackedPublicWbCardBuyerPriceSource(ARTIFACTS)).execute(
        SppProxyRequest(
            snapshot_type="spp_proxy",
            snapshot_date="2026-05-06",
            nm_ids=[210183919],
            price_seller_discounted_by_nm_id={},
        )
    ).result
    _assert_equal(missing_seller.kind, "empty", "missing seller price result kind")
    if "missing_or_zero_price_seller_discounted" not in missing_seller.detail:
        raise AssertionError(f"missing seller price detail mismatch, got {missing_seller.detail}")

    zero_seller = calculate_spp_proxy(price_seller_discounted=0, public_buyer_price=770)
    _assert_equal(zero_seller[2], "missing_or_zero_price_seller_discounted", "zero seller diagnostic")

    negative_case = SppProxyBlock(ArtifactBackedPublicWbCardBuyerPriceSource(ARTIFACTS)).execute(
        SppProxyRequest(
            snapshot_type="spp_proxy",
            snapshot_date="2026-05-06",
            nm_ids=[210183919],
            price_seller_discounted_by_nm_id={210183919: 700},
        )
    ).result
    _assert_equal(negative_case.kind, "empty", "buyer higher than seller result kind")
    if "public_buyer_price_exceeds_price_seller_discounted" not in negative_case.detail:
        raise AssertionError(f"negative SPP diagnostic mismatch, got {negative_case.detail}")


def _assert_current_only_source_does_not_fetch_historical() -> None:
    calls: list[str] = []

    def http_get(url: str, timeout_seconds: float) -> tuple[int, str, dict[str, str]]:
        calls.append(url)
        return 200, HTML_FIXTURE.read_text(encoding="utf-8"), {}

    source = HttpBackedPublicWbCardBuyerPriceSource(
        http_get=http_get,
        business_date_factory=lambda: "2026-05-06",
    )
    historical = source.fetch(
        SppProxyRequest(
            snapshot_type="spp_proxy",
            snapshot_date="2026-05-05",
            nm_ids=[210183919],
        )
    )
    _assert_equal(calls, [], "historical current-only source must not fetch")
    _assert_equal(historical["data"]["items"], [], "historical current-only source items")

    current = source.fetch(
        SppProxyRequest(
            snapshot_type="spp_proxy",
            snapshot_date="2026-05-06",
            nm_ids=[210183919],
        )
    )
    _assert_equal(len(calls), 1, "current source must fetch public card URL")
    _assert_equal(current["data"]["items"][0]["public_buyer_price"], 770.0, "current source buyer price")


def _assert_public_api_v4_fallback_after_card_antibot() -> None:
    calls: list[str] = []
    api_payload = {
        "products": [
            {
                "id": 259460529,
                "sizes": [
                    {
                        "price": {
                            "basic": 169600,
                            "product": 35400,
                        }
                    }
                ],
            }
        ]
    }

    def http_get(url: str, timeout_seconds: float) -> tuple[int, str, dict[str, str]]:
        del timeout_seconds
        calls.append(url)
        if "detail.aspx" in url:
            return 498, '<html><script src="/__wbaas/challenges/antibot/app.js"></script></html>', {"server": "wbaas"}
        if "/cards/v4/detail" in url:
            return 200, json.dumps(api_payload), {"content-type": "application/json"}
        raise AssertionError(f"v4 public card API must be attempted before older fallbacks, got {url}")

    source = HttpBackedPublicWbCardBuyerPriceSource(
        http_get=http_get,
        business_date_factory=lambda: "2026-06-08",
    )
    current = source.fetch(
        SppProxyRequest(
            snapshot_type="spp_proxy",
            snapshot_date="2026-06-08",
            nm_ids=[259460529],
        )
    )
    _assert_equal(len(calls), 2, "card antibot should fall through to v4 API once")
    _assert_equal(current["data"]["items"][0]["public_buyer_price"], 354.0, "v4 sizes.price.product extraction")
    if "/cards/v4/detail" not in calls[1]:
        raise AssertionError(f"v4 public card API must be first fallback, got {calls}")


def _assert_isolated_destination_override() -> None:
    calls: list[str] = []
    api_payload = {
        "products": [{"id": 259460529, "sizes": [{"price": {"product": 35400}}]}]
    }

    def http_get(url: str, timeout_seconds: float) -> tuple[int, str, dict[str, str]]:
        del timeout_seconds
        calls.append(url)
        if "detail.aspx" in url:
            return 498, "", {}
        return 200, json.dumps(api_payload), {"content-type": "application/json"}

    source = HttpBackedPublicWbCardBuyerPriceSource(
        dest="-1257786",
        http_get=http_get,
        business_date_factory=lambda: "2026-06-08",
    )
    request = SppProxyRequest(snapshot_type="spp_proxy", snapshot_date="2026-06-08", nm_ids=[259460529])
    source.for_destination("-6441813").fetch(request)
    if "dest=-6441813" not in calls[1]:
        raise AssertionError(f"retargeted anonymous control did not use requested buyer destination: {calls}")
    calls.clear()
    source.fetch(request)
    if "dest=-1257786" not in calls[1]:
        raise AssertionError("per-read destination override must not mutate the module 35 default source")
    try:
        source.for_destination("-6441813&token=unsafe")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid WB destination context must be rejected")


if __name__ == "__main__":
    main()
