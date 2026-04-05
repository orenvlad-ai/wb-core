"""Адаптерная граница блока sf period."""

import json
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import load_runtime_config
from packages.contracts.sf_period_block import SfPeriodRequest


class SfPeriodSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: SfPeriodRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedSfPeriodSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SfPeriodRequest) -> Mapping[str, Any]:
        del request
        path = self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        return json.loads(path.read_text(encoding="utf-8"))


class HttpBackedSfPeriodSource:
    """Минимальный HTTP adapter к official sales funnel period endpoint."""

    def __init__(
        self,
        base_url: str = "https://seller-analytics-api.wildberries.ru",
        token_env_var: str = "WB_TOKEN",
        base_url_env_var: str = "WB_SELLER_ANALYTICS_API_BASE_URL",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds

    def fetch(self, request: SfPeriodRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )

        response_payload = self._post_period(
            base_url=runtime.base_url,
            token=runtime.token,
            snapshot_date=request.snapshot_date,
            nm_ids=request.nm_ids,
            timeout_seconds=runtime.timeout_seconds,
        )
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            **response_payload,
        }

    def _post_period(
        self,
        *,
        base_url: str,
        token: str,
        snapshot_date: str,
        nm_ids: list[int],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        url = f"{base_url}/api/analytics/v3/sales-funnel/products"
        body = json.dumps(
            {
                "selectedPeriod": {
                    "start": snapshot_date,
                    "end": snapshot_date,
                },
                "nmIds": nm_ids,
                "limit": max(len(nm_ids), 1),
                "offset": 0,
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            url=url,
            data=body,
            method="POST",
            headers={
                "Authorization": token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(
                f"official sf_period request failed with status {exc.code}: {body}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(f"official sf_period request transport failed: {exc}") from exc
