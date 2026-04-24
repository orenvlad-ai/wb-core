"""Адаптерная граница блока promo by price."""

import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from packages.contracts.promo_by_price_block import PromoByPriceRequest


class PromoByPriceSource(Protocol):
    def fetch(self, request: PromoByPriceRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedPromoByPriceSource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: PromoByPriceRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class RuleBackedPromoByPriceSource:
    """Минимальный fixture-backed rule-source path без live spreadsheet/runtime."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: PromoByPriceRequest) -> Mapping[str, Any]:
        if request.scenario != "normal":
            raise ValueError("rule-backed promo source supports only normal scenario")

        payload = json.loads(
            (
                self._artifacts_root
                / "rule_source"
                / "normal__template__rules__fixture.json"
            ).read_text(encoding="utf-8")
        )

        active_skus = payload.get("active_skus")
        prices = payload.get("prices")
        rules = payload.get("rules")
        if not isinstance(active_skus, list) or not isinstance(prices, list) or not isinstance(rules, list):
            raise ValueError("rule fixture must contain active_skus, prices, rules lists")

        requested = set(request.nm_ids)
        active_nm_ids = {
            sku.get("nmId")
            for sku in active_skus
            if isinstance(sku, Mapping) and isinstance(sku.get("nmId"), int)
        }

        price_by_key: dict[tuple[str, int], float] = {}
        for row in prices:
            if not isinstance(row, Mapping):
                raise ValueError("price row must be object")
            nm_id = row.get("nmId")
            date = row.get("date")
            price = row.get("price_seller_discounted")
            if not isinstance(nm_id, int) or not isinstance(date, str) or not isinstance(price, (int, float)):
                raise ValueError("price row must contain nmId/date/price_seller_discounted")
            if nm_id in requested and nm_id in active_nm_ids:
                price_by_key[(date, nm_id)] = float(price)

        rules_by_nm: dict[int, list[dict[str, Any]]] = {}
        for row in rules:
            if not isinstance(row, Mapping):
                raise ValueError("rule row must be object")
            nm_id = row.get("nmId")
            plan_price = row.get("plan_price")
            start_date = row.get("start_date")
            end_date = row.get("end_date")
            if not isinstance(nm_id, int) or not isinstance(plan_price, (int, float)):
                raise ValueError("rule row must contain numeric nmId/plan_price")
            if not isinstance(start_date, str) or not isinstance(end_date, str):
                raise ValueError("rule row must contain start_date/end_date")
            if nm_id not in requested:
                continue
            rules_by_nm.setdefault(nm_id, []).append(
                {
                    "plan_price": float(plan_price),
                    "start_date": start_date,
                    "end_date": end_date,
                }
            )

        rows: list[dict[str, Any]] = []
        for nm_id in sorted(requested):
            for date in _iter_date_range(request.date_from, request.date_to):
                active_rules = [
                    rule
                    for rule in rules_by_nm.get(nm_id, [])
                    if rule["start_date"] <= date <= rule["end_date"]
                ]
                if not active_rules:
                    continue
                price = price_by_key.get((date, nm_id))
                count = 0.0
                participation = 0.0
                best_plan = 0.0
                if isinstance(price, float) and price > 0:
                    eligible_rules = [
                        rule
                        for rule in active_rules
                        if price < rule["plan_price"]
                    ]
                    count = float(len(eligible_rules))
                    participation = 1.0 if count > 0 else 0.0
                    if eligible_rules:
                        best_plan = max(rule["plan_price"] for rule in eligible_rules)
                rows.append(
                    {
                        "date": date,
                        "nmId": nm_id,
                        "promo_count_by_price": count,
                        "promo_entry_price_best": best_plan,
                        "promo_participation": participation,
                    }
                )

        return {
            "date_from": request.date_from,
            "date_to": request.date_to,
            "requested_nm_ids": request.nm_ids,
            "data": {"rows": rows},
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


from datetime import date, timedelta
