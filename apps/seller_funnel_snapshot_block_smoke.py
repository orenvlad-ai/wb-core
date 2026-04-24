"""Минимальный локальный smoke-check для seller funnel snapshot block."""

from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.seller_funnel_snapshot_block import ArtifactBackedSellerFunnelSnapshotSource
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest


ARTIFACTS = ROOT / "artifacts" / "seller_funnel_snapshot_block"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_case(name: str, target_path: Path) -> None:
    expected_target = _load_json(target_path)
    source = ArtifactBackedSellerFunnelSnapshotSource(ARTIFACTS)
    block = SellerFunnelSnapshotBlock(source)
    request = SellerFunnelSnapshotRequest(
        snapshot_type="sales_funnel_daily",
        date="2026-04-04",
        scenario=name.replace("-", "_"),
    )
    actual_target = asdict(block.execute(request))
    if actual_target != expected_target:
        raise SystemExit(
            f"{name}: smoke-check failed\n"
            f"expected={json.dumps(expected_target, ensure_ascii=False, sort_keys=True)}\n"
            f"actual={json.dumps(actual_target, ensure_ascii=False, sort_keys=True)}"
        )
    print(f"{name}: ok")


def _check_relevant_filter_ignores_invalid_irrelevant_rows() -> None:
    payload = {
        "kind": "success",
        "date": "2026-04-21",
        "count": 3,
        "items": [
            {
                "nm_id": 101,
                "name": "Relevant SKU",
                "vendor_code": "REL-101",
                "view_count": 14982,
                "open_card_count": 1713,
                "ctr": 11,
            },
            {
                "nm_id": 202,
                "name": "Other valid SKU",
                "vendor_code": "OTHER-202",
                "view_count": 1,
                "open_card_count": 1,
                "ctr": 100,
            },
            {
                "nm_id": 303,
                "name": "Irrelevant broken SKU",
                "vendor_code": "BROKEN-303",
                "view_count": None,
                "open_card_count": 1,
                "ctr": None,
            },
        ],
    }
    block = SellerFunnelSnapshotBlock(_StaticSellerFunnelSource(payload))
    result = block.execute(
        SellerFunnelSnapshotRequest(
            snapshot_type="seller_funnel_snapshot",
            date="2026-04-21",
            nm_ids=(101,),
        )
    ).result
    actual = asdict(result)
    if actual["count"] != 1 or len(actual["items"]) != 1:
        raise AssertionError(f"filtered result must include only relevant rows, got {actual}")
    item = actual["items"][0]
    if item["nm_id"] != 101 or item["view_count"] != 14982 or item["open_card_count"] != 1713 or item["ctr"] != 11:
        raise AssertionError(f"relevant row values were not preserved, got {item}")
    note = str(getattr(result, "detail", ""))
    for expected in (
        "seller_funnel_snapshot_filter=enabled_nm_ids",
        "raw_rows=3",
        "relevant_rows=1",
        "ignored_non_relevant_rows=2",
        "ignored_non_relevant_invalid_rows=1",
    ):
        if expected not in note:
            raise AssertionError(f"filter diagnostic {expected!r} missing from {note!r}")
    print("relevant-filter-invalid-irrelevant: ok")


def _check_relevant_invalid_rows_stay_strict() -> None:
    payload = {
        "kind": "success",
        "date": "2026-04-21",
        "count": 1,
        "items": [
            {
                "nm_id": 101,
                "name": "Relevant broken SKU",
                "vendor_code": "REL-101",
                "view_count": None,
                "open_card_count": 1713,
                "ctr": 11,
            },
        ],
    }
    block = SellerFunnelSnapshotBlock(_StaticSellerFunnelSource(payload))
    try:
        block.execute(
            SellerFunnelSnapshotRequest(
                snapshot_type="seller_funnel_snapshot",
                date="2026-04-21",
                nm_ids=(101,),
            )
        )
    except ValueError as exc:
        if "nm_id=101: view_count must be int" not in str(exc):
            raise AssertionError(f"relevant invalid row must keep explicit strict error, got {exc}") from exc
    else:
        raise AssertionError("relevant invalid seller funnel rows must not be silently ignored")
    print("relevant-filter-invalid-relevant: ok")


def main() -> None:
    _check_case(
        "normal",
        ARTIFACTS / "target" / "normal__template__target__fixture.json",
    )
    _check_case(
        "not-found",
        ARTIFACTS / "target" / "not-found__template__target__fixture.json",
    )
    _check_relevant_filter_ignores_invalid_irrelevant_rows()
    _check_relevant_invalid_rows_stay_strict()
    print("smoke-check passed")


class _StaticSellerFunnelSource:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload

    def fetch(self, request: SellerFunnelSnapshotRequest) -> Mapping[str, Any]:
        return self.payload


if __name__ == "__main__":
    main()
