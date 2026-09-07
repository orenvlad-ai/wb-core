"""Bounded settlement reconciliation with preserved COGS and one-submit CAS."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from packages.application.storage_registry import StoreRegistry
from packages.application.wb_finance_payout import (
    SUMMARY_FIELDS,
    loyalty,
    money,
    patch_settlement,
    standalone_adjustment,
    reconcile,
)

TABLES = (
    "wb_finance_weekly_aggregates",
    "wb_finance_weekly_sku_aggregates",
    "wb_finance_weekly_reconciliation",
)


def digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


def target(request: dict[str, Any]) -> tuple[StoreRegistry, Any]:
    registry = StoreRegistry(Path(request["runtime_dir"]))
    manifest = registry.load()
    if manifest.manifest_sha256 != request["storage_manifest_sha256"]:
        raise ValueError("finance_payout_storage_drift")
    actual = (
        (Path(request["runtime_dir"]).parent / "app" / ".wb-core-runtime-sha")
        .read_text()
        .strip()
    )
    if actual != request["runtime_sha"]:
        raise ValueError("finance_payout_runtime_drift")
    return registry, manifest


def connect(registry: StoreRegistry, manifest: Any, store: str, mode: str) -> Any:
    conn = registry.connect(
        store,
        mode=mode,
        manifest=manifest,
        operation="finance_payout_reconcile",
        timeout_ms=3000,
    )
    conn.row_factory = sqlite3.Row
    if mode == "ro":
        conn.execute("PRAGMA query_only=ON")
    return conn


def selection(conn: Any, seller: str, weeks: list[str]) -> dict[str, Any]:
    marks = ",".join("?" for _ in weeks)
    return {
        t: [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM "
                + t
                + " WHERE seller_id=? AND week_start IN ("
                + marks
                + ") ORDER BY week_start"
                + (",nm_id" if t.endswith("sku_aggregates") else ""),
                (seller, *weeks),
            )
        ]
        for t in (*TABLES, "wb_finance_weekly_sync")
    }


def build(request: dict[str, Any]) -> dict[str, Any]:
    from packages.application.wb_finance_weekly import (
        _nomenclature_identity_index,
        _resolve_finance_nm_id,
    )

    registry, manifest = target(request)
    source = request.get("source")
    if source is None:
        source = json.loads(Path(request["source_path"]).read_text())
    if digest(source) != request["source_sha256"] or source.get("status") != 200:
        raise ValueError("finance_payout_source_drift")
    weeks = request["weeks"]
    if not weeks or weeks != sorted(set(weeks)) or len(weeks) > 60:
        raise ValueError("finance_payout_scope_invalid")
    for w in weeks:
        date.fromisoformat(w)
    seller = request["seller_id"]
    with closing(connect(registry, manifest, "operational", "ro")) as conn, closing(
        connect(registry, manifest, "finance_raw", "ro")
    ) as raw:
        conn.execute("BEGIN")
        raw.execute("BEGIN")
        before = selection(conn, seller, weeks)
        aggregates = {r["week_start"]: r for r in before[TABLES[0]]}
        syncs = {r["week_start"]: r for r in before["wb_finance_weekly_sync"]}
        recons = {r["week_start"]: r for r in before[TABLES[2]]}
        if set(aggregates) != set(weeks) or set(recons) != set(weeks):
            raise ValueError("finance_payout_loaded_week_missing")
        alias, ambiguous, _, _ = _nomenclature_identity_index(conn)
        updates = []
        changes = []
        for w in weeks:
            aggregate = aggregates[w]
            end = aggregate["week_end"]
            sync = syncs[w]
            batch = raw.execute(
                "SELECT b.batch_id FROM finance_raw_ingest_batches b LEFT JOIN finance_raw_outbox e ON e.batch_id=b.batch_id WHERE b.status='committed' AND b.seller_id IN (?, '*') AND b.week_start<=? AND b.week_end>=? ORDER BY COALESCE(e.sequence_no,0) DESC,COALESCE(b.committed_at,b.created_at) DESC,b.batch_id DESC LIMIT 1",
                (seller, w, end),
            ).fetchone()
            if batch is None:
                raise ValueError("finance_payout_raw_batch_missing")
            by_sku = defaultdict(
                lambda: [money("0"), money("0"), money("0"), money("0")]
            )
            hashes = []
            for r in raw.execute(
                "SELECT r.raw_json,r.row_hash FROM finance_raw_rows r INDEXED BY finance_raw_rows_by_week CROSS JOIN finance_raw_batch_rows l ON l.raw_row_id=r.raw_row_id AND l.batch_id=? WHERE r.seller_id=? AND r.week_start=? AND r.week_end=?",
                (batch[0], seller, w, end),
            ):
                hashes.append(r["row_hash"])
                row = json.loads(r["raw_json"])
                points, fee = loyalty(row)
                income, expense = standalone_adjustment(row)
                if not any((points, fee, income, expense)):
                    continue
                nm, method, problem = _resolve_finance_nm_id(
                    row, alias_to_nm=alias, ambiguous_aliases=ambiguous
                )
                if (
                    not nm
                    and method == "unresolved"
                    and str(row.get("nmId") or "").strip() in ("", "0")
                ):
                    nm = "__account__"
                if not nm:
                    raise ValueError("finance_payout_loyalty_identity_unresolved")
                by_sku[nm][0] += points
                by_sku[nm][1] += fee
                by_sku[nm][2] += income
                by_sku[nm][3] += expense
            raw_hash = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
            if raw_hash != sync["content_hash"] or len(hashes) != sync["raw_row_count"]:
                raise ValueError("finance_payout_raw_sync_mismatch")
            points = sum(v[0] for v in by_sku.values())
            fee = sum(v[1] for v in by_sku.values())
            old = json.loads(aggregate["metrics_json"])
            positive = sum(v[2] for v in by_sku.values())
            corrections = sum(v[3] for v in by_sku.values())
            new = patch_settlement(
                old, money(points), money(fee), money(positive), money(corrections)
            )
            summaries = [
                {k: r[k] for k in SUMMARY_FIELDS if k in r}
                for r in source["data"]
                if str(r.get("reportId"))
                in set(json.loads(aggregate["report_ids_json"]))
            ]
            result = reconcile(
                new, summaries, json.loads(aggregate["report_ids_json"]), w, end
            )
            if result["payout_status"] != "ok":
                raise ValueError(
                    "finance_payout_official_mismatch:"
                    + w
                    + ":"
                    + result["payout_status"]
                )
            updates.append(
                {
                    "table": TABLES[0],
                    "column": "metrics_json",
                    "key": [seller, w, end],
                    "value": json.dumps(new, ensure_ascii=False),
                }
            )
            sku_rows = [r for r in before[TABLES[1]] if r["week_start"] == w]
            if set(by_sku) - {r["nm_id"] for r in sku_rows}:
                raise ValueError("finance_payout_sku_projection_missing")
            for row in sku_rows:
                p, f, income, expense = by_sku.get(row["nm_id"], (money("0"),) * 4)
                patched = patch_settlement(
                    json.loads(row["metrics_json"]), p, f, income, expense
                )
                updates.append(
                    {
                        "table": TABLES[1],
                        "column": "metrics_json",
                        "key": [seller, w, end, row["nm_id"]],
                        "value": json.dumps(patched, ensure_ascii=False),
                    }
                )
            detail = json.loads(recons[w]["detail_json"])
            detail.update(official_reports=summaries, **result)
            updates.append(
                {
                    "table": TABLES[2],
                    "column": "detail_json",
                    "key": [seller, w, end],
                    "value": json.dumps(detail, ensure_ascii=False),
                }
            )
            changes.append(
                {
                    "week_start": w,
                    "calculated_payout": new["calculated_payout"],
                    "loyalty_expense_delta": str(
                        points
                        + fee
                        - money(old.get("loyalty_points", "0"))
                        - money(old.get("loyalty_fee", "0"))
                    ),
                    "result_delta": str(
                        money(new["calculated_payout"])
                        - money(old["net_revenue"])
                        + money(old["total_wb_expenses"])
                        - money(old["positive_adjustments"])
                    ),
                    "cogs_preserved": new.get("cogs") == old.get("cogs"),
                }
            )
        return {
            "before": before,
            "updates": updates,
            "changes": changes,
            "prestate_sha256": digest(before),
            "candidate_sha256": digest(updates),
        }


class FinancePayoutAdapter:
    def preview(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        plan = build(request)
        return {
            "prepared": plan,
            "operation_id": operation_id,
            "target": str(Path(request["runtime_dir"]).resolve()),
            "scope": {
                "seller_id": request["seller_id"],
                "weeks": request["weeks"],
                "changes": plan["changes"],
            },
            "prestate_sha256": plan["prestate_sha256"],
            "candidate_sha256": plan["candidate_sha256"],
            "recovery": {
                "kind": "transactional_before_image",
                "audit_id": operation_id,
            },
        }

    def apply(
        self, request: dict[str, Any], operation_id: str, preview: dict[str, Any]
    ) -> dict[str, Any]:
        plan = preview["prepared"]
        if any(plan[k] != preview[k] for k in ("prestate_sha256", "candidate_sha256")):
            raise ValueError("finance_payout_candidate_drift")
        registry, manifest = target(request)
        with closing(connect(registry, manifest, "operational", "rw")) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM wb_finance_projection_audit WHERE audit_id=?",
                    (operation_id,),
                ).fetchone():
                    raise ValueError("finance_payout_operation_already_exists")
                if (
                    digest(selection(conn, request["seller_id"], request["weeks"]))
                    != plan["prestate_sha256"]
                ):
                    raise ValueError("finance_payout_prestate_drift")
                target(request)
                for u in plan["updates"]:
                    where = "seller_id=? AND week_start=? AND week_end=?" + (
                        " AND nm_id=?" if len(u["key"]) == 4 else ""
                    )
                    cur = conn.execute(
                        "UPDATE "
                        + u["table"]
                        + " SET "
                        + u["column"]
                        + "=? WHERE "
                        + where,
                        (u["value"], *u["key"]),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("finance_payout_target_missing")
                # The before image and after image are committed atomically with
                # the update, providing recovery and idempotent readback.
                conn.execute(
                    "INSERT INTO wb_finance_projection_audit VALUES(?,?,?,?,?,?,?)",
                    (
                        operation_id,
                        request["seller_id"],
                        "finance_payout_reconcile_v1",
                        plan["candidate_sha256"],
                        json.dumps({"weeks": request["weeks"]}),
                        json.dumps(plan, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"operation_id": operation_id, "disposition": "submitted"}

    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        registry, manifest = target(request)
        with closing(connect(registry, manifest, "operational", "ro")) as conn:
            row = conn.execute(
                "SELECT seller_id,action,result_json FROM wb_finance_projection_audit WHERE audit_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                return {"operation_id": operation_id, "state": "not_submitted"}
            if (
                row["seller_id"] != request["seller_id"]
                or row["action"] != "finance_payout_reconcile_v1"
            ):
                raise ValueError("finance_payout_operation_identity_mismatch")
            plan = json.loads(row["result_json"])
            for u in plan["updates"]:
                where = "seller_id=? AND week_start=? AND week_end=?" + (
                    " AND nm_id=?" if len(u["key"]) == 4 else ""
                )
                actual = conn.execute(
                    "SELECT " + u["column"] + " FROM " + u["table"] + " WHERE " + where,
                    u["key"],
                ).fetchone()
                if actual is None or actual[0] != u["value"]:
                    return {"operation_id": operation_id, "state": "failed"}
        return {
            "operation_id": operation_id,
            "state": "applied",
            "weeks": len(plan["changes"]),
            "cogs_preserved": all(r["cogs_preserved"] for r in plan["changes"]),
        }
