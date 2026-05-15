"""Fixture-backed smoke for the bounded 1C/Soykasoft WB stocks source."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.onec_stocks_block import ArtifactBackedOnecStocksSource
from packages.application.onec_stocks_block import (
    OnecStocksBlock,
    normalize_onec_stocks_payload,
    parse_onec_stocks_payload,
)
from packages.contracts.onec_stocks_block import (
    ONEC_STOCKS_PARTIAL_FETCH_META_KEY,
    OnecStocksRequest,
)


ARTIFACTS = ROOT / "artifacts" / "onec_stocks_block"
EXPECTED_STAGE_NAMES = ["В_пути", "ВБ", "Фулфиллмент"]


def _load_fixture() -> dict:
    source = ArtifactBackedOnecStocksSource(ARTIFACTS)
    return dict(
        source.fetch(
            OnecStocksRequest(
                snapshot_type="onec_stocks",
                account_id="000000001",
                nm_ids=[428855306],
            )
        )
    )


def _check_parser(payload: dict) -> None:
    parsed = parse_onec_stocks_payload(payload)
    if parsed.meta.version != "1.0":
        raise AssertionError(f"unexpected meta.version: {parsed.meta.version}")
    if parsed.meta.currency != "RUB":
        raise AssertionError(f"unexpected meta.currency: {parsed.meta.currency}")
    if len(parsed.items) != 1:
        raise AssertionError(f"unexpected item count: {len(parsed.items)}")
    stage_names = list(parsed.items[0].stages)
    if stage_names != EXPECTED_STAGE_NAMES:
        raise AssertionError(f"unexpected fixture stages: {stage_names}")
    print("parser: ok")


def _check_dynamic_stage_parser(payload: dict) -> None:
    dynamic_payload = deepcopy(payload)
    dynamic_stage_name = "Новая секция 1С / тест"
    dynamic_payload["items"][0]["stages"][dynamic_stage_name] = {
        "qty": 1,
        "unit_cost_rub": 2.0,
        "cost_total_rub": 2.0,
    }
    parsed = parse_onec_stocks_payload(dynamic_payload)
    if dynamic_stage_name not in parsed.items[0].stages:
        raise AssertionError("parser must preserve previously unknown 1C stage names")
    print("dynamic-stage-parser: ok")


def _check_normalization(payload: dict) -> None:
    envelope = normalize_onec_stocks_payload(payload)
    result = envelope.result
    if result.kind != "success":
        raise AssertionError(f"unexpected result kind: {result.kind}")
    if result.item_count != 1 or result.stage_count != 3:
        raise AssertionError(
            f"unexpected counts: item_count={result.item_count}, stage_count={result.stage_count}"
        )
    if result.dynamic_stage_names != EXPECTED_STAGE_NAMES:
        raise AssertionError(f"unexpected dynamic stages: {result.dynamic_stage_names}")
    if [row.stage_name for row in result.items] != EXPECTED_STAGE_NAMES:
        raise AssertionError("normalizer must keep source stage rows separate")
    if any(row.canonical_stage_code is not None for row in result.items):
        raise AssertionError("canonical stage codes must be empty without mapping config")
    print("normalization: ok")


def _check_mapping_boundary(payload: dict) -> None:
    envelope = normalize_onec_stocks_payload(
        payload,
        stage_mapping={"ВБ": "WB_STOCK"},
    )
    result = envelope.result
    if result.kind != "success":
        raise AssertionError(f"unexpected mapped result kind: {result.kind}")
    if result.stage_count != 3:
        raise AssertionError("stage mapping boundary must not aggregate source stages")
    mapped_rows = {row.stage_name: row.canonical_stage_code for row in result.items}
    if mapped_rows.get("ВБ") != "WB_STOCK":
        raise AssertionError(f"expected WB_STOCK mapping, got {mapped_rows.get('ВБ')}")
    if mapped_rows.get("В_пути") is not None or mapped_rows.get("Фулфиллмент") is not None:
        raise AssertionError("unmapped source stages must stay unmapped")
    print("stage-mapping-boundary: ok")


def _check_block(payload: dict) -> None:
    source = ArtifactBackedOnecStocksSource(ARTIFACTS)
    block = OnecStocksBlock(source)
    result = block.execute(
        OnecStocksRequest(
            snapshot_type="onec_stocks",
            account_id=str(payload["meta"]["account_id"]),
            nm_ids=[428855306],
        )
    ).result
    if result.kind != "success" or result.stage_count != 3:
        raise AssertionError(f"unexpected block result: {result}")
    print("block: ok")


def _check_partial_block(payload: dict) -> None:
    partial_payload = deepcopy(payload)
    partial_payload[ONEC_STOCKS_PARTIAL_FETCH_META_KEY] = {
        "requested_count": 2,
        "requested_nm_ids": [428855306, 210183919],
        "successful_request_count": 1,
        "failure_count": 1,
        "missing_nm_ids": [210183919],
        "status_codes": {"401": 1},
        "error_kinds": {"http": 1},
    }
    block = OnecStocksBlock(_StaticOnecStocksSource(partial_payload))
    result = block.execute(
        OnecStocksRequest(
            snapshot_type="onec_stocks",
            account_id=str(payload["meta"]["account_id"]),
            nm_ids=[428855306, 210183919],
        )
    ).result
    if result.kind != "incomplete":
        raise AssertionError(f"partial block must return incomplete, got {result.kind}")
    if result.requested_count != 2 or result.covered_count != 1:
        raise AssertionError(
            f"unexpected partial counts: requested={result.requested_count}, covered={result.covered_count}"
        )
    if result.missing_nm_ids != [210183919]:
        raise AssertionError(f"unexpected partial missing nmIds: {result.missing_nm_ids}")
    if result.snapshot_date != str(payload["meta"]["date"]):
        raise AssertionError(f"partial snapshot date mismatch: {result.snapshot_date}")
    if "status_codes=401:1" not in result.detail:
        raise AssertionError(f"partial detail must keep sanitized status code counts, got {result.detail}")
    print("partial-block: ok")


class _StaticOnecStocksSource:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def fetch(self, request: OnecStocksRequest) -> dict:
        return deepcopy(self._payload)


def main() -> None:
    payload = _load_fixture()
    _check_parser(payload)
    _check_dynamic_stage_parser(payload)
    _check_normalization(payload)
    _check_mapping_boundary(payload)
    _check_block(payload)
    _check_partial_block(payload)
    print("smoke-check passed")


if __name__ == "__main__":
    main()
