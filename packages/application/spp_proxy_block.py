"""Application layer for SPP proxy metric."""

from __future__ import annotations

from typing import Any, Mapping

from packages.adapters.spp_proxy_block import SppProxySource
from packages.contracts.spp_proxy_block import (
    SppProxyEmpty,
    SppProxyEnvelope,
    SppProxyIncomplete,
    SppProxyItem,
    SppProxyRequest,
    SppProxySuccess,
)


def calculate_spp_proxy(
    *,
    price_seller_discounted: Any,
    public_buyer_price: Any,
) -> tuple[float | None, float | None, str]:
    seller_price = _to_positive_float(price_seller_discounted)
    if seller_price is None:
        return None, None, "missing_or_zero_price_seller_discounted"
    buyer_price = _to_positive_float(public_buyer_price)
    if buyer_price is None:
        return None, None, "missing_public_buyer_price"
    rub_delta = seller_price - buyer_price
    if rub_delta < 0:
        return None, None, "public_buyer_price_exceeds_price_seller_discounted"
    return round(rub_delta / seller_price, 6), round(rub_delta, 2), ""


def transform_public_card_price_payload(
    payload: Mapping[str, Any],
    request: SppProxyRequest,
) -> SppProxyEnvelope:
    snapshot_date = _require_str(payload, "snapshot_date")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("public card payload must contain data object")
    items_raw = data.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("public card payload must contain data.items list")

    buyer_price_by_nm_id: dict[int, Any] = {}
    public_diagnostics_by_nm_id: dict[int, Any] = {}
    for item in items_raw:
        if not isinstance(item, Mapping):
            raise ValueError("public card item must be object")
        nm_id = _require_int(item, "nmId")
        buyer_price_by_nm_id[nm_id] = item.get("public_buyer_price")
        public_diagnostics_by_nm_id[nm_id] = item.get("diagnostics") or {}

    result_items: list[SppProxyItem] = []
    missing_reasons: dict[int, str] = {}
    diagnostics_by_nm_id: dict[str, Any] = {}
    for nm_id in request.nm_ids:
        seller_price_raw = request.price_seller_discounted_by_nm_id.get(nm_id)
        public_price_raw = buyer_price_by_nm_id.get(nm_id)
        spp_proxy, spp_proxy_rub, reason = calculate_spp_proxy(
            price_seller_discounted=seller_price_raw,
            public_buyer_price=public_price_raw,
        )
        if spp_proxy is None or spp_proxy_rub is None:
            missing_reasons[nm_id] = reason
            diagnostics_by_nm_id[str(nm_id)] = {
                "reason": reason,
                "price_seller_discounted": seller_price_raw,
                "public_buyer_price": public_price_raw,
                "public_source": public_diagnostics_by_nm_id.get(nm_id) or {},
            }
            continue
        seller_price = float(seller_price_raw)
        public_price = float(public_price_raw)
        result_items.append(
            SppProxyItem(
                nm_id=nm_id,
                spp_proxy=spp_proxy,
                price_seller_discounted=round(seller_price, 2),
                public_buyer_price=round(public_price, 2),
                spp_proxy_rub=spp_proxy_rub,
            )
        )
        diagnostics_by_nm_id[str(nm_id)] = {
            "reason": "ok",
            "price_seller_discounted": round(seller_price, 2),
            "public_buyer_price": round(public_price, 2),
            "spp_proxy_rub": spp_proxy_rub,
            "public_source": public_diagnostics_by_nm_id.get(nm_id) or {},
        }

    requested_count = len(request.nm_ids)
    covered_count = len(result_items)
    missing_nm_ids = sorted(set(request.nm_ids) - {item.nm_id for item in result_items})
    diagnostics = {
        "requested_count": requested_count,
        "covered_count": covered_count,
        "missing_count": len(missing_nm_ids),
        "missing_reasons": {str(key): value for key, value in sorted(missing_reasons.items())},
        "items": diagnostics_by_nm_id,
        "source": payload.get("source") or {},
        "source_diagnostics": payload.get("diagnostics") or {},
    }

    if covered_count == requested_count:
        return SppProxyEnvelope(
            result=SppProxySuccess(
                kind="success",
                snapshot_date=snapshot_date,
                count=covered_count,
                requested_count=requested_count,
                covered_count=covered_count,
                items=result_items,
                detail="SPP proxy calculated from seller discounted price and public buyer price",
                diagnostics=diagnostics,
            )
        )
    detail = _build_detail(missing_reasons)
    if covered_count > 0:
        return SppProxyEnvelope(
            result=SppProxyIncomplete(
                kind="incomplete",
                snapshot_date=snapshot_date,
                count=covered_count,
                requested_count=requested_count,
                covered_count=covered_count,
                items=result_items,
                missing_nm_ids=missing_nm_ids,
                detail=detail,
                diagnostics=diagnostics,
            )
        )
    return SppProxyEnvelope(
        result=SppProxyEmpty(
            kind="empty",
            snapshot_date=snapshot_date,
            count=0,
            requested_count=requested_count,
            covered_count=0,
            items=[],
            detail=detail,
            diagnostics=diagnostics,
        )
    )


class SppProxyBlock:
    """Server-owned SPP proxy application block."""

    def __init__(self, source: SppProxySource) -> None:
        self._source = source

    def execute(self, request: SppProxyRequest) -> SppProxyEnvelope:
        payload = self._source.fetch(request)
        return transform_public_card_price_payload(payload, request)


def _build_detail(missing_reasons: Mapping[int, str]) -> str:
    if not missing_reasons:
        return ""
    counts: dict[str, int] = {}
    for reason in missing_reasons.values():
        counts[reason] = counts.get(reason, 0) + 1
    return "; ".join(f"{reason}={counts[reason]}" for reason in sorted(counts))


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be int")
    return value


def _to_positive_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return numeric
