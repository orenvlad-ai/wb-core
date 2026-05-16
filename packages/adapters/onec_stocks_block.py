"""Adapter boundary for the bounded 1C/Soykasoft WB stocks source."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib import error, parse, request as urllib_request

from packages.contracts.onec_stocks_block import (
    ONEC_STOCKS_PARTIAL_FETCH_META_KEY,
    OnecStocksRequest,
)


ONEC_STOCKS_BASE_URL_ENV = "ONEC_STOCKS_BASE_URL"
ONEC_STOCKS_BASIC_USER_ENV = "ONEC_STOCKS_BASIC_USER"
ONEC_STOCKS_BASIC_PASSWORD_ENV = "ONEC_STOCKS_BASIC_PASSWORD"
ONEC_STOCKS_TOKEN_ENV = "ONEC_STOCKS_TOKEN"
ONEC_STOCKS_TIMEOUT_SECONDS_ENV = "ONEC_STOCKS_TIMEOUT_SECONDS"
ONEC_STOCKS_SMOKE_ACCOUNT_ID_ENV = "ONEC_STOCKS_SMOKE_ACCOUNT_ID"
ONEC_STOCKS_SMOKE_NM_ID_ENV = "ONEC_STOCKS_SMOKE_NM_ID"
ONEC_STOCKS_ENDPOINT_PATH = "/hs/soykasoft/stocks_wb"
DEFAULT_ONEC_STOCKS_TIMEOUT_SECONDS = 30.0


class OnecStocksSource(Protocol):
    """Source interface for raw 1C stocks payloads."""

    def fetch(self, request: OnecStocksRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedOnecStocksSource:
    """Local fixture-backed source for parser and normalization smokes."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: OnecStocksRequest) -> Mapping[str, Any]:
        path = self._resolve_source_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_source_path(self, scenario: str) -> Path:
        if scenario == "success":
            return self._artifacts_root / "source" / "success__stocks_wb__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class OnecStocksRuntimeError(RuntimeError):
    """Runtime-boundary error that reports env names only, never secret values."""


class OnecStocksHttpError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"1C stocks upstream returned HTTP {status_code}")


@dataclass(frozen=True)
class OnecStocksRuntimeConfig:
    base_url: str
    basic_user: str = field(repr=False)
    basic_password: str = field(repr=False)
    token: str = field(repr=False)
    timeout_seconds: float = DEFAULT_ONEC_STOCKS_TIMEOUT_SECONDS


@dataclass(frozen=True)
class _OnecStocksFetchFailure:
    nm_id: int
    kind: str
    status_code: int | None = None


class HttpBackedOnecStocksSource:
    """HTTP adapter for the confirmed 1C `/hs/soykasoft/stocks_wb` method."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._opener = opener or urllib_request.urlopen

    def fetch(self, request: OnecStocksRequest) -> Mapping[str, Any]:
        runtime = load_onec_stocks_runtime_config()
        nm_ids = sorted({int(nm_id) for nm_id in request.nm_ids})
        if not nm_ids:
            raise ValueError("1C stocks live adapter requires at least one nmId")

        if len(nm_ids) > 1:
            account_payload = self._fetch_account_snapshot(
                runtime=runtime,
                account_id=request.account_id,
                requested_nm_ids=nm_ids,
                failures=[],
                fallback_label="account_snapshot_primary",
            )
            if account_payload is not None:
                return account_payload

        payloads: list[Mapping[str, Any]] = []
        failures: list[_OnecStocksFetchFailure] = []
        for nm_id in nm_ids:
            try:
                payloads.append(
                    self._fetch_one(runtime=runtime, account_id=request.account_id, nm_id=nm_id)
                )
            except OnecStocksHttpError as exc:
                failures.append(
                    _OnecStocksFetchFailure(
                        nm_id=nm_id,
                        kind="http",
                        status_code=exc.status_code,
                    )
                )
            except OnecStocksRuntimeError:
                failures.append(_OnecStocksFetchFailure(nm_id=nm_id, kind="runtime"))

        if not payloads:
            fallback_payload = self._fetch_account_snapshot(
                runtime=runtime,
                account_id=request.account_id,
                requested_nm_ids=nm_ids,
                failures=failures,
                fallback_label="account_snapshot_after_per_sku_failure",
            )
            if fallback_payload is not None:
                return fallback_payload
            raise _all_requested_nm_ids_failed_error(nm_ids=nm_ids, failures=failures)
        payload = payloads[0] if len(payloads) == 1 else _merge_onec_stock_payloads(payloads)
        if not failures:
            return payload
        payload_with_partial_meta = dict(payload)
        payload_with_partial_meta[ONEC_STOCKS_PARTIAL_FETCH_META_KEY] = {
            "requested_count": len(nm_ids),
            "requested_nm_ids": nm_ids,
            "successful_request_count": len(payloads),
            "failure_count": len(failures),
            "missing_nm_ids": [failure.nm_id for failure in failures],
            "status_codes": _status_code_counts(failures),
            "error_kinds": _kind_counts(failures),
        }
        return payload_with_partial_meta

    def _fetch_account_snapshot(
        self,
        *,
        runtime: OnecStocksRuntimeConfig,
        account_id: str,
        requested_nm_ids: list[int],
        failures: list[_OnecStocksFetchFailure],
        fallback_label: str,
    ) -> Mapping[str, Any] | None:
        try:
            payload = self._fetch_one(runtime=runtime, account_id=account_id, nm_id=None)
        except (OnecStocksHttpError, OnecStocksRuntimeError):
            return None

        filtered_payload = _filter_onec_stock_payload_items(
            payload,
            requested_nm_ids=requested_nm_ids,
        )
        missing_nm_ids = _missing_requested_nm_ids_from_payload(
            filtered_payload,
            requested_nm_ids=requested_nm_ids,
        )
        if len(missing_nm_ids) == len(requested_nm_ids):
            return None
        if not missing_nm_ids:
            return filtered_payload
        payload_with_partial_meta = dict(filtered_payload)
        payload_with_partial_meta[ONEC_STOCKS_PARTIAL_FETCH_META_KEY] = {
            "requested_count": len(requested_nm_ids),
            "requested_nm_ids": requested_nm_ids,
            "successful_request_count": 1,
            "failure_count": len(missing_nm_ids),
            "missing_nm_ids": missing_nm_ids,
            "status_codes": _status_code_counts(failures),
            "error_kinds": _kind_counts(failures),
            "fallback": fallback_label,
        }
        return payload_with_partial_meta

    def _fetch_one(
        self,
        *,
        runtime: OnecStocksRuntimeConfig,
        account_id: str,
        nm_id: int | None,
    ) -> Mapping[str, Any]:
        url = _build_onec_stocks_url(
            base_url=runtime.base_url,
            account_id=account_id,
            nm_id=nm_id,
        )
        http_request = urllib_request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": _basic_auth_header(
                    runtime.basic_user,
                    runtime.basic_password,
                ),
                "token": runtime.token,
            },
            method="GET",
        )
        try:
            with self._opener(http_request, timeout=runtime.timeout_seconds) as response:
                raw_payload = response.read()
        except error.HTTPError as exc:
            raise OnecStocksHttpError(exc.code) from exc
        except (TimeoutError, error.URLError) as exc:
            raise OnecStocksRuntimeError("1C stocks upstream transport error") from exc

        try:
            payload = json.loads(raw_payload.decode("utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise OnecStocksRuntimeError("1C stocks upstream returned non-JSON payload") from exc

        if not isinstance(payload, Mapping):
            raise OnecStocksRuntimeError("1C stocks upstream JSON root must be an object")
        return payload


def load_onec_stocks_runtime_config() -> OnecStocksRuntimeConfig:
    missing = missing_onec_stocks_live_env()
    if missing:
        raise OnecStocksRuntimeError(
            "missing required 1C stocks env: " + ", ".join(missing)
        )
    return OnecStocksRuntimeConfig(
        base_url=_require_env(ONEC_STOCKS_BASE_URL_ENV).rstrip("/"),
        basic_user=_require_env(ONEC_STOCKS_BASIC_USER_ENV),
        basic_password=_require_env(ONEC_STOCKS_BASIC_PASSWORD_ENV),
        token=_require_env(ONEC_STOCKS_TOKEN_ENV),
        timeout_seconds=_read_timeout_seconds(),
    )


def missing_onec_stocks_live_env() -> list[str]:
    required = [
        ONEC_STOCKS_BASE_URL_ENV,
        ONEC_STOCKS_BASIC_USER_ENV,
        ONEC_STOCKS_BASIC_PASSWORD_ENV,
        ONEC_STOCKS_TOKEN_ENV,
    ]
    return [name for name in required if not os.environ.get(name, "").strip()]


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise OnecStocksRuntimeError(f"required env {name} is not set")
    return value


def _read_timeout_seconds() -> float:
    raw_value = os.environ.get(ONEC_STOCKS_TIMEOUT_SECONDS_ENV, "").strip()
    if not raw_value:
        return DEFAULT_ONEC_STOCKS_TIMEOUT_SECONDS
    try:
        timeout_seconds = float(raw_value)
    except ValueError as exc:
        raise OnecStocksRuntimeError(
            f"{ONEC_STOCKS_TIMEOUT_SECONDS_ENV} must be numeric seconds"
        ) from exc
    if timeout_seconds <= 0:
        raise OnecStocksRuntimeError(f"{ONEC_STOCKS_TIMEOUT_SECONDS_ENV} must be > 0")
    return timeout_seconds


def _build_onec_stocks_url(*, base_url: str, account_id: str, nm_id: int | None) -> str:
    params = {"account_id": account_id}
    if nm_id is not None:
        params["nmId"] = str(nm_id)
    query = parse.urlencode(params)
    return f"{base_url.rstrip('/')}{ONEC_STOCKS_ENDPOINT_PATH}?{query}"


def _basic_auth_header(user: str, password: str) -> str:
    raw_value = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw_value).decode("ascii")


def _merge_onec_stock_payloads(payloads: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not payloads:
        raise OnecStocksRuntimeError("1C stocks payload merge requires at least one payload")
    first = payloads[0]
    meta = first.get("meta")
    if not isinstance(meta, Mapping):
        raise OnecStocksRuntimeError("1C stocks upstream JSON meta must be an object")

    merged_items: list[Any] = []
    for payload in payloads:
        payload_meta = payload.get("meta")
        if not isinstance(payload_meta, Mapping):
            raise OnecStocksRuntimeError("1C stocks upstream JSON meta must be an object")
        items = payload.get("items")
        if not isinstance(items, list):
            raise OnecStocksRuntimeError("1C stocks upstream JSON items must be a list")
        merged_items.extend(items)
    return {"meta": dict(meta), "items": merged_items}


def _filter_onec_stock_payload_items(
    payload: Mapping[str, Any],
    *,
    requested_nm_ids: list[int],
) -> Mapping[str, Any]:
    requested = {str(item) for item in requested_nm_ids}
    items = payload.get("items")
    if not isinstance(items, list):
        raise OnecStocksRuntimeError("1C stocks upstream JSON items must be a list")
    filtered_items = [
        item
        for item in items
        if isinstance(item, Mapping) and str(item.get("nmId") or "").strip() in requested
    ]
    result = dict(payload)
    result["items"] = filtered_items
    return result


def _missing_requested_nm_ids_from_payload(
    payload: Mapping[str, Any],
    *,
    requested_nm_ids: list[int],
) -> list[int]:
    items = payload.get("items")
    if not isinstance(items, list):
        return sorted(set(requested_nm_ids))
    covered = {
        int(str(item.get("nmId")).strip())
        for item in items
        if isinstance(item, Mapping) and str(item.get("nmId") or "").strip().isdigit()
    }
    return sorted(set(requested_nm_ids) - covered)


def _all_requested_nm_ids_failed_error(
    *,
    nm_ids: list[int],
    failures: list[_OnecStocksFetchFailure],
) -> OnecStocksRuntimeError:
    return OnecStocksRuntimeError(
        "1C stocks upstream failed for all requested nmIds; "
        f"requested_count={len(nm_ids)}; "
        f"failure_count={len(failures)}; "
        f"status_codes={_format_count_mapping(_status_code_counts(failures))}; "
        f"error_kinds={_format_count_mapping(_kind_counts(failures))}"
    )


def _status_code_counts(failures: list[_OnecStocksFetchFailure]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for failure in failures:
        if failure.status_code is None:
            continue
        key = str(failure.status_code)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _kind_counts(failures: list[_OnecStocksFetchFailure]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for failure in failures:
        key = failure.kind.strip() or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _format_count_mapping(value: Mapping[str, int]) -> str:
    if not value:
        return "none"
    return ",".join(f"{key}:{value[key]}" for key in sorted(value))
