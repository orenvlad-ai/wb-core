"""One-time DATA_VITRINA -> server history reconciler for factory-order orderCount coverage."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_sales_history import (
    describe_runtime_sales_history_coverage,
    extract_data_vitrina_order_count_window,
    load_runtime_sales_history_payloads,
    replace_runtime_sales_history_window,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess


DEFAULT_SPREADSHEET_ID = "1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE"
DEFAULT_SHEET_NAME = "DATA_VITRINA"
DEFAULT_DATE_FROM = "2026-03-01"
DEFAULT_DATE_TO = "2026-04-18"


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract-live-data-vitrina-window")
    extract_parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    extract_parser.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    extract_parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    extract_parser.add_argument("--date-to", default=DEFAULT_DATE_TO)
    extract_parser.add_argument("--output", required=True)

    reconcile_parser = subparsers.add_parser("reconcile-runtime-window")
    reconcile_parser.add_argument("--runtime-dir", required=True)
    reconcile_parser.add_argument("--input", required=True)
    reconcile_parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    reconcile_parser.add_argument("--date-to", default=DEFAULT_DATE_TO)
    reconcile_parser.add_argument(
        "--captured-at",
        default=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )

    diff_parser = subparsers.add_parser("diff-runtime-window")
    diff_parser.add_argument("--runtime-dir", required=True)
    diff_parser.add_argument("--input", required=True)
    diff_parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    diff_parser.add_argument("--date-to", default=DEFAULT_DATE_TO)

    args = parser.parse_args()
    if args.command == "extract-live-data-vitrina-window":
        _run_extract(args)
        return
    if args.command == "reconcile-runtime-window":
        _run_reconcile(args)
        return
    if args.command == "diff-runtime-window":
        _run_diff(args)
        return
    raise SystemExit(f"unsupported command: {args.command}")


def _run_extract(args: argparse.Namespace) -> None:
    token = _refresh_access_token()
    values = _fetch_sheet_values(
        access_token=token,
        spreadsheet_id=args.spreadsheet_id,
        sheet_name=args.sheet_name,
    )
    window = extract_data_vitrina_order_count_window(
        values,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    payload = {
        "spreadsheet_id": args.spreadsheet_id,
        "sheet_name": args.sheet_name,
        "date_from": window.date_from,
        "date_to": window.date_to,
        "summary": {
            "sku_count": window.sku_count,
            "day_count": window.day_count,
            "item_count": window.item_count,
            "total_row_mismatch_count": window.total_row_mismatch_count,
        },
        "exact_date_payloads": {
            snapshot_date: {
                "kind": payload.kind,
                "date_from": payload.date_from,
                "date_to": payload.date_to,
                "count": payload.count,
                "items": [asdict(item) for item in payload.items],
            }
            for snapshot_date, payload in window.exact_date_payloads.items()
        },
    }
    output_path = Path(args.output)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"extract: ok -> {output_path}")
    print(f"extract: date_window -> {window.date_from}..{window.date_to}")
    print(f"extract: sku_count -> {window.sku_count}")
    print(f"extract: day_count -> {window.day_count}")
    print(f"extract: item_count -> {window.item_count}")
    print(f"extract: total_row_mismatch_count -> {window.total_row_mismatch_count}")


def _run_reconcile(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    expected = _parse_exact_date_payloads(payload.get("exact_date_payloads") or {})
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))

    coverage_before = asdict(describe_runtime_sales_history_coverage(runtime))
    before = _diff_runtime_against_expected(
        runtime=runtime,
        date_from=args.date_from,
        date_to=args.date_to,
        expected=expected,
    )
    replace_summary = replace_runtime_sales_history_window(
        runtime=runtime,
        date_from=args.date_from,
        date_to=args.date_to,
        exact_date_payloads=expected,
        captured_at=args.captured_at,
    )
    after = _diff_runtime_against_expected(
        runtime=runtime,
        date_from=args.date_from,
        date_to=args.date_to,
        expected=expected,
    )
    out = {
        "requested_range": {
            "date_from": args.date_from,
            "date_to": args.date_to,
        },
        "expected_summary": {
            "snapshot_count": len(expected),
            "sku_count": len(_expected_nm_ids(expected)),
            "item_count": sum(len(item.items) for item in expected.values()),
        },
        "coverage_before": coverage_before,
        "before_diff": before,
        "replace_summary": replace_summary,
        "coverage_after": asdict(describe_runtime_sales_history_coverage(runtime)),
        "after_diff": after,
        "captured_at": args.captured_at,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def _run_diff(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    expected = _parse_exact_date_payloads(payload.get("exact_date_payloads") or {})
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    out = {
        "requested_range": {
            "date_from": args.date_from,
            "date_to": args.date_to,
        },
        "expected_summary": {
            "snapshot_count": len(expected),
            "sku_count": len(_expected_nm_ids(expected)),
            "item_count": sum(len(item.items) for item in expected.values()),
        },
        "coverage_before": asdict(describe_runtime_sales_history_coverage(runtime)),
        "before_diff": _diff_runtime_against_expected(
            runtime=runtime,
            date_from=args.date_from,
            date_to=args.date_to,
            expected=expected,
        ),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def _diff_runtime_against_expected(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
    expected: dict[str, SalesFunnelHistorySuccess],
) -> dict[str, Any]:
    current = load_runtime_sales_history_payloads(
        runtime=runtime,
        date_from=date_from,
        date_to=date_to,
    )
    missing_dates: list[str] = []
    missing_pairs = 0
    value_mismatch_count = 0
    sample_mismatches: list[str] = []
    expected_nm_ids = _expected_nm_ids(expected)
    for snapshot_date in sorted(expected):
        expected_map = _collect_order_count_map(expected[snapshot_date])
        current_payload = current.get(snapshot_date)
        current_map = _collect_order_count_map(current_payload)
        if current_payload is None or str(getattr(current_payload, "kind", "")) != "success":
            missing_dates.append(snapshot_date)
            missing_pairs += len(expected_map)
            continue
        missing_nm_ids = sorted(set(expected_map) - set(current_map))
        missing_pairs += len(missing_nm_ids)
        if missing_nm_ids and len(sample_mismatches) < 5:
            sample_mismatches.append(f"{snapshot_date}: missing_nm_ids={','.join(str(item) for item in missing_nm_ids)}")
        if missing_nm_ids:
            missing_dates.append(snapshot_date)
        for nm_id, expected_value in expected_map.items():
            current_value = current_map.get(nm_id)
            if current_value is None:
                continue
            if round(float(current_value), 6) != round(float(expected_value), 6):
                value_mismatch_count += 1
                if len(sample_mismatches) < 5:
                    sample_mismatches.append(
                        f"{snapshot_date}:{nm_id} expected={round(float(expected_value), 6)} current={round(float(current_value), 6)}"
                    )
    return {
        "expected_snapshot_count": len(expected),
        "expected_nm_id_count": len(expected_nm_ids),
        "current_snapshot_count": len(current),
        "missing_dates": missing_dates,
        "missing_pairs": missing_pairs,
        "value_mismatch_count": value_mismatch_count,
        "sample_mismatches": sample_mismatches,
    }


def _expected_nm_ids(expected: dict[str, SalesFunnelHistorySuccess]) -> list[int]:
    out: set[int] = set()
    for payload in expected.values():
        out.update(_collect_order_count_map(payload))
    return sorted(out)


def _parse_exact_date_payloads(raw_payloads: dict[str, Any]) -> dict[str, SalesFunnelHistorySuccess]:
    out: dict[str, SalesFunnelHistorySuccess] = {}
    for snapshot_date, raw_payload in sorted(raw_payloads.items()):
        items: list[SalesFunnelHistoryItem] = []
        for item in raw_payload.get("items") or []:
            items.append(
                SalesFunnelHistoryItem(
                    date=str(item["date"]),
                    nm_id=int(item["nm_id"]),
                    metric=str(item["metric"]),
                    value=float(item["value"]),
                )
            )
        out[snapshot_date] = SalesFunnelHistorySuccess(
            kind="success",
            date_from=str(raw_payload.get("date_from", snapshot_date)),
            date_to=str(raw_payload.get("date_to", snapshot_date)),
            count=len(items),
            items=items,
        )
    return out


def _collect_order_count_map(payload: Any | None) -> dict[int, float]:
    if payload is None:
        return {}
    out: dict[int, float] = {}
    for item in list(getattr(payload, "items", []) or []):
        metric = str(getattr(item, "metric", "") or "")
        nm_id = getattr(item, "nm_id", None)
        value = getattr(item, "value", None)
        if metric != "orderCount" or not isinstance(nm_id, int) or not isinstance(value, (int, float)):
            continue
        out[nm_id] = float(value)
    return out


def _fetch_sheet_values(
    *,
    access_token: str,
    spreadsheet_id: str,
    sheet_name: str,
) -> list[list[Any]]:
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{quote(sheet_name + '!A1:ZZZ', safe='!A:Z0-9')}"
        "?majorDimension=ROWS"
        "&valueRenderOption=UNFORMATTED_VALUE"
        "&dateTimeRenderOption=SERIAL_NUMBER"
    )
    payload = _curl_json(url, access_token=access_token)
    return list(payload.get("values") or [])


def _refresh_access_token() -> str:
    clasprc_path = Path.home() / ".clasprc.json"
    config = json.loads(clasprc_path.read_text(encoding="utf-8"))
    profile = config.get("tokens", {}).get("default")
    if not isinstance(profile, dict):
        raise ValueError("missing default clasp profile in ~/.clasprc.json")
    payload = json.loads(
        _run_command(
            [
                "curl",
                "-sS",
                "https://oauth2.googleapis.com/token",
                "-d",
                f"client_id={profile['client_id']}",
                "-d",
                f"client_secret={profile['client_secret']}",
                "-d",
                f"refresh_token={profile['refresh_token']}",
                "-d",
                "grant_type=refresh_token",
            ]
        )
    )
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError(f"unable to refresh Google access token: {payload}")
    return token


def _curl_json(url: str, *, access_token: str) -> dict[str, Any]:
    raw = _run_command(
        [
            "curl",
            "-sS",
            "-H",
            f"Authorization: Bearer {access_token}",
            "-w",
            "\\nHTTP_STATUS:%{http_code}",
            url,
        ]
    )
    body, status = raw.rsplit("\nHTTP_STATUS:", 1)
    code = int(status)
    if code >= 400:
        raise RuntimeError(f"HTTP {code}: {body.strip()}")
    if not body.strip():
        return {}
    return json.loads(body)


def _run_command(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


if __name__ == "__main__":
    main()
