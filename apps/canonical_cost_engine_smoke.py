"""Targeted invariants for the unified canonical cost engine and baseline."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.canonical_cost_engine import (  # noqa: E402
    BASELINE_BUSINESS_APPROVED_PRIMARY_WAC,
    BASELINE_ONEC,
    CanonicalCostBlocked,
    CanonicalCostEngine,
    POSTCUTOVER_NORMALIZATION_MANIFEST,
    POSTCUTOVER_NORMALIZATION_POLICY,
    UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION,
    UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST,
    UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2,
    UNMATCHED_DOPRINATO_ABSORPTION_POLICY,
    UNMATCHED_DOPRINATO_ABSORPTION_POLICY_V2,
    _ff_opening_boundary_context,
    _normalized_acceptance_plan,
    _source_anomaly_preflight_conn,
    _unmatched_doprinato_manifest_decision,
    _unmatched_doprinato_manifest_report,
    _unmatched_doprinato_manifest_report_v2,
    _wb_movement_evidence,
    _wb_supply_cache_evidence,
    allocate_partial_payment,
    ensure_canonical_cost_schema,
    reconcile_outstanding_layers,
    resolve_ff_operation_effective_date,
    roll_wac,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
    _serialize_sheet_vitrina_plan,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


def main() -> int:
    _partial_payment()
    _wac_and_snapshot_stability()
    _outstanding_reconciliation()
    _ff_operation_effective_date_resolution()
    _targeted_remediation_stays_outside_opening_collapse()
    _cutover_boundary_and_normalization_policy()
    _exact_unmatched_doprinato_absorption_manifest()
    _baseline_and_physical_sources()
    print("canonical_cost_engine_smoke: ok")
    return 0


def _partial_payment() -> None:
    rows = allocate_partial_payment(
        [
            {"nm_id": 1, "qty": 60, "invoice_value": 600},
            {"nm_id": 2, "qty": 40, "invoice_value": 400},
        ],
        paid_share="0.15",
        paid_rub="1500",
    )
    _eq(rows[0]["paid_equivalent_quantity"], Decimal("9"), "15% first SKU")
    _eq(rows[1]["paid_equivalent_quantity"], Decimal("6"), "15% second SKU")
    _eq(sum((row["paid_capital_rub"] for row in rows), Decimal("0")), Decimal("1500"), "payment allocation")


def _wac_and_snapshot_stability() -> None:
    qty, capital, wac = roll_wac(quantity=0, capital=0, receipt_quantity=100, receipt_unit_cost=10)
    _eq(wac, Decimal("10"), "first receipt WAC")
    qty, capital, wac = roll_wac(
        quantity=qty, capital=capital, receipt_quantity=100, receipt_unit_cost=20
    )
    _eq(wac, Decimal("15"), "two receipt WAC")
    debit_snapshot = wac
    qty, capital, wac_after = roll_wac(
        quantity=qty, capital=capital, writeoff_quantity=50
    )
    _eq(wac_after, Decimal("15"), "ordinary writeoff preserves remaining WAC")
    qty, capital, newer_wac = roll_wac(
        quantity=qty, capital=capital, receipt_quantity=50, receipt_unit_cost=30
    )
    _eq(debit_snapshot, Decimal("15"), "older WB supply snapshot is immutable")
    if newer_wac == debit_snapshot:
        raise AssertionError("newer FF receipt must update current WAC")


def _outstanding_reconciliation() -> None:
    layers = [
        _layer("s1", "2026-07-02", 10, 100),
        _layer("s2", "2026-07-03", 10, 200),
    ]
    after = reconcile_outstanding_layers(
        layers,
        [
            _doprinato("d1", "2026-07-04", 6, original="s1"),
            _doprinato("d2", "2026-07-05", 4, original="s1"),
            _doprinato("d3", "2026-07-06", 5),
        ],
    )
    _eq(Decimal(after[0]["open_quantity"]), Decimal("0"), "sent100 accepted90 +6 +4")
    _eq(Decimal(after[1]["open_quantity"]), Decimal("5"), "strict FIFO keeps exact second layer")
    first_capital = Decimal(after[0]["open_quantity"]) * Decimal(after[0]["recognized_unit_cost_rub"])
    second_capital = Decimal(after[1]["open_quantity"]) * Decimal(after[1]["recognized_unit_cost_rub"])
    _eq(first_capital + second_capital, Decimal("1000"), "outstanding weighted layer capital")
    repeated = reconcile_outstanding_layers(layers, [_doprinato("same", "2026-07-04", 6), _doprinato("same", "2026-07-04", 6)])
    _eq(Decimal(repeated[0]["open_quantity"]), Decimal("4"), "repeat is idempotent")
    try:
        reconcile_outstanding_layers(layers, [_doprinato("bad", "2026-07-04", 21)])
    except CanonicalCostBlocked as exc:
        if exc.code != "doprinato_unmatched_surplus":
            raise
    else:
        raise AssertionError("over-doprinato must fail closed")
    future = [_layer("future", "2026-07-10", 10, 300)]
    try:
        reconcile_outstanding_layers(future, [_doprinato("early", "2026-07-09", 1)])
    except CanonicalCostBlocked:
        pass
    else:
        raise AssertionError("future outstanding must not be a FIFO candidate")


def _ff_operation_effective_date_resolution() -> None:
    with TemporaryDirectory() as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            _insert_primary(conn)
            operation = _insert_legacy_wb_operation_fixture(conn)
            resolution = resolve_ff_operation_effective_date(conn, operation)
            _eq(resolution.effective_date, "2026-05-07", "legacy WB fact date")
            _eq(
                resolution.provenance["source_field"],
                "normalized.fact_date",
                "legacy WB date provenance",
            )
            _eq(
                resolution.provenance["supply_id"],
                "38978468",
                "exact supply provenance",
            )
            movements = _wb_movement_evidence(conn, as_of_date="2026-07-01")
            _eq(
                sum((Decimal(str(item["open_quantity"])) for item in movements), Decimal("0")),
                Decimal("0"),
                "legacy underaccepted is audit-only at the cutover boundary",
            )
            source_timestamp = resolve_ff_operation_effective_date(
                conn,
                {
                    "operation_id": "ordinary-post-cutover",
                    "source_type": "wb_supply",
                    "source_key": "wb_supply_debit:supply:post",
                    "source_object_id": "post",
                    "created_at": "2026-07-09T00:00:00Z",
                    "diagnostics_json": '{"source_timestamp":"2026-07-04T12:00:00Z"}',
                },
            )
            _eq(source_timestamp.effective_date, "2026-07-04", "ordinary source timestamp")
            targeted = resolve_ff_operation_effective_date(
                conn,
                {
                    "operation_id": "targeted-40561872",
                    "source_type": "wb_supply",
                    "source_key": "wb_supply_debit:supply:40561872",
                    "source_object_id": "40561872",
                    "created_at": "2026-07-12T00:00:00Z",
                    "diagnostics_json": '{"supply_timestamp":"2026-07-02T12:38:24+00:00"}',
                },
            )
            _eq(targeted.effective_date, "2026-07-02", "targeted remediation timestamp")
            supplier = resolve_ff_operation_effective_date(
                conn,
                {
                    "operation_id": "supplier-receipt",
                    "source_type": "supplier_shipment",
                    "source_object_id": "primary-june",
                    "created_at": "2026-07-09T00:00:00Z",
                    "diagnostics_json": "{}",
                },
            )
            _eq(supplier.effective_date, "2026-06-23", "supplier acceptance semantics")
            manual = resolve_ff_operation_effective_date(
                conn,
                {
                    "operation_id": "manual-correction",
                    "source_type": "runtime_repair",
                    "created_at": "2026-07-09T18:28:28Z",
                    "diagnostics_json": "{}",
                },
            )
            _eq(manual.effective_date, "2026-07-09", "manual/correction created-at semantics")
            try:
                resolve_ff_operation_effective_date(
                    conn,
                    {
                        "operation_id": "missing-supply",
                        "source_type": "wb_supply",
                        "source_key": "wb_supply_debit:supply:missing",
                        "source_object_id": "missing",
                        "created_at": "2026-07-09T00:00:00Z",
                        "diagnostics_json": "{}",
                    },
                )
            except CanonicalCostBlocked as exc:
                _eq(
                    exc.code,
                    "wb_supply_effective_date_supply_missing",
                    "missing WB supply fails closed",
                )
            else:
                raise AssertionError("WB operation without its supply must block")
            conn.commit()
        engine = CanonicalCostEngine(runtime=runtime)
        audit = engine.ff_operation_date_audit()
        exact = next(
            item for item in audit["operations"]
            if item["operation_id"] == "ffso_034a89fb11b24ddbace9"
        )
        _eq(exact["resolved_business_date"], "2026-05-07", "exact legacy fixture date")
        _eq(exact["line_count"], 5, "exact legacy fixture line count")
        _eq(exact["sent_quantity"], "1250", "exact legacy fixture sent quantity")
        _eq(exact["accepted_quantity"], "1247", "exact legacy fixture accepted quantity")
        _eq(
            exact["line_set_fingerprint"],
            "sha256:671bb89a57a1e2bec2551defb553bf1e17d9958cb0a73f6e82e435ccd7a2c62e",
            "exact operation-wide line-set fingerprint",
        )


def _cutover_boundary_and_normalization_policy() -> None:
    operation = {
        "operation_id": "post-normalization-fixture",
        "supply_id": "supply-fixture",
        "source_key": "wb_supply_debit:supply:supply-fixture",
        "business_date": "2026-07-04",
        "line_set_fingerprint": "sha256:sent",
        "accepted_line_set_fingerprint": "sha256:accepted",
        "evidence_fingerprint": "sha256:evidence",
    }
    sent = {111: Decimal("100"), 222: Decimal("100")}
    accepted = {111: Decimal("105"), 222: Decimal("90")}
    no_manifest = _normalized_acceptance_plan(
        operation=operation, sent_by_nm=sent, accepted_by_nm=accepted
    )
    _eq(no_manifest[111]["effective_accepted"], Decimal("100"), "raw surplus is capped")
    _eq(no_manifest[222]["effective_accepted"], Decimal("90"), "future/unlisted supply is strict")
    POSTCUTOVER_NORMALIZATION_MANIFEST[operation["operation_id"]] = dict(operation)
    try:
        normalized = _normalized_acceptance_plan(
            operation=operation, sent_by_nm=sent, accepted_by_nm=accepted
        )
        _eq(normalized[111]["direct_accepted"], Decimal("100"), "direct acceptance")
        _eq(normalized[222]["normalized_accepted"], Decimal("5"), "same-supply shortage allocation")
        _eq(
            sum((item["effective_accepted"] for item in normalized.values()), Decimal("0")),
            Decimal("195"),
            "aggregate accepted quantity is conserved",
        )
        _eq(
            sum((item["open"] for item in normalized.values()), Decimal("0")),
            Decimal("5"),
            "sent equals effective accepted plus underaccepted",
        )
        changed = dict(operation)
        changed["evidence_fingerprint"] = "sha256:drift"
        drifted = _normalized_acceptance_plan(
            operation=changed, sent_by_nm=sent, accepted_by_nm=accepted
        )
        _eq(
            drifted[222]["normalized_accepted"],
            Decimal("0"),
            "exact manifest fingerprint drift fails closed",
        )
    finally:
        POSTCUTOVER_NORMALIZATION_MANIFEST.pop(operation["operation_id"], None)

    with TemporaryDirectory() as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            _insert_primary(conn)
            operation_row = _insert_legacy_wb_operation_fixture(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ff_stock_operations(
                    operation_id,operation_type,source_type,source_key,source_object_id,
                    source_object_label,created_at,created_by,sku_count,total_quantity_delta,
                    total_quantity_abs,warnings_json,diagnostics_json
                ) VALUES('legacy-audit-opening','opening_balance','manual','legacy-audit-opening',
                         'legacy-audit-opening','opening','2026-07-01T00:00:00Z','fixture',
                         5,1250,1250,'[]','{}')
                """
            )
            for line_no, nm_id in enumerate(
                (259460529, 259465495, 391662410, 428855306, 497414624),
                start=1,
            ):
                conn.execute(
                    "INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(operation_id,line_no,nm_id,quantity_delta,raw_json) VALUES('legacy-audit-opening',?,?,250,'{}')",
                    (line_no, nm_id),
                )
            conn.commit()
        engine = CanonicalCostEngine(runtime=runtime)
        report = engine.source_anomaly_preflight(date_to="2026-07-13")
        _eq(report["legacy_audit_operation_count"], 1, "legacy operation is fully audited")
        _eq(report["post_cutover_operation_count"], 0, "legacy operation is not replayed")
        _eq(report["anomalies"], [], "legacy accepted/sent mismatch is out of apply gate")
        if any(
            item.get("blocker_class") == "accepted_quantity_exceeds_sent"
            for item in report["unresolved_anomalies"]
        ):
            raise AssertionError("pre-cutover accepted/sent evidence cannot block apply")
        _eq(
            report["legacy_operations"][0]["operation_id"],
            operation_row["operation_id"],
            "legacy audit retains exact source identity",
        )


def _exact_unmatched_doprinato_absorption_manifest() -> None:
    report = _unmatched_doprinato_manifest_report()
    report_v2 = _unmatched_doprinato_manifest_report_v2()
    _eq(report["policy"], UNMATCHED_DOPRINATO_ABSORPTION_POLICY, "policy")
    _eq(report["supply_count"], 10, "exact manifest supply count")
    _eq(report["sku_count"], 7, "exact manifest SKU count")
    _eq(report["unit_count"], "11", "exact manifest unit count")
    _eq(
        report["recognized_reference_exposure_rub"],
        "1188.486778",
        "recognized audit exposure",
    )
    _eq(
        report["paid_reference_exposure_rub"],
        "951.278606",
        "paid audit exposure",
    )
    if not str(report["manifest_fingerprint"]).startswith("sha256:"):
        raise AssertionError("manifest must expose a stable SHA-256 fingerprint")
    _eq(
        report["manifest_fingerprint"],
        "sha256:d9dc86710a5a2a6cf607d898807e4d9f2ba40fd9bb77ff9fc98e5e6c9b3d0945",
        "V1 fingerprint remains unchanged",
    )
    _eq(report_v2["policy"], UNMATCHED_DOPRINATO_ABSORPTION_POLICY_V2, "V2 policy")
    _eq(report_v2["row_count"], 9, "V2 exact row count")
    _eq(report_v2["supply_count"], 5, "V2 affected supply count")
    _eq(report_v2["unit_count"], "12", "V2 exact unit count")
    _eq(
        report_v2["manifest_fingerprint"],
        "sha256:1076507575d785395dde47be185fa1144c49c1e4af7b404ea252e87652656161",
        "V2 manifest fingerprint",
    )
    _eq(
        report_v2["recognized_reference_exposure_rub"],
        "1385.410826",
        "V2 recognized audit exposure",
    )
    _eq(
        report_v2["paid_reference_exposure_rub"],
        "1385.410826",
        "V2 paid audit exposure",
    )

    all_manifest_rows = [
        *UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.values(),
        *UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2.values(),
    ]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        ensure_canonical_cost_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions(
                baseline_id,version,cutover_date,primary_shipment_id,
                primary_accepted_ff_date,primary_quantity,primary_sku_count,
                weighted_ff_unit_cost_rub,fallback_sku_count,
                business_approved_sku_count,fingerprint,report_json,is_current,
                created_at
            ) VALUES('baseline',1,'2026-07-01','shipment','2026-06-24','1',1,
                     '111.181389',3,2,'baseline-fingerprint','{}',1,
                     '2026-07-13T00:00:00Z')
            """
        )
        for expected in all_manifest_rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO sheet_vitrina_v1_canonical_cost_baseline_lines(
                    baseline_id,nm_id,stage,physical_quantity,
                    paid_equivalent_quantity,recognized_unit_cost_rub,
                    paid_unit_cost_rub,recognized_capital_rub,paid_capital_rub,
                    cost_covered_quantity,confirmed_quantity,source_type,
                    source_identity,source_date,provenance_json,line_fingerprint
                ) VALUES('baseline',?,?, '1','1',?,?,? ,?,'1','1',
                         'primary_supplier_shipment','shipment','2026-06-24','{}',?)
                """,
                (
                    expected["nm_id"],
                    expected["cost_reference_stage"],
                    expected["recognized_reference_unit_cost_rub"],
                    expected["paid_reference_unit_cost_rub"],
                    expected["recognized_reference_unit_cost_rub"],
                    expected["paid_reference_unit_cost_rub"],
                    f"line-{expected['nm_id']}-{expected['cost_reference_stage']}",
                ),
            )
        conn.commit()
        for expected in UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.values():
            fact = {
                "supply_id": expected["supply_id"],
                "accepted_date": expected["business_date"],
                "nm_id": expected["nm_id"],
                "warehouse": expected["warehouse"],
                "destination": expected["destination"],
                "accepted_quantity": expected["quantity"],
                "original_supply_id": "",
                "is_doprinato": True,
                "is_final_accepted": True,
                "source_identity": expected["source_identity"],
            }
            fact["raw_row_line_fingerprint"] = expected[
                "raw_row_line_fingerprint"
            ]
            decision = _unmatched_doprinato_manifest_decision(conn, fact)
            if decision is None or not decision["matched"]:
                raise AssertionError(
                    f"exact absorption row did not match: {expected['supply_id']}"
                )
            _eq(
                decision["classification"],
                UNMATCHED_DOPRINATO_ABSORPTION_CLASSIFICATION,
                "audit-only classification",
            )
        drift = dict(fact)
        drift["raw_row_line_fingerprint"] = "sha256:changed"
        drift_decision = _unmatched_doprinato_manifest_decision(conn, drift)
        if drift_decision is None or drift_decision["matched"]:
            raise AssertionError("changed source fingerprint must fail closed")
        future = dict(fact, supply_id="future-1", source_identity="supply:future-1")
        if _unmatched_doprinato_manifest_decision(conn, future) is not None:
            raise AssertionError("future unmatched doprinato received manifest approval")
        removed = UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.pop(
            str(fact["supply_id"])
        )
        try:
            if _unmatched_doprinato_manifest_decision(conn, fact) is not None:
                raise AssertionError("removed manifest row must return to strict path")
        finally:
            UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST[
                str(fact["supply_id"])
            ] = removed

        for expected in UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.values():
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_supplies(
                    supply_id,cache_key,normalized_row_json,raw_goods_json,
                    warehouse_id,status_id,quantity_for_size_filter,fact_date,
                    synced_at
                ) VALUES(?,?,?,?,?,5,?,?,?)
                """,
                (
                    expected["supply_id"],
                    expected["source_identity"],
                    json.dumps(
                        {
                            "supply_id": expected["supply_id"],
                            "status_id": 5,
                            "fact_date": expected["business_date"],
                            "warehouse_name": expected["warehouse"],
                            "destination_name": expected["destination"],
                            "virtual_type_id": 5,
                            "type_label": "Допринято",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        [
                            {
                                "nmID": expected["nm_id"],
                                "acceptedQuantity": int(expected["quantity"]),
                            }
                        ]
                    ),
                    expected["warehouse"],
                    int(expected["quantity"]),
                    expected["business_date"],
                    f"{expected['business_date']}T00:00:00Z",
                ),
            )
        # V2 shares persisted supply rows with V1 but pins additional exact
        # SKU lines.  Keep one authoritative raw supply row per supply.
        for supply_id in sorted(
            {str(row["supply_id"]) for row in UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2.values()}
        ):
            source = conn.execute(
                "SELECT raw_goods_json,quantity_for_size_filter FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=?",
                (supply_id,),
            ).fetchone()
            goods = json.loads(str(source["raw_goods_json"]))
            extra = [
                {
                    "nmID": int(row["nm_id"]),
                    "acceptedQuantity": int(row["quantity"]),
                }
                for row in UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2.values()
                if str(row["supply_id"]) == supply_id
            ]
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=?,quantity_for_size_filter=? WHERE supply_id=?",
                (
                    json.dumps([*goods, *extra]),
                    int(source["quantity_for_size_filter"] or 0)
                    + sum(int(row["acceptedQuantity"]) for row in extra),
                    supply_id,
                ),
            )
        conn.commit()
        original_fingerprints = {
            supply_id: str(row["raw_row_line_fingerprint"])
            for supply_id, row in UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.items()
        }
        original_v2_fingerprints = {
            key: {
                field: str(row[field])
                for field in (
                    "raw_source_row_fingerprint",
                    "raw_source_line_fingerprint",
                    "raw_row_line_fingerprint",
                    "semantic_evidence_fingerprint",
                )
            }
            for key, row in UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2.items()
        }
        last_v2_fact = None
        for fact_row in _wb_supply_cache_evidence(conn, date_to="2026-07-13"):
            expected = UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.get(
                str(fact_row["supply_id"])
            )
            if expected is not None and int(fact_row["nm_id"]) == int(
                expected["nm_id"]
            ):
                expected["raw_row_line_fingerprint"] = str(
                    fact_row["raw_row_line_fingerprint"]
                )
            expected_v2 = UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2.get(
                (str(fact_row["supply_id"]), int(fact_row["nm_id"]))
            )
            if expected_v2 is not None:
                last_v2_fact = dict(fact_row)
                for field in (
                    "raw_source_row_fingerprint",
                    "raw_source_line_fingerprint",
                    "raw_row_line_fingerprint",
                    "semantic_evidence_fingerprint",
                ):
                    expected_v2[field] = str(fact_row[field])
                decision_v2 = _unmatched_doprinato_manifest_decision(
                    conn, fact_row
                )
                if decision_v2 is None or not decision_v2["matched"]:
                    raise AssertionError(
                        "exact V2 absorption row did not match: "
                        f"{fact_row['supply_id']}/{fact_row['nm_id']}"
                    )
        if last_v2_fact is None:
            raise AssertionError("V2 fixture did not produce source evidence")
        original_identity_drift = dict(
            last_v2_fact, original_supply_id="fabricated-original"
        )
        drift_decision_v2 = _unmatched_doprinato_manifest_decision(
            conn, original_identity_drift
        )
        if drift_decision_v2 is None or drift_decision_v2["matched"]:
            raise AssertionError("fabricated V2 original_supply_id must fail closed")
        last_v2_expected = UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2[
            (str(last_v2_fact["supply_id"]), int(last_v2_fact["nm_id"]))
        ]
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_canonical_cost_baseline_lines
            SET recognized_unit_cost_rub='999'
            WHERE baseline_id='baseline' AND nm_id=? AND stage=?
            """,
            (
                int(last_v2_expected["nm_id"]),
                str(last_v2_expected["cost_reference_stage"]),
            ),
        )
        cost_drift_decision = _unmatched_doprinato_manifest_decision(
            conn, last_v2_fact
        )
        if cost_drift_decision is None or cost_drift_decision["matched"]:
            raise AssertionError("changed current canonical V2 cost must fail closed")
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_canonical_cost_baseline_lines
            SET recognized_unit_cost_rub=?
            WHERE baseline_id='baseline' AND nm_id=? AND stage=?
            """,
            (
                str(last_v2_expected["recognized_reference_unit_cost_rub"]),
                int(last_v2_expected["nm_id"]),
                str(last_v2_expected["cost_reference_stage"]),
            ),
        )
        preflight = _source_anomaly_preflight_conn(
            conn, date_to="2026-07-13", baseline_costs={}
        )
        _eq(preflight["status"], "ok", "exact manifest clears strict preflight")
        _eq(len(preflight["anomalies"]), 19, "all exact rows are audited")
        _eq(
            preflight["unmatched_doprinato_absorption"]["matched_unit_count"],
            "23",
            "all exact units matched",
        )
        if preflight["unmatched_doprinato_absorption"]["all_rows_match"] is not True:
            raise AssertionError("full exact manifest did not match")
        _eq(
            preflight["unmatched_doprinato_absorption"]["approved_row_count"],
            19,
            "combined manifest row count",
        )
        if any(
            item["raw_quantities"]["movement_quantity_delta"] != "0"
            or item["raw_quantities"]["recognized_capital_delta_rub"] != "0"
            or item["raw_quantities"]["paid_capital_delta_rub"] != "0"
            or item["raw_quantities"]["confirmation_quantity_delta"] != "0"
            or item["raw_quantities"]["underaccepted_quantity_delta"] != "0"
            for item in preflight["anomalies"]
        ):
            raise AssertionError(
                "absorption manifest created quantity/capital/confirmation/underaccepted"
            )
        movements = _wb_movement_evidence(
            conn, as_of_date="2026-07-13", anomaly_report=preflight
        )
        _eq(movements, [], "absorbed evidence does not create a movement")

        removed_supply_id = "40778405"
        removed = UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST.pop(removed_supply_id)
        try:
            removed_report = _source_anomaly_preflight_conn(
                conn,
                date_to="2026-07-13",
                baseline_costs={259466031: {
                    "recognized_unit_cost_rub": Decimal("100.146048"),
                    "paid_unit_cost_rub": Decimal("100.146048"),
                }},
            )
            if removed_report["status"] != "blocked" or not any(
                item.get("supply_id") == removed_supply_id
                for item in removed_report["unresolved_anomalies"]
            ):
                raise AssertionError("removed exact row did not return to blocker")
        finally:
            UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST[removed_supply_id] = removed

        removed_v2_key = ("40765458", 497414624)
        removed_v2 = UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2.pop(
            removed_v2_key
        )
        try:
            removed_v2_report = _source_anomaly_preflight_conn(
                conn, date_to="2026-07-13", baseline_costs={}
            )
            if removed_v2_report["status"] != "blocked" or not any(
                item.get("supply_id") == removed_v2_key[0]
                and int(item.get("nm_id") or 0) == removed_v2_key[1]
                for item in removed_v2_report["unresolved_anomalies"]
            ):
                raise AssertionError("removed V2 row did not return to blocker")
        finally:
            UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2[
                removed_v2_key
            ] = removed_v2

        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_supplies(
                supply_id,cache_key,normalized_row_json,raw_goods_json,
                warehouse_id,status_id,quantity_for_size_filter,fact_date,
                synced_at
            ) VALUES('future-unmatched','supply:future-unmatched',?,?,
                     'Электросталь',5,1,'2026-07-13','2026-07-13T00:00:00Z')
            """,
            (
                json.dumps(
                    {
                        "supply_id": "future-unmatched",
                        "status_id": 5,
                        "fact_date": "2026-07-13",
                        "warehouse_name": "Электросталь",
                        "destination_name": "Электросталь",
                        "virtual_type_id": 5,
                        "type_label": "Допринято",
                    },
                    ensure_ascii=False,
                ),
                json.dumps([{"nmID": 259466031, "acceptedQuantity": 1}]),
            ),
        )
        conn.commit()
        future_report = _source_anomaly_preflight_conn(
            conn, date_to="2026-07-13", baseline_costs={259466031: {
                "recognized_unit_cost_rub": Decimal("100.146048"),
                "paid_unit_cost_rub": Decimal("100.146048"),
            }}
        )
        if future_report["status"] != "blocked" or not any(
            item.get("supply_id") == "future-unmatched"
            for item in future_report["unresolved_anomalies"]
        ):
            raise AssertionError("future unmatched doprinato must stay fail-closed")
        for supply_id, fingerprint in original_fingerprints.items():
            UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST[supply_id][
                "raw_row_line_fingerprint"
            ] = fingerprint
        for key, fingerprints in original_v2_fingerprints.items():
            UNMATCHED_DOPRINATO_ABSORPTION_MANIFEST_V2[key].update(
                fingerprints
            )
    finally:
        conn.close()


def _targeted_remediation_stays_outside_opening_collapse() -> None:
    import json

    with TemporaryDirectory() as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint(
                    slot,checkpoint_id,created_at,created_by,reason,
                    baseline_cache_keys_json,baseline_source_keys_json,
                    baseline_supply_ids_json,baseline_record_count,diagnostics_json
                ) VALUES('current','targeted-checkpoint','2026-07-01T00:00:00Z','fixture',
                         'fixture',?,?,?,1,'{}')
                """,
                (
                    json.dumps(["supply:40561872"]),
                    json.dumps(["wb_supply_debit:supply:40561872"]),
                    json.dumps(["40561872"]),
                ),
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ff_stock_operations(
                    operation_id,operation_type,source_type,source_key,source_object_id,
                    source_object_label,created_at,created_by,sku_count,total_quantity_delta,
                    total_quantity_abs,warnings_json,diagnostics_json
                ) VALUES('targeted-40561872','auto_writeoff','wb_supply',
                         'wb_supply_debit:supply:40561872','40561872','targeted',
                         '2026-07-12T00:00:00Z','fixture',1,-31500,31500,'[]',?)
                """,
                (json.dumps({
                    "reason": "targeted_pre_activation_remediation",
                    "supply_timestamp": "2026-07-02T12:38:24+00:00",
                }),),
            )
            context = _ff_opening_boundary_context(conn)
        if "targeted-40561872" in context["checkpoint_operation_ids"]:
            raise AssertionError("targeted remediation must remain a real post-cutover debit")


def _baseline_and_physical_sources() -> None:
    with TemporaryDirectory() as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            _insert_primary(conn)
            _insert_fallback_production(conn, nm_id=222)
            _insert_fallback_production(
                conn, nm_id=497415593, shipment_id="business-approved-497415593"
            )
            _insert_fallback_production(
                conn, nm_id=497416931, shipment_id="business-approved-497416931"
            )
            _insert_supplier_payment(
                conn, shipment_id="fallback-production", cny="150", rub="1500"
            )
            # The activation/opening receipt includes the pre-cutover sent
            # quantity; canonical replay collapses the accepted part into WB
            # opening and retains only sent-accepted as opening outstanding.
            _insert_ff_balance(conn, nm_id=111, quantity=6750)
            _insert_opening_boundary_wb_supply(conn)
            _insert_checkpoint(conn)
            _insert_snapshot(
                conn,
                "2026-05-16",
                {
                    222: {"onec_FF_STOCK_unit_cost_rub": 80},
                    497415593: {"onec_FF_STOCK_unit_cost_rub": 50},
                    497416931: {"onec_FF_STOCK_unit_cost_rub": 60},
                },
            )
            _insert_snapshot(conn, "2026-05-17", {222: {"onec_FF_STOCK_unit_cost_rub": 90}})
            _insert_snapshot(conn, "2026-07-01", {111: {"stock_total": 93250}, 222: {"stock_total": 0}})
            conn.commit()
        engine = CanonicalCostEngine(runtime=runtime, timestamp_factory=lambda: "2026-07-12T00:00:00Z")
        primary = engine.discover_primary_baseline_shipment()
        _eq(primary["shipment_id"], "primary-june", "primary discovery")
        _eq(Decimal(primary["weighted_ff_unit_cost_rub"]), Decimal("111.181389"), "expected FF average")
        plan = engine.build_baseline_plan()
        _eq(plan["primary_sku_count"], 1, "primary SKU count")
        _eq(plan["fallback_sku_count"], 1, "fallback SKU count")
        _eq(plan["business_approved_sku_count"], 2, "bounded business-approved count")
        fallback = plan["fallbacks"][0]
        _eq(fallback["as_of_date"], "2026-05-16", "nearest allowed 1C date")
        _eq(fallback["source_type"], BASELINE_ONEC, "1C quality provenance")
        _eq(fallback["unit_cost_rub"], "80", "post-cutoff 1C is forbidden")
        if "near_future_proxy" in str(plan):
            raise AssertionError("future proxy is forbidden")
        approved = plan["business_approved_fallbacks"]
        _eq(
            {item["nm_id"] for item in approved},
            {497415593, 497416931},
            "only exact business-approved nmIDs receive the estimate",
        )
        for item in approved:
            _eq(item["source_type"], BASELINE_BUSINESS_APPROVED_PRIMARY_WAC, "source type")
            _eq(item["unit_cost_rub"], "111.181389", "exact current primary WAC")
            provenance = item["provenance"]
            _eq(provenance["primary_shipment_id"], "primary-june", "primary shipment provenance")
            _eq(provenance["ff_cost_layer_id"], "ff-primary", "current FF layer provenance")
            _eq(provenance["approved_nm_ids"], [497415593, 497416931], "bounded approval provenance")
            _eq(
                provenance["reason"],
                "discontinued_immaterial_sku_business_approved_estimate",
                "business reason provenance",
            )
        approved_lines = [
            line for line in plan["lines"]
            if line["nm_id"] in {497415593, 497416931}
        ]
        if not approved_lines:
            raise AssertionError("business-approved opening lines missing")
        for line in approved_lines:
            _eq(line["recognized_unit_cost_rub"], "111.181389", "estimated recognized cost")
            _eq(line["confirmed_quantity"], "0", "estimate is not document-confirmed")
            _eq(line["cost_covered_quantity"], line["physical_quantity"], "estimate covers physical qty")
            _eq(line["paid_equivalent_quantity"], "0", "estimate does not invent payment")
            _eq(line["paid_capital_rub"], "0", "paid capital remains factual only")
        _eq(plan["physical"]["111"]["FF"], "6750", "FF physical quantity comes from ledger")
        _eq(plan["cost_coverage"], "1", "baseline coverage 100%")
        production = next(
            line for line in plan["lines"]
            if line["nm_id"] == 222 and line["stage"] == "PRODUCTION"
        )
        _eq(production["physical_quantity"], "100", "full production quantity")
        _eq(
            production["paid_equivalent_quantity"], "15",
            "15% payment is allocated over the full production line set",
        )
        _eq(production["paid_capital_rub"], "1500", "factual paid capital")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_ff_cost_layers SET weighted_avg_ff_unit_cost_rub=111.185 WHERE layer_id='ff-primary'"
            )
            conn.commit()
        within_tolerance = engine.build_baseline_plan()
        _eq(
            {item["unit_cost_rub"] for item in within_tolerance["business_approved_fallbacks"]},
            {"111.185"},
            "estimate is computed from the current primary layer, not hardcoded",
        )
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_ff_cost_layers SET weighted_avg_ff_unit_cost_rub=111.181389 WHERE layer_id='ff-primary'"
            )
            conn.commit()
        engine.materialize_baseline_plan(plan)
        result = engine.rebuild(date_from="2026-07-01", date_to="2026-07-01")
        if result.daily_rows_changed <= 0:
            raise AssertionError("first unified projection must materialize rows")
        second = engine.rebuild(date_from="2026-07-01", date_to="2026-07-01")
        _eq(second.daily_rows_changed, 0, "repeat daily materialization")
        with _connect(runtime.db_path) as conn:
            ff = conn.execute(
                "SELECT physical_quantity FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE as_of_date='2026-07-01' AND nm_id=111 AND stage='FF'"
            ).fetchone()
            wb = conn.execute(
                "SELECT physical_quantity FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE as_of_date='2026-07-01' AND nm_id=111 AND stage='WB'"
            ).fetchone()
            opening_outstanding = conn.execute(
                """
                SELECT open_quantity,recognized_unit_cost_rub
                FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers
                WHERE is_current=1 AND original_supply_id='legacy-opening-supply'
                """
            ).fetchone()
        _eq(ff[0], "6750", "daily FF/ledger reconciliation")
        _eq(wb[0], "93250", "WB physical quantity comes from official stock")
        _eq(opening_outstanding, None, "pre-cutover outstanding is legacy audit-only")
        _canonical_outstanding_sql(engine, runtime)
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_ff_cost_layers SET weighted_avg_ff_unit_cost_rub=112 WHERE layer_id='ff-primary'"
            )
            conn.commit()
        try:
            engine.build_baseline_plan()
        except CanonicalCostBlocked as exc:
            _eq(exc.code, "primary_baseline_shipment_not_unique", "out-of-tolerance primary blocks")
        else:
            raise AssertionError("changed primary WAC must block baseline")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_ff_cost_layers SET weighted_avg_ff_unit_cost_rub=111.181389,is_current=0 WHERE layer_id='ff-primary'"
            )
            conn.commit()
        try:
            engine.build_baseline_plan()
        except CanonicalCostBlocked as exc:
            _eq(exc.code, "primary_baseline_shipment_not_unique", "missing current primary blocks")
        else:
            raise AssertionError("missing current primary layer must block baseline")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_ff_cost_layers SET is_current=1 WHERE layer_id='ff-primary'"
            )
            conn.commit()
        with _connect(runtime.db_path) as conn:
            _insert_fallback_production(conn, nm_id=333, shipment_id="missing-cost")
            conn.commit()
        try:
            engine.build_baseline_plan()
        except CanonicalCostBlocked as exc:
            if exc.code != "baseline_cost_coverage_incomplete":
                raise
        else:
            raise AssertionError("missing opening SKU cost must block the whole baseline")


def _canonical_outstanding_sql(
    engine: CanonicalCostEngine, runtime: RegistryUploadDbBackedRuntime
) -> None:
    changed = engine._replace_versioned_movement_plans(  # noqa: SLF001 - targeted persistence smoke
        [
            {
                "operation_id": "wb-debit-1", "supply_id": "wb-supply-1", "nm_id": 111,
                "effective_date": "2026-07-02", "sent_quantity": "100",
                "paid_equivalent_quantity": "100", "cost_coverage_share": "1",
                "confirmation_share": "1", "recognized_unit_cost_rub": "120",
                "paid_unit_cost_rub": "110", "recognized_capital_rub": "12000",
                "paid_capital_rub": "11000", "ff_wac_quantity_before": "6750",
                "source_operation_key": "wb_supply_debit:cache-wb-supply-1",
            }
        ]
    )
    _eq(changed, 1, "movement snapshot persisted")
    with _connect(runtime.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ff_stock_operations(
                operation_id,operation_type,source_type,source_key,source_object_id,
                source_object_label,created_at,created_by,sku_count,total_quantity_delta,
                total_quantity_abs,warnings_json,diagnostics_json
            ) VALUES('wb-debit-1','auto_writeoff','wb_supply','wb_supply_debit:cache-wb-supply-1',
                     'wb-supply-1','WB supply 1','2026-07-02T00:00:00Z','fixture',
                     1,-100,100,'[]','{}')
            """
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                operation_id,line_no,nm_id,quantity_delta,raw_json
            ) VALUES('wb-debit-1',1,111,-100,'{}')
            """
        )
        _insert_wb_supply(conn, "wb-supply-1", 90, "2026-07-03")
        conn.commit()
    engine._materialize_outstanding_layers("2026-07-03")  # noqa: SLF001
    _eq(_open_qty(runtime), "10", "accepted 90 leaves outstanding 10")
    with _connect(runtime.db_path) as conn:
        _insert_wb_supply(
            conn,
            "dop-1",
            6,
            "2026-07-04",
            doprinato=True,
            original_supply_id="wb-supply-1",
        )
        conn.commit()
    engine._materialize_outstanding_layers("2026-07-04")  # noqa: SLF001
    _eq(_open_qty(runtime), "4", "doprinato 6 leaves outstanding 4")
    with _connect(runtime.db_path) as conn:
        _insert_wb_supply(
            conn,
            "dop-2",
            4,
            "2026-07-05",
            doprinato=True,
            original_supply_id="wb-supply-1",
        )
        conn.commit()
    engine._materialize_outstanding_layers("2026-07-05")  # noqa: SLF001
    _eq(_open_qty(runtime), "0", "doprinato 4 closes outstanding")
    _eq(
        engine.physical_quantities_as_of("2026-07-05")[111]["FF_TO_WB"],
        Decimal("0"),
        "doprinato closes its original post-cutover layer without legacy outstanding",
    )
    with _connect(runtime.db_path) as conn:
        _insert_snapshot(conn, "2026-07-03", {111: {"stock_total": 93340}})
        _insert_snapshot(conn, "2026-07-04", {111: {"stock_total": 93346}})
        _insert_snapshot(conn, "2026-07-05", {111: {"stock_total": 93350}})
        _insert_snapshot(conn, "2026-07-06", {111: {"stock_total": 90000}})
        _insert_snapshot(conn, "2026-07-07", {111: {"stock_total": 90010}})
        conn.commit()
    states = engine._wb_cost_states(  # noqa: SLF001 - rolling-state invariant smoke
        ["2026-07-01", "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07"]
    )
    opening_capital = Decimal("93250") * Decimal("111.181389")
    _eq(
        Decimal(states["2026-07-03"][111]["recognized_capital"]),
        opening_capital + Decimal("90") * Decimal("120"),
        "original acceptance enters WB with debit snapshot cost",
    )
    _eq(
        Decimal(states["2026-07-05"][111]["recognized_capital"]),
        opening_capital + Decimal("100") * Decimal("120"),
        "doprinato 6+4 enters WB with the original layer",
    )
    wac_before_reduction = Decimal(states["2026-07-05"][111]["recognized_capital"]) / Decimal("93350")
    wac_after_reduction = Decimal(states["2026-07-06"][111]["recognized_capital"]) / Decimal("90000")
    _eq(wac_after_reduction, wac_before_reduction, "WB stock reduction preserves WAC")
    growth = states["2026-07-07"][111]
    _eq(growth["quality"], "unexplained_growth_existing_wac", "unexplained growth is explicit")
    _eq(
        Decimal(growth["recognized_capital"]) / Decimal("90010"),
        wac_after_reduction,
        "unexplained growth uses only the existing WAC estimate",
    )
    import json
    with _connect(runtime.db_path) as conn:
        conn.execute(
            "UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id='wb-supply-1'",
            (json.dumps([{"nmID": 111, "acceptedQuantity": 101, "quantity": 101}]),),
        )
        conn.commit()
    try:
        engine.physical_quantities_as_of("2026-07-07")
    except CanonicalCostBlocked as exc:
        _eq(
            exc.code,
            "cutover_source_anomaly_preflight_blocked",
            "ordinary post-cutover over-acceptance remains blocked by exhaustive preflight",
        )
        if "accepted_quantity_exceeds_sent" not in str(exc.details):
            raise AssertionError("raw post-cutover over-acceptance must remain visible")
    else:
        raise AssertionError("ordinary accepted quantity above sent must fail closed")
    with _connect(runtime.db_path) as conn:
        conn.execute(
            "UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id='wb-supply-1'",
            (json.dumps([{"nmID": 111, "acceptedQuantity": 90, "quantity": 90}]),),
        )
        conn.commit()


def _insert_wb_supply(
    conn,
    supply_id: str,
    accepted: int,
    fact_date: str,
    *,
    doprinato: bool = False,
    warehouse: str = "W",
    destination: str = "D",
    original_supply_id: str = "",
) -> None:
    normalized = {
        "supply_id": supply_id, "status_id": 5, "fact_date": fact_date,
        "warehouse_name": warehouse, "destination_name": destination,
        "original_supply_id": original_supply_id,
        "virtual_type_id": 5 if doprinato else 0,
        "type_label": "Допринято" if doprinato else "Обычная",
    }
    import json
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_wb_supplies(
            supply_id,cache_key,normalized_row_json,raw_goods_json,warehouse_id,status_id,
            quantity_for_size_filter,fact_date,synced_at
        ) VALUES(?,?,?,?,?,5,?,?,?)
        """,
        (
            supply_id, f"cache-{supply_id}", json.dumps(normalized, ensure_ascii=False),
            json.dumps([{"nmID": 111, "acceptedQuantity": accepted, "quantity": accepted}], ensure_ascii=False),
            warehouse, accepted, fact_date, f"{fact_date}T12:00:00Z",
        ),
    )


def _insert_legacy_wb_operation_fixture(conn) -> dict[str, object]:
    import json

    operation_id = "ffso_034a89fb11b24ddbace9"
    source_key = "wb_supply_debit:supply:38978468"
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_wb_supplies(
            supply_id,cache_key,wb_supply_id,normalized_row_json,raw_goods_json,
            warehouse_id,status_id,quantity_for_size_filter,supply_date,fact_date,synced_at
        ) VALUES(?,?,?,?,?,'210001',5,1250,'2026-05-07','2026-05-07',
                 '2026-06-10T21:01:35Z')
        """,
        (
            "38978468",
            "supply:38978468",
            "38978468",
            json.dumps(
                {
                    "supply_id": "38978468",
                    "wb_supply_id": "38978468",
                    "cache_key": "supply:38978468",
                    "status_id": 5,
                    "fact_date": "2026-05-07T13:09:53+03:00",
                    "supply_date": "2026-05-07T00:00:00+03:00",
                    "accepted_quantity": 1247,
                    "warehouse_name": "W",
                    "destination_name": "D",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                [
                    {"nmID": 259460529, "quantity": 250, "acceptedQuantity": 250},
                    {"nmID": 259465495, "quantity": 250, "acceptedQuantity": 247},
                    {"nmID": 391662410, "quantity": 250, "acceptedQuantity": 250},
                    {"nmID": 428855306, "quantity": 250, "acceptedQuantity": 250},
                    {"nmID": 497414624, "quantity": 250, "acceptedQuantity": 250},
                ],
                ensure_ascii=False,
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operations(
            operation_id,operation_type,source_type,source_key,source_object_id,
            source_object_label,created_at,created_by,sku_count,total_quantity_delta,
            total_quantity_abs,warnings_json,diagnostics_json
        ) VALUES(?, 'auto_writeoff','wb_supply',?,'38978468','38978468',
                 '2026-07-09T05:11:09Z','system',5,-1250,1250,'[]','{}')
        """,
        (operation_id, source_key),
    )
    nm_ids = (259460529, 259465495, 391662410, 428855306, 497414624)
    for line_no, nm_id in enumerate(nm_ids, start=1):
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                operation_id,line_no,nm_id,quantity_delta,raw_json
            ) VALUES(?,?,?,-250,'{}')
            """,
            (operation_id, line_no, nm_id),
        )
    return {
        "operation_id": operation_id,
        "operation_type": "auto_writeoff",
        "source_type": "wb_supply",
        "source_key": source_key,
        "source_object_id": "38978468",
        "created_at": "2026-07-09T05:11:09Z",
        "diagnostics_json": "{}",
        "total_quantity_abs": 1250,
    }


def _open_qty(runtime: RegistryUploadDbBackedRuntime) -> str:
    with _connect(runtime.db_path) as conn:
        row = conn.execute(
            "SELECT open_quantity FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE is_current=1 AND original_supply_id='wb-supply-1'"
        ).fetchone()
    return str(row[0])


def _insert_primary(conn) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_shipments(
            shipment_id,created_at,updated_at,shipment_date,actual_shipment_date,
            actual_ff_acceptance_date,order_status,expenses_complete,invoice_no,invoice_date,
            currency,product_qty_total,product_amount_total,extras_amount_total,invoice_amount_total,
            match_status,warnings_json,errors_json
        ) VALUES('primary-june','2026-06-01T00:00:00Z','2026-06-24T00:00:00Z','2026-06-01',
                 '2026-06-10','2026-06-23','accepted_ff',1,'INV-1','2026-06-01','CNY',
                 100000,1000000,0,1000000,'all_matched','[]','[]')
        """
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
            line_id,shipment_id,line_type,sort_order,internal_sku,internal_nm_id,internal_name,
            qty,unit_price,amount,currency,match_status,manual_override,raw_json
        ) VALUES('line-primary','primary-june','product',1,'SKU-111',111,'SKU 111',100000,10,1000000,
                 'CNY','matched',0,'{}')
        """
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layers(
            layer_id,supplier_shipment_id,status,accepted_ff_date,calculated_at,effective_cny_rate,
            invoice_amount_total_cny,invoice_extras_total_cny,product_qty_total,common_expense_pool_rub,
            common_expense_per_unit_rub,weighted_avg_ff_unit_cost_rub,reconciliation_status,
            reconciliation_delta_rub,inputs_hash,version,is_current,source_status_json,component_status_json
        ) VALUES('ff-primary','primary-june','confirmed','2026-06-23','2026-06-24T00:00:00Z',10,
                 1000000,0,100000,1118138.9,11.181389,111.181389,'ok',0,'primary-hash',1,1,'{}','{}')
        """
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layer_lines(
            layer_line_id,layer_id,supplier_shipment_id,supplier_line_id,nm_id,sku,display_name,qty,
            invoice_unit_price_cny,sku_purchase_cost_rub,allocated_common_expenses_per_unit_rub,
            sku_ff_unit_cost_rub,line_total_cost_rub,allocation_method,source_status
        ) VALUES('ff-line-primary','ff-primary','primary-june','line-primary',111,'SKU-111','SKU 111',
                 100000,10,100,11.181389,111.181389,11118138.9,'qty_based_common_pool','confirmed')
        """
    )


def _insert_fallback_production(conn, *, nm_id: int, shipment_id: str = "fallback-production") -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_shipments(
            shipment_id,created_at,updated_at,shipment_date,order_status,expenses_complete,
            invoice_no,invoice_date,currency,product_qty_total,product_amount_total,extras_amount_total,
            invoice_amount_total,match_status,warnings_json,errors_json
        ) VALUES(?,?,?,?, 'production',0,?,?, 'CNY',100,1000,0,1000,'all_matched','[]','[]')
        """,
        (shipment_id, "2026-06-25T00:00:00Z", "2026-06-25T00:00:00Z", "2026-06-25", shipment_id, "2026-06-25"),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
            line_id,shipment_id,line_type,sort_order,internal_sku,internal_nm_id,internal_name,
            qty,unit_price,amount,currency,match_status,manual_override,raw_json
        ) VALUES(?,?, 'product',1,?,?,?,100,10,1000,'CNY','matched',0,'{}')
        """,
        (f"line-{shipment_id}", shipment_id, f"SKU-{nm_id}", nm_id, f"SKU {nm_id}"),
    )


def _insert_opening_boundary_wb_supply(conn) -> None:
    import json

    supply_id = "legacy-opening-supply"
    cache_key = f"supply:{supply_id}"
    normalized = {
        "supply_id": supply_id,
        "wb_supply_id": supply_id,
        "cache_key": cache_key,
        "status_id": 5,
        "fact_date": "2026-05-07",
        "supply_date": "2026-05-07",
        "warehouse_name": "W",
        "destination_name": "D",
        "accepted_quantity": 247,
    }
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_wb_supplies(
            supply_id,cache_key,wb_supply_id,normalized_row_json,raw_goods_json,
            warehouse_id,status_id,quantity_for_size_filter,supply_date,fact_date,synced_at
        ) VALUES(?,?,?,?,?,'W',5,250,'2026-05-07','2026-05-07','2026-06-10T00:00:00Z')
        """,
        (
            supply_id,
            cache_key,
            supply_id,
            json.dumps(normalized),
            json.dumps([{"nmID": 111, "quantity": 250, "acceptedQuantity": 247}]),
        ),
    )


def _insert_checkpoint(conn) -> None:
    import json

    supply_id = "legacy-opening-supply"
    cache_key = f"supply:{supply_id}"

    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint(
            slot,checkpoint_id,created_at,created_by,reason,
            baseline_cache_keys_json,baseline_source_keys_json,
            baseline_supply_ids_json,baseline_record_count,diagnostics_json
        ) VALUES('current','fixture-checkpoint','2026-07-01T00:00:00Z','fixture','fixture',?,?,?,1,'{}')
        """,
        (
            json.dumps(["supply:legacy-opening-supply"]),
            json.dumps(["wb_supply_debit:supply:legacy-opening-supply"]),
            json.dumps(["legacy-opening-supply"]),
        ),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operations(
            operation_id,operation_type,source_type,source_key,source_object_id,
            source_object_label,created_at,created_by,sku_count,total_quantity_delta,
            total_quantity_abs,warnings_json,diagnostics_json
        ) VALUES('legacy-opening-debit','auto_writeoff','wb_supply',?,?,'legacy opening',
                 '2026-07-09T05:11:09Z','system',1,-250,250,'[]','{}')
        """,
        (f"wb_supply_debit:{cache_key}", supply_id),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
            operation_id,line_no,nm_id,quantity_delta,raw_json
        ) VALUES('legacy-opening-debit',1,111,-250,'{}')
        """
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operations(
            operation_id,operation_type,source_type,source_key,source_object_id,
            source_object_label,created_at,created_by,sku_count,total_quantity_delta,
            total_quantity_abs,warnings_json,diagnostics_json
        ) VALUES('legacy-opening-debit-repair','correction_receipt','runtime_repair',
                 'runtime_repair:legacy-opening-debit','legacy-opening-debit','repair',
                 '2026-07-09T06:00:00Z','system',1,250,250,'[]',
                 '{"original_operation_id":"legacy-opening-debit"}')
        """
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
            operation_id,line_no,nm_id,quantity_delta,raw_json
        ) VALUES('legacy-opening-debit-repair',1,111,250,'{}')
        """
    )


def _insert_ff_balance(conn, *, nm_id: int, quantity: int) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operations(
            operation_id,operation_type,source_type,source_key,source_object_id,source_object_label,
            created_at,created_by,sku_count,total_quantity_delta,total_quantity_abs,warnings_json,diagnostics_json
        ) VALUES('opening-ff','opening_balance','manual','opening-ff','opening-ff','opening',
                 '2026-07-01T00:00:00Z','fixture',1,?,?, '[]','{}')
        """,
        (quantity, quantity),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
            operation_id,line_no,nm_id,quantity_delta,raw_json
        ) VALUES('opening-ff',1,?,?,'{}')
        """,
        (nm_id, quantity),
    )


def _insert_supplier_payment(
    conn, *, shipment_id: str, cny: str, rub: str
) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_cny_ledger_operations(
            operation_id,operation_type,source_document_id,source_order_id,
            operation_date,operation_datetime,sequence_key,cny_delta,rub_value_delta,
            status,error_reason,created_at,updated_at
        ) VALUES(?, 'supplier_payment_out', ?, ?, '2026-06-30',
                 '2026-06-30T00:00:00Z', ?, ?, ?, 'posted', '',
                 '2026-06-30T00:00:00Z', '2026-06-30T00:00:00Z')
        """,
        (
            f"payment-{shipment_id}", f"document-{shipment_id}", shipment_id,
            f"20260630:{shipment_id}", f"-{cny}", f"-{rub}",
        ),
    )


def _insert_snapshot(conn, day: str, values: dict[int, dict[str, float]]) -> None:
    rows = []
    for nm_id, metrics in values.items():
        for metric_key, value in metrics.items():
            rows.append([f"{nm_id} {metric_key}", f"SKU:{nm_id}|{metric_key}", value])
    plan = SheetVitrinaV1Envelope(
        plan_version="canonical-cost-fixture",
        snapshot_id=f"snapshot-{day}",
        as_of_date=day,
        date_columns=[day],
        temporal_slots=[SheetVitrinaV1TemporalSlot(slot_key="day", slot_label="day", column_date=day)],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA", write_start_cell="A1", write_rect=f"A1:C{len(rows)+1}",
                clear_range="A:Z", write_mode="overwrite", partial_update_allowed=False,
                header=["label", "key", day], rows=rows, row_count=len(rows), column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS", write_start_cell="A1", write_rect="A1:B1",
                clear_range="A:B", write_mode="overwrite", partial_update_allowed=False,
                header=["key", "value"], rows=[], row_count=0, column_count=2,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO registry_upload_versions(bundle_version,uploaded_at,activated_at) VALUES(?,?,?)",
        (f"bundle-{day}", f"{day}T00:00:00Z", f"{day}T00:00:00Z"),
    )
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ready_snapshots(
            bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (f"bundle-{day}", f"{day}T00:00:00Z", day, plan.snapshot_id, plan.plan_version, f"{day}T01:00:00Z", _serialize_sheet_vitrina_plan(plan)),
    )


def _layer(supply_id: str, accepted_date: str, qty: int, cost: int) -> dict[str, object]:
    return {
        "original_supply_id": supply_id, "nm_id": 1, "warehouse": "W", "destination": "D",
        "open_quantity": str(qty), "accepted_date": accepted_date, "writeoff_date": accepted_date,
        "recognized_unit_cost_rub": str(cost), "paid_unit_cost_rub": str(cost), "provenance": {},
    }


def _doprinato(supply_id: str, accepted_date: str, qty: int, *, original: str = "") -> dict[str, object]:
    return {
        "supply_id": supply_id, "nm_id": 1, "warehouse": "W", "destination": "D",
        "accepted_quantity": str(qty), "accepted_date": accepted_date, "original_supply_id": original,
    }


def _eq(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
