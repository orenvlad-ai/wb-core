"""Synthetic regression for the exact five-document overhead backfill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import packages.application.ff_pool_overhead_backfill as subject  # noqa: E402
from packages.application.ff_pool_documents import (  # noqa: E402
    TARGETED_RECALC_QUEUE_TABLE,
    FfPoolDocumentService,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    ensure_ff_pool_fbs_lifecycle_schema,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import (  # noqa: E402
    ensure_warehouse_functional_schema,
)
from packages.contracts.ff_pool_documents import DocumentIdentity  # noqa: E402


SHA = "a" * 40


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        result = self.value.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.value += timedelta(seconds=1)
        return result


def _identity(name: str, clock: Clock) -> DocumentIdentity:
    return DocumentIdentity(
        request_id=f"fixture:{name}",
        source_system="synthetic_fixture",
        source_type="pool_overhead",
        source_id=name,
        source_revision=f"{name}:v1",
        idempotency_epoch=1,
        actor="synthetic",
        business_date="2026-08-21",
    )


class FakeWarehouse:
    def __init__(self, *, runtime: RegistryUploadDbBackedRuntime) -> None:
        self.runtime = runtime

    def build_targeted_recovery_plan(self, **kwargs: object) -> dict[str, object]:
        return {
            "plan_fingerprint": "sha256:" + "2" * 64,
            "targeted_recalc_requests": kwargs["targeted_recalc_requests"],
        }

    def apply_plan(self, plan: dict[str, object], **_kwargs: object) -> dict[str, object]:
        with sqlite3.connect(self.runtime.db_path) as conn:
            for item in plan["targeted_recalc_requests"]:  # type: ignore[index]
                conn.execute(
                    f"UPDATE {TARGETED_RECALC_QUEUE_TABLE} SET "
                    "status='complete',started_at=requested_at,"
                    "finished_at=requested_at,error=NULL WHERE queue_id=?",
                    (item["queue_id"],),  # type: ignore[index]
                )
            conn.commit()
        return {"idempotent": False, "active_version": {"version_id": "v1"}}


class FakeFinance:
    def recalculate_stale_cost_weeks(self, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "applied",
            "fingerprint": "sha256:" + "4" * 64,
            "recalculated_week_count": 1,
            "non_target_preserved": True,
            "phase_timings_ms": {"writer_lock_hold": 1.0},
        }


def main() -> None:
    with TemporaryDirectory(prefix="ff-overhead-backfill-") as raw:
        root = Path(raw)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
        root.mkdir(parents=True, exist_ok=True)
        (root / ".wb-core-runtime-sha").write_text(SHA + "\n", encoding="utf-8")
        clock = Clock()
        service = FfPoolDocumentService(
            db_path=runtime.db_path,
            runtime_dir=root,
            timestamp_factory=clock,
            resume=False,
        )
        with sqlite3.connect(runtime.db_path) as conn:
            ensure_ff_pool_fbs_lifecycle_schema(conn)
            conn.commit()
        with sqlite3.connect(runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            for facility_id, code, name, city in (
                ("fac_msk", "MSK", "FF Москва", "Москва"),
                ("fac_ore", "ORE", "FF Оренбург", "Оренбург"),
            ):
                now = clock()
                conn.execute(
                    f"INSERT INTO {FACILITIES_TABLE}(facility_id,code,name,active,"
                    "display_timezone,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        facility_id,
                        code,
                        name,
                        1,
                        "Asia/Yekaterinburg",
                        now,
                        now,
                    ),
                )
                conn.execute(
                    f"INSERT INTO {FACILITY_PROFILES_TABLE}(facility_id,city,"
                    "future_fields_json,created_at,updated_at) VALUES(?,?,'{}',?,?)",
                    (facility_id, city, now, now),
                )
            conn.execute(
                f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,"
                "reader_enabled,source_revision,created_at,metadata_json) "
                "VALUES(1,1,1,'fixture',?,'{}')",
                (clock(),),
            )
            conn.executemany(
                f"INSERT INTO {BALANCES_TABLE}(facility_id,pool,nm_id,projection_epoch,"
                "quantity,capital_rub,wac_rub,source_watermark,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "fac_msk",
                        "FBS",
                        101,
                        1,
                        10,
                        "100.0000000000000000001",
                        "10.00000000000000000001",
                        "fixture",
                        clock(),
                    ),
                    ("fac_ore", "FBS", 202, 1, 20, "200.00", "10.00", "fixture", clock()),
                ],
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_functional_versions("
                "version_id,cutover_id,version_kind,effective_at,business_effective_date,"
                "published_at,status,plan_fingerprint,local_source_digest,"
                "source_watermarks_json,created_at) VALUES("
                "'v1','fixture','fixture',?,'2026-08-21',?,'good',"
                "'sha256:fixture','sha256:fixture','{}',?)",
                (clock(), clock(), clock()),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_functional_active("
                "slot,version_id,updated_at) VALUES(1,'v1',?)",
                (clock(),),
            )
            for nm_id, quantity, wac, capital in (
                (
                    101,
                    "10",
                    "10.00000000000000000001",
                    "100.0000000000000000001",
                ),
                (202, "20", "10.00", "200.00"),
            ):
                conn.execute(
                    "INSERT INTO sheet_vitrina_v1_warehouse_functional_balances("
                    "version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,"
                    "cost_covered_quantity,quality,certified,wb_quantity,"
                    "wb_in_way_to_client,wb_in_way_from_client,provenance_json) "
                    "VALUES('v1','ff',?,?,?,?,?,'exact',1,'0','0','0','{}')",
                    (nm_id, quantity, wac, capital, quantity),
                )
            conn.commit()

        documents = []
        amounts = (
            ("fac_msk", "msk-1", "30000.00"),
            ("fac_msk", "msk-2", "30000.00"),
            ("fac_msk", "msk-3", "30000.00"),
            ("fac_msk", "msk-4", "25206.50"),
            ("fac_ore", "ore-1", "60000.00"),
        )
        for facility_id, name, amount in amounts:
            preview = service.accept_preview(
                identity=_identity(name, clock),
                document_kind="pool_overhead",
                manifest={
                    "facility_id": facility_id,
                    "scope": "FBS",
                    "amount_rub": amount,
                    "category": "storage",
                    "comment": "",
                    "source_mode": "manual",
                },
            )
            posted = service.post(str(preview["request_id"]))
            assert posted["state"] == "posted", posted
            documents.append(str(posted["document"]["document_id"]))

        # Recreate the legacy production state: detail ledger already includes
        # all five costs, while aggregate current projection and queue do not.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(f"DELETE FROM {TARGETED_RECALC_QUEUE_TABLE}")
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_balances "
                "SET capital_rub='100.0000000000000000001',"
                "wac_rub='10.00000000000000000001' "
                "WHERE version_id='v1' AND warehouse_key='ff' AND nm_id=101"
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_balances "
                "SET capital_rub='200.00',wac_rub='10.00' "
                "WHERE version_id='v1' AND warehouse_key='ff' AND nm_id=202"
            )
            conn.commit()

        mutation = subject.FfPoolOverheadBackfill(
            runtime_dir=root,
            deployed_sha=SHA,
            timestamp_factory=clock,
        )
        plan = mutation.build_plan()
        assert plan["apply_allowed"] is True, plan["blockers"]
        assert plan["scope"]["document_ids"] == documents
        assert plan["expected_effects"]["capital_delta_rub"] == "175206.50"
        assert plan["expected_effects"]["quantity_delta"] == 0
        assert plan["invariants"]["city_scope"] == {
            "Москва": {"document_count": 4, "amount_rub": "115206.50"},
            "Оренбург": {"document_count": 1, "amount_rub": "60000.00"},
        }

        original_warehouse = subject.WarehouseFunctionalBlock
        original_build_economics = subject.build_functional_economics_backfill_plan
        original_apply_economics = subject.apply_functional_economics_backfill_plan
        original_finance = subject.block_from_env
        subject.WarehouseFunctionalBlock = FakeWarehouse
        subject.build_functional_economics_backfill_plan = lambda *_args, **_kwargs: {
            "plan_fingerprint": "sha256:" + "3" * 64
        }
        subject.apply_functional_economics_backfill_plan = (
            lambda *_args, **_kwargs: {
                "changed_snapshot_count": 1,
                "database_written": True,
                "rollback_manifest_digest": "sha256:" + "5" * 64,
            }
        )
        subject.block_from_env = lambda *_args, **_kwargs: FakeFinance()
        try:
            result = mutation.apply(
                plan,
                fingerprint=str(plan["fingerprint"]),
                approval_reference="synthetic-owner-gate",
                actor="synthetic",
                backup_dir=(root / "backups").resolve(),
                evidence_dir=(root / "evidence").resolve(),
            )
            assert result["status"] == "complete", result
            assert result["readback"]["quantity_unchanged"] is True
            assert result["readback"]["past_fulfilled_lifecycle_unchanged"] is True
            assert result["readback"]["documents_unchanged"] is True
            assert result["readback"]["non_target_unchanged"] is True
            assert result["readback"]["pre_change_invariants_verified"] is True
            assert result["readback"]["no_duplicate_submit"] is True
            exact_moscow = next(
                item
                for item in result["readback"]["target_projection"]
                if int(item["nm_id"]) == 101
            )
            assert exact_moscow["aggregate_capital_rub"] == (
                "115306.5000000000000000001"
            )
            assert exact_moscow["detail_capital_rub"] == (
                "115306.5000000000000000001"
            )
            assert Path(result["backup"]["receipt_path"]).is_file()
            assert result["backup"]["reused"] is False
            assert all(
                item["finance_status"] == "complete"
                and item["finance_source_fingerprint"] == "sha256:" + "4" * 64
                for item in result["readback"]["queues"]
            )
            repeated = mutation.apply(
                plan,
                fingerprint=str(plan["fingerprint"]),
                approval_reference="synthetic-owner-gate",
                actor="synthetic",
                backup_dir=(root / "backups").resolve(),
                evidence_dir=(root / "evidence").resolve(),
            )
            assert repeated["idempotent"] is True
            assert repeated["backup"]["receipt_path"] == result["backup"]["receipt_path"]
        finally:
            subject.WarehouseFunctionalBlock = original_warehouse
            subject.build_functional_economics_backfill_plan = original_build_economics
            subject.apply_functional_economics_backfill_plan = original_apply_economics
            subject.block_from_env = original_finance
    print("ff_pool_overhead_backfill_smoke: OK")


if __name__ == "__main__":
    main()
