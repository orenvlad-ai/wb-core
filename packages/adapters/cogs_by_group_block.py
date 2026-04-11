"""Адаптерная граница блока cogs by group."""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol

from packages.contracts.cogs_by_group_block import CogsByGroupRequest


class CogsByGroupSource(Protocol):
    def fetch(self, request: CogsByGroupRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedCogsByGroupSource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: CogsByGroupRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class RuleBackedCogsByGroupSource:
    """Минимальный fixture-backed rule-source path без live spreadsheet/runtime."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: CogsByGroupRequest) -> Mapping[str, Any]:
        if request.scenario != "normal":
            raise ValueError("rule-backed cogs source supports only normal scenario")

        payload = json.loads(
            (
                self._artifacts_root
                / "rule_source"
                / "normal__template__rules__fixture.json"
            ).read_text(encoding="utf-8")
        )
        if payload.get("metric_enabled") is not True:
            raise ValueError("rule fixture must keep cost_price_rub metric enabled")

        active_skus = payload.get("active_skus")
        rules = payload.get("rules")
        if not isinstance(active_skus, list) or not isinstance(rules, list):
            raise ValueError("rule fixture must contain active_skus and rules lists")

        requested = set(request.nm_ids)
        active_group_set: set[str] = set()
        sku_group_map: dict[int, str] = {}
        for sku in active_skus:
            if not isinstance(sku, Mapping):
                raise ValueError("active sku row must be object")
            nm_id = sku.get("nmId")
            group = sku.get("group")
            if not isinstance(nm_id, int) or not isinstance(group, str) or not group.strip():
                raise ValueError("active sku row must contain nmId/group")
            sku_group_map[nm_id] = group.strip()
            if nm_id in requested:
                active_group_set.add(group.strip())

        rules_by_group: dict[str, list[dict[str, Any]]] = {}
        seen_dup: set[tuple[str, str]] = set()
        rules_group_set: set[str] = set()
        for row in rules:
            if not isinstance(row, Mapping):
                raise ValueError("rule row must be object")
            group = row.get("group")
            cost = row.get("cost_price_rub")
            effective_from = row.get("effective_from")
            if not isinstance(group, str) or not group.strip():
                raise ValueError("rule row must contain non-empty group")
            if not isinstance(cost, (int, float)):
                raise ValueError("rule row must contain numeric cost_price_rub")
            if not isinstance(effective_from, str):
                raise ValueError("rule row must contain effective_from")
            key = (group.strip(), effective_from)
            if key in seen_dup:
                raise ValueError(f"duplicate rule detected: {group}|{effective_from}")
            seen_dup.add(key)
            rules_by_group.setdefault(group.strip(), []).append(
                {"effective_from": effective_from, "cost_price_rub": float(cost)}
            )
            rules_group_set.add(group.strip())

        unknown_groups = sorted(group for group in rules_group_set if group not in active_group_set)
        if unknown_groups:
            raise ValueError(f"unknown groups in rule fixture: {', '.join(unknown_groups)}")

        missing_groups = sorted(group for group in active_group_set if group not in rules_by_group)
        if missing_groups:
            raise ValueError(f"missing group rules in fixture: {', '.join(missing_groups)}")

        for rows in rules_by_group.values():
            rows.sort(key=lambda item: item["effective_from"])

        out_rows: list[dict[str, Any]] = []
        for nm_id in sorted(requested):
            group = sku_group_map.get(nm_id)
            if not group:
                continue
            for date_key in _iter_date_range(request.date_from, request.date_to):
                value = _resolve_cost(rules_by_group[group], date_key)
                if value is None:
                    continue
                out_rows.append(
                    {
                        "date": date_key,
                        "nmId": nm_id,
                        "cost_price_rub": value,
                    }
                )

        return {
            "date_from": request.date_from,
            "date_to": request.date_to,
            "requested_nm_ids": request.nm_ids,
            "data": {"rows": out_rows},
        }


def _iter_date_range(date_from: str, date_to: str) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must be >= date_from")
    out: list[str] = []
    current = start
    while current <= end:
        out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _resolve_cost(rules: list[dict[str, Any]], date_key: str) -> float | None:
    current: float | None = None
    for row in rules:
        if row["effective_from"] <= date_key:
            current = row["cost_price_rub"]
        else:
            break
    return current
