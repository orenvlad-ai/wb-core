#!/usr/bin/env python3
"""Deterministic C/C+1 forward-lane and pinned-backlog recovery smoke."""

from __future__ import annotations

import gc
import json
import resource
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_cutover_production_smoke import (  # noqa: E402
    GATE_AT,
    SHA,
    SHIPMENT_ID,
    _Clock,
    _barrier,
    _seed,
)
from apps.ff_pool_fbs_lifecycle_smoke import _insert_post_t_order  # noqa: E402
from packages.application.ff_pool_cutover_production import (  # noqa: E402
    FfPoolCutoverProductionMutation,
)
from packages.application.ff_pool_fbs_forward_recovery import (  # noqa: E402
    FfPoolFbsForwardRecoveryError,
    FfPoolFbsForwardRecoveryMutation,
)
from packages.application import ff_pool_fbs_forward_recovery as recovery_module  # noqa: E402
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    BACKLOG_RECOVERY_TARGETS_TABLE,
    DRAIN_STATE_TABLE,
    EVENTS_TABLE,
    FfPoolFbsLifecycleError,
    FORWARD_STATE_TABLE,
    IDENTITY_PENDING_TABLE,
    process_post_t_fbs_lifecycle,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.warehouse_functional import (  # noqa: E402
    ensure_warehouse_functional_schema,
)
from packages.application.wb_fbs_orders import WbFbsOrdersCollector  # noqa: E402


class _RecoveryClock:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self) -> str:
        self.index += 1
        return f"2026-08-16T00:00:{self.index:02d}Z"


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = _prepared_runtime(root / "primary")
        _insert_backlog(runtime.db_path)
        runner = FfPoolFbsForwardRecoveryMutation(
            runtime_dir=runtime.runtime_dir,
            deployed_sha=SHA,
            timestamp_factory=_RecoveryClock(),
        )
        # Deploy installs only inert schema.  Before the exact C/C+1 gate, the
        # legacy lane must fail closed on the bad SKU and leave the whole
        # backlog, cursor, balances and capital untouched.
        legacy_balance = _balance_rows(runtime.db_path)
        with sqlite3.connect(runtime.db_path) as conn:
            legacy_cursor = int(
                conn.execute(
                    f"SELECT last_status_observation_sequence FROM {DRAIN_STATE_TABLE}"
                ).fetchone()[0]
            )
            try:
                conn.execute("BEGIN IMMEDIATE")
                process_post_t_fbs_lifecycle(
                    conn,
                    occurred_at="2026-08-16T00:00:00Z",
                    limit=100,
                    schema_ready=True,
                )
            except FfPoolFbsLifecycleError as exc:
                conn.rollback()
                assert exc.code == "order_sku_unmapped"
            else:
                raise AssertionError("pre-gate legacy suffix must remain fail closed")
        assert _balance_rows(runtime.db_path) == legacy_balance
        with sqlite3.connect(runtime.db_path) as conn:
            assert int(
                conn.execute(
                    f"SELECT last_status_observation_sequence FROM {DRAIN_STATE_TABLE}"
                ).fetchone()[0]
            ) == legacy_cursor

        plan = runner.build_plan()
        assert plan["apply_allowed"] is True
        boundary = plan["boundary"]
        cutoff = int(boundary["source_max_status_observation_sequence"])
        old_cursor = int(boundary["old_lifecycle_cursor_sequence"])
        assert cutoff > old_cursor
        assert boundary["forward_start_status_observation_sequence"] == cutoff + 1
        assert plan["target"]["count"] == 4
        assert plan["predicted_effects"]["outcome_counts"] == {
            "event_applied": 3,
            "identity_quarantine": 1,
        }
        assert plan["predicted_effects"]["total_quantity_delta"] == -2
        assert plan["predicted_effects"]["total_capital_delta_rub"] == "-20"

        # Freshness-only timestamps and continuous source ingress are not part
        # of target equality.  The original reviewed fingerprint remains valid.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {DRAIN_STATE_TABLE} SET updated_at='2026-08-16T00:10:00Z'"
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_supplies_fbs_collector_state "
                "SET last_attempt_at='2026-08-16T00:10:01Z',"
                "last_success_at='2026-08-16T00:10:01Z'"
            )
            conn.commit()
        repeated_plan = runner.build_plan()
        assert repeated_plan["fingerprint"] == plan["fingerprint"]
        assert repeated_plan["target"]["status_observation_sequences"] == (
            plan["target"]["status_observation_sequences"]
        )
        with sqlite3.connect(runtime.db_path) as conn:
            for order_id in range(9700, 9706):
                _insert_post_t_order(
                    conn,
                    order_id=order_id,
                    supplier="complete" if order_id == 9700 else "new",
                    wb="sorted" if order_id == 9700 else "waiting",
                    observed_at=f"2026-08-16T00:2{order_id - 9700}:00Z",
                )
            conn.commit()

        past_event_max = int(
            plan["past_fulfilled_invariant"]["pinned_event_sequence_max"]
        )
        past_before = _handoff_digest(runtime.db_path, maximum=past_event_max)
        balance_before = _balance_rows(runtime.db_path)
        try:
            runner.apply(
                plan,
                fingerprint=plan["fingerprint"],
                approval_reference="synthetic-owner-gate",
                actor="smoke",
                evidence_dir=root / "evidence",
                crash="after_commit_before_response",
            )
        except FfPoolFbsForwardRecoveryError as exc:
            assert exc.code == "simulated_ambiguous_transport"
        else:
            raise AssertionError("ambiguous-transport simulation must interrupt response")
        readback = runner.readback(fingerprint=plan["fingerprint"])
        assert readback["status"] == "completed"
        assert readback["cutoff_sequence"] == cutoff
        assert readback["forward_cursor_sequence"] == cutoff
        assert readback["target_count"] == 4
        repeated = runner.verify_noop(
            plan,
            fingerprint=plan["fingerprint"],
        )
        assert repeated["status"] == "completed_no_op"
        assert repeated["query_only"] is True
        assert repeated["repeat_submit_performed"] is False
        assert repeated["would_write"] is False

        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            old_cursor_after = int(
                conn.execute(
                    f"SELECT last_status_observation_sequence FROM {DRAIN_STATE_TABLE}"
                ).fetchone()[0]
            )
            assert old_cursor_after == old_cursor
            assert conn.execute(
                f"SELECT COUNT(*) FROM {BACKLOG_RECOVERY_TARGETS_TABLE}"
            ).fetchone()[0] == 4
            assert conn.execute(
                f"SELECT COUNT(*) FROM {BACKLOG_RECOVERY_TARGETS_TABLE} "
                "WHERE source_status_observation_sequence>?",
                (cutoff,),
            ).fetchone()[0] == 0
            assert conn.execute(
                f"SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} WHERE order_id=9600"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id=9600"
            ).fetchone()[0] == 0

            # C+1..N is independent of the unresolved old SKU.  The first
            # ordinary pass uses only the forward cursor and processes it now.
            conn.execute("BEGIN IMMEDIATE")
            forward = process_post_t_fbs_lifecycle(
                conn,
                occurred_at="2026-08-16T00:30:00Z",
                limit=100,
                schema_ready=True,
            )
            conn.commit()
            assert forward["lane"] == "forward"
            assert forward["processed_count"] == 6
            assert forward["summary"]["fulfilled"] == 1
            assert forward["summary"]["reserved"] == 6
            assert forward["identity_pending_count"] == 0
            assert int(
                conn.execute(
                    f"SELECT last_status_observation_sequence FROM {FORWARD_STATE_TABLE}"
                ).fetchone()[0]
            ) > cutoff

        processor = WbFbsOrdersCollector(
            db_path=runtime.db_path,
            timestamp_factory=lambda: "2026-08-16T00:30:10Z",
            enabled=False,
        ).orders_page()["lifecycle_processor"]
        assert processor["processor_lane"] == "forward"
        assert processor["forward_cutoff_sequence"] == cutoff
        assert processor["backlog_old_cursor_sequence"] == old_cursor
        assert processor["backlog_recovery_status"] == "completed"
        assert processor["lag_observation_count"] == 0
        assert processor["pending_identity_count"] == 1
        assert processor["pending_reason_counts"] == {"order_sku_unmapped": 1}

        balance_after = _balance_rows(runtime.db_path)
        assert _quantity(balance_after, 101) == _quantity(balance_before, 101) - 3
        assert _capital(balance_after, 101) == _capital(balance_before, 101) - Decimal("30")
        assert {
            key: value for key, value in balance_after.items() if key != 101
        } == {key: value for key, value in balance_before.items() if key != 101}
        assert _handoff_digest(runtime.db_path, maximum=past_event_max) == past_before
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} "
                "WHERE order_id IN (9601,9603,9700) AND event_type='handoff_debit'"
            ).fetchone()[0] == 3
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id=9600"
            ).fetchone()[0] == 0
            conn.execute("BEGIN IMMEDIATE")
            noop = process_post_t_fbs_lifecycle(
                conn,
                occurred_at="2026-08-16T00:31:00Z",
                limit=100,
                schema_ready=True,
            )
            conn.commit()
            assert noop["processed_count"] == 0
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} "
                "WHERE order_id IN (9601,9603,9700) AND event_type='handoff_debit'"
            ).fetchone()[0] == 3

        # Exact business evidence, unlike freshness, is target-scoped CAS.
        drift_runtime = _prepared_runtime(root / "drift")
        _insert_backlog(drift_runtime.db_path)
        drift_runner = FfPoolFbsForwardRecoveryMutation(
            runtime_dir=drift_runtime.runtime_dir,
            deployed_sha=SHA,
            timestamp_factory=_RecoveryClock(),
        )
        drift_plan = drift_runner.build_plan()
        with sqlite3.connect(drift_runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_ff_pool_balances "
                "SET capital_rub='101' "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            )
            conn.commit()
        try:
            drift_runner.apply(
                drift_plan,
                fingerprint=drift_plan["fingerprint"],
                approval_reference="synthetic-owner-gate",
                actor="smoke",
                evidence_dir=root / "drift-evidence",
            )
        except FfPoolFbsForwardRecoveryError as exc:
            assert exc.code == "target_source_drift"
        else:
            raise AssertionError("target business drift must fail closed")
        assert drift_runner.readback(fingerprint=drift_plan["fingerprint"])[
            "status"
        ] == "not_applied"

        # A canonical after-image mismatch must preserve a private, field-level
        # privacy-safe diff before the writer transaction rolls back.
        after_image_runtime = _prepared_runtime(root / "after-image-drift")
        _insert_backlog(after_image_runtime.db_path)
        after_image_runner = FfPoolFbsForwardRecoveryMutation(
            runtime_dir=after_image_runtime.runtime_dir,
            deployed_sha=SHA,
            timestamp_factory=_RecoveryClock(),
        )
        after_image_plan = after_image_runner.build_plan()
        after_image_evidence = root / "after-image-drift-evidence"
        try:
            after_image_runner.apply(
                after_image_plan,
                fingerprint=after_image_plan["fingerprint"],
                approval_reference="synthetic-owner-gate",
                actor="smoke",
                evidence_dir=after_image_evidence,
                crash="simulate_after_image_drift",
            )
        except FfPoolFbsForwardRecoveryError as exc:
            assert exc.code == "target_after_image_drift"
            evidence_path = Path(str(exc.details["evidence_path"]))
            assert evidence_path.is_file()
            assert evidence_path.stat().st_mode & 0o077 == 0
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            assert evidence["phase"] == "inside_writer_before_rollback"
            assert evidence["privacy"] == {
                "order_ids_included": False,
                "status_sequences_included": False,
                "pii_included": False,
                "target_identity": "sha256_digest_only",
            }
            assert any(
                row["path"] == "$.effect.total_quantity_delta"
                for row in evidence["diffs"]
            )
            assert "9601" not in evidence_path.read_text(encoding="utf-8")
        else:
            raise AssertionError("canonical after-image drift must fail closed")
        assert after_image_runner.readback(
            fingerprint=after_image_plan["fingerprint"]
        )["status"] == "not_applied"

        _assert_production_scale_projection(root / "scale")

    print("ff_pool_fbs_forward_recovery_smoke: OK")
    return 0


def _prepared_runtime(root: Path) -> RegistryUploadDbBackedRuntime:
    runtime_dir = root / "runtime"
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    runtime_dir.mkdir(parents=True)
    runtime.save_nomenclature_item(
        {
            "item_id": "forward-recovery-nm-101",
            "is_active": True,
            "is_hidden": False,
            "our_sku": "seller-101",
            "nm_id": 101,
            "barcode": "sku-101",
            "nomenclature_name": "Synthetic forward recovery SKU",
            "created_at": GATE_AT,
            "updated_at": GATE_AT,
        }
    )
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_functional_schema(conn)
        _ensure_schema(conn)
        _seed(conn)
        conn.commit()
    env_file = root / "runtime.env"
    env_file.write_text("WB_FBS_COLLECTOR_ENABLED=true\n", encoding="utf-8")
    cutover = FfPoolCutoverProductionMutation(
        runtime_dir=runtime_dir,
        env_file=env_file,
        deployed_sha=SHA,
        timestamp_factory=_Clock(),
    )
    gate = cutover.build_gate_plan(excluded_shipment_ids=[SHIPMENT_ID])
    applied = cutover.apply(
        gate,
        fingerprint=gate["fingerprint"],
        approval_reference="synthetic-cutover-gate",
        actor="smoke",
        backup_dir=root / "cutover-backups",
        external_barrier_evidence=_barrier(),
    )
    assert applied["status"] == "applied_reconciled"
    return runtime


def _insert_backlog(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        _insert_post_t_order(
            conn,
            order_id=9600,
            supplier="complete",
            wb="sorted",
            observed_at="2026-08-16T00:01:00Z",
            source_nm_id=999,
            source_chrt_id=1999,
            seller_sku="synthetic-unmapped",
            barcode="synthetic-unmapped",
        )
        _insert_post_t_order(
            conn,
            order_id=9601,
            supplier="complete",
            wb="sorted",
            observed_at="2026-08-16T00:02:00Z",
        )
        _insert_post_t_order(
            conn,
            order_id=9602,
            supplier="new",
            wb="waiting",
            observed_at="2026-08-16T00:03:00Z",
        )
        _insert_post_t_order(
            conn,
            order_id=9603,
            supplier="complete",
            wb="sorted",
            observed_at="2026-08-16T00:04:00Z",
        )
        conn.commit()


def _assert_production_scale_projection(root: Path) -> None:
    target_count = 40_000
    unrelated_bytes = 192 * 1024 * 1024
    max_rss_growth_bytes = 512 * 1024 * 1024
    runtime = _prepared_runtime(root)
    start = datetime(2026, 8, 16, 1, 0, tzinfo=timezone.utc)
    with sqlite3.connect(runtime.db_path) as conn:
        for index in range(target_count):
            _insert_post_t_order(
                conn,
                order_id=20_000 + index,
                supplier="new",
                wb="waiting",
                observed_at=(start + timedelta(seconds=index)).isoformat().replace(
                    "+00:00", "Z"
                ),
            )
        # This payload models unrelated production-scale operational history.
        # A whole-database backup would materialize it; target projection must
        # never read or copy it.
        conn.execute(
            "CREATE TABLE synthetic_unrelated_operational_payload("
            "row_id INTEGER PRIMARY KEY,payload BLOB NOT NULL)"
        )
        conn.execute(
            "INSERT INTO synthetic_unrelated_operational_payload(row_id,payload) "
            "VALUES(1,zeroblob(?))",
            (unrelated_bytes,),
        )
        conn.commit()

    gc.collect()
    before_rss = _peak_rss_bytes()
    source_stat_before = runtime.db_path.stat()
    runner = FfPoolFbsForwardRecoveryMutation(
        runtime_dir=runtime.runtime_dir,
        deployed_sha=SHA,
        timestamp_factory=_RecoveryClock(),
    )
    plan = runner.build_plan()
    after_rss = _peak_rss_bytes()
    planner = dict(plan["planner"])
    assert plan["target"]["count"] == target_count
    assert plan["predicted_effects"]["outcome_counts"] == {
        "event_applied": target_count
    }
    assert planner["source_query_only"] is True
    assert planner["source_explicit_read_transaction"] is True
    assert planner["whole_database_backup"] is False
    assert planner["scratch_backend"] == (
        "private_file_backed_coherent_dependency_snapshot"
    )
    assert planner["scratch_file_mode"] == "0600"
    assert planner["scratch_temp_store"] == "file"
    assert planner["scratch_removed_after_preview"] is True
    assert planner["full_relevant_schema_cloned"] is True
    assert planner["schema_digest_equal"] is True
    assert planner["schema_evidence"]["trigger_count"] >= 20
    assert planner["foreign_key_check"] == "pass"
    assert "synthetic_unrelated_operational_payload" not in planner["table_row_counts"]
    assert int(planner["copied_payload_bytes"]) < 256 * 1024 * 1024
    assert int(planner["scratch_bytes"]) < 384 * 1024 * 1024
    assert max(0, after_rss - before_rss) < max_rss_growth_bytes
    assert runtime.db_path.stat().st_size > unrelated_bytes
    source_stat_after = runtime.db_path.stat()
    assert source_stat_after.st_size == source_stat_before.st_size
    assert source_stat_after.st_mtime_ns == source_stat_before.st_mtime_ns
    assert not list(runner.scratch_dir.glob("coherent-preview-*.sqlite3*"))
    with sqlite3.connect(runtime.db_path) as conn:
        assert conn.execute(
            "SELECT length(payload) FROM synthetic_unrelated_operational_payload "
            "WHERE row_id=1"
        ).fetchone()[0] == unrelated_bytes
        assert conn.execute(
            f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE order_id>=20000"
        ).fetchone()[0] == 0

    original_chunk_size = recovery_module.PROJECTION_CHUNK_SIZE
    try:
        recovery_module.PROJECTION_CHUNK_SIZE = 97
        repeated = runner.build_plan()
    finally:
        recovery_module.PROJECTION_CHUNK_SIZE = original_chunk_size
    assert repeated["planner"]["chunk_size"] == 97
    assert repeated["target"]["stable_business_digest"] == (
        plan["target"]["stable_business_digest"]
    )
    assert repeated["past_fulfilled_invariant"] == plan["past_fulfilled_invariant"]
    assert repeated["predicted_effects"] == plan["predicted_effects"]
    assert repeated["planner"]["schema_evidence"] == planner["schema_evidence"]
    assert repeated["planner"]["canonical_write_seeds"] == (
        planner["canonical_write_seeds"]
    )
    assert not list(runner.scratch_dir.glob("coherent-preview-*.sqlite3*"))
    production_source = (
        ROOT / "packages/application/ff_pool_fbs_forward_recovery.py"
    ).read_text(encoding="utf-8")
    assert ".backup(" not in production_source
    assert 'sqlite3.connect(":memory:")' not in production_source


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _balance_rows(path: Path) -> dict[int, tuple[int, Decimal]]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT nm_id,quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
            "WHERE facility_id='fac_moscow' AND pool='FBS' ORDER BY nm_id"
        ).fetchall()
    return {int(row[0]): (int(row[1]), Decimal(str(row[2]))) for row in rows}


def _quantity(rows: dict[int, tuple[int, Decimal]], nm_id: int) -> int:
    return rows[nm_id][0]


def _capital(rows: dict[int, tuple[int, Decimal]], nm_id: int) -> Decimal:
    return rows[nm_id][1]


def _handoff_digest(path: Path, *, maximum: int) -> str:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            f"""SELECT event_id,order_id,event_type,frozen_wac_rub,evidence_digest
                FROM {EVENTS_TABLE}
                WHERE event_sequence<=?
                  AND event_type IN ('opening_handoff_debit','handoff_debit')
                ORDER BY event_sequence""",
            (maximum,),
        ).fetchall()
    return json.dumps(rows, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
