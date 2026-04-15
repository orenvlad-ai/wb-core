"""Адаптерная граница блока ads compact."""

import json
import time
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, parse, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.contracts.ads_compact_block import AdsCompactRequest


class AdsCompactSource(Protocol):
    def fetch(self, request: AdsCompactRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedAdsCompactSource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: AdsCompactRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedAdsCompactSource:
    def __init__(
        self,
        base_url: str = "https://advert-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_ADVERT_API_BASE_URL",
        timeout_seconds: float = 30.0,
        max_ids_per_request: int = 50,
        batch_sleep_seconds: float = 22.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._max_ids_per_request = max_ids_per_request
        self._batch_sleep_seconds = batch_sleep_seconds

    def fetch(self, request: AdsCompactRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        count_payload = self._get_json(
            url=f"{runtime.base_url}/adv/v1/promotion/count",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )
        advert_ids = self._extract_non_archived_advert_ids(count_payload)
        rows = self._fetch_compact_rows(
            base_url=runtime.base_url,
            token=runtime.token,
            advert_ids=advert_ids,
            snapshot_date=request.snapshot_date,
            nm_ids=request.nm_ids,
            timeout_seconds=runtime.timeout_seconds,
        )
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "data": {"rows": rows},
        }

    def _fetch_compact_rows(
        self,
        *,
        base_url: str,
        token: str,
        advert_ids: list[int],
        snapshot_date: str,
        nm_ids: list[int],
        timeout_seconds: float,
    ) -> list[Mapping[str, Any]]:
        wanted = set(nm_ids)
        fetched_at = f"{snapshot_date} 21:30:00"
        agg: dict[tuple[str, int], dict[str, Any]] = {}
        batches = [
            advert_ids[i : i + self._max_ids_per_request]
            for i in range(0, len(advert_ids), self._max_ids_per_request)
        ]

        for index, batch in enumerate(batches):
            payload = self._get_json(
                url=(
                    f"{base_url}/adv/v3/fullstats?"
                    f"{parse.urlencode({'ids': ','.join(str(x) for x in batch), 'beginDate': snapshot_date, 'endDate': snapshot_date})}"
                ),
                token=token,
                timeout_seconds=timeout_seconds,
            )
            items = payload if isinstance(payload, list) else []
            for advert in items:
                if not isinstance(advert, Mapping):
                    continue
                days = advert.get("days")
                if not isinstance(days, list):
                    continue
                for day in days:
                    if not isinstance(day, Mapping):
                        continue
                    day_snapshot = _normalize_snapshot_date(day.get("date"))
                    if day_snapshot != snapshot_date:
                        continue
                    apps = day.get("apps")
                    if not isinstance(apps, list):
                        continue
                    for app in apps:
                        if not isinstance(app, Mapping):
                            continue
                        nms = app.get("nms")
                        if not isinstance(nms, list):
                            continue
                        for item in nms:
                            if not isinstance(item, Mapping):
                                continue
                            nm_id = item.get("nmId")
                            if not isinstance(nm_id, int) or nm_id not in wanted:
                                continue
                            key = (snapshot_date, nm_id)
                            if key not in agg:
                                agg[key] = {
                                    "fetched_at": fetched_at,
                                    "snapshot_date": snapshot_date,
                                    "nmId": nm_id,
                                    "ads_views": 0.0,
                                    "ads_clicks": 0.0,
                                    "ads_atbs": 0.0,
                                    "ads_orders": 0.0,
                                    "ads_sum": 0.0,
                                    "ads_sum_price": 0.0,
                                }
                            row = agg[key]
                            row["ads_views"] += _to_float(item.get("views"))
                            row["ads_clicks"] += _to_float(item.get("clicks"))
                            row["ads_atbs"] += _to_float(item.get("atbs"))
                            row["ads_orders"] += _to_float(item.get("orders"))
                            row["ads_sum"] += _to_float(item.get("sum"))
                            row["ads_sum_price"] += _to_float(item.get("sum_price"))
            if index < len(batches) - 1:
                time.sleep(self._batch_sleep_seconds)

        return [agg[key] for key in sorted(agg)]

    def _get_json(self, *, url: str, token: str, timeout_seconds: float) -> Any:
        req = urllib_request.Request(url=url, headers={"Authorization": token}, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(
                f"official ads compact request failed with status {exc.code}: {body}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"official ads compact request transport failed: {exc}"
            ) from exc

    def _extract_non_archived_advert_ids(self, payload: Mapping[str, Any]) -> list[int]:
        allowed_statuses = {4, 9, 11}
        ids: set[int] = set()
        adverts = payload.get("adverts")
        if isinstance(adverts, list):
            for group in adverts:
                if not isinstance(group, Mapping):
                    continue
                if group.get("status") not in allowed_statuses:
                    continue
                advert_list = group.get("advert_list")
                if not isinstance(advert_list, list):
                    continue
                for advert in advert_list:
                    if not isinstance(advert, Mapping):
                        continue
                    advert_id = advert.get("advertId", advert.get("id"))
                    if isinstance(advert_id, int) and advert_id > 0:
                        ids.add(advert_id)
        return sorted(ids)


def _normalize_snapshot_date(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if "T" in normalized:
        return normalized[:10]
    if " " in normalized:
        return normalized[:10]
    return normalized


def _to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0
