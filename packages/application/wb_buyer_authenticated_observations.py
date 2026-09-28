"""Forward-only, account-scoped WB buyer price observations.

This source never substitutes anonymous card prices.  Its calculated
non-wallet difference is not the separately identifiable WB discount (SPP).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from zoneinfo import ZoneInfo

from packages.business_time import business_date_from_timestamp


SOURCE_KEY = "wb_buyer_authenticated"
MAX_REQUESTED = 1000  # Denominator is never silently clipped to the Chrome batch limit.
MAX_SELLER_SKEW_SECONDS = 300
BUSINESS_ZONE = ZoneInfo("Asia/Yekaterinburg")


def _connect(runtime: Any, *, write: bool = False) -> sqlite3.Connection:
    path = runtime.db_path
    connection = sqlite3.connect(f"file:{path}?mode={'rw' if write else 'ro'}", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    if not write:
        connection.execute("PRAGMA query_only=ON")
    return connection


def load_active_requested_nm_ids(runtime: Any) -> list[int]:
    """Read the collector roster without the runtime's schema-writing loader."""
    with _connect(runtime) as connection:
        rows = connection.execute("""
            SELECT config.nm_id FROM registry_upload_config_v2 AS config
            JOIN registry_upload_current_state AS current
              ON current.bundle_version=config.bundle_version AND current.slot=1
            WHERE config.enabled=1 ORDER BY config.nm_id
        """).fetchall()
    return [int(row[0]) for row in rows]


def load_source_requested_nm_ids(runtime: Any, business_date: str) -> tuple[list[int], str]:
    """Use a frozen collection roster, or the immutable bundle active that day.

    An imported single-card diagnostic is not a 1-SKU operational roster.
    When the historical eligible roster cannot be proved, return no claimed
    denominator. The caller reports not_available/eligible_roster_unknown;
    an absence of rows can never become a 0/0 success.
    """
    day_end = datetime.fromisoformat(business_date).replace(tzinfo=BUSINESS_ZONE) + timedelta(days=1)
    with _connect(runtime) as connection:
        try:
            runs = connection.execute("""
                SELECT run_id,started_at,requested_nm_ids_json FROM wb_buyer_authenticated_runs
                WHERE business_date=? ORDER BY started_at DESC,run_id DESC
            """, (business_date,)).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error).lower():
                raise
            runs = []
        for run in runs:
            if str(run["run_id"]).startswith("buyer-auth-evidence-"):
                continue
            scope = [int(item) for item in json.loads(run["requested_nm_ids_json"])]
            return sorted(set(scope)), "frozen_collection_run"
        # Single-card imports retain their original observation timestamp.
        # A bundle activated later that day cannot retroactively define their
        # eligible denominator. For an empty day, only describe the roster at
        # the end of that day; no measurement claim is made.
        observed_at = _timestamp(str(runs[0]["started_at"])) if runs else None
        cutoff = observed_at or day_end.astimezone(timezone.utc)
        versions = connection.execute("SELECT bundle_version,activated_at FROM registry_upload_versions").fetchall()
        eligible_versions = []
        for version in versions:
            try:
                activated = _timestamp(str(version["activated_at"]))
            except ValueError:
                continue
            within_cutoff = activated <= cutoff if observed_at else activated < cutoff
            if within_cutoff:
                eligible_versions.append((activated, str(version["bundle_version"])))
        if eligible_versions:
            _, bundle_version = max(eligible_versions)
            rows = connection.execute("""
                SELECT nm_id FROM registry_upload_config_v2
                WHERE bundle_version=? AND enabled=1 ORDER BY nm_id
            """, (bundle_version,)).fetchall()
            scope = sorted({int(row[0]) for row in rows})
            if scope:
                return scope, "versioned_at_observation" if observed_at else "versioned_day_bundle"
    return [], "unknown_historical_roster"


def ensure_schema(runtime: Any) -> None:
    """Create only the two source-owned tables in the active operational store."""
    with _connect(runtime, write=True) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS wb_buyer_authenticated_runs (
                run_id TEXT PRIMARY KEY,
                business_date TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                requested_nm_ids_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS wb_buyer_authenticated_runs_date
            ON wb_buyer_authenticated_runs(business_date, started_at);
            CREATE TABLE IF NOT EXISTS wb_buyer_authenticated_observations (
                run_id TEXT NOT NULL REFERENCES wb_buyer_authenticated_runs(run_id),
                nm_id INTEGER NOT NULL,
                business_date TEXT NOT NULL,
                measured_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, nm_id)
            );
            CREATE INDEX IF NOT EXISTS wb_buyer_authenticated_observations_nm_time
            ON wb_buyer_authenticated_observations(nm_id, measured_at);
            CREATE TABLE IF NOT EXISTS wb_buyer_authenticated_publications (
                run_id TEXT NOT NULL,
                business_date TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                status TEXT NOT NULL,
                job_id TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(run_id,business_date)
            );
        """)


def begin_run(runtime: Any, *, run_id: str, started_at: str, requested_nm_ids: list[int]) -> str:
    if not run_id or not 1 <= len(requested_nm_ids) <= MAX_REQUESTED or len(set(requested_nm_ids)) != len(requested_nm_ids):
        raise ValueError("invalid buyer observation run scope")
    if any(not isinstance(nm_id, int) or not 0 < nm_id < 1_000_000_000_000 for nm_id in requested_nm_ids):
        raise ValueError("invalid buyer observation nm_id")
    business_date = business_date_from_timestamp(started_at)
    ensure_schema(runtime)
    with _connect(runtime, write=True) as connection:
        connection.execute(
            "INSERT INTO wb_buyer_authenticated_runs(run_id,business_date,started_at,status,requested_nm_ids_json) VALUES(?,?,?,?,?)",
            (run_id, business_date, started_at, "running", json.dumps(requested_nm_ids, separators=(",", ":"))),
        )
    return business_date


def finish_run(runtime: Any, *, run_id: str, finished_at: str, status: str) -> None:
    if status not in {"completed", "partial", "error", "auth_unavailable", "busy"}:
        raise ValueError("invalid buyer observation run status")
    with _connect(runtime, write=True) as connection:
        result = connection.execute(
            "UPDATE wb_buyer_authenticated_runs SET finished_at=?,status=? WHERE run_id=? AND status='running'",
            (finished_at, status, run_id),
        )
        if result.rowcount != 1:
            raise ValueError("buyer observation run is not current")


def run_ordinal(runtime: Any, run_id: str) -> int:
    """Stable zero-based sequence for rotating the bounded browser start SKU."""
    with _connect(runtime) as connection:
        current = connection.execute(
            "SELECT started_at FROM wb_buyer_authenticated_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if current is None:
            raise ValueError("buyer observation run missing")
        count = connection.execute(
            "SELECT COUNT(*) FROM wb_buyer_authenticated_runs "
            "WHERE started_at < ? OR (started_at = ? AND run_id <= ?)",
            (current["started_at"], current["started_at"], run_id),
        ).fetchone()[0]
    return int(count) - 1


def begin_publication(runtime: Any, *, run_id: str, business_date: str, requested_at: str) -> tuple[bool, str]:
    """Reserve exactly one HTTP publication attempt; ambiguous POST is never retried."""
    with _connect(runtime, write=True) as connection:
        result = connection.execute(
            "INSERT OR IGNORE INTO wb_buyer_authenticated_publications(run_id,business_date,requested_at,status) VALUES(?,?,?,'launching')",
            (run_id, business_date, requested_at),
        )
        if result.rowcount == 1:
            return True, "launching"
        existing = connection.execute(
            "SELECT status FROM wb_buyer_authenticated_publications WHERE run_id=? AND business_date=?",
            (run_id, business_date),
        ).fetchone()
        return False, str(existing["status"] if existing is not None else "ambiguous")


def finish_publication(runtime: Any, *, run_id: str, business_date: str,
                       status: str, job_id: str = "", completed_at: str = "") -> None:
    if status not in {"completed", "error", "ambiguous", "blocked"}:
        raise ValueError("invalid buyer publication status")
    with _connect(runtime, write=True) as connection:
        connection.execute(
            "UPDATE wb_buyer_authenticated_publications SET status=?,job_id=?,completed_at=? WHERE run_id=? AND business_date=? AND status='launching'",
            (status, job_id[:100], completed_at, run_id, business_date),
        )


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0 < number < 10_000_000 else None


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value < 1_000_000_000 else None


def _seller_sizes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 40:
        return []
    rows = []
    for size in value:
        if not isinstance(size, Mapping) or not isinstance(size.get("sizeID"), int):
            return []
        rows.append({
            "size_id": size["sizeID"],
            "price_rub": _positive(size.get("price")),
            "discounted_price_rub": _positive(size.get("discountedPrice")),
            "club_discounted_price_rub": _positive(size.get("clubDiscountedPrice")),
        })
    return rows


def classify_observation(
    *, nm_id: int, buyer: Mapping[str, Any], seller: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep raw labelled prices; calculate only an explicitly qualified delta."""
    measured_at = str(buyer.get("measured_at") or "")
    _timestamp(measured_at)
    wallet = _positive(buyer.get("wallet_price"))
    nonwallet = _positive(buyer.get("normal_price"))
    authenticated = buyer.get("authenticated_session_proof") is True and str(buyer.get("session_status")) == "authenticated_surface"
    observed = str(buyer.get("status")) == "observed" and authenticated and wallet is not None and nonwallet is not None
    seller_sizes = _seller_sizes((seller or {}).get("sizes"))
    session_checked_at = str(buyer.get("session_checked_at") or "")
    if session_checked_at:
        _timestamp(session_checked_at)
    profile_reference = str(buyer.get("profile_reference") or "")
    auth_run_reference = str(buyer.get("auth_run_reference") or "")
    if len(profile_reference) > 100 or len(auth_run_reference) > 100:
        raise ValueError("invalid buyer proof reference")
    lifecycle = str(buyer.get("reader_lifecycle_status") or "clean")
    if lifecycle not in {"clean", "interrupted", "cleanup_failed", "resource_busy"}:
        raise ValueError("invalid buyer reader lifecycle")
    destination = buyer.get("destination_context")
    destination_id = _nonnegative_int(destination.get("dest_id")) if isinstance(destination, Mapping) else None
    result: dict[str, Any] = {
        "nm_id": nm_id, "measured_at": measured_at,
        "status": "observed" if observed else "unavailable",
        "reason": "" if observed else str(buyer.get("reason") or "authenticated_price_unavailable")[:80],
        "buyer_wallet_price_rub": wallet if observed else None,
        "buyer_nonwallet_price_rub": nonwallet if observed else None,
        "effective_nonwallet_discount": None,
        "effective_discount_reason": "seller_context_unavailable",
        "authenticated_spp": None,
        "spp_evidence": "components_unknown",
        "account_identity": "unverified",
        "destination_context": {"status": "identified", "dest_id": destination_id} if destination_id is not None else {"status": "unknown"},
        "variant_context": "unknown",
        "currency": "RUB",
        "session_checked_at": session_checked_at,
        "profile_reference": profile_reference,
        "auth_run_reference": auth_run_reference,
        "reader_lifecycle_status": lifecycle,
        "buyer_source": "durable_chrome_visible_price_detail",
        "seller_source": "official_goods_filter" if seller else "unavailable",
        "seller_measured_at": str((seller or {}).get("measured_at") or ""),
        "seller_discount_pct": _nonnegative_int((seller or {}).get("discount")),
        "seller_club_discount_pct": _nonnegative_int((seller or {}).get("clubDiscount")),
        "seller_sizes": seller_sizes,
        "seller_discounted_price_rub": None,
        "seller_size_ids": [row["size_id"] for row in seller_sizes],
    }
    if not observed or not seller:
        return result
    if not seller_sizes:
        return result
    normalized = []
    for size in seller_sizes:
        value = size["discounted_price_rub"]
        size_id = size["size_id"]
        if value is None:
            return result
        normalized.append((size_id, value))
    if len({item[1] for item in normalized}) != 1:
        result["effective_discount_reason"] = "seller_variants_have_different_prices"
        return result
    result["variant_context"] = "single_size" if len(normalized) == 1 else "uniform_seller_sizes"
    baseline = normalized[0][1]
    result["seller_discounted_price_rub"] = baseline
    try:
        skew = abs((_timestamp(str(seller.get("measured_at") or "")) - _timestamp(measured_at)).total_seconds())
    except ValueError:
        result["effective_discount_reason"] = "seller_timestamp_missing"
        return result
    if skew > MAX_SELLER_SKEW_SECONDS:
        result["effective_discount_reason"] = "seller_buyer_timestamp_skew"
        return result
    if nonwallet > baseline:
        result["effective_discount_reason"] = "buyer_price_exceeds_seller_discounted"
        return result
    result["effective_nonwallet_discount"] = round((baseline - nonwallet) / baseline, 6)
    result["effective_discount_reason"] = "calculated_current_profile_nonwallet"
    return result


def import_verified_prior_evidence(runtime: Any, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Import one actual earlier diagnostic with its original timestamps.

    The caller must independently verify the private file's SHA256 before this
    single DB mutation.  No measurement time or account identity is invented.
    """
    evidence_id = str(evidence.get("evidence_id") or "")
    nm_id = evidence.get("nm_id")
    buyer_raw = evidence.get("buyer")
    seller_raw = evidence.get("seller")
    if not evidence_id.startswith("wbc0081-") or type(nm_id) is not int or not isinstance(buyer_raw, Mapping) or not isinstance(seller_raw, Mapping):
        raise ValueError("invalid prior buyer evidence")
    if buyer_raw.get("authenticated_surface") is not True:
        raise ValueError("prior buyer evidence lacks authenticated surface")
    measured_at = str(buyer_raw.get("measured_at") or "")
    if business_date_from_timestamp(measured_at) != str(evidence.get("business_date") or ""):
        raise ValueError("prior buyer evidence date mismatch")
    sizes = seller_raw.get("sizes")
    if not isinstance(sizes, list):
        raise ValueError("prior seller size evidence missing")
    buyer = {
        "status": "observed", "session_status": "authenticated_surface",
        "authenticated_session_proof": True, "measured_at": measured_at,
        "wallet_price": buyer_raw.get("wallet_price_rub"),
        "normal_price": buyer_raw.get("nonwallet_price_rub"),
    }
    prior_destination = buyer_raw.get("destination")
    if isinstance(prior_destination, Mapping):
        buyer["destination_context"] = {"dest_id": prior_destination.get("dest_id")}
    seller = {
        "measured_at": str(seller_raw.get("measured_at") or ""),
        "discount": seller_raw.get("discount"), "clubDiscount": seller_raw.get("club_discount"),
        "sizes": [{"sizeID": size.get("size_id"), "price": size.get("price_rub"),
                   "discountedPrice": size.get("discounted_price_rub"),
                   "clubDiscountedPrice": size.get("club_discounted_price_rub")}
                  for size in sizes if isinstance(size, Mapping)],
    }
    observation = classify_observation(nm_id=nm_id, buyer=buyer, seller=seller)
    if observation["status"] != "observed":
        raise ValueError("prior buyer evidence has no observed price")
    observation["buyer_source"] = "verified_prior_live_diagnostic_evidence"
    observation["evidence_id"] = evidence_id
    run_id = "buyer-auth-evidence-" + hashlib.sha256(evidence_id.encode()).hexdigest()[:24]
    begin_run(runtime, run_id=run_id, started_at=measured_at, requested_nm_ids=[nm_id])
    append_observation(runtime, run_id=run_id, observation=observation)
    finish_run(runtime, run_id=run_id, finished_at=measured_at, status="completed")
    return {"run_id": run_id, "business_date": business_date_from_timestamp(measured_at), "nm_id": nm_id}


def append_observation(runtime: Any, *, run_id: str, observation: Mapping[str, Any]) -> None:
    nm_id = observation.get("nm_id")
    measured_at = str(observation.get("measured_at") or "")
    if not isinstance(nm_id, int) or nm_id <= 0 or not measured_at:
        raise ValueError("invalid buyer observation")
    business_date = business_date_from_timestamp(measured_at)
    serialized = json.dumps(dict(observation), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized) > 16384:
        raise ValueError("buyer observation too large")
    with _connect(runtime, write=True) as connection:
        run = connection.execute("SELECT business_date,requested_nm_ids_json,status FROM wb_buyer_authenticated_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None or run["status"] != "running" or nm_id not in json.loads(run["requested_nm_ids_json"]):
            raise ValueError("buyer observation does not belong to active run/date")
        existing = connection.execute("SELECT payload_json FROM wb_buyer_authenticated_observations WHERE run_id=? AND nm_id=?", (run_id, nm_id)).fetchone()
        if existing is not None:
            if existing["payload_json"] == serialized:
                return
            raise ValueError("buyer observation already exists with different content")
        connection.execute("INSERT INTO wb_buyer_authenticated_observations(run_id,nm_id,business_date,measured_at,status,payload_json) VALUES(?,?,?,?,?,?)",
                           (run_id, nm_id, business_date, measured_at, str(observation.get("status") or "unavailable"), serialized))


def load_daily_projection(runtime: Any, business_date: str, requested_nm_ids: list[int]) -> dict[str, Any]:
    """Latest same-day valid value plus separate latest attempt, never public fallback."""
    wanted = list(dict.fromkeys(int(item) for item in requested_nm_ids))
    empty = {"snapshot_date": business_date, "kind": "empty", "requested_count": len(wanted),
             "covered_count": 0, "missing_nm_ids": wanted, "items": [],
             "diagnostics": {"first_observed_business_date": "", "first_run_business_date": "",
                             "latest_attempt_reason_by_nm_id": {}}}
    try:
        with _connect(runtime) as connection:
            first_run = connection.execute("SELECT MIN(business_date) FROM wb_buyer_authenticated_runs").fetchone()[0] or ""
            first_observed = connection.execute("SELECT MIN(business_date) FROM wb_buyer_authenticated_observations WHERE status='observed'").fetchone()[0] or ""
            rows = connection.execute("""SELECT o.nm_id,o.payload_json,o.status,o.measured_at,r.status AS run_status
                FROM wb_buyer_authenticated_observations o JOIN wb_buyer_authenticated_runs r ON r.run_id=o.run_id
                WHERE o.business_date=? ORDER BY o.measured_at DESC,o.run_id DESC""", (business_date,)).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return empty
        raise
    # A new completed login run is a new account context even if the same
    # profile directory is reused. Never compose one daily current snapshot
    # out of rows measured in two different login contexts.
    latest_context: tuple[str, str] | None = None
    for row in rows:
        payload = json.loads(row["payload_json"])
        if not payload.get("auth_run_reference"):
            continue
        latest_context = (str(payload.get("auth_run_reference") or ""),
                          str(payload.get("profile_reference") or ""))
        break
    if latest_context is None:
        for row in rows:
            if row["status"] != "observed":
                continue
            payload = json.loads(row["payload_json"])
            latest_context = ("", str(payload.get("profile_reference") or ""))
            break
    attempts: dict[int, dict[str, Any]] = {}
    observed: dict[int, dict[str, Any]] = {}
    for row in rows:
        nm_id = int(row["nm_id"])
        if nm_id not in wanted:
            continue
        payload = json.loads(row["payload_json"])
        attempts.setdefault(nm_id, payload)
        context = (str(payload.get("auth_run_reference") or ""),
                   str(payload.get("profile_reference") or ""))
        if row["status"] == "observed" and payload.get("buyer_nonwallet_price_rub") is not None and context == latest_context:
            observed.setdefault(nm_id, payload)
    items = [observed[nm_id] for nm_id in wanted if nm_id in observed]
    missing = [nm_id for nm_id in wanted if nm_id not in observed]
    latest_run_status = str(rows[0]["run_status"] or "") if rows else ""
    return {"snapshot_date": business_date, "kind": "success" if items and not missing and latest_run_status == "completed" else "incomplete" if items else "empty",
            "requested_count": len(wanted), "covered_count": len(items), "missing_nm_ids": missing, "items": items,
            "diagnostics": {"first_observed_business_date": first_observed, "first_run_business_date": first_run,
                            "latest_run_status": latest_run_status,
                            "current_auth_run_reference": latest_context[0] if latest_context else "",
                            "latest_attempt_reason_by_nm_id": {str(nm_id): str(payload.get("reason") or payload.get("effective_discount_reason") or "")
                                                               for nm_id, payload in attempts.items()},
                            "latest_attempt_measured_at_by_nm_id": {str(nm_id): str(payload.get("measured_at") or "")
                                                                   for nm_id, payload in attempts.items()}}}
