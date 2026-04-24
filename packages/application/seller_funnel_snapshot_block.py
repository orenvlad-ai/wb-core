"""Application-слой блока seller funnel snapshot."""

from typing import Any, Iterable, Mapping

from packages.adapters.seller_funnel_snapshot_block import SellerFunnelSnapshotSource
from packages.contracts.seller_funnel_snapshot_block import (
    SellerFunnelSnapshotEnvelope,
    SellerFunnelSnapshotItem,
    SellerFunnelSnapshotNotFound,
    SellerFunnelSnapshotRequest,
    SellerFunnelSnapshotSuccess,
)


def transform_legacy_payload(
    payload: Mapping[str, Any],
    *,
    nm_ids: Iterable[int] | None = None,
) -> SellerFunnelSnapshotEnvelope:
    """Преобразует legacy payload в target contract shape."""

    if "detail" in payload:
        detail = payload["detail"]
        if not isinstance(detail, str):
            raise ValueError("legacy not-found payload must contain string detail")
        return SellerFunnelSnapshotEnvelope(
            result=SellerFunnelSnapshotNotFound(kind="not_found", detail=detail)
        )

    items_raw = payload.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("legacy success payload must contain items list")

    relevant_nm_ids = tuple(nm_ids or ())
    selected_items_raw, filter_note = _select_relevant_items(items_raw, relevant_nm_ids)
    items = [_build_item(item) for item in selected_items_raw]
    success = SellerFunnelSnapshotSuccess(
        kind="success",
        date=_require_str(payload, "date"),
        count=len(items) if relevant_nm_ids else _require_int(payload, "count"),
        items=items,
    )
    if filter_note:
        object.__setattr__(success, "detail", filter_note)
    return SellerFunnelSnapshotEnvelope(
        result=success
    )


def _build_item(item: Mapping[str, Any]) -> SellerFunnelSnapshotItem:
    if not isinstance(item, Mapping):
        raise ValueError("seller funnel item must be object")
    nm_id = _require_int(item, "nm_id")
    try:
        view_count = _require_int(item, "view_count")
        open_card_count = _require_int(item, "open_card_count")
        ctr = _require_int(item, "ctr")
    except ValueError as exc:
        raise ValueError(f"nm_id={nm_id}: {exc}") from exc
    return SellerFunnelSnapshotItem(
        nm_id=nm_id,
        name=_require_str(item, "name"),
        vendor_code=_require_str(item, "vendor_code"),
        view_count=view_count,
        open_card_count=open_card_count,
        ctr=ctr,
    )


def _select_relevant_items(
    items_raw: list[Any],
    nm_ids: tuple[int, ...],
) -> tuple[list[Any], str]:
    if not nm_ids:
        return items_raw, ""

    relevant_nm_ids = {int(item) for item in nm_ids}
    selected: list[Any] = []
    ignored_non_relevant_rows = 0
    ignored_non_relevant_invalid_rows = 0

    for item in items_raw:
        nm_id = _raw_nm_id(item)
        if nm_id in relevant_nm_ids:
            selected.append(item)
            continue
        ignored_non_relevant_rows += 1
        if _raw_item_is_invalid(item):
            ignored_non_relevant_invalid_rows += 1

    note = (
        "seller_funnel_snapshot_filter=enabled_nm_ids; "
        f"raw_rows={len(items_raw)}; "
        f"relevant_rows={len(selected)}; "
        f"ignored_non_relevant_rows={ignored_non_relevant_rows}; "
        f"ignored_non_relevant_invalid_rows={ignored_non_relevant_invalid_rows}"
    )
    return selected, note


def _raw_nm_id(item: Any) -> int | None:
    if not isinstance(item, Mapping):
        return None
    value = item.get("nm_id")
    if not isinstance(value, int):
        return None
    return value


def _raw_item_is_invalid(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return True
    try:
        _build_item(item)
    except ValueError:
        return True
    return False


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


class SellerFunnelSnapshotBlock:
    """Минимальный application-slice для seller funnel snapshot."""

    def __init__(self, source: SellerFunnelSnapshotSource) -> None:
        self._source = source

    def execute(self, request: SellerFunnelSnapshotRequest) -> SellerFunnelSnapshotEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload, nm_ids=request.nm_ids)
