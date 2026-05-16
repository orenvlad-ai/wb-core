"""Fixture-backed smoke for the bounded 1C/Soykasoft WB stocks source."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from urllib import error, parse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.onec_stocks_block import (
    ArtifactBackedOnecStocksSource,
    HttpBackedOnecStocksSource,
)
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


def _check_http_account_snapshot_fallback(payload: dict) -> None:
    opener = _PerSkuUnauthorizedThenAccountSnapshot(payload)
    with _temporary_onec_live_env():
        block = OnecStocksBlock(HttpBackedOnecStocksSource(opener=opener))
        full_result = block.execute(
            OnecStocksRequest(
                snapshot_type="onec_stocks",
                account_id=str(payload["meta"]["account_id"]),
                nm_ids=[428855306],
            )
        ).result
        if full_result.kind != "success" or full_result.stage_count != 3:
            raise AssertionError(f"fallback full coverage must be success, got {full_result}")

    account_snapshot_urls = [
        url
        for url in opener.urls
        if "nmId=" not in parse.urlparse(url).query
    ]
    if len(account_snapshot_urls) != 1:
        raise AssertionError(
            f"expected one account snapshot fallback request, got {account_snapshot_urls}"
        )
    if any("account_id=000000001" not in url for url in account_snapshot_urls):
        raise AssertionError(f"unexpected account snapshot URLs: {account_snapshot_urls}")
    print("http-account-snapshot-fallback: ok")


def _check_http_account_snapshot_primary_multi_sku(payload: dict) -> None:
    opener = _AccountSnapshotOnly(payload)
    with _temporary_onec_live_env():
        block = OnecStocksBlock(HttpBackedOnecStocksSource(opener=opener))
        partial_result = block.execute(
            OnecStocksRequest(
                snapshot_type="onec_stocks",
                account_id=str(payload["meta"]["account_id"]),
                nm_ids=[428855306, 210183919],
            )
        ).result

    if partial_result.kind != "incomplete":
        raise AssertionError(f"primary account snapshot partial coverage must be incomplete, got {partial_result}")
    if partial_result.requested_count != 2 or partial_result.covered_count != 1:
        raise AssertionError(
            "unexpected primary account snapshot partial counts: "
            f"requested={partial_result.requested_count}, covered={partial_result.covered_count}"
        )
    if partial_result.missing_nm_ids != [210183919]:
        raise AssertionError(
            f"unexpected primary account snapshot partial missing nmIds: {partial_result.missing_nm_ids}"
        )
    if "status_codes=" in partial_result.detail or "account_snapshot_primary" not in partial_result.detail:
        raise AssertionError(
            "primary account snapshot partial detail must not invent per-SKU failure counts, "
            f"got {partial_result.detail}"
        )
    account_snapshot_urls = [
        url
        for url in opener.urls
        if "nmId=" not in parse.urlparse(url).query
    ]
    if len(account_snapshot_urls) != 1:
        raise AssertionError(
            f"expected one primary account snapshot request, got {account_snapshot_urls}"
        )
    if any("account_id=000000001" not in url for url in account_snapshot_urls):
        raise AssertionError(f"unexpected account snapshot URLs: {account_snapshot_urls}")
    if any("nmId=" in parse.urlparse(url).query for url in opener.urls):
        raise AssertionError(f"multi-SKU account snapshot must avoid per-SKU URLs first, got {opener.urls}")
    print("http-account-snapshot-primary-multi-sku: ok")


class _StaticOnecStocksSource:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def fetch(self, request: OnecStocksRequest) -> dict:
        return deepcopy(self._payload)


class _PerSkuUnauthorizedThenAccountSnapshot:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.urls: list[str] = []

    def __call__(self, http_request, timeout: float):
        del timeout
        url = http_request.get_full_url()
        self.urls.append(url)
        parsed_url = parse.urlparse(url)
        query = parse.parse_qs(parsed_url.query)
        if "nmId" in query:
            raise error.HTTPError(url, 401, "unauthorized", hdrs=None, fp=None)
        return _FakeHttpResponse(self._payload)


class _AccountSnapshotOnly:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.urls: list[str] = []

    def __call__(self, http_request, timeout: float):
        del timeout
        url = http_request.get_full_url()
        self.urls.append(url)
        parsed_url = parse.urlparse(url)
        query = parse.parse_qs(parsed_url.query)
        if "nmId" in query:
            raise AssertionError(f"unexpected per-SKU request before account snapshot: {url}")
        return _FakeHttpResponse(self._payload)


class _FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _temporary_onec_live_env:
    _VALUES = {
        "ONEC_STOCKS_BASE_URL": "https://onec.example",
        "ONEC_STOCKS_BASIC_USER": "user",
        "ONEC_STOCKS_BASIC_PASSWORD": "password",
        "ONEC_STOCKS_TOKEN": "token",
    }

    def __enter__(self):
        self._previous = {name: os.environ.get(name) for name in self._VALUES}
        os.environ.update(self._VALUES)
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


def main() -> None:
    payload = _load_fixture()
    _check_parser(payload)
    _check_dynamic_stage_parser(payload)
    _check_normalization(payload)
    _check_mapping_boundary(payload)
    _check_block(payload)
    _check_partial_block(payload)
    _check_http_account_snapshot_fallback(payload)
    _check_http_account_snapshot_primary_multi_sku(payload)
    print("smoke-check passed")


if __name__ == "__main__":
    main()
