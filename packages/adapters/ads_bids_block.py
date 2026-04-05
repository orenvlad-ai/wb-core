"""Адаптерная граница блока ads bids."""

import json
import time
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, parse, request as urllib_request

from packages.adapters.official_api_runtime import load_runtime_config
from packages.contracts.ads_bids_block import AdsBidsRequest


class AdsBidsSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: AdsBidsRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedAdsBidsSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: AdsBidsRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedAdsBidsSource:
    """Минимальный HTTP adapter к official promotion bids endpoints."""

    def __init__(
        self,
        base_url: str = "https://advert-api.wildberries.ru",
        token_env_var: str = "WB_TOKEN",
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

    def fetch(self, request: AdsBidsRequest) -> Mapping[str, Any]:
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
        advert_ids = self._extract_advert_ids(count_payload)
        rows = self._fetch_raw_rows(
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
            "data": {
                "rows": rows,
            },
        }

    def _fetch_raw_rows(
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
        rows: list[Mapping[str, Any]] = []
        batches = [
            advert_ids[i : i + self._max_ids_per_request]
            for i in range(0, len(advert_ids), self._max_ids_per_request)
        ]

        for index, batch in enumerate(batches):
            query = parse.urlencode({"ids": ",".join(str(x) for x in batch), "statuses": 9})
            payload = self._get_json(
                url=f"{base_url}/api/advert/v2/adverts?{query}",
                token=token,
                timeout_seconds=timeout_seconds,
            )
            adverts = payload.get("adverts")
            if not isinstance(adverts, list):
                adverts = []

            for advert in adverts:
                advert_id = advert.get("id") or advert.get("advertId")
                if not isinstance(advert_id, int):
                    continue
                bid_type = str(advert.get("bid_type") or "")
                placements = advert.get("settings", {}).get("placements") or {}
                allow_search = placements.get("search") is not False
                allow_reco = placements.get("recommendations") is not False
                nm_settings = advert.get("nm_settings")
                if not isinstance(nm_settings, list):
                    continue

                for nm_setting in nm_settings:
                    nm_id = nm_setting.get("nm_id")
                    if not isinstance(nm_id, int) or nm_id not in wanted:
                        continue

                    bids_kopecks = nm_setting.get("bids_kopecks")
                    if not isinstance(bids_kopecks, Mapping):
                        bids = nm_setting.get("bids")
                        if isinstance(bids, Mapping):
                            bids_kopecks = {
                                "search": float(bids.get("search", 0)) * 100,
                                "recommendations": float(bids.get("recommendations", 0)) * 100,
                            }
                        else:
                            bids_kopecks = {}

                    if allow_search and bids_kopecks.get("search") is not None:
                        rows.append(
                            {
                                "fetched_at": fetched_at,
                                "snapshot_date": snapshot_date,
                                "nmId": nm_id,
                                "advertId": advert_id,
                                "bid_type": bid_type,
                                "placement": "search",
                                "bid_kopecks": float(bids_kopecks["search"]),
                            }
                        )
                    if allow_reco and bids_kopecks.get("recommendations") is not None:
                        rows.append(
                            {
                                "fetched_at": fetched_at,
                                "snapshot_date": snapshot_date,
                                "nmId": nm_id,
                                "advertId": advert_id,
                                "bid_type": bid_type,
                                "placement": "recommendations",
                                "bid_kopecks": float(bids_kopecks["recommendations"]),
                            }
                        )

            if index < len(batches) - 1:
                time.sleep(self._batch_sleep_seconds)

        return rows

    def _get_json(self, *, url: str, token: str, timeout_seconds: float) -> Mapping[str, Any]:
        req = urllib_request.Request(url=url, headers={"Authorization": token}, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(
                f"official ads bids request failed with status {exc.code}: {body}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(f"official ads bids request transport failed: {exc}") from exc

        if not isinstance(payload, Mapping):
            raise RuntimeError("official ads bids request returned non-object payload")
        return payload

    def _extract_advert_ids(self, root: Mapping[str, Any]) -> list[int]:
        ids: set[int] = set()

        def push(value: Any) -> None:
            if isinstance(value, int) and value > 0:
                ids.add(value)

        def walk(node: Any, depth: int) -> None:
            if depth > 6 or node is None:
                return
            if isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)
                return
            if isinstance(node, Mapping):
                for key in ("advertId", "advert_id", "id"):
                    push(node.get(key))
                for value in node.values():
                    walk(value, depth + 1)

        adverts = root.get("adverts")
        if adverts is not None:
            walk(adverts, 0)
        walk(root, 0)
        return sorted(ids)
