"""Адаптерная граница блока sku display bundle."""

import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from packages.contracts.sku_display_bundle_block import SkuDisplayBundleRequest


class SkuDisplayBundleSource(Protocol):
    def fetch(self, request: SkuDisplayBundleRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedSkuDisplayBundleSource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SkuDisplayBundleRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class ConfigFixtureBackedSkuDisplayBundleSource:
    """Безопасный CONFIG-like fixture path без live spreadsheet/runtime."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SkuDisplayBundleRequest) -> Mapping[str, Any]:
        payload = json.loads(self._resolve_config_path(request.scenario).read_text(encoding="utf-8"))

        if payload.get("sheet") != "CONFIG":
            raise ValueError("config fixture must come from CONFIG sheet")

        headers = payload.get("headers")
        rows = payload.get("rows")
        if not isinstance(headers, list) or not isinstance(rows, list):
            raise ValueError("config fixture must contain headers and rows")

        indices = _resolve_indices(headers)
        out_rows: list[dict[str, Any]] = []
        for raw_row in rows:
            if not isinstance(raw_row, list):
                raise ValueError("config row must be list")
            sku = _parse_nm_id(_value_at(raw_row, indices["sku"]))
            active = _parse_bool_like(_value_at(raw_row, indices["active"]))
            comment = _parse_non_empty_str(_value_at(raw_row, indices["comment"]), "comment")
            group = _parse_non_empty_str(_value_at(raw_row, indices["group"]), "group")
            out_rows.append(
                {
                    "sku": sku,
                    "active": active,
                    "comment": comment,
                    "group": group,
                }
            )

        return {
            "source": {
                "sheet": "CONFIG",
                "mode": "config_fixture",
                "fields": ["sku(nmId)", "active", "comment", "group"],
            },
            "data": {"rows": out_rows},
        }

    def _resolve_config_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "config_source" / "normal__template__config__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "config_source" / "empty__template__config__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


def _resolve_indices(headers: list[Any]) -> dict[str, int]:
    header_map = {str(value): index for index, value in enumerate(headers)}
    required = {
        "sku": ("sku(nmId)", "nmId", "sku"),
        "active": ("active",),
        "comment": ("comment",),
        "group": ("group",),
    }
    out: dict[str, int] = {}
    for key, variants in required.items():
        for variant in variants:
            if variant in header_map:
                out[key] = header_map[variant]
                break
        else:
            raise ValueError(f"missing required CONFIG header for {key}")
    return out


def _value_at(row: list[Any], index: int) -> Any:
    if index >= len(row):
        raise ValueError("config row shorter than headers")
    return row[index]


def _parse_nm_id(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError("sku(nmId) must be int-like")


def _parse_non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty string")
    return value.strip()


def _parse_bool_like(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    raise ValueError("active must be boolean-like")
