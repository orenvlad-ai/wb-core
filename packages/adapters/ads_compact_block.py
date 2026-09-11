"""Адаптерная граница блока ads compact."""

import json
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import time
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, parse, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.contracts.ads_compact_block import AdsCompactRequest
from packages.contracts.ads_daily_report import (
    AdsDatedRoster, AdsReportError, validate_ads_campaign_batch, validate_dated_roster,
    validate_ads_request, checked_ads_count, COUNTS,
)
from packages.contracts.source_attempt_diagnostics import (
    SourceAttemptError, new_attempt, observed_now, source_digest,
)


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
        complete_catalog: bool = False,
        dated_roster: AdsDatedRoster | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._max_ids_per_request = max_ids_per_request
        self._batch_sleep_seconds = batch_sleep_seconds
        self._complete_catalog = complete_catalog
        self._dated_roster = dated_roster
        if type(max_ids_per_request) is not int or not 1 <= max_ids_per_request <= 50:
            raise ValueError("ads_campaign_batch_limit_invalid")

    def fetch(self, request: AdsCompactRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        diagnostics = new_attempt("ads_compact", request.snapshot_date)
        diagnostics.update(expected_campaign_ids=None, returned_campaign_ids=None,
                           missing_campaign_ids=None, duplicate_campaign_ids=None,
                           attempted_campaign_ids=None, not_attempted_campaign_ids=None,
                           missing_from_received_batches_campaign_ids=None,
                           response_error_campaign_ids=None,
                           batch_count=None, batches=[], source_digest=None,
                           counter_basis="adapter_attempt_evidence")
        try:
            count_payload = self._get_json(
                url=f"{runtime.base_url}/adv/v1/promotion/count",
                token=runtime.token,
                timeout_seconds=runtime.timeout_seconds,
            )
            diagnostics["source_observed_at"] = observed_now()
            diagnostics["campaign_list_digest"] = source_digest(count_payload)
            advert_ids = self._extract_non_archived_advert_ids(count_payload, snapshot_date=request.snapshot_date)
            rows = self._fetch_compact_rows(
                base_url=runtime.base_url, token=runtime.token, advert_ids=advert_ids,
                snapshot_date=request.snapshot_date, nm_ids=request.nm_ids,
                timeout_seconds=runtime.timeout_seconds, diagnostics=diagnostics,
            )
        except SourceAttemptError as exc:
            if exc.diagnostics.get("schema_version"):
                raise
            diagnostics.update(exc.diagnostics)
            raise SourceAttemptError(exc.code, diagnostics) from exc
        except Exception as exc:
            safe_codes = {"ads_catalog_campaign_list_invalid", "ads_catalog_campaign_list_incomplete",
                          "ads_catalog_campaign_count_mismatch", "ads_catalog_campaign_identity_invalid"}
            code = str(exc) if str(exc) in safe_codes else "ads_source_failed"
            raise SourceAttemptError(code, diagnostics) from exc
        diagnostics["attempt_status"] = "returned"
        diagnostics["source_digest"] = source_digest({"campaign_list": diagnostics.get("campaign_list_digest"),
                                                      "batches": [b["digest"] for b in diagnostics["batches"]]})
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": diagnostics,
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
        diagnostics: dict[str, Any] | None = None,
    ) -> list[Mapping[str, Any]]:
        if self._complete_catalog:
            try:
                validate_ads_request(snapshot_date=snapshot_date, nm_ids=nm_ids, campaign_ids=advert_ids)
            except AdsReportError as exc:
                raise SourceAttemptError(str(exc), diagnostics or new_attempt("ads_compact", snapshot_date)) from exc
        wanted = set(nm_ids)
        fetched_at = f"{snapshot_date} 21:30:00"
        agg: dict[tuple[str, int], dict[str, Any]] = {}
        batches = [
            advert_ids[i : i + self._max_ids_per_request]
            for i in range(0, len(advert_ids), self._max_ids_per_request)
        ]

        if diagnostics is None:
            diagnostics = new_attempt("ads_compact", snapshot_date)
        diagnostics.update(expected_campaign_ids=list(advert_ids), returned_campaign_ids=[],
                           missing_campaign_ids=list(advert_ids), duplicate_campaign_ids=[],
                           attempted_campaign_ids=[], not_attempted_campaign_ids=list(advert_ids),
                           missing_from_received_batches_campaign_ids=[], response_error_campaign_ids=[],
                           batch_count=len(batches), batches=[], source_digest=None,
                           counter_basis="observed_campaign_responses")
        returned_ids: list[int] = []
        attempted_ids: list[int] = []
        for index, batch in enumerate(batches):
            attempted_ids.extend(batch)
            diagnostics.update(attempted_campaign_ids=list(attempted_ids),
                               not_attempted_campaign_ids=sorted(set(advert_ids) - set(attempted_ids)))
            batch_evidence = {"batch_index": index + 1, "source_date": snapshot_date,
                              "expected_campaign_ids": list(batch), "status": "started",
                              "attempt_started_at": observed_now(), "response_kind": None,
                              "returned_campaign_ids": None, "missing_campaign_ids": None,
                              "duplicate_campaign_ids": None, "digest": None}
            diagnostics["batches"].append(batch_evidence)
            try:
                payload = self._get_json(
                    url=(
                        f"{base_url}/adv/v3/fullstats?"
                        f"{parse.urlencode({'ids': ','.join(str(x) for x in batch), 'beginDate': snapshot_date, 'endDate': snapshot_date})}"
                    ),
                    token=token,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                batch_evidence["status"] = "error"
                diagnostics["response_error_campaign_ids"].extend(batch)
                code = exc.code if isinstance(exc, SourceAttemptError) else "ads_transport_error"
                if isinstance(exc, SourceAttemptError):
                    batch_evidence.update(exc.diagnostics)
                raise SourceAttemptError(code, diagnostics) from exc
            diagnostics["source_observed_at"] = observed_now()
            batch_evidence["response_kind"] = (
                "null" if payload is None else "array" if isinstance(payload, list)
                else "object" if isinstance(payload, Mapping) else "boolean" if isinstance(payload, bool)
                else "number" if isinstance(payload, (int, float)) else "string" if isinstance(payload, str)
                else "invalid"
            )
            items = payload if isinstance(payload, list) else []
            ids, noncanonical_ids, invalid_id_count = _campaign_id_evidence(items)
            returned_ids.extend(ids)
            batch_evidence.update(status="returned", source_observed_at=diagnostics["source_observed_at"], returned_campaign_ids=ids,
                                  missing_campaign_ids=sorted(set(batch) - set(ids)),
                                  duplicate_campaign_ids=sorted(i for i, n in Counter(ids).items() if n > 1),
                                  unexpected_campaign_ids=sorted(set(ids) - set(batch)), digest=source_digest(payload))
            batch_evidence.update(noncanonical_campaign_ids=noncanonical_ids,
                                  invalid_campaign_identity_count=invalid_id_count)
            diagnostics["missing_from_received_batches_campaign_ids"].extend(batch_evidence["missing_campaign_ids"])
            for code, present in (("noncanonical_campaign_identity", noncanonical_ids),
                                  ("invalid_campaign_identity", invalid_id_count)):
                if present and code not in diagnostics["anomaly_codes"]:
                    diagnostics["anomaly_codes"].append(code)
            diagnostics.update(returned_campaign_ids=list(returned_ids),
                               missing_campaign_ids=sorted(set(advert_ids) - set(returned_ids)),
                               duplicate_campaign_ids=sorted(i for i, n in Counter(returned_ids).items() if n > 1),
                               source_digest=source_digest([b["digest"] for b in diagnostics["batches"]]))
            _record_ads_anomalies(items, batch_evidence, diagnostics)
            if self._complete_catalog:
                try:
                    validation = validate_ads_campaign_batch(
                        payload, campaign_ids=batch, snapshot_date=snapshot_date)
                except AdsReportError as exc:
                    raise SourceAttemptError(str(exc), diagnostics) from exc
                batch_evidence["validation_contract"] = validation["contract"]
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
                            for field in COUNTS:
                                metric_key = f"ads_{field}"
                                if self._complete_catalog:
                                    # The batch validator already checked each input.
                                    # Sum as int before projecting to binary64.
                                    try:
                                        row[metric_key] = checked_ads_count(int(row[metric_key]) + int(item[field]))
                                    except AdsReportError as exc:
                                        raise SourceAttemptError(str(exc), diagnostics) from exc
                                else:
                                    row[metric_key] += _to_float(item.get(field))
                            row["ads_sum"] += _to_float(item.get("sum"))
                            row["ads_sum_price"] += _to_float(item.get("sum_price"))
            if index < len(batches) - 1:
                time.sleep(self._batch_sleep_seconds)

        if self._complete_catalog:
            missing_nms = sorted(wanted - {nm for _, nm in agg})
            diagnostics["unobserved_nm_ids"] = missing_nms
            diagnostics["dated_roster_state"] = "unknown"
            # A current campaign catalog cannot establish the historical universe.
            # Only an explicitly qualified dated roster admits inferred zero rows.
            if self._dated_roster is not None or missing_nms:
                try:
                    binding = validate_dated_roster(self._dated_roster,
                        campaign_ids=advert_ids, snapshot_date=snapshot_date)
                except AdsReportError as exc:
                    raise SourceAttemptError(str(exc), diagnostics) from exc
                diagnostics["dated_roster_state"] = binding["state"]
                diagnostics["dated_roster"] = binding
            for nm in missing_nms:
                agg[(snapshot_date, nm)] = {
                    'fetched_at': fetched_at, 'snapshot_date': snapshot_date, 'nmId': nm,
                    'ads_views': 0.0, 'ads_clicks': 0.0, 'ads_atbs': 0.0,
                    'ads_orders': 0.0, 'ads_sum': 0.0, 'ads_sum_price': 0.0,
                }
            if any(not math.isfinite(row[key]) for row in agg.values()
                   for key in row if key.startswith("ads_")):
                raise SourceAttemptError("ads_catalog_aggregate_nonfinite", diagnostics)
            for row in agg.values():
                for field in COUNTS:
                    row[f"ads_{field}"] = float(row[f"ads_{field}"])
        return [agg[key] for key in sorted(agg)]

    def _get_json(self, *, url: str, token: str, timeout_seconds: float) -> Any:
        req = urllib_request.Request(url=url, headers={"Authorization": token}, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            exc.read()  # Provider bodies are never exposed in STATUS or logs.
            raise SourceAttemptError("ads_http_error", {"http_status": int(exc.code)}) from exc
        except error.URLError as exc:
            raise SourceAttemptError("ads_transport_error", {"http_status": None}) from exc

    def _extract_non_archived_advert_ids(self, payload: Mapping[str, Any], *, snapshot_date: str | None = None) -> list[int]:
        allowed_statuses = {7, 9, 11} if self._complete_catalog else {4, 9, 11}
        ids: set[int] = set()
        adverts = payload.get("adverts")
        if self._complete_catalog and payload.get('all') == 0 and adverts is None:
            adverts = []
        if self._complete_catalog:
            if not isinstance(adverts, list) or type(payload.get('all')) is not int:
                raise ValueError('ads_catalog_campaign_list_invalid')
            if any(not isinstance(g, Mapping) or not isinstance(g.get('advert_list'), list)
                   or type(g.get('count')) is not int or g['count'] < 0
                   or g.get('count') != len(g['advert_list']) for g in adverts):
                raise ValueError('ads_catalog_campaign_list_incomplete')
            if sum(len(g['advert_list']) for g in adverts) != payload['all']:
                raise ValueError('ads_catalog_campaign_count_mismatch')
            all_ids = [a.get('advertId', a.get('id')) for g in adverts for a in g['advert_list'] if isinstance(a, Mapping)]
            if any(type(i) is not int or i <= 0 for i in all_ids) or len(set(all_ids)) != payload['all']:
                raise ValueError('ads_catalog_campaign_identity_invalid')
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
                    # A currently stopped campaign last changed before this
                    # Moscow business day could not serve during the day. Keep
                    # same-day stops and unknown timestamps in the stats check.
                    if self._complete_catalog and snapshot_date and group.get("status") in (7, 11):
                        try:
                            changed = datetime.fromisoformat(str(advert.get("changeTime", "")).replace("Z", "+00:00"))
                            if changed.tzinfo is not None and changed.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat() < snapshot_date:
                                continue
                        except ValueError:
                            pass
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


def _campaign_id_evidence(items: list[Any]) -> tuple[list[int], list[int], int]:
    """Describe legacy numeric ID equality without changing source acceptance."""
    ids: list[int] = []
    noncanonical: list[int] = []
    invalid_count = 0
    for item in items:
        raw = item.get("advertId") if isinstance(item, Mapping) else None
        if type(raw) is int:
            normalized = raw
        elif type(raw) is bool or (
            type(raw) is float and math.isfinite(raw) and raw.is_integer()
        ):
            # 5.0 (and True for 1) match integer IDs in the existing validator.
            # Normalize only this evidence; the validator still sees raw input.
            normalized = int(raw)
            noncanonical.append(normalized)
        else:
            invalid_count += 1
            continue
        ids.append(normalized)
        if normalized <= 0:
            invalid_count += 1
    return ids, noncanonical, invalid_count


def _record_ads_anomalies(items: list[Any], batch: dict[str, Any], diagnostics: dict[str, Any]) -> None:
    """Observe existing lossy numeric behavior; acceptance belongs to stage И7."""
    positive_empty = []
    missing_fields: dict[str, int] = {}
    invalid_fields: dict[str, int] = {}
    for advert in items:
        if not isinstance(advert, Mapping):
            continue
        spend = advert.get("sum")
        if isinstance(spend, (int, float)) and spend > 0 and advert.get("days") == []:
            if type(advert.get("advertId")) is int:
                positive_empty.append(advert["advertId"])
        for day in advert.get("days", []) if isinstance(advert.get("days"), list) else []:
            for app in day.get("apps", []) if isinstance(day, Mapping) and isinstance(day.get("apps"), list) else []:
                for item in app.get("nms", []) if isinstance(app, Mapping) and isinstance(app.get("nms"), list) else []:
                    if not isinstance(item, Mapping):
                        continue
                    for field in ("views", "clicks", "atbs", "orders", "sum", "sum_price"):
                        value = item.get(field)
                        if value is None:
                            missing_fields[field] = missing_fields.get(field, 0) + 1
                        else:
                            try:
                                finite = type(value) in (int, float) and math.isfinite(value)
                            except OverflowError:
                                # JSON integers can exceed float range even in
                                # rows the business mapper correctly ignores.
                                finite = False
                            if not finite:
                                invalid_fields[field] = invalid_fields.get(field, 0) + 1
    batch.update(positive_sum_empty_days_campaign_ids=positive_empty,
                 missing_metric_fields=missing_fields, invalid_metric_fields=invalid_fields)
    codes = diagnostics["anomaly_codes"]
    for code, present in (("campaign_positive_sum_without_days", positive_empty),
                          ("missing_metric_fields", missing_fields),
                          ("invalid_metric_fields", invalid_fields)):
        if present and code not in codes:
            codes.append(code)
