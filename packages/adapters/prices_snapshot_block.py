"""Адаптерная граница блока prices snapshot."""

import json
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import load_runtime_config
from packages.contracts.prices_snapshot_block import PricesSnapshotRequest


class PricesSnapshotSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: PricesSnapshotRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedPricesSnapshotSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: PricesSnapshotRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedPricesSnapshotSource:
    """Минимальный HTTP adapter к official prices endpoint."""

    def __init__(
        self,
        base_url: str = "https://discounts-prices-api.wildberries.ru",
        token_env_var: str = "WB_TOKEN",
        base_url_env_var: str = "WB_OFFICIAL_API_BASE_URL",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds

    def fetch(self, request: PricesSnapshotRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )

        response_payload = self._post_goods_filter(
            base_url=runtime.base_url,
            token=runtime.token,
            nm_ids=request.nm_ids,
            timeout_seconds=runtime.timeout_seconds,
        )
        if response_payload.get("error"):
            detail = str(response_payload.get("errorText") or "unknown official prices API error")
            raise RuntimeError(f"official prices API returned error payload: {detail}")

        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            **response_payload,
        }

    def _post_goods_filter(
        self,
        *,
        base_url: str,
        token: str,
        nm_ids: list[int],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        url = f"{base_url}/api/v2/list/goods/filter"
        body = json.dumps({"nmList": nm_ids}).encode("utf-8")
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
                f"official prices request failed with status {exc.code}: {body}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(f"official prices request transport failed: {exc}") from exc
