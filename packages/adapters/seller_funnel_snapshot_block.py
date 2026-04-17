"""Адаптерная граница блока seller funnel snapshot."""

import json
import os
from pathlib import Path
import ssl
from typing import Any, Mapping, Protocol
from urllib import error, parse, request as urllib_request

from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest


class SellerFunnelSnapshotSource(Protocol):
    """Источник seller funnel snapshot для application-слоя."""

    def fetch(self, request: SellerFunnelSnapshotRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedSellerFunnelSnapshotSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SellerFunnelSnapshotRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "not_found":
            return self._artifacts_root / "legacy" / "not-found__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedSellerFunnelSnapshotSource:
    """Минимальный HTTP adapter к legacy daily endpoint."""

    def __init__(self, base_url: str = "https://api.selleros.pro") -> None:
        self._base_url = base_url.rstrip("/")

    def fetch(self, request: SellerFunnelSnapshotRequest) -> Mapping[str, Any]:
        try:
            return self._load_json(self._build_url(request))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            if exc.code == 404:
                explicit_not_found = json.loads(body)
                if request.scenario != "normal":
                    return explicit_not_found
                return self._resolve_latest_match_or_not_found(request, explicit_not_found)
            raise RuntimeError(f"daily request failed with status {exc.code}: {body}") from exc

    def _build_url(self, snapshot_request: SellerFunnelSnapshotRequest) -> str:
        query = parse.urlencode(self._build_query(snapshot_request))
        return f"{self._base_url}/v1/sales-funnel/daily?{query}"

    def _build_latest_url(self) -> str:
        return f"{self._base_url}/v1/sales-funnel/daily"

    def _build_query(self, snapshot_request: SellerFunnelSnapshotRequest) -> dict[str, str]:
        if snapshot_request.scenario == "normal":
            return {"date": snapshot_request.date}
        if snapshot_request.scenario == "not_found":
            return {"date": "1900-01-01"}
        raise ValueError(f"unsupported scenario: {snapshot_request.scenario}")

    def _resolve_latest_match_or_not_found(
        self,
        request: SellerFunnelSnapshotRequest,
        explicit_not_found: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            latest_payload = self._load_json(self._build_latest_url())
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            if exc.code == 404:
                return explicit_not_found
            raise RuntimeError(f"daily request failed with status {exc.code}: {body}") from exc

        if str(latest_payload.get("date", "") or "") == request.date:
            return latest_payload

        return {
            "detail": (
                f"requested_date={request.date}; "
                f"latest_available_date={str(latest_payload.get('date', '') or '')}; "
                "resolution_rule=explicit_or_latest_date_match"
            )
        }

    def _load_json(self, url: str) -> Mapping[str, Any]:
        with _open_url(url) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)


def _open_url(url: str):
    try:
        return urllib_request.urlopen(url)
    except error.URLError as exc:
        ssl_reason = getattr(exc, "reason", None)
        if (
            os.environ.get("SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK", "").strip() == "1"
            and isinstance(ssl_reason, ssl.SSLCertVerificationError)
        ):
            return urllib_request.urlopen(url, context=ssl._create_unverified_context())
        raise
