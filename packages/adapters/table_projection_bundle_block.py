"""Адаптерная граница блока table projection bundle."""

import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from packages.contracts.table_projection_bundle_block import TableProjectionBundleRequest


class TableProjectionBundleSource(Protocol):
    def fetch(self, request: TableProjectionBundleRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedTableProjectionBundleSource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: TableProjectionBundleRequest) -> Mapping[str, Any]:
        path = self._resolve_input_bundle_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_input_bundle_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "input_bundle" / "normal__template__input-bundle__fixture.json"
        if scenario == "minimal":
            return self._artifacts_root / "input_bundle" / "minimal__template__input-bundle__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class ComposedReferenceTableProjectionBundleSource:
    """Композиция projection input поверх уже существующих module fixtures."""

    def __init__(self, repo_root: Path, artifacts_root: Path) -> None:
        self._repo_root = repo_root
        self._artifacts_root = artifacts_root

    def fetch(self, request: TableProjectionBundleRequest) -> Mapping[str, Any]:
        manifest = json.loads(self._resolve_reference_path(request.scenario).read_text(encoding="utf-8"))
        sources = manifest.get("sources")
        if not isinstance(sources, Mapping):
            raise ValueError("reference manifest must contain sources object")

        loaded: dict[str, Any] = {}
        sku_bundle = self._load_fixture_path(_require_str(sources, "sku_display_bundle"))
        loaded["sku_display_bundle"] = sku_bundle

        requested_nm_ids = _extract_requested_nm_ids(sku_bundle)
        for source_key, rel_path in sources.items():
            if source_key == "sku_display_bundle":
                continue
            if not isinstance(rel_path, str):
                raise ValueError(f"reference path for {source_key} must be string")
            loaded[source_key] = _reduce_fixture_for_requested_nm_ids(
                self._load_fixture_path(rel_path),
                requested_nm_ids,
            )

        return {"upstream": loaded}

    def _resolve_reference_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "reference" / "normal__template__module-output-map.json"
        if scenario == "minimal":
            return self._artifacts_root / "reference" / "minimal__template__module-output-map.json"
        raise ValueError(f"unsupported scenario: {scenario}")

    def _load_fixture_path(self, rel_path: str) -> Mapping[str, Any]:
        path = self._repo_root / rel_path
        return json.loads(path.read_text(encoding="utf-8"))


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


def _extract_requested_nm_ids(sku_bundle: Mapping[str, Any]) -> list[int]:
    result = sku_bundle.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("sku display bundle must contain result object")
    items = result.get("items")
    if not isinstance(items, list):
        raise ValueError("sku display bundle result.items must be list")
    nm_ids: list[int] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("sku display item must be object")
        nm_id = item.get("nm_id")
        if not isinstance(nm_id, int):
            raise ValueError("sku display item nm_id must be int")
        nm_ids.append(nm_id)
    return nm_ids


def _reduce_fixture_for_requested_nm_ids(
    fixture: Mapping[str, Any],
    requested_nm_ids: list[int],
) -> Mapping[str, Any]:
    result = fixture.get("result")
    if not isinstance(result, Mapping):
        return fixture

    items = result.get("items")
    if not isinstance(items, list):
        return fixture

    requested = set(requested_nm_ids)
    reduced_items: list[Any] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        nm_id = item.get("nm_id")
        if isinstance(nm_id, int) and nm_id in requested:
            reduced_items.append(item)

    reduced_result = dict(result)
    reduced_result["items"] = reduced_items
    reduced_result["count"] = len(reduced_items)
    reduced_fixture = dict(fixture)
    reduced_fixture["result"] = reduced_result
    return reduced_fixture
