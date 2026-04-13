"""Адаптерная граница блока web-source snapshot."""

import json
import os
from pathlib import Path
import ssl
from typing import Any, Mapping, Protocol
from urllib import error, parse, request as urllib_request

from packages.contracts.web_source_snapshot_block import WebSourceSnapshotRequest


class WebSourceSnapshotSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: WebSourceSnapshotRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedWebSourceSnapshotSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: WebSourceSnapshotRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "not_found":
            return self._artifacts_root / "legacy" / "not-found__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedWebSourceSnapshotSource:
    """Минимальный HTTP adapter к legacy snapshot endpoint."""

    def __init__(self, base_url: str = "https://api.selleros.pro") -> None:
        self._base_url = base_url.rstrip("/")

    def fetch(self, request: WebSourceSnapshotRequest) -> Mapping[str, Any]:
        url = self._build_url(request)
        try:
            with _open_url(url) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            if exc.code == 404:
                return json.loads(body)
            raise RuntimeError(f"snapshot request failed with status {exc.code}: {body}") from exc

    def _build_url(self, snapshot_request: WebSourceSnapshotRequest) -> str:
        params = self._build_query(snapshot_request)
        query = parse.urlencode(params)
        return f"{self._base_url}/v1/search-analytics/snapshot?{query}"

    def _build_query(self, snapshot_request: WebSourceSnapshotRequest) -> dict[str, str]:
        if snapshot_request.scenario == "normal":
            return {
                "date_from": snapshot_request.date_from,
                "date_to": snapshot_request.date_to,
            }
        if snapshot_request.scenario == "not_found":
            return {
                "date_from": "1900-01-01",
                "date_to": "1900-01-01",
            }
        raise ValueError(f"unsupported scenario: {snapshot_request.scenario}")


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
