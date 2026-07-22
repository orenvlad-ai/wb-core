"""Bounded read-only reconciliation for Partner Report Finance expenses.

The diagnostic deliberately reads the persisted raw Finance rows and indexed
Partner inputs without invoking a preview or changing runtime state.  It is an
evidence tool, not an alternative calculation source.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from packages.application.ads_snapshot_payload import resolve_ads_snapshot_payload
from packages.application.wb_finance_weekly import (
    _decimal,
    _nomenclature_identity_index,
    _resolve_finance_nm_id,
    classify_deduction,
)


ZERO = Decimal("0")
MONEY = Decimal("0.0001")
RATIO = Decimal("0.000000000001")
DIAGNOSTIC_VERSION = "partner_finance_production_diagnostic_v2"
ADS_SOURCE_KEY = "ads_compact"
ADS_SOURCE_ROLE = "accepted_closed_day_snapshot"
REQUIRED_SETTING_FIELDS = (
    "partner_share_pct",
    "invested_capital_rub",
    "replenishment_reserve_pct",
    "weekly_office_expense_rub",
    "tax_rate_pct",
    "common_expense_rule",
)
MARKETING_CANDIDATE_TOKENS = (
    "advert",
    "campaign",
    "cpm",
    "media",
    "marketing",
    "promo",
    "promotion",
    "баннер",
    "медиа",
    "промо",
)
MAX_IDENTITY_ANOMALY_KEYS = 10_000
MAX_ACCUMULATED_OPERATION_GROUPS = 10_000
MAX_ACCUMULATED_MARKETING_CANDIDATES = 10_000


class PartnerFinanceDiagnosticError(ValueError):
    """Fail-closed scope or source contract error."""


@dataclass(frozen=True)
class DiagnosticScope:
    database: Path
    seller_id: str = "canonical"
    nm_id: str = ""
    weeks: tuple[str, ...] = ()
    server_settings: bool = False
    max_weeks: int = 64
    max_groups: int = 200
    max_examples: int = 3


def run_partner_finance_diagnostic(scope: DiagnosticScope) -> dict[str, Any]:
    """Return deterministic bounded evidence from one coherent read snapshot."""

    database = scope.database.expanduser().resolve()
    if not database.is_file():
        raise PartnerFinanceDiagnosticError("runtime SQLite database does not exist")
    if scope.max_weeks < 1 or scope.max_weeks > 128:
        raise PartnerFinanceDiagnosticError("max_weeks must be between 1 and 128")
    if scope.max_groups < 1 or scope.max_groups > 500:
        raise PartnerFinanceDiagnosticError("max_groups must be between 1 and 500")
    if scope.max_examples < 1 or scope.max_examples > 10:
        raise PartnerFinanceDiagnosticError("max_examples must be between 1 and 10")

    uri = "file:" + quote(str(database), safe="/:") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=60)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise PartnerFinanceDiagnosticError("SQLite query_only could not be enabled")
        conn.execute("BEGIN")
        try:
            result = _run_in_snapshot(conn, scope=scope, database=database)
        finally:
            conn.rollback()
    return result


def _run_in_snapshot(
    conn: sqlite3.Connection,
    *,
    scope: DiagnosticScope,
    database: Path,
) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    }
    required = {
        "temporal_source_slot_snapshots",
        "wb_finance_weekly_raw_rows",
        "wb_finance_weekly_sku_aggregates",
        "wb_finance_weekly_sync",
    }
    missing = sorted(required - tables)
    if missing:
        raise PartnerFinanceDiagnosticError(
            "required diagnostic tables are missing: " + ", ".join(missing)
        )

    nm_id, weeks, selection = _resolve_scope(conn, scope=scope, tables=tables)
    if len(weeks) > scope.max_weeks:
        raise PartnerFinanceDiagnosticError(
            f"selected week count {len(weeks)} exceeds max_weeks={scope.max_weeks}"
        )
    week_bounds = _week_bounds(
        conn,
        seller_id=scope.seller_id,
        weeks=weeks,
    )
    blockers: list[dict[str, Any]] = []
    missing_weeks = [week for week in weeks if week not in week_bounds]
    blockers.extend(
        {"code": "finance_week_missing", "week_start": week}
        for week in missing_weeks
    )

    alias_to_nm, ambiguous_aliases, _groups, _items = _nomenclature_identity_index(
        conn
    )
    source_hasher = hashlib.sha256()
    source_hasher.update(DIAGNOSTIC_VERSION.encode("utf-8"))
    identity_mismatches: list[dict[str, str]] = []
    identity_mismatch_count = 0
    mismatch_keys: set[tuple[str, str]] = set()
    mismatch_bound_exceeded = False
    scanned_raw_row_count = 0

    week_results: list[dict[str, Any]] = []
    group_state: dict[tuple[str, ...], dict[str, Any]] = {}
    negative_state: dict[str, Any] = {
        "row_count": 0,
        "signed_deduction": ZERO,
        "system_amount": ZERO,
        "examples": [],
    }
    candidate_state: dict[str, dict[str, Any]] = {}
    invalid_raw_state: dict[str, Any] = {"row_count": 0, "examples": []}
    operation_group_bound_exceeded = False
    candidate_bound_exceeded = False

    for week in weeks:
        bounds = week_bounds.get(week)
        if bounds is None:
            continue
        sku_projection = _projection(
            conn,
            seller_id=scope.seller_id,
            week_start=week,
            week_end=bounds["week_end"],
            nm_id=nm_id,
        )
        account_projection = _projection(
            conn,
            seller_id=scope.seller_id,
            week_start=week,
            week_end=bounds["week_end"],
            nm_id="__account__",
        )
        if sku_projection is None or account_projection is None:
            blockers.append(
                {
                    "code": "finance_sku_aggregate_missing",
                    "week_start": week,
                    "nm_id": nm_id,
                }
            )
            (
                source_only_count,
                source_only_mismatch_count,
                source_only_bound_exceeded,
            ) = _scan_week_source_only(
                conn,
                seller_id=scope.seller_id,
                week=week,
                source_hasher=source_hasher,
                mismatch_keys=mismatch_keys,
                identity_mismatches=identity_mismatches,
                invalid_raw_state=invalid_raw_state,
                max_examples=scope.max_examples,
            )
            scanned_raw_row_count += source_only_count
            identity_mismatch_count += source_only_mismatch_count
            mismatch_bound_exceeded = (
                mismatch_bound_exceeded or source_only_bound_exceeded
            )
            continue
        source_hasher.update(
            _canonical_json(
                [
                    week,
                    sku_projection["formula_version"],
                    sku_projection["raw_source_digest"],
                    sku_projection["metrics_json"],
                    account_projection["raw_source_digest"],
                    account_projection["metrics_json"],
                ]
            )
        )
        sku_metrics = json.loads(str(sku_projection["metrics_json"] or "{}"))
        account_metrics = json.loads(
            str(account_projection["metrics_json"] or "{}")
        )
        account_coverage = json.loads(
            str(account_projection["coverage_json"] or "{}")
        )
        selected_revenue = _decimal(sku_metrics.get("net_revenue"))
        global_revenue = _decimal(account_coverage.get("global_net_revenue"))
        allocation_ratio = (
            selected_revenue / global_revenue if global_revenue > ZERO else None
        )
        if allocation_ratio is None:
            blockers.append(
                {"code": "common_expense_zero_revenue_base", "week_start": week}
            )

        account_total = _decimal(
            account_metrics.get("profit_period_expenses")
        ) - _decimal(account_metrics.get("positive_adjustments"))
        account_first_three = (
            _decimal(account_metrics.get("transit_logistics"))
            - _decimal(account_metrics.get("capitalized_transit_logistics"))
            + _decimal(account_metrics.get("subscriptions"))
            + _decimal(account_metrics.get("paid_services"))
        )
        account_other_source = account_total - account_first_three
        allocated_account_other = (
            account_other_source * allocation_ratio
            if allocation_ratio is not None
            else ZERO
        )
        direct_other = _decimal(sku_metrics.get("other_deductions"))
        current_other = direct_other + allocated_account_other
        direct_marketing = _decimal(sku_metrics.get("marketing"))
        account_marketing = _decimal(account_metrics.get("marketing"))
        allocated_marketing = (
            account_marketing * allocation_ratio
            if allocation_ratio is not None
            else ZERO
        )
        ads = _ads_for_week(
            conn,
            nm_id=nm_id,
            week_start=date.fromisoformat(week),
            week_end=date.fromisoformat(bounds["week_end"]),
            source_hasher=source_hasher,
        )
        blockers.extend(ads["blockers"])

        parsed_current_other = ZERO
        selected_row_count = 0
        account_row_count = 0
        raw_cursor = conn.execute(
            """SELECT week_start,report_id,rrd_id,row_hash,raw_json
               FROM wb_finance_weekly_raw_rows
               WHERE seller_id=? AND week_start=?
               ORDER BY report_id,rrd_id""",
            (scope.seller_id, week),
        )
        for stored in raw_cursor:
            scanned_raw_row_count += 1
            raw_text = str(stored["raw_json"] or "{}")
            source_hasher.update(
                _canonical_json(
                    [
                        week,
                        str(stored["report_id"]),
                        str(stored["rrd_id"]),
                        str(stored["row_hash"]),
                        hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                    ]
                )
            )
            try:
                raw = json.loads(raw_text)
            except json.JSONDecodeError:
                _record_invalid_raw(
                    invalid_raw_state,
                    week=week,
                    report_id=str(stored["report_id"]),
                    rrd_id=str(stored["rrd_id"]),
                    max_examples=scope.max_examples,
                )
                continue
            raw_report = str(raw.get("reportId") or "")
            raw_rrd = str(raw.get("rrdId") or "")
            if (
                raw_report != str(stored["report_id"])
                or raw_rrd != str(stored["rrd_id"])
            ):
                identity_mismatch_count += 1
                if raw_report and raw_rrd:
                    if len(mismatch_keys) < MAX_IDENTITY_ANOMALY_KEYS:
                        mismatch_keys.add((raw_report, raw_rrd))
                    elif (raw_report, raw_rrd) not in mismatch_keys:
                        mismatch_bound_exceeded = True
                if len(identity_mismatches) < scope.max_examples:
                    identity_mismatches.append(
                        {
                            "week_start": week,
                            "stored_report_id": str(stored["report_id"]),
                            "stored_rrd_id": str(stored["rrd_id"]),
                            "raw_report_id": raw_report,
                            "raw_rrd_id": raw_rrd,
                        }
                    )
            resolved_nm, method, _problem = _resolve_finance_nm_id(
                raw,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            if resolved_nm == nm_id:
                path = "direct"
                coefficient = Decimal("1")
                selected_row_count += 1
            elif (
                not resolved_nm
                and method == "unresolved"
                and str(raw.get("nmId") or "").strip() in {"", "0"}
            ):
                path = "allocated_account"
                coefficient = allocation_ratio or ZERO
                account_row_count += 1
            else:
                continue
            components = _expense_components(
                raw,
                stored_report_id=str(stored["report_id"]),
                stored_rrd_id=str(stored["rrd_id"]),
                path=path,
                coefficient=coefficient,
                # An allocated-account row is unresolved and therefore cannot
                # match a canonical nmId/supply capitalization layer. Direct
                # acceptance/transit components never contribute to current
                # Partner Other; their gross raw values are provenance only.
                capitalization={},
            )
            for component in components:
                parsed_current_other += component["current_other_contribution"]
                if not _add_group(
                    group_state,
                    raw=raw,
                    component=component,
                    week=week,
                    report_id=str(stored["report_id"]),
                    rrd_id=str(stored["rrd_id"]),
                    max_examples=scope.max_examples,
                    max_accumulated_groups=MAX_ACCUMULATED_OPERATION_GROUPS,
                ):
                    operation_group_bound_exceeded = True
            deduction = _decimal(raw.get("deduction"))
            if deduction < ZERO:
                negative_state["row_count"] += 1
                negative_state["signed_deduction"] += deduction
                negative_state["system_amount"] += abs(deduction)
                if len(negative_state["examples"]) < scope.max_examples:
                    negative_state["examples"].append(
                        {
                            "week_start": week,
                            "report_id": str(stored["report_id"]),
                            "rrd_id": str(stored["rrd_id"]),
                            "signed_deduction_rub": deduction,
                            "system_abs_amount_rub": abs(deduction),
                        }
                    )
            bucket = classify_deduction(raw) if deduction else ""
            candidate_tokens = _marketing_candidate_tokens(raw) if bucket == "other_deductions" else []
            if candidate_tokens:
                if not _add_candidate(
                    candidate_state,
                    raw=raw,
                    week=week,
                    report_id=str(stored["report_id"]),
                    rrd_id=str(stored["rrd_id"]),
                    deduction=deduction,
                    tokens=candidate_tokens,
                    max_examples=scope.max_examples,
                    max_accumulated_candidates=MAX_ACCUMULATED_MARKETING_CANDIDATES,
                ):
                    candidate_bound_exceeded = True

        parsing_delta = current_other - parsed_current_other
        week_results.append(
            {
                "week_start": week,
                "week_end": bounds["week_end"],
                "selected_finance_row_count": selected_row_count,
                "account_finance_row_count": account_row_count,
                "selected_net_revenue_rub": _money(selected_revenue),
                "global_net_revenue_rub": _money(global_revenue),
                "allocation_coefficient": _ratio(allocation_ratio),
                "ads_compact_marketing_rub": _optional_money(ads["amount"]),
                "ads_coverage": ads["coverage"],
                "direct_finance_marketing_rub": _money(direct_marketing),
                "account_finance_marketing_rub": _money(account_marketing),
                "allocated_finance_marketing_rub": _money(allocated_marketing),
                "current_other_withholdings_rub": _money(current_other),
                "residual_without_finance_marketing_rub": _money(
                    current_other - allocated_marketing
                ),
                "parsed_current_other_withholdings_rub": _money(
                    parsed_current_other
                ),
                "parsed_reconciliation_delta_rub": _money(parsing_delta),
                "finance_formula_version": str(
                    sku_projection["formula_version"] or ""
                ),
            }
        )

    if invalid_raw_state["row_count"]:
        blockers.append(
            {
                "code": "finance_raw_json_invalid",
                "row_count": int(invalid_raw_state["row_count"]),
                "examples": invalid_raw_state["examples"],
            }
        )
    if mismatch_bound_exceeded:
        blockers.append(
            {
                "code": "finance_identity_anomaly_bound_exceeded",
                "maximum_distinct_mismatch_keys": MAX_IDENTITY_ANOMALY_KEYS,
                "identity_mismatch_count": identity_mismatch_count,
            }
        )
    if operation_group_bound_exceeded:
        blockers.append(
            {
                "code": "finance_operation_group_bound_exceeded",
                "maximum_accumulated_groups": MAX_ACCUMULATED_OPERATION_GROUPS,
            }
        )
    if candidate_bound_exceeded:
        blockers.append(
            {
                "code": "finance_marketing_candidate_bound_exceeded",
                "maximum_accumulated_candidates": MAX_ACCUMULATED_MARKETING_CANDIDATES,
            }
        )
    duplicate_rows = _logical_duplicate_evidence(
        conn,
        seller_id=scope.seller_id,
        weeks=weeks,
        mismatch_keys=mismatch_keys,
        max_examples=scope.max_examples,
    )
    finalized_groups = _finalize_groups(
        group_state,
        current_other_by_week={
            item["week_start"]: _decimal(item["current_other_withholdings_rub"])
            for item in week_results
        },
    )
    groups_digest = "sha256:" + hashlib.sha256(
        _canonical_json(finalized_groups)
    ).hexdigest()
    shown_groups = finalized_groups[: scope.max_groups]
    omitted_groups = finalized_groups[scope.max_groups :]
    omitted_summary = {
        "group_count": len(omitted_groups),
        "signed_source_sum_rub": _money(
            sum((_decimal(item["signed_source_sum_rub"]) for item in omitted_groups), ZERO)
        ),
        "system_amount_sum_rub": _money(
            sum((_decimal(item["system_amount_sum_rub"]) for item in omitted_groups), ZERO)
        ),
        "current_other_contribution_sum_rub": _money(
            sum(
                (
                    _decimal(item["current_other_contribution_rub"])
                    for item in omitted_groups
                ),
                ZERO,
            )
        ),
    }
    negative = _negative_evidence(negative_state)
    candidates = _finalize_candidates(candidate_state)
    source_digest = "sha256:" + source_hasher.hexdigest()
    seller_ref = "sha256:" + hashlib.sha256(scope.seller_id.encode("utf-8")).hexdigest()[:16]
    core = {
        "diagnostic_version": DIAGNOSTIC_VERSION,
        "status": "incomplete" if blockers else "ready",
        "read_only_contract": "sqlite_uri_mode_ro+pragma_query_only+transaction_rollback",
        "database_label": database.name,
        "seller_ref": seller_ref,
        "selection": selection,
        "nm_id": nm_id,
        "scanned_finance_raw_row_count": scanned_raw_row_count,
        "weeks": week_results,
        "operation_groups": shown_groups,
        "operation_group_count": len(finalized_groups),
        "operation_groups_digest": groups_digest,
        "omitted_operation_groups": omitted_summary,
        "duplicates": {
            "logical_duplicate_identity_count": len(duplicate_rows),
            "examples": duplicate_rows[: scope.max_examples],
            "stored_vs_raw_identity_mismatch_count": identity_mismatch_count,
            "stored_vs_raw_identity_mismatch_examples": identity_mismatches,
        },
        "invalid_raw_json_evidence": {
            "row_count": int(invalid_raw_state["row_count"]),
            "examples": invalid_raw_state["examples"],
        },
        "negative_deduction_evidence": negative,
        "unknown_marketing_name_candidates": candidates[: scope.max_groups],
        "unknown_marketing_candidate_count": len(candidates),
        "source_digest": source_digest,
        "blockers": blockers,
        "bounds": {
            "max_weeks": scope.max_weeks,
            "max_groups": scope.max_groups,
            "max_examples_per_group": scope.max_examples,
            "max_identity_anomaly_keys": MAX_IDENTITY_ANOMALY_KEYS,
            "max_accumulated_operation_groups": MAX_ACCUMULATED_OPERATION_GROUPS,
            "max_accumulated_marketing_candidates": MAX_ACCUMULATED_MARKETING_CANDIDATES,
        },
    }
    fingerprint = "sha256:" + hashlib.sha256(_canonical_json(core)).hexdigest()
    return {
        **core,
        "fingerprint": fingerprint,
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _resolve_scope(
    conn: sqlite3.Connection,
    *,
    scope: DiagnosticScope,
    tables: set[str],
) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    requested_weeks = _validated_weeks(scope.weeks)
    if scope.server_settings:
        setting_tables = {
            "partner_report_settings_current",
            "partner_report_settings_versions",
        }
        missing = sorted(setting_tables - tables)
        if missing:
            raise PartnerFinanceDiagnosticError(
                "server-settings mode requires: " + ", ".join(missing)
            )
        query = """SELECT current.nm_id,versions.settings_version_id,
                          versions.parameters_json,versions.fingerprint,versions.created_at
                   FROM partner_report_settings_current AS current
                   JOIN partner_report_settings_versions AS versions
                     ON versions.settings_version_id=current.settings_version_id
                   WHERE current.seller_id=?"""
        params: list[Any] = [scope.seller_id]
        if scope.nm_id:
            query += " AND current.nm_id=?"
            params.append(scope.nm_id)
        query += " ORDER BY versions.created_at DESC,current.nm_id LIMIT 50"
        rows = conn.execute(query, params).fetchall()
        complete: list[sqlite3.Row] = []
        for row in rows:
            try:
                parameters = json.loads(str(row["parameters_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if all(parameters.get(field) not in (None, "") for field in REQUIRED_SETTING_FIELDS):
                complete.append(row)
        if not complete:
            raise PartnerFinanceDiagnosticError(
                "no complete current Partner setting matches server-settings scope"
            )
        selected = complete[0]
        nm_id = str(selected["nm_id"])
        weeks = requested_weeks or tuple(
            str(row["week_start"])
            for row in conn.execute(
                """SELECT week_start FROM wb_finance_weekly_sync
                   WHERE seller_id=? ORDER BY week_start""",
                (scope.seller_id,),
            ).fetchall()
        )
        if not weeks:
            raise PartnerFinanceDiagnosticError("server-settings scope has no Finance weeks")
        return nm_id, weeks, {
            "mode": "server_settings",
            "settings_version_id": str(selected["settings_version_id"]),
            "settings_fingerprint": str(selected["fingerprint"]),
            "complete_setting_candidate_count": len(complete),
            "week_selection": "explicit" if requested_weeks else "all_finance_weeks",
        }
    if not scope.nm_id:
        raise PartnerFinanceDiagnosticError("explicit mode requires nm_id")
    if not requested_weeks:
        raise PartnerFinanceDiagnosticError("explicit mode requires at least one week")
    return scope.nm_id, requested_weeks, {
        "mode": "explicit",
        "week_selection": "explicit",
    }


def _validated_weeks(values: Iterable[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for value in values:
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError as exc:
            raise PartnerFinanceDiagnosticError(
                f"invalid week_start: {value!r}"
            ) from exc
        result.add(parsed.isoformat())
    return tuple(sorted(result))


def _week_bounds(
    conn: sqlite3.Connection,
    *,
    seller_id: str,
    weeks: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    if not weeks:
        return {}
    placeholders = ",".join("?" for _ in weeks)
    return {
        str(row["week_start"]): {
            "week_end": str(row["week_end"]),
            "status": str(row["status"]),
        }
        for row in conn.execute(
            f"""SELECT week_start,week_end,status FROM wb_finance_weekly_sync
                WHERE seller_id=? AND week_start IN ({placeholders})
                ORDER BY week_start""",
            (seller_id, *weeks),
        ).fetchall()
    }


def _projection(
    conn: sqlite3.Connection,
    *,
    seller_id: str,
    week_start: str,
    week_end: str,
    nm_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT formula_version,metrics_json,coverage_json,raw_source_digest
           FROM wb_finance_weekly_sku_aggregates
           WHERE seller_id=? AND week_start=? AND week_end=? AND nm_id=?""",
        (seller_id, week_start, week_end, nm_id),
    ).fetchone()


def _scan_week_source_only(
    conn: sqlite3.Connection,
    *,
    seller_id: str,
    week: str,
    source_hasher: Any,
    mismatch_keys: set[tuple[str, str]],
    identity_mismatches: list[dict[str, str]],
    invalid_raw_state: dict[str, Any],
    max_examples: int,
) -> tuple[int, int, bool]:
    """Preserve raw count/digest/identity evidence when projections are absent."""

    row_count = 0
    mismatch_count = 0
    bound_exceeded = False
    cursor = conn.execute(
        """SELECT report_id,rrd_id,row_hash,raw_json
           FROM wb_finance_weekly_raw_rows
           WHERE seller_id=? AND week_start=?
           ORDER BY report_id,rrd_id""",
        (seller_id, week),
    )
    for stored in cursor:
        row_count += 1
        raw_text = str(stored["raw_json"] or "{}")
        source_hasher.update(
            _canonical_json(
                [
                    week,
                    str(stored["report_id"]),
                    str(stored["rrd_id"]),
                    str(stored["row_hash"]),
                    hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                ]
            )
        )
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            _record_invalid_raw(
                invalid_raw_state,
                week=week,
                report_id=str(stored["report_id"]),
                rrd_id=str(stored["rrd_id"]),
                max_examples=max_examples,
            )
            continue
        raw_report = str(raw.get("reportId") or "")
        raw_rrd = str(raw.get("rrdId") or "")
        if (
            raw_report == str(stored["report_id"])
            and raw_rrd == str(stored["rrd_id"])
        ):
            continue
        mismatch_count += 1
        if raw_report and raw_rrd:
            if len(mismatch_keys) < MAX_IDENTITY_ANOMALY_KEYS:
                mismatch_keys.add((raw_report, raw_rrd))
            elif (raw_report, raw_rrd) not in mismatch_keys:
                bound_exceeded = True
        if len(identity_mismatches) < max_examples:
            identity_mismatches.append(
                {
                    "week_start": week,
                    "stored_report_id": str(stored["report_id"]),
                    "stored_rrd_id": str(stored["rrd_id"]),
                    "raw_report_id": raw_report,
                    "raw_rrd_id": raw_rrd,
                }
            )
    return row_count, mismatch_count, bound_exceeded


def _record_invalid_raw(
    state: dict[str, Any],
    *,
    week: str,
    report_id: str,
    rrd_id: str,
    max_examples: int,
) -> None:
    state["row_count"] += 1
    if len(state["examples"]) < max_examples:
        state["examples"].append(
            {"week_start": week, "report_id": report_id, "rrd_id": rrd_id}
        )


def _ads_for_week(
    conn: sqlite3.Connection,
    *,
    nm_id: str,
    week_start: date,
    week_end: date,
    source_hasher: Any,
) -> dict[str, Any]:
    total = ZERO
    blockers: list[dict[str, Any]] = []
    covered = 0
    confirmed_empty = 0
    cursor = week_start
    while cursor <= week_end:
        day = cursor.isoformat()
        source = conn.execute(
            """SELECT captured_at,payload_json FROM temporal_source_slot_snapshots
               WHERE source_key=? AND snapshot_date=? AND snapshot_role=?""",
            (ADS_SOURCE_KEY, day, ADS_SOURCE_ROLE),
        ).fetchone()
        if source is None:
            blockers.append({"code": "ads_date_missing", "date": day, "nm_id": nm_id})
            cursor += timedelta(days=1)
            continue
        payload_text = str(source["payload_json"] or "")
        source_hasher.update(
            _canonical_json(
                [
                    ADS_SOURCE_KEY,
                    day,
                    str(source["captured_at"] or ""),
                    hashlib.sha256(payload_text.encode("utf-8")).hexdigest(),
                ]
            )
        )
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = {}
        result, _origin = resolve_ads_snapshot_payload(payload)
        kind = str((result or {}).get("kind") or "invalid")
        matched = [
            item
            for item in (result or {}).get("items") or []
            if isinstance(item, dict)
            and str(item.get("nm_id", item.get("nmId", "")) or "") == nm_id
        ]
        if kind == "empty":
            confirmed_empty += 1
        elif kind == "success" and matched:
            try:
                total += sum((_strict_decimal(item.get("ads_sum")) for item in matched), ZERO)
            except PartnerFinanceDiagnosticError:
                blockers.append(
                    {"code": "ads_value_invalid", "date": day, "nm_id": nm_id}
                )
            else:
                covered += 1
        else:
            blockers.append(
                {"code": "ads_sku_coverage_missing", "date": day, "nm_id": nm_id}
            )
        cursor += timedelta(days=1)
    return {
        "amount": None if blockers else total,
        "blockers": blockers,
        "coverage": {
            "expected_days": (week_end - week_start).days + 1,
            "covered_days": covered,
            "confirmed_empty_days": confirmed_empty,
            "blocker_count": len(blockers),
        },
    }


def _expense_components(
    raw: Mapping[str, Any],
    *,
    stored_report_id: str,
    stored_rrd_id: str,
    path: str,
    coefficient: Decimal,
    capitalization: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    doc = str(raw.get("docTypeName") or "").casefold()
    direction = Decimal("1") if doc == "продажа" else Decimal("-1") if doc == "возврат" else ZERO

    def add(
        key: str,
        source_amount: Decimal,
        system_amount: Decimal,
        target: str,
        *,
        classifier_bucket: str,
        current_other: bool,
    ) -> None:
        if source_amount == ZERO and system_amount == ZERO:
            return
        allocated = system_amount * coefficient
        contribution = allocated if current_other else ZERO
        components.append(
            {
                "component": key,
                "signed_source_amount": source_amount,
                "system_amount": system_amount,
                "allocation_coefficient": coefficient,
                "allocated_amount": allocated,
                "current_other_contribution": contribution,
                "classifier_bucket": classifier_bucket,
                "semantic_target": target,
                "path": path,
            }
        )

    if direction:
        revenue = _decimal(raw.get("retailPriceWithDisc"))
        for_pay = _decimal(raw.get("forPay"))
        acquiring = _decimal(raw.get("acquiringFee")) * direction
        combined = (revenue - for_pay) * direction
        agent = combined - acquiring
        add(
            "agent_remuneration",
            agent,
            agent,
            "Агентское вознаграждение WB",
            classifier_bucket="finance_field:agent_remuneration",
            current_other=path == "allocated_account",
        )
        add(
            "acquiring",
            acquiring,
            acquiring,
            "Эквайринг",
            classifier_bucket="finance_field:acquiring",
            current_other=path == "allocated_account",
        )
    for key, raw_field, target in (
        ("logistics", "deliveryService", "Логистика WB"),
        ("storage", "paidStorage", "Хранение WB"),
        ("penalties", "penalty", "Штрафы/корректировки"),
    ):
        value = _decimal(raw.get(raw_field))
        add(
            key,
            value,
            value,
            target,
            classifier_bucket=f"finance_field:{key}",
            current_other=path == "allocated_account",
        )
    acceptance = _decimal(raw.get("paidAcceptance"))
    acceptance_addback = _decimal(
        (capitalization.get((stored_report_id, stored_rrd_id, "acceptance")) or {}).get(
            "addback_rub"
        )
    )
    add(
        "acceptance",
        acceptance,
        acceptance - acceptance_addback,
        "Платная приёмка",
        classifier_bucket="finance_field:acceptance",
        current_other=path == "allocated_account",
    )

    deduction = _decimal(raw.get("deduction"))
    if deduction:
        bucket = classify_deduction(raw)
        system = abs(deduction)
        if bucket == "transit_logistics":
            transit_addback = _decimal(
                (capitalization.get((stored_report_id, stored_rrd_id, "transit")) or {}).get(
                    "addback_rub"
                )
            )
            system -= transit_addback
            target = "Транзитная логистика, не подтверждённая как капитализированная"
        elif bucket == "subscriptions":
            target = "Подписка WB Jam"
        elif bucket == "paid_services":
            target = "Платные сервисы WB"
        elif bucket == "marketing":
            target = "Исключить из Partner Finance; канонический источник — ads_compact"
        else:
            target = "Отдельное удержание: " + _operation_name(raw)
        add(
            "deduction",
            deduction,
            system,
            target,
            classifier_bucket=bucket,
            current_other=(
                bucket in {"marketing", "other_deductions"}
                if path == "allocated_account"
                else bucket == "other_deductions"
            ),
        )

    adjustment = _decimal(raw.get("additionalPayment"))
    if doc not in {"продажа", "возврат"} and adjustment:
        system = -adjustment if adjustment > ZERO else abs(adjustment)
        add(
            "wb_remuneration_adjustment",
            adjustment,
            system,
            "Штрафы/корректировки",
            classifier_bucket=(
                "finance_field:positive_adjustment"
                if adjustment > ZERO
                else "finance_field:correction"
            ),
            current_other=path == "allocated_account",
        )
    return components


def _add_group(
    groups: dict[tuple[str, ...], dict[str, Any]],
    *,
    raw: Mapping[str, Any],
    component: Mapping[str, Any],
    week: str,
    report_id: str,
    rrd_id: str,
    max_examples: int,
    max_accumulated_groups: int,
) -> bool:
    deduction = _decimal(raw.get("deduction"))
    deduction_sign = (
        "negative" if deduction < ZERO else "positive" if deduction > ZERO else "none"
    )
    nm_presence = (
        "present"
        if str(raw.get("nmId") or "").strip() not in {"", "0"}
        else "missing"
    )
    dimensions = (
        _safe_text(raw.get("bonusTypeName")),
        _safe_text(raw.get("sellerOperName")),
        _safe_text(raw.get("paymentProcessing")),
        _safe_text(raw.get("docTypeName")),
        nm_presence,
        deduction_sign,
        str(component["classifier_bucket"]),
        str(component["path"]),
        str(component["semantic_target"]),
    )
    if dimensions not in groups and len(groups) >= max_accumulated_groups:
        return False
    state = groups.setdefault(
        dimensions,
        {
            "dimensions": dimensions,
            "row_count": 0,
            "signed_source_sum": ZERO,
            "system_amount_sum": ZERO,
            "allocated_amount_sum": ZERO,
            "current_other_contribution": ZERO,
            "coefficients": set(),
            "examples": [],
        },
    )
    state["row_count"] += 1
    state["signed_source_sum"] += _decimal(component["signed_source_amount"])
    state["system_amount_sum"] += _decimal(component["system_amount"])
    state["allocated_amount_sum"] += _decimal(component["allocated_amount"])
    state["current_other_contribution"] += _decimal(
        component["current_other_contribution"]
    )
    state["coefficients"].add(_ratio(_decimal(component["allocation_coefficient"])))
    if len(state["examples"]) < max_examples:
        state["examples"].append(
            {"week_start": week, "report_id": report_id, "rrd_id": rrd_id}
        )
    return True


def _finalize_groups(
    groups: Mapping[tuple[str, ...], Mapping[str, Any]],
    *,
    current_other_by_week: Mapping[str, Decimal],
) -> list[dict[str, Any]]:
    current_total = sum(current_other_by_week.values(), ZERO)
    result: list[dict[str, Any]] = []
    for state in groups.values():
        dimensions = state["dimensions"]
        contribution = _decimal(state["current_other_contribution"])
        share = contribution / current_total * Decimal("100") if current_total else None
        result.append(
            {
                "bonus_type_name": dimensions[0],
                "seller_oper_name": dimensions[1],
                "payment_processing": dimensions[2],
                "doc_type_name": dimensions[3],
                "nm_id_presence": dimensions[4],
                "deduction_sign": dimensions[5],
                "finance_classifier_bucket": dimensions[6],
                "accounting_path": dimensions[7],
                "semantic_partner_target": dimensions[8],
                "row_count": int(state["row_count"]),
                "signed_source_sum_rub": _money(state["signed_source_sum"]),
                "system_amount_sum_rub": _money(state["system_amount_sum"]),
                "allocation_coefficients": sorted(state["coefficients"]),
                "allocated_amount_sum_rub": _money(state["allocated_amount_sum"]),
                "current_other_contribution_rub": _money(contribution),
                "share_pct_of_current_other": _optional_ratio(share),
                "examples": state["examples"],
            }
        )
    return sorted(
        result,
        key=lambda item: (
            -abs(_decimal(item["current_other_contribution_rub"])),
            -abs(_decimal(item["allocated_amount_sum_rub"])),
            str(item["finance_classifier_bucket"]),
            str(item["semantic_partner_target"]),
            str(item["bonus_type_name"]),
        ),
    )


def _add_candidate(
    candidates: dict[str, dict[str, Any]],
    *,
    raw: Mapping[str, Any],
    week: str,
    report_id: str,
    rrd_id: str,
    deduction: Decimal,
    tokens: list[str],
    max_examples: int,
    max_accumulated_candidates: int,
) -> bool:
    name = _operation_name(raw)
    if name not in candidates and len(candidates) >= max_accumulated_candidates:
        return False
    state = candidates.setdefault(
        name,
        {
            "operation_name": name,
            "row_count": 0,
            "signed_deduction": ZERO,
            "system_amount": ZERO,
            "matched_tokens": set(),
            "examples": [],
        },
    )
    state["row_count"] += 1
    state["signed_deduction"] += deduction
    state["system_amount"] += abs(deduction)
    state["matched_tokens"].update(tokens)
    if len(state["examples"]) < max_examples:
        state["examples"].append(
            {"week_start": week, "report_id": report_id, "rrd_id": rrd_id}
        )
    return True


def _finalize_candidates(candidates: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = [
        {
            "operation_name": state["operation_name"],
            "row_count": int(state["row_count"]),
            "signed_deduction_rub": _money(state["signed_deduction"]),
            "system_abs_amount_rub": _money(state["system_amount"]),
            "matched_candidate_tokens": sorted(state["matched_tokens"]),
            "examples": state["examples"],
        }
        for state in candidates.values()
    ]
    return sorted(
        result,
        key=lambda item: (
            -abs(_decimal(item["system_abs_amount_rub"])),
            str(item["operation_name"]),
        ),
    )


def _logical_duplicate_evidence(
    conn: sqlite3.Connection,
    *,
    seller_id: str,
    weeks: tuple[str, ...],
    mismatch_keys: set[tuple[str, str]],
    max_examples: int,
) -> list[dict[str, Any]]:
    """Prove logical duplicates without retaining every primary key in memory.

    Stored identities are unique by primary key. Therefore a logical duplicate
    can exist only for a raw identity involved in at least one stored/raw
    mismatch. A second ordered streaming pass is needed only when such an
    anomaly exists.
    """

    if not mismatch_keys or not weeks:
        return []
    occurrences: dict[tuple[str, str], dict[str, Any]] = {
        key: {"count": 0, "stored_examples": []} for key in mismatch_keys
    }
    placeholders = ",".join("?" for _ in weeks)
    cursor = conn.execute(
        f"""SELECT week_start,report_id,rrd_id,raw_json
            FROM wb_finance_weekly_raw_rows
            WHERE seller_id=? AND week_start IN ({placeholders})
            ORDER BY week_start,report_id,rrd_id""",
        (seller_id, *weeks),
    )
    for stored in cursor:
        try:
            raw = json.loads(str(stored["raw_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        key = (str(raw.get("reportId") or ""), str(raw.get("rrdId") or ""))
        state = occurrences.get(key)
        if state is None:
            continue
        state["count"] += 1
        if len(state["stored_examples"]) < max_examples:
            state["stored_examples"].append(
                {
                    "week_start": str(stored["week_start"]),
                    "stored_report_id": str(stored["report_id"]),
                    "stored_rrd_id": str(stored["rrd_id"]),
                }
            )
    return [
        {
            "report_id": key[0],
            "rrd_id": key[1],
            "occurrences": int(state["count"]),
            "stored_examples": state["stored_examples"],
        }
        for key, state in sorted(occurrences.items())
        if state["count"] > 1
    ]


def _negative_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    signed = _decimal(state["signed_deduction"])
    system = _decimal(state["system_amount"])
    return {
        "row_count": int(state["row_count"]),
        "signed_deduction_sum_rub": _money(signed),
        "current_system_abs_sum_rub": _money(system),
        "abs_vs_signed_expense_uplift_rub": _money(system - signed),
        "examples": [
            {
                **{key: value for key, value in item.items() if not isinstance(value, Decimal)},
                "signed_deduction_rub": _money(item["signed_deduction_rub"]),
                "system_abs_amount_rub": _money(item["system_abs_amount_rub"]),
            }
            for item in state["examples"]
        ],
    }


def _marketing_candidate_tokens(raw: Mapping[str, Any]) -> list[str]:
    text = " ".join(
        str(raw.get(key) or "")
        for key in (
            "bonusTypeName",
            "sellerOperName",
            "paymentProcessing",
            "docTypeName",
        )
    ).casefold()
    return sorted({token for token in MARKETING_CANDIDATE_TOKENS if token in text})


def _operation_name(raw: Mapping[str, Any]) -> str:
    values = [
        _safe_text(raw.get(key))
        for key in (
            "bonusTypeName",
            "sellerOperName",
            "paymentProcessing",
            "docTypeName",
        )
    ]
    return " / ".join(value for value in values if value) or "Неизвестное удержание"


def _safe_text(value: Any) -> str:
    return " ".join(str(value or "").split())[:160]


def _strict_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        raise PartnerFinanceDiagnosticError("required decimal is missing")
    try:
        parsed = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PartnerFinanceDiagnosticError("decimal is invalid") from exc
    if not parsed.is_finite():
        raise PartnerFinanceDiagnosticError("decimal is non-finite")
    return parsed


def _money(value: Any) -> str:
    return format(_decimal(value).quantize(MONEY), "f")


def _optional_money(value: Decimal | None) -> str | None:
    return None if value is None else _money(value)


def _ratio(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(RATIO), "f")


def _optional_ratio(value: Decimal | None) -> str | None:
    return _ratio(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
