"""Sanitized diagnostics for WB Acceptance Expenses report as a transit-cost candidate."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV  # noqa: E402


DEFAULT_BASE_URL = "https://seller-analytics-api.wildberries.ru"
DEFAULT_TARGET_VALUES = (15523.72, 11543.52, 14062.54, 10726.11)
DEFAULT_TARGET_INCOME_IDS = ("39265519", "39265492", "39265590", "39265571")


def main() -> None:
    args = _parse_args()
    token = str(os.environ.get(args.token_env) or "").strip()
    if not token:
        raise SystemExit(f"{args.token_env} is required")
    if _period_days(args.date_from, args.date_to) > 31:
        raise SystemExit("Acceptance Expenses report period must be <= 31 days")

    client = AcceptanceExpensesClient(
        base_url=args.base_url,
        token=token,
        timeout_seconds=args.timeout_seconds,
    )
    report: dict[str, Any] = {
        "endpoint_family": "acceptance_report",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "raw_sensitive_values_printed": False,
        "create": {},
        "status_checks": [],
        "download": {},
        "match_summary": {},
    }
    try:
        create_payload = client.create_report(args.date_from, args.date_to)
        task_id = _extract_task_id(create_payload)
        report["create"] = {"status": "ok", "task_id_present": bool(task_id)}
        if not task_id:
            report["create"]["payload_keys"] = sorted(create_payload.keys()) if isinstance(create_payload, Mapping) else []
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            raise SystemExit(2)
        rows: list[Mapping[str, Any]] = []
        for attempt in range(max(1, args.poll_attempts)):
            if attempt:
                time.sleep(max(1.0, args.poll_interval_seconds))
            status_payload = client.check_status(task_id)
            status_value = _extract_task_status(status_payload)
            report["status_checks"].append({"attempt": attempt + 1, "status": status_value})
            if status_value == "done":
                rows = client.download_report(task_id)
                break
            if status_value in {"failed", "canceled", "cancelled"}:
                break
        report["download"] = {
            "status": "ok" if rows else "not_downloaded",
            "row_count": len(rows),
            "key_sets": _row_key_sets(rows),
        }
        report["match_summary"] = summarize_acceptance_expenses(
            rows,
            target_values=DEFAULT_TARGET_VALUES,
            target_income_ids=DEFAULT_TARGET_INCOME_IDS,
        )
    except WbReportsHttpError as exc:
        report["error"] = {
            "type": "http",
            "status_code": exc.status_code,
            "content_type": exc.content_type,
            "body_prefix": exc.body_prefix,
            "permission_blocker": exc.status_code in {401, 403},
        }
    except WbReportsTransportError as exc:
        report["error"] = {"type": "transport", "message": str(exc)[:500]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


class WbReportsHttpError(RuntimeError):
    def __init__(self, status_code: int, body: str, *, content_type: str = "") -> None:
        self.status_code = int(status_code)
        self.content_type = content_type
        self.body_prefix = _sanitize_body_prefix(body)
        super().__init__(f"WB reports API returned status {status_code}: {self.body_prefix}")


class WbReportsTransportError(RuntimeError):
    pass


class AcceptanceExpensesClient:
    def __init__(self, *, base_url: str, token: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def create_report(self, date_from: str, date_to: str) -> Mapping[str, Any]:
        query = urllib_parse.urlencode({"dateFrom": date_from, "dateTo": date_to})
        payload = self._request_json(f"{self.base_url}/api/v1/acceptance_report?{query}")
        if not isinstance(payload, Mapping):
            raise WbReportsTransportError("acceptance report create returned invalid JSON shape")
        return payload

    def check_status(self, task_id: str) -> Mapping[str, Any]:
        payload = self._request_json(f"{self.base_url}/api/v1/acceptance_report/tasks/{urllib_parse.quote(task_id, safe='')}/status")
        if not isinstance(payload, Mapping):
            raise WbReportsTransportError("acceptance report status returned invalid JSON shape")
        return payload

    def download_report(self, task_id: str) -> list[Mapping[str, Any]]:
        payload = self._request_json(
            f"{self.base_url}/api/v1/acceptance_report/tasks/{urllib_parse.quote(task_id, safe='')}/download",
            allow_no_data=True,
        )
        if payload is None:
            return []
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, Mapping)]
        raise WbReportsTransportError("acceptance report download returned invalid JSON shape")

    def _request_json(self, url: str, *, allow_no_data: bool = False) -> Any:
        req = urllib_request.Request(url=url, headers={"Authorization": self.token, "Accept": "application/json"}, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", response.getcode()) or 0)
                content_type = str(response.headers.get("Content-Type") or "").strip()
                raw_body = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if allow_no_data and exc.code == 204:
                return None
            raise WbReportsHttpError(exc.code, body, content_type=str(exc.headers.get("Content-Type") or "")) from exc
        except (urllib_error.URLError, OSError) as exc:
            raise WbReportsTransportError(f"WB reports API transport failed: {exc}") from exc
        if allow_no_data and status_code == 204:
            return None
        if status_code < 200 or status_code >= 300:
            raise WbReportsHttpError(status_code, raw_body, content_type=content_type)
        if not raw_body.strip():
            raise WbReportsTransportError("WB reports API returned empty response")
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WbReportsTransportError(
                f"WB reports API returned non-JSON response: content-type={content_type}; body_prefix={_sanitize_body_prefix(raw_body)}"
            ) from exc


def summarize_acceptance_expenses(
    rows: list[Mapping[str, Any]],
    *,
    target_values: tuple[float, ...],
    target_income_ids: tuple[str, ...],
) -> dict[str, Any]:
    by_income: dict[str, float] = defaultdict(float)
    row_count_by_income: dict[str, int] = defaultdict(int)
    value_matches: list[dict[str, Any]] = []
    target_value_set = {round(float(value), 2) for value in target_values}
    for row in rows:
        income_id = str(row.get("incomeId") or row.get("incomeID") or row.get("income_id") or "").strip()
        total = _optional_float(row.get("total"))
        if income_id and total is not None:
            by_income[income_id] += total
            row_count_by_income[income_id] += 1
        if total is not None and round(total, 2) in target_value_set:
            value_matches.append(_compact_row(row))
    target_income_totals = {
        income_id: {
            "total": round(by_income.get(income_id, 0.0), 2),
            "row_count": row_count_by_income.get(income_id, 0),
            "target_value_match": round(by_income.get(income_id, 0.0), 2) in target_value_set,
        }
        for income_id in target_income_ids
    }
    aggregate_matches = [
        {"incomeId": income_id, "total": round(total, 2), "row_count": row_count_by_income.get(income_id, 0)}
        for income_id, total in sorted(by_income.items())
        if round(total, 2) in target_value_set
    ]
    return {
        "target_values": list(target_values),
        "target_income_totals": target_income_totals,
        "row_value_matches": value_matches[:20],
        "aggregate_value_matches": aggregate_matches[:20],
        "unique_income_id_count": len(by_income),
    }


def _compact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("incomeId", "incomeID", "nmID", "count", "total", "giCreateDate", "shkCreateDate", "subjectName")
    return {key: row.get(key) for key in keys if key in row}


def _extract_task_id(payload: Mapping[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    return str(data.get("taskId") or data.get("task_id") or data.get("id") or "").strip()


def _extract_task_status(payload: Mapping[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    return str(data.get("status") or "").strip().lower()


def _row_key_sets(rows: list[Mapping[str, Any]]) -> list[list[str]]:
    result: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        keys = tuple(sorted(str(key) for key in row.keys()))
        if keys not in seen:
            seen.add(keys)
            result.append(list(keys))
    return result[:10]


def _period_days(date_from: str, date_to: str) -> int:
    return (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sanitize_body_prefix(body: str, *, limit: int = 500) -> str:
    text = " ".join(str(body or "").replace("\x00", "").split())
    return text[:limit]


def _parse_args() -> argparse.Namespace:
    default_to = date.today()
    default_from = default_to - timedelta(days=30)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", default=default_from.isoformat())
    parser.add_argument("--date-to", default=default_to.isoformat())
    parser.add_argument("--base-url", default=os.environ.get("WB_SELLER_ANALYTICS_API_BASE_URL") or DEFAULT_BASE_URL)
    parser.add_argument("--token-env", default=DEFAULT_WB_API_TOKEN_ENV)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-attempts", type=int, default=12)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    return parser.parse_args()


if __name__ == "__main__":
    main()
