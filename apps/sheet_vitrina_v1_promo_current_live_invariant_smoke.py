"""Live/public smoke for current promo-by-price invariant.

The smoke is read-only by default. It reads the public sheet-vitrina
status/web-vitrina/plan surfaces and verifies that expected ended/no-download
promo artifacts are diagnostic-only, not fatal, while current promo rows remain
present.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET_FILE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "hosted_runtime_target__selleros_api.json"
)
ALLOW_INSECURE_ENV = "SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK"

PROMO_SOURCE_KEY = "promo_by_price"
TODAY_SLOT = "today_current"
CORE_PROMO_METRIC_KEYS = (
    "promo_participation",
    "promo_count_by_price",
    "promo_entry_price_best",
)
AGGREGATE_PROMO_METRIC_KEYS = (
    "total_promo_participation",
    "total_promo_count_by_price",
    "avg_promo_entry_price_best",
)
EXPECTED_NON_MATERIALIZABLE_STATES = {
    "ended_without_download",
    "metadata_only_ended_without_download",
    "non_materializable_expected",
}
EXPECTED_NON_MATERIALIZABLE_REASONS = {
    "ended_without_download",
    "metadata_only_ended_without_download",
    "non_materializable_expected",
}


def main() -> None:
    args = _parse_args()
    base_url = _resolve_base_url(args)
    allow_insecure = args.allow_insecure or os.environ.get(ALLOW_INSECURE_ENV) == "1"

    routes: list[dict[str, Any]] = []
    status_payload = _get_json(
        _join_url(base_url, "/v1/sheet-vitrina-v1/status"),
        route_name="status",
        timeout_seconds=args.timeout_seconds,
        allow_insecure=allow_insecure,
        routes=routes,
    )
    as_of_date = _resolve_as_of_date(status_payload)
    today_current = _resolve_today_current(status_payload)

    web_payload = _get_json(
        _join_url(base_url, "/v1/sheet-vitrina-v1/web-vitrina"),
        route_name="web_vitrina",
        timeout_seconds=args.timeout_seconds,
        allow_insecure=allow_insecure,
        routes=routes,
    )
    plan_payload = _get_json(
        _join_url(base_url, f"/v1/sheet-vitrina-v1/plan?{urllib_parse.urlencode({'as_of_date': as_of_date})}"),
        route_name="plan",
        timeout_seconds=args.timeout_seconds,
        allow_insecure=allow_insecure,
        routes=routes,
    )

    job_payload: dict[str, Any] | None = None
    if args.job_id:
        job_payload = _get_json(
            _join_url(base_url, f"/v1/sheet-vitrina-v1/job?{urllib_parse.urlencode({'job_id': args.job_id})}"),
            route_name="job",
            timeout_seconds=args.timeout_seconds,
            allow_insecure=allow_insecure,
            routes=routes,
        )

    diagnostics = _resolve_refresh_diagnostics(plan_payload, job_payload=job_payload)
    promo_slot = _find_promo_today_slot(diagnostics)
    promo_summary = _assert_promo_source_invariant(promo_slot)
    row_summary = _assert_current_promo_rows(web_payload, today_current=today_current)

    output = {
        "ok": True,
        "base_url": base_url,
        "as_of_date": as_of_date,
        "today_current": today_current,
        "routes": routes,
        "promo_by_price_today_current": promo_summary,
        "promo_metric_rows": row_summary,
        "insecure_fallback_used": any(item.get("insecure") for item in routes),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    print(
        "sheet_vitrina_v1_promo_current_live_invariant: ok "
        f"as_of_date={as_of_date} today_current={today_current} "
        f"status={promo_summary['status']} origin={promo_summary['origin']} "
        f"covered={promo_summary['covered_count']}/{promo_summary['requested_count']} "
        f"fatal_missing={promo_summary['fatal_missing_artifact_count']} "
        f"expected_non_materializable={promo_summary['non_materializable_expected_count']}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only live/public invariant smoke for promo_by_price[today_current].",
    )
    parser.add_argument("--base-url", default="", help="Public base URL. Defaults to target file public_base_url.")
    parser.add_argument(
        "--target-file",
        type=Path,
        default=DEFAULT_TARGET_FILE,
        help=f"Hosted runtime target JSON. Defaults to {DEFAULT_TARGET_FILE}.",
    )
    parser.add_argument("--job-id", default="", help="Optional completed refresh job id to read as extra diagnostics.")
    parser.add_argument("--timeout-seconds", type=float, default=90.0, help="Per-route HTTP timeout.")
    parser.add_argument(
        "--allow-insecure",
        action="store_true",
        help=f"Allow TLS verification fallback, equivalent to {ALLOW_INSECURE_ENV}=1.",
    )
    return parser.parse_args()


def _resolve_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return _normalize_base_url(args.base_url)
    payload = json.loads(args.target_file.read_text(encoding="utf-8"))
    return _normalize_base_url(str(payload.get("public_base_url") or ""))


def _normalize_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise ValueError("base URL is required")
    return normalized


def _join_url(base_url: str, path_and_query: str) -> str:
    if not path_and_query.startswith("/"):
        path_and_query = "/" + path_and_query
    return f"{base_url}{path_and_query}"


def _get_json(
    url: str,
    *,
    route_name: str,
    timeout_seconds: float,
    allow_insecure: bool,
    routes: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        status, payload = _open_json(url, timeout_seconds=timeout_seconds, context=None)
        routes.append({"route": route_name, "url": _redact_url(url), "http_status": status, "insecure": False})
    except (ssl.SSLError, urllib_error.URLError) as exc:
        if not allow_insecure:
            raise AssertionError(f"{route_name} route unavailable: {url}: {exc}") from exc
        context = ssl._create_unverified_context()
        try:
            status, payload = _open_json(url, timeout_seconds=timeout_seconds, context=context)
        except Exception as insecure_exc:  # pragma: no cover - live-only diagnostic fallback
            raise AssertionError(
                f"{route_name} route unavailable even with insecure TLS fallback: {url}: {insecure_exc}"
            ) from insecure_exc
        routes.append({"route": route_name, "url": _redact_url(url), "http_status": status, "insecure": True})
    if status != 200:
        raise AssertionError(f"{route_name} route must return 200, got {status}: {payload}")
    if not isinstance(payload, dict):
        raise AssertionError(f"{route_name} route must return JSON object, got {type(payload).__name__}")
    return payload


def _open_json(
    url: str,
    *,
    timeout_seconds: float,
    context: ssl.SSLContext | None,
) -> tuple[int, Any]:
    request = urllib_request.Request(url, headers={"Accept": "application/json", "User-Agent": "wb-core-smoke/1"})
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds, context=context) as response:
            raw = response.read()
            return int(response.getcode()), json.loads(raw.decode("utf-8"))
    except urllib_error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {"error": raw.decode("utf-8", errors="replace")[:500]}
        return int(exc.code), payload


def _redact_url(url: str) -> str:
    parsed = urllib_parse.urlparse(url)
    return urllib_parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def _resolve_as_of_date(status_payload: dict[str, Any]) -> str:
    value = str(status_payload.get("as_of_date") or "").strip()
    if value:
        return value
    manual_result = (status_payload.get("manual_context") or {}).get("last_manual_refresh_result") or {}
    value = str(manual_result.get("as_of_date") or "").strip()
    if value:
        return value
    raise AssertionError(f"status payload does not expose as_of_date: keys={sorted(status_payload)}")


def _resolve_today_current(status_payload: dict[str, Any]) -> str:
    server_context = status_payload.get("server_context") or {}
    value = str(server_context.get("today_current_date") or "").strip()
    if value:
        return value
    for slot in status_payload.get("temporal_slots") or []:
        if str(slot.get("slot_key") or "") == TODAY_SLOT:
            value = str(slot.get("column_date") or "").strip()
            if value:
                return value
    date_columns = status_payload.get("date_columns") or []
    if date_columns:
        return str(date_columns[-1])
    raise AssertionError("status payload does not expose today_current date")


def _resolve_refresh_diagnostics(
    plan_payload: dict[str, Any],
    *,
    job_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    diagnostics = (plan_payload.get("metadata") or {}).get("refresh_diagnostics")
    if isinstance(diagnostics, dict) and diagnostics.get("source_slots"):
        return diagnostics
    if job_payload is not None:
        result = job_payload.get("result") or {}
        diagnostics = result.get("refresh_diagnostics")
        if isinstance(diagnostics, dict) and diagnostics.get("source_slots"):
            return diagnostics
    raise AssertionError("plan/job payload does not expose metadata.refresh_diagnostics.source_slots")


def _find_promo_today_slot(diagnostics: dict[str, Any]) -> dict[str, Any]:
    for slot in diagnostics.get("source_slots") or []:
        if slot.get("source_key") == PROMO_SOURCE_KEY and slot.get("slot_kind") == TODAY_SLOT:
            return slot
    raise AssertionError("metadata.refresh_diagnostics.source_slots missing promo_by_price[today_current]")


def _assert_promo_source_invariant(slot: dict[str, Any]) -> dict[str, Any]:
    status = str(slot.get("status") or "")
    semantic_status = str(slot.get("semantic_status") or "")
    if status != "success" or semantic_status != "success":
        raise AssertionError(
            "promo_by_price[today_current] is not healthy: "
            f"status={status!r} semantic_status={semantic_status!r} "
            f"origin={slot.get('origin')!r} note_kind={slot.get('note_kind')!r} "
            f"requested_count={slot.get('requested_count')!r} covered_count={slot.get('covered_count')!r}"
        )

    requested_count = _int_or_none(_counter(slot, "requested_count"))
    covered_count = _int_or_none(_counter(slot, "covered_count"))
    if requested_count is not None and covered_count is not None and requested_count != covered_count:
        raise AssertionError(
            "promo_by_price[today_current] must cover all requested rows when successful, "
            f"got covered_count={covered_count}, requested_count={requested_count}"
        )

    fatal_count = _int_or_none(_counter(slot, "fatal_missing_artifact_count"))
    if fatal_count not in (None, 0):
        raise AssertionError(f"fatal_missing_artifact_count must be 0, got {fatal_count}")
    true_loss_count = _int_or_none(_counter(slot, "true_artifact_loss_count"))
    if true_loss_count not in (None, 0):
        raise AssertionError(f"true_artifact_loss_count must be 0, got {true_loss_count}")

    promo_diagnostics = slot.get("promo_diagnostics") or {}
    fatal_artifacts = _diagnostic_list(promo_diagnostics, "fatal_missing_artifacts")
    expected_artifacts = _diagnostic_list(promo_diagnostics, "expected_non_materializable_artifacts")
    missing_artifacts = _diagnostic_list(promo_diagnostics, "missing_campaign_artifacts")
    if fatal_artifacts:
        raise AssertionError(f"fatal_missing_artifacts must be empty for current live invariant, got {fatal_artifacts}")

    expected_keys = {_artifact_identity(item) for item in expected_artifacts if _artifact_identity(item)}
    fatal_keys = {_artifact_identity(item) for item in fatal_artifacts if _artifact_identity(item)}
    overlap = expected_keys.intersection(fatal_keys)
    if overlap:
        raise AssertionError(f"expected non-materializable artifacts are also fatal: {sorted(overlap)}")

    campaign_2242 = _find_campaign_2242(expected_artifacts + fatal_artifacts + missing_artifacts)
    if campaign_2242 is not None:
        campaign_key = _artifact_identity(campaign_2242)
        if campaign_key not in expected_keys:
            raise AssertionError(f"campaign 2242 must be expected non-materializable, got {campaign_2242}")
        artifact_state = str(campaign_2242.get("artifact_state") or "")
        reason = str(campaign_2242.get("validation_failure_reason") or campaign_2242.get("non_materializable_reason") or "")
        if artifact_state not in EXPECTED_NON_MATERIALIZABLE_STATES:
            raise AssertionError(f"campaign 2242 artifact_state must be ended/no-download, got {campaign_2242}")
        if reason not in EXPECTED_NON_MATERIALIZABLE_REASONS:
            raise AssertionError(f"campaign 2242 validation reason must be metadata-only ended/no-download, got {campaign_2242}")
        if campaign_2242.get("workbook_required") is not False:
            raise AssertionError(f"campaign 2242 must have workbook_required=false, got {campaign_2242}")

    fallback = promo_diagnostics.get("fallback") or {}
    if fallback:
        if fallback.get("candidate_rejected") is True:
            raise AssertionError(f"current promo candidate must not be rejected, got fallback={fallback}")
        if fallback.get("fallback_reason"):
            raise AssertionError(f"fallback must not be used as fresh success, got fallback={fallback}")

    return {
        "status": status,
        "semantic_status": semantic_status,
        "origin": slot.get("origin"),
        "note_kind": slot.get("note_kind"),
        "requested_count": requested_count,
        "covered_count": covered_count,
        "missing_count": _int_or_none(slot.get("missing_count")),
        "candidate_accepted": fallback.get("candidate_accepted"),
        "candidate_rejected": fallback.get("candidate_rejected"),
        "fallback_reason": fallback.get("fallback_reason"),
        "invalid_reason": fallback.get("invalid_reason"),
        "fatal_missing_artifact_count": fatal_count,
        "true_artifact_loss_count": true_loss_count,
        "non_materializable_expected_count": _int_or_none(_counter(slot, "non_materializable_expected_count")),
        "ended_without_download_count": _int_or_none(_counter(slot, "ended_without_download_count")),
        "expected_non_materializable_artifact_count": len(expected_artifacts),
        "fatal_missing_artifact_list_count": len(fatal_artifacts),
        "campaign_2242_present": campaign_2242 is not None,
        "campaign_2242_state": campaign_2242.get("artifact_state") if campaign_2242 else None,
        "campaign_2242_reason": campaign_2242.get("validation_failure_reason") if campaign_2242 else None,
        "campaign_2242_workbook_required": campaign_2242.get("workbook_required") if campaign_2242 else None,
    }


def _counter(slot: dict[str, Any], key: str) -> Any:
    if key in slot:
        return slot.get(key)
    promo_diagnostics = slot.get("promo_diagnostics") or {}
    counters = promo_diagnostics.get("counters") or {}
    if key in counters:
        return counters.get(key)
    summary = promo_diagnostics.get("artifact_validation_summary") or {}
    if key in summary:
        return summary.get(key)
    return promo_diagnostics.get(key)


def _diagnostic_list(promo_diagnostics: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = promo_diagnostics.get(key) or []
    if not isinstance(value, list):
        raise AssertionError(f"promo diagnostics field {key} must be a list, got {type(value).__name__}")
    return [item for item in value if isinstance(item, dict)]


def _artifact_identity(item: dict[str, Any]) -> str:
    return str(
        item.get("normalized_artifact_key")
        or item.get("archive_key")
        or item.get("campaign_key")
        or item.get("campaign_id")
        or item.get("promo_id")
        or ""
    )


def _find_campaign_2242(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if str(item.get("campaign_id") or "") == "2242" or str(item.get("promo_id") or "") == "2242":
            return item
        if "2242__pending__" in _artifact_identity(item):
            return item
    return None


def _assert_current_promo_rows(web_payload: dict[str, Any], *, today_current: str) -> list[dict[str, Any]]:
    rows = web_payload.get("rows")
    if not isinstance(rows, list):
        raise AssertionError("web-vitrina payload must expose rows[]")

    summaries: list[dict[str, Any]] = []
    metric_keys = CORE_PROMO_METRIC_KEYS + AGGREGATE_PROMO_METRIC_KEYS
    for metric_key in metric_keys:
        metric_rows = [row for row in rows if isinstance(row, dict) and row.get("metric_key") == metric_key]
        if not metric_rows:
            if metric_key in CORE_PROMO_METRIC_KEYS:
                raise AssertionError(f"web-vitrina missing required promo metric rows for {metric_key}")
            continue
        values = [
            (row.get("values_by_date") or {}).get(today_current)
            for row in metric_rows
            if isinstance(row.get("values_by_date"), dict)
        ]
        present_values = [value for value in values if value is not None and value != ""]
        if not present_values:
            raise AssertionError(f"web-vitrina current promo metric {metric_key} is all blank for {today_current}")
        summaries.append(
            {
                "metric_key": metric_key,
                "rows": len(metric_rows),
                "current_values_present": len(present_values),
                "current_nonzero": sum(1 for value in present_values if isinstance(value, (int, float)) and value != 0),
                "current_zero": sum(1 for value in present_values if isinstance(value, (int, float)) and value == 0),
                "current_blank_or_null": len(values) - len(present_values),
            }
        )
    return summaries


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AssertionError(f"expected integer-like value, got {value!r}") from None


if __name__ == "__main__":
    main()
