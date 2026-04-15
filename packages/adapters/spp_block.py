"""Адаптерная граница блока spp."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, parse, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.contracts.spp_block import SppRequest


class SppSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: SppRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedSppSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SppRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedSppSource:
    """Минимальный HTTP adapter к official statistics sales endpoint."""

    def __init__(
        self,
        base_url: str = "https://statistics-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_STATISTICS_API_BASE_URL",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds

    def fetch(self, request: SppRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        sales_rows = self._get_sales(
            base_url=runtime.base_url,
            token=runtime.token,
            snapshot_date=request.snapshot_date,
            timeout_seconds=runtime.timeout_seconds,
        )
        aggregated = self._aggregate_by_nm_id(
            rows=sales_rows,
            snapshot_date=request.snapshot_date,
            nm_ids=request.nm_ids,
        )
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "data": {
                "items": aggregated,
            },
        }

    def _get_sales(
        self,
        *,
        base_url: str,
        token: str,
        snapshot_date: str,
        timeout_seconds: float,
    ) -> list[Mapping[str, Any]]:
        url = f"{base_url}/api/v1/supplier/sales?{parse.urlencode({'dateFrom': snapshot_date})}"
        req = urllib_request.Request(
            url=url,
            headers={"Authorization": token},
            method="GET",
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(f"official spp request failed with status {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"official spp request transport failed: {exc}") from exc

        if not isinstance(payload, list):
            raise RuntimeError("official spp request returned non-list payload")
        return [row for row in payload if isinstance(row, Mapping)]

    def _aggregate_by_nm_id(
        self,
        *,
        rows: list[Mapping[str, Any]],
        snapshot_date: str,
        nm_ids: list[int],
    ) -> list[Mapping[str, Any]]:
        wanted = set(nm_ids)
        by_nm_id: dict[int, dict[str, float | int]] = defaultdict(lambda: {"sum": 0.0, "count": 0})

        for row in rows:
            if self._extract_sale_date(row) != snapshot_date:
                continue

            nm_id = row.get("nmId")
            if not isinstance(nm_id, int) or nm_id not in wanted:
                continue

            raw_spp = row.get("spp")
            try:
                spp_num = float(raw_spp)
            except (TypeError, ValueError):
                continue

            normalized = spp_num / 100.0 if spp_num > 1 else spp_num
            acc = by_nm_id[nm_id]
            acc["sum"] = float(acc["sum"]) + normalized
            acc["count"] = int(acc["count"]) + 1

        items: list[Mapping[str, Any]] = []
        for nm_id in sorted(by_nm_id.keys()):
            acc = by_nm_id[nm_id]
            count = int(acc["count"])
            if count <= 0:
                continue
            items.append(
                {
                    "nmId": nm_id,
                    "spp_avg": float(acc["sum"]) / count,
                    "spp_count": count,
                }
            )
        return items

    def _extract_sale_date(self, row: Mapping[str, Any]) -> str:
        source = str(row.get("date") or row.get("lastChangeDate") or "").strip()
        if len(source) >= 10 and source[4:5] == "-":
            return source[:10]
        if len(source) >= 10 and source[2:3] == ".":
            return f"{source[6:10]}-{source[3:5]}-{source[:2]}"
        return ""
