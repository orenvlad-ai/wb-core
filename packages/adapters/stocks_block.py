"""Адаптерная граница блока stocks."""

import json
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import load_runtime_config
from packages.contracts.stocks_block import StocksRequest


class StocksSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: StocksRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedStocksSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: StocksRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "partial":
            return self._artifacts_root / "legacy" / "partial__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedStocksSource:
    """Минимальный HTTP adapter к official stocks endpoint."""

    def __init__(
        self,
        base_url: str = "https://seller-analytics-api.wildberries.ru",
        token_env_var: str = "WB_TOKEN",
        base_url_env_var: str = "WB_SELLER_ANALYTICS_API_BASE_URL",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds

    def fetch(self, request: StocksRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        rows: list[Mapping[str, Any]] = []
        snapshot_ts = f"{request.snapshot_date} 21:30:00"
        for nm_id in request.nm_ids:
            response_payload = self._post_sizes(
                base_url=runtime.base_url,
                token=runtime.token,
                snapshot_date=request.snapshot_date,
                nm_id=nm_id,
                timeout_seconds=runtime.timeout_seconds,
            )
            rows.extend(
                self._parse_response_to_rows(
                    payload=response_payload,
                    snapshot_date=request.snapshot_date,
                    snapshot_ts=snapshot_ts,
                    nm_id=nm_id,
                )
            )
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "data": {
                "rows": rows,
            },
        }

    def _post_sizes(
        self,
        *,
        base_url: str,
        token: str,
        snapshot_date: str,
        nm_id: int,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        body = json.dumps(
            {
                "nmID": nm_id,
                "currentPeriod": {"start": snapshot_date, "end": snapshot_date},
                "stockType": "",
                "orderBy": {"field": "avgOrders", "mode": "desc"},
                "includeOffice": True,
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            url=f"{base_url}/api/v2/stocks-report/products/sizes",
            data=body,
            method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(f"official stocks request failed with status {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"official stocks request transport failed: {exc}") from exc

        if not isinstance(payload, Mapping):
            raise RuntimeError("official stocks request returned non-object payload")
        return payload

    def _parse_response_to_rows(
        self,
        *,
        payload: Mapping[str, Any],
        snapshot_date: str,
        snapshot_ts: str,
        nm_id: int,
    ) -> list[Mapping[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return []

        rows: list[Mapping[str, Any]] = []
        offices = data.get("offices")
        if isinstance(offices, list):
            for office in offices:
                if not isinstance(office, Mapping):
                    continue
                metrics = office.get("metrics")
                metric_source = metrics if isinstance(metrics, Mapping) else office
                stock_count = metric_source.get("stockCount")
                if not isinstance(stock_count, (int, float)):
                    continue
                rows.append(
                    {
                        "snapshot_date": snapshot_date,
                        "snapshot_ts": snapshot_ts,
                        "nmId": nm_id,
                        "regionName": str(office.get("regionName") or office.get("region") or ""),
                        "stockCount": float(stock_count),
                    }
                )
        return rows
