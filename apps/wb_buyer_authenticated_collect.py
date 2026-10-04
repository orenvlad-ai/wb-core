#!/usr/bin/env python3
"""Bounded, forward-only authenticated buyer price collection.

The configured Web Vitrina slots control cadence, independent of refresh
outcomes for other sources. This worker fetches its own official seller quote,
never edits seller prices, launches the SPP tester or uses an anonymous card.
"""

from __future__ import annotations

import argparse
from datetime import datetime, time as clock_time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_buyer_chrome_price, wb_buyer_chrome_runtime  # noqa: E402
from apps.sheet_vitrina_v1_auto_refresh_tick import (  # noqa: E402
    _build_web_auth_cookie, _poll_job, _post_json, _read_env_file,
)
from packages.adapters.prices_snapshot_block import HttpBackedPricesSnapshotSource  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.sheet_vitrina_v1_auto_refresh import SheetVitrinaV1AutoRefreshSchedulesBlock  # noqa: E402
from packages.application.wb_buyer_authenticated_observations import (  # noqa: E402
    append_observation, begin_publication, begin_run, classify_observation,
    finish_publication, finish_run, load_active_requested_nm_ids, run_ordinal,
)
from packages.business_time import business_date_from_timestamp  # noqa: E402
from packages.contracts.prices_snapshot_block import PricesSnapshotRequest  # noqa: E402


HOSTED_RUNTIME = Path("/opt/wb-core-runtime/state")
MAX_CHROME_ROWS = 40
MIN_AFTER_SLOT_SECONDS = 5 * 60
MAX_AFTER_SLOT_SECONDS = 45 * 60


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_seller_goods(payload: Mapping[str, Any], wanted: list[int], measured_at: str) -> dict[int, dict[str, Any]]:
    data = payload.get("data")
    goods = data.get("listGoods") if isinstance(data, Mapping) else None
    if not isinstance(goods, list):
        return {}
    result: dict[int, dict[str, Any]] = {}
    wanted_set = set(wanted)
    for good in goods:
        if not isinstance(good, Mapping) or type(good.get("nmID")) is not int or good["nmID"] not in wanted_set:
            continue
        sizes = good.get("sizes")
        if not isinstance(sizes, list) or len(sizes) > 40:
            continue
        safe_sizes = []
        for size in sizes:
            if not isinstance(size, Mapping):
                safe_sizes = []
                break
            safe_sizes.append({key: size.get(key) for key in ("sizeID", "price", "discountedPrice", "clubDiscountedPrice")})
        result[good["nmID"]] = {
            "measured_at": measured_at, "discount": good.get("discount"),
            "clubDiscount": good.get("clubDiscount"), "sizes": safe_sizes,
        }
    return result


def _requested_ids(runtime: RegistryUploadDbBackedRuntime) -> list[int]:
    return load_active_requested_nm_ids(runtime)


def _recent_configured_slot(runtime_dir: Path, now: datetime) -> tuple[str, str] | None:
    """Follow configured cadence without depending on unrelated refresh success."""
    schedules = SheetVitrinaV1AutoRefreshSchedulesBlock(runtime_dir=runtime_dir).build_payload().get("schedules") or []
    candidates = []
    for schedule in schedules:
        if not isinstance(schedule, Mapping) or not schedule.get("enabled", True):
            continue
        schedule_id = str(schedule.get("id") or "")
        try:
            hour, minute = (int(part) for part in str(schedule.get("local_time_hhmm") or "").split(":"))
            zone = ZoneInfo(str(schedule.get("timezone") or "Asia/Yekaterinburg"))
            local_now = now.astimezone(zone)
            due_local = datetime.combine(local_now.date(), clock_time(hour, minute), tzinfo=zone)
            if due_local > local_now:
                due_local -= timedelta(days=1)
            due = due_local.astimezone(timezone.utc)
        except (ValueError, TypeError, KeyError):
            continue
        age = (now - due).total_seconds()
        if schedule_id and MIN_AFTER_SLOT_SECONDS <= age <= MAX_AFTER_SLOT_SECONDS:
            candidates.append((due, schedule_id, due.isoformat()))
    if not candidates:
        return None
    _, schedule_id, due_at = max(candidates)
    return schedule_id, due_at


def _run_id(schedule_id: str, due_at: str) -> str:
    digest = hashlib.sha256(f"{schedule_id}|{due_at}".encode()).hexdigest()[:24]
    return f"buyer-auth-obs-{digest}"


def collect_once(
    runtime: RegistryUploadDbBackedRuntime, *, run_id: str, requested_nm_ids: list[int],
    seller_source: Any = None, buyer_read: Any = None, now: Any = _utc_now,
) -> dict[str, Any]:
    """Persist all requested rows, including explicit missing reasons, before publication."""
    if not requested_nm_ids:
        return {"status": "no_active_skus", "requested_count": 0}
    if getattr(runtime, "runtime_dir", None) == HOSTED_RUNTIME:
        # This append-only source is small, but must preserve the same root
        # reserve as the durable Chrome profile even before the browser starts.
        estimated_peak = 65_536 * len(requested_nm_ids)
        if wb_buyer_chrome_runtime._available(Path("/")) - estimated_peak < wb_buyer_chrome_runtime._root_reserve():
            return {"status": "storage_reserve", "requested_count": len(requested_nm_ids)}
    started_at = now()
    business_date = begin_run(runtime, run_id=run_id, started_at=started_at, requested_nm_ids=requested_nm_ids)
    source = seller_source or HttpBackedPricesSnapshotSource()
    reader = buyer_read or wb_buyer_chrome_price.read_prices
    seller: dict[int, dict[str, Any]] = {}
    seller_status = "unavailable"
    try:
        raw = source.fetch(PricesSnapshotRequest(snapshot_type="current", snapshot_date=business_date,
                                                 nm_ids=requested_nm_ids))
        seller = _sanitize_seller_goods(raw, requested_nm_ids, now())
        seller_status = "observed" if seller else "unavailable"
    except Exception:
        # Official adapter exceptions can include raw HTTP response bodies.
        # Never log or persist them; buyer observations remain useful alone.
        seller_status = "unavailable"
    # A slow first card can consume the five-minute browser budget. Rotate
    # the *start* even for today's 33-SKU roster so later slots reach the
    # tail. The step is coprime with the roster size, eventually reaching all
    # positions; the first repeat starts near the previous tail.
    roster_size = len(requested_nm_ids)
    rotation_step = max(1, roster_size - 8)
    while math.gcd(rotation_step, roster_size) != 1:
        rotation_step -= 1
    offset = run_ordinal(runtime, run_id) * rotation_step % roster_size
    rotated = requested_nm_ids[offset:] + requested_nm_ids[:offset]
    selected = rotated[:MAX_CHROME_ROWS]
    buyer_rows: dict[int, dict[str, Any]] = {}
    try:
        for row in reader(selected):
            if isinstance(row, dict) and type(row.get("nm_id")) is int and row["nm_id"] in selected:
                buyer_rows.setdefault(row["nm_id"], row)
    except Exception:
        pass  # Missing selected rows are explicitly persisted below.
    observed_count = 0
    effective_count = 0
    attempted_dates: set[str] = set()
    reader_lifecycle = "clean"
    status = "partial"
    try:
        # A reader can reject before launching Chrome (busy, reserve, orphan).
        # Still record the current opaque login generation on its unavailable
        # rows, so a failed first read after relogin invalidates older prices.
        fallback_references = wb_buyer_chrome_price.current_context_references()
        for nm_id in requested_nm_ids:
            row = buyer_rows.get(nm_id)
            if row is None:
                reason = "batch_chunk_deferred" if nm_id not in selected else "buyer_reader_failed"
                row = {"nm_id": nm_id, "measured_at": now(), "status": "price_unavailable", "reason": reason,
                       "session_status": "probe_error", "authenticated_session_proof": False}
            row = {**fallback_references, **row}
            observation = classify_observation(nm_id=nm_id, buyer=row, seller=seller.get(nm_id))
            append_observation(runtime, run_id=run_id, observation=observation)
            # A new login context with zero measured prices must still clear
            # previously published prices from the prior context.
            attempted_dates.add(business_date_from_timestamp(observation["measured_at"]))
            if observation["reader_lifecycle_status"] != "clean":
                reader_lifecycle = observation["reader_lifecycle_status"]
            observed_count += observation["status"] == "observed"
            effective_count += observation["effective_nonwallet_discount"] is not None
        status = "completed" if observed_count == len(requested_nm_ids) and reader_lifecycle == "clean" else "partial"
    finally:
        finish_run(runtime, run_id=run_id, finished_at=now(), status=status)
    return {"status": status, "run_id": run_id, "business_date": business_date,
            "requested_count": len(requested_nm_ids), "buyer_covered_count": observed_count,
            "effective_discount_covered_count": effective_count, "seller_status": seller_status,
            "reader_lifecycle_status": reader_lifecycle, "publication_business_dates": sorted(attempted_dates)}


def publish_once(runtime: RegistryUploadDbBackedRuntime, *, run_id: str, business_date: str,
                 base_url: str = "http://127.0.0.1:8765", cookie: str,
                 post_json: Any = _post_json, poll_job: Any = _poll_job,
                 now: Any = _utc_now) -> dict[str, str]:
    """Use the existing Web Vitrina group job and its CAS merge, once per run/date."""
    fresh, previous_status = begin_publication(runtime, run_id=run_id, business_date=business_date, requested_at=now())
    if not fresh:
        return {"status": "already_completed" if previous_status == "completed" else "ambiguous_previous_attempt"}
    status, job_id = "ambiguous", ""
    try:
        launched = post_json(
            base_url + "/v1/sheet-vitrina-v1/web-vitrina/group-refresh",
            {"source_group_id": "wb_buyer_authenticated", "as_of_date": business_date},
            cookie=cookie, timeout=60,
        )
        job_id = str(launched.get("job_id") or "")[:100]
        if launched.get("single_flight") and not job_id:
            status = "blocked"
        elif job_id:
            terminal = poll_job(base_url=base_url, job_path="/v1/sheet-vitrina-v1/job",
                                job_id=job_id, cookie=cookie, timeout_seconds=900, poll_seconds=5)
            status = "completed" if str(terminal.get("status") or "").lower() in {"success", "completed"} else "error"
        else:
            status = "completed" if str(launched.get("status") or "").lower() in {"success", "completed"} else "ambiguous"
    except Exception:
        # The POST may have reached the server. No automatic repeat.
        status = "ambiguous"
    finally:
        finish_publication(runtime, run_id=run_id, business_date=business_date,
                           status=status, job_id=job_id, completed_at=now())
    return {"status": status, "job_id": job_id}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default=str(HOSTED_RUNTIME))
    parser.add_argument("--tick", action="store_true")
    parser.add_argument("--run-now", action="store_true")
    args = parser.parse_args()
    if args.tick == args.run_now:
        parser.error("choose exactly one of --tick and --run-now")
    os.environ.update({key: value for key, value in _read_env_file(Path("/opt/wb-ai/.env")).items() if key not in os.environ})
    runtime_dir = Path(args.runtime_dir)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir)
    if args.tick:
        due = _recent_configured_slot(runtime_dir, datetime.now(timezone.utc))
        if due is None:
            print(json.dumps({"status": "no_recent_configured_slot"}))
            return 0
        run_id = _run_id(*due)
    else:
        run_id = "buyer-auth-obs-manual-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        result = collect_once(runtime, run_id=run_id, requested_nm_ids=_requested_ids(runtime))
    except Exception as error:
        # SQLite details, WB payloads and exception strings are never emitted.
        if "UNIQUE constraint failed" in str(error):
            print(json.dumps({"status": "already_collected", "run_id": run_id}))
            return 0
        print(json.dumps({"status": "collector_failed", "stage": "persist_or_read", "error_class": type(error).__name__}))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    if result["status"] == "storage_reserve":
        return 1
    if result.get("publication_business_dates"):
        cookie = _build_web_auth_cookie(os.environ, required=True)
        all_ok = True
        for publication_date in result["publication_business_dates"]:
            published = publish_once(runtime, run_id=run_id, business_date=publication_date, cookie=cookie)
            print(json.dumps({"publication_status": published["status"], "run_id": run_id,
                              "business_date": publication_date, "job_id": published.get("job_id", "")}, separators=(",", ":")))
            all_ok &= published["status"] in {"completed", "already_completed"}
        return 0 if all_ok else 1
    return 0


from packages.application.business_data_procedure_admission import guard_cli

main = guard_cli(default_runtime='.runtime/registry_upload')(main)


if __name__ == "__main__":
    raise SystemExit(main())
