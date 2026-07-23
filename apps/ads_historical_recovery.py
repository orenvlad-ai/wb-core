#!/usr/bin/env python3
"""Dry-run/apply/readback CLI for bounded historical ``ads_compact`` recovery."""

from __future__ import annotations

import argparse
from datetime import date
import json
import math
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import time
from typing import Any, Sequence
from urllib import error, parse, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ads_historical_recovery import (  # noqa: E402
    AdsHistoricalRecovery,
    AdsHistoricalRecoveryError,
    AdsHistoricalNoStatisticsError,
    AdsHistoricalRecoveryScope,
    DEFAULT_NM_IDS,
    DEFAULT_TARGET_DATES,
    MAX_IDS_PER_REQUEST,
    MAX_WINDOW_DAYS,
    MIN_REQUEST_INTERVAL_SECONDS,
)
from packages.application.registry_upload_db_backed_runtime import DB_FILENAME  # noqa: E402
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)


class OfficialAdsHistoricalSource:
    """Minimal official WB promotion/fullstats client with a global 3-rpm gate."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://advert-api.wildberries.ru",
        timeout_seconds: float = 60.0,
        min_request_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        if (
            not math.isfinite(min_request_interval_seconds)
            or min_request_interval_seconds < MIN_REQUEST_INTERVAL_SECONDS
        ):
            raise AdsHistoricalRecoveryError(
                "official fullstats minimum interval must be at least 20 seconds (3 rpm)"
            )
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise AdsHistoricalRecoveryError("official ads timeout must be finite and positive")
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self.min_request_interval_seconds = float(min_request_interval_seconds)
        self._last_fullstats_request: float | None = None

    def list_campaigns(self) -> Any:
        return self._get_json(f"{self._base_url}/adv/v1/promotion/count")

    def fetch_fullstats(
        self, *, campaign_ids: Sequence[int], date_from: date, date_to: date
    ) -> Any:
        if not campaign_ids or len(campaign_ids) > MAX_IDS_PER_REQUEST:
            raise AdsHistoricalRecoveryError(
                f"fullstats requires 1..{MAX_IDS_PER_REQUEST} campaign IDs"
            )
        if (date_to - date_from).days + 1 > MAX_WINDOW_DAYS:
            raise AdsHistoricalRecoveryError(
                f"fullstats period exceeds {MAX_WINDOW_DAYS} inclusive days"
            )
        self._wait_for_fullstats_slot()
        query = parse.urlencode(
            {
                "ids": ",".join(str(value) for value in campaign_ids),
                "beginDate": date_from.isoformat(),
                "endDate": date_to.isoformat(),
            }
        )
        try:
            return self._get_json(
                f"{self._base_url}/adv/v3/fullstats?{query}",
                allow_http_200_null_no_statistics=True,
            )
        finally:
            self._last_fullstats_request = time.monotonic()

    def _wait_for_fullstats_slot(self) -> None:
        if self._last_fullstats_request is None:
            return
        elapsed = time.monotonic() - self._last_fullstats_request
        remaining = self.min_request_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _get_json(
        self, url: str, *, allow_http_200_null_no_statistics: bool = False
    ) -> Any:
        if not self._token:
            raise AdsHistoricalRecoveryError("WB_API_TOKEN is required")
        req = urllib_request.Request(
            url=url,
            headers={"Authorization": self._token},
            method="GET",
        )
        try:
            with urllib_request.urlopen(req, timeout=self._timeout_seconds) as response:
                body = response.read()
                response_status = int(getattr(response, "status", 0) or 0)
                response_content_type = str(
                    getattr(response, "headers", {}).get("Content-Type", "") or ""
                )
        except error.HTTPError as exc:
            body = exc.read()
            if exc.code == 400 and _is_confirmed_no_statistics_http_400(body):
                raise AdsHistoricalNoStatisticsError(
                    "official fullstats confirmed no statistics for this advertising period",
                    signal="structured_no_statistics_envelope",
                ) from exc
            raise AdsHistoricalRecoveryError(
                f"official ads request failed with HTTP {exc.code}"
            ) from exc
        except error.URLError as exc:
            raise AdsHistoricalRecoveryError(
                f"official ads request transport failed: {exc.reason}"
            ) from exc
        if allow_http_200_null_no_statistics and _is_confirmed_no_statistics_http_200_null(
            body,
            status=response_status,
            content_type=response_content_type,
        ):
            raise AdsHistoricalNoStatisticsError(
                "official fullstats HTTP 200 JSON null confirmed no statistics "
                "for this exact campaign window",
                signal="http_200_application_json_null",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdsHistoricalRecoveryError(
                "official ads request returned invalid JSON"
            ) from exc
        if _is_confirmed_no_statistics_payload(payload):
            raise AdsHistoricalNoStatisticsError(
                "official fullstats confirmed no statistics for this advertising period",
                signal="structured_no_statistics_envelope",
            )
        return payload


def _is_confirmed_no_statistics_http_400(body: bytes) -> bool:
    """Recognize only WB's exact structured no-statistics response."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _is_confirmed_no_statistics_payload(payload)


def _is_confirmed_no_statistics_http_200_null(
    body: bytes, *, status: int, content_type: str
) -> bool:
    """Recognize the exact production-observed singleton no-statistics sentinel."""

    media_type = content_type.partition(";")[0].strip().casefold()
    return status == 200 and media_type == "application/json" and body.strip() == b"null"


def _is_confirmed_no_statistics_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    detail = str(payload.get("detail") or "").strip().casefold()
    origin = str(payload.get("origin") or "").strip().casefold()
    try:
        status = int(payload.get("status"))
    except (TypeError, ValueError):
        return False
    return (
        status == 400
        and origin == "camp-api-public-cache"
        and detail == "there are no statistics for this advertising period"
    )


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in os.environ:
            continue
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = []
        os.environ[key] = parsed[0] if parsed else value.strip().strip("\"'")


def _scope_from_args(args: argparse.Namespace) -> AdsHistoricalRecoveryScope:
    nm_ids = args.nm_id if args.nm_id else list(DEFAULT_NM_IDS)
    target_dates = (
        [date.fromisoformat(value) for value in args.target_date]
        if args.target_date
        else list(DEFAULT_TARGET_DATES)
    )
    return AdsHistoricalRecoveryScope.build(
        nm_ids=nm_ids,
        target_dates=target_dates,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        default=os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload"),
    )
    parser.add_argument("--env-file", default="/opt/wb-ai/.env")
    parser.add_argument("--nm-id", action="append", type=int, default=[])
    parser.add_argument(
        "--target-date",
        action="append",
        default=[],
        help="exact YYYY-MM-DD target; repeat for every approved missing date",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--readback", action="store_true")
    parser.add_argument("--confirm-fingerprint", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--reviewed-plan-stdin", action="store_true")
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "WB_ADVERT_API_BASE_URL", "https://advert-api.wildberries.ru"
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--min-request-interval-seconds",
        type=float,
        default=MIN_REQUEST_INTERVAL_SECONDS,
    )
    args = parser.parse_args(argv)

    if args.apply:
        if not args.confirm_fingerprint:
            parser.error("--apply requires --confirm-fingerprint from the exact dry-run")
        if not args.approval_reference:
            parser.error("--apply requires a fresh --approval-reference")
        if not args.backup_dir:
            parser.error("--apply requires an explicit --backup-dir")
        if not args.reviewed_plan_stdin:
            parser.error("--apply requires --reviewed-plan-stdin")

    try:
        _load_env(Path(args.env_file))
        scope = _scope_from_args(args)
        runtime_dir = Path(args.runtime_dir)
        db_path = runtime_dir / DB_FILENAME
        source = OfficialAdsHistoricalSource(
            token=os.environ.get("WB_API_TOKEN", ""),
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            min_request_interval_seconds=args.min_request_interval_seconds,
        )
        recovery = AdsHistoricalRecovery(db_path=db_path, source=source)
        if args.readback:
            result = recovery.readback(scope)
        elif not args.apply:
            result = recovery.plan(scope)
        else:
            reviewed_plan = json.load(sys.stdin)
            # Serialize exact reviewed-plan validation -> coherent backup ->
            # transaction -> readback with every warehouse writer.
            with warehouse_functional_write_lock(runtime_dir):
                result = recovery.apply(
                    scope,
                    reviewed_plan=reviewed_plan,
                    expected_fingerprint=args.confirm_fingerprint,
                    approval_reference=args.approval_reference,
                    backup_dir=Path(args.backup_dir),
                )
    except (AdsHistoricalRecoveryError, OSError, sqlite3.Error, ValueError) as exc:
        result = {"status": "error", "error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "error":
        return 1
    if args.readback and (
        result.get("status") != "ready" or bool(result.get("blockers"))
    ):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
