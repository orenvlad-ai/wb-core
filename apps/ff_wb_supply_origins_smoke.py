"""Focused Stage 4 domain checks for FBW supply FF-origin assignment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_documents import (  # noqa: E402
    DOCUMENTS_TABLE,
    REQUESTS_TABLE,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
)
from packages.application.ff_wb_supply_origins import (  # noqa: E402
    ASSIGNMENTS_TABLE,
    FfWbSupplyOriginAssignments,
    FfWbSupplyOriginError,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_recovery_policy import DOMAIN_TABLE_PREFIXES  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        result = self.value.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.value += timedelta(seconds=1)
        return result


def main() -> None:
    _schema_absent_is_controlled()
    with TemporaryDirectory(prefix="ff-wb-origin-") as directory:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory) / "runtime")
        runtime.list_wb_supplies()
        clock = Clock()
        service = FfWbSupplyOriginAssignments(db_path=runtime.db_path, timestamp_factory=clock)
        _seed_supplies(runtime)
        _default_off(service, runtime.db_path)
        facilities = _enable_fixture_writer(runtime.db_path, clock)
        _assignment_lifecycle(service, runtime.db_path, facilities)
    print("ff_wb_supply_origins_smoke: OK")


def _schema_absent_is_controlled() -> None:
    with TemporaryDirectory(prefix="ff-wb-origin-absent-") as directory:
        db_path = Path(directory) / "empty.sqlite3"
        db_path.touch()
        service = FfWbSupplyOriginAssignments(
            db_path=db_path, timestamp_factory=lambda: "2026-08-12T00:00:00Z"
        )
        page = service.assignments_page()
        assert page["status"] == "schema_absent"
        assert ASSIGNMENTS_TABLE in page["missing_tables"]


def _seed_supplies(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_wb_supply_rows(
        rows=[
            {
                "supply_id": "supply:41000001",
                "cache_key": "supply:41000001",
                "wb_supply_id": "41000001",
                "preorder_id": "",
                "status_id": 3,
                "warehouse_id": "507",
                "raw_list_hash": "sha256:list-one",
                "raw_detail_hash": "sha256:detail-one",
                "raw_goods_hash": "sha256:goods-one",
                "number_label": "41000001",
                "type_label": "Поставка",
            },
            {
                "supply_id": "preorder:9901",
                "cache_key": "preorder:9901",
                "wb_supply_id": "",
                "preorder_id": "9901",
                "status_id": 1,
                "number_label": "9901",
                "type_label": "Предзаказ",
            },
        ],
        warehouses=[],
        synced_at="2026-08-12T07:59:00Z",
    )


def _domain_counts(db_path: Path) -> dict[str, int]:
    tables = [ASSIGNMENTS_TABLE, OPERATIONS_TABLE, LINES_TABLE, DOCUMENTS_TABLE, REQUESTS_TABLE]
    with sqlite3.connect(db_path) as conn:
        return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def _default_off(service: FfWbSupplyOriginAssignments, db_path: Path) -> None:
    before = _domain_counts(db_path)
    detail = service.assignment_detail("41000001")
    page = service.assignments_page()
    after = _domain_counts(db_path)
    assert before == after
    assert detail["reason"] == "facility_pool_feature_off"
    assert not detail["assignment_allowed"] and detail["current_assignment"] is None
    assert page["assignments"] == [] and page["etag"].startswith('"sha256:')
    try:
        service.assign_origin(
            "41000001",
            {
                "request_id": "stage4:off:request",
                "facility_id": "missing",
                "expected_assignment_id": "",
            },
            actor="fixture",
        )
    except FfWbSupplyOriginError as exc:
        assert exc.code == "facility_pool_feature_off" and exc.http_status == 409
    else:
        raise AssertionError("default-off assignment must fail closed")
    assert _domain_counts(db_path) == before


def _enable_fixture_writer(db_path: Path, clock: Clock) -> tuple[str, str, str]:
    rows = (
        ("facility-a", "FF-001", "Москва Север", 1),
        ("facility-b", "FF-002", "Оренбург", 1),
        ("facility-inactive", "FF-003", "Архивный FF", 0),
    )
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            f"""INSERT INTO {FACILITIES_TABLE}(
                   facility_id,code,name,active,display_timezone,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            [(*row, "Asia/Yekaterinburg", clock(), clock()) for row in rows],
        )
        conn.execute(
            f"""INSERT INTO {FEATURE_EPOCHS_TABLE}(
                   epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json
               ) VALUES(1,1,0,'stage4-fixture-writer',?,'{{}}')""",
            (clock(),),
        )
        conn.commit()
    return "facility-a", "facility-b", "facility-inactive"


def _assignment_lifecycle(
    service: FfWbSupplyOriginAssignments,
    db_path: Path,
    facilities: tuple[str, str, str],
) -> None:
    facility_a, facility_b, inactive = facilities
    ready = service.assignment_detail("supply:41000001")
    assert ready["assignment_allowed"] and len(ready["facilities"]) == 2
    assert ready["policy"] == {
        "pool": "FBO",
        "append_only": True,
        "correction_requires_current_assignment": True,
        "creates_pool_movement": False,
        "mutates_wb": False,
    }

    payload = {
        "request_id": "stage4:assign:first",
        "facility_id": facility_a,
        "expected_assignment_id": "",
        "reason": "Фактическая отгрузка",
    }
    first = service.assign_origin("41000001", payload, actor="fixture-operator")
    repeated = service.assign_origin("41000001", payload, actor="fixture-operator")
    assert not first["idempotent"] and repeated["idempotent"]
    assert first["assignment"] == repeated["assignment"]
    assignment_id = first["assignment"]["assignment_id"]
    assert first["assignment"]["pool"] == "FBO"
    assert first["assignment"]["feature_epoch"] == 1
    assert first["assignment"]["source_revision"].startswith("sha256:")
    assert not first["creates_pool_movement"]

    try:
        service.assign_origin(
            "41000001",
            {
                **payload,
                "facility_id": facility_b,
                "expected_assignment_id": assignment_id,
            },
            actor="fixture-operator",
        )
    except FfWbSupplyOriginError as exc:
        assert exc.code == "request_id_conflict"
    else:
        raise AssertionError("request id reuse with different semantics must fail")

    for bad_expected in ("", "wbfo_stale"):
        try:
            service.assign_origin(
                "41000001",
                {
                    "request_id": f"stage4:stale:{bad_expected or 'empty'}",
                    "facility_id": facility_b,
                    "expected_assignment_id": bad_expected,
                },
                actor="fixture-operator",
            )
        except FfWbSupplyOriginError as exc:
            assert exc.code == "stale_origin_assignment"
        else:
            raise AssertionError("stale CAS must fail")

    try:
        service.assign_origin(
            "41000001",
            {
                "request_id": "stage4:inactive:facility",
                "facility_id": inactive,
                "expected_assignment_id": assignment_id,
            },
            actor="fixture-operator",
        )
    except FfWbSupplyOriginError as exc:
        assert exc.code == "facility_inactive"
    else:
        raise AssertionError("inactive origin must fail")

    try:
        service.assign_origin(
            "preorder:9901",
            {
                "request_id": "stage4:preorder:block",
                "facility_id": facility_b,
                "expected_assignment_id": "",
            },
            actor="fixture-operator",
        )
    except FfWbSupplyOriginError as exc:
        assert exc.code == "wb_supply_has_no_real_id"
    else:
        raise AssertionError("preorder without a real WB id must fail")

    correction = service.assign_origin(
        "41000001",
        {
            "request_id": "stage4:assign:correction",
            "facility_id": facility_b,
            "expected_assignment_id": assignment_id,
            "reason": "Исправление склада-источника",
        },
        actor="fixture-operator",
    )
    assert correction["assignment"]["supersedes_assignment_id"] == assignment_id
    detail = service.assignment_detail("41000001")
    assert detail["current_assignment"]["facility_id"] == facility_b
    assert len(detail["history"]) == 2
    page = service.assignments_page(limit=1)
    assert page["page"]["total"] == 1 and page["assignments"][0]["facility_id"] == facility_b
    audit = service.assignments_page(current_only=False, limit=1)
    assert audit["page"]["total"] == 2 and audit["page"]["has_next"]

    counts = _domain_counts(db_path)
    assert counts[ASSIGNMENTS_TABLE] == 2
    assert counts[OPERATIONS_TABLE] == 0
    assert counts[LINES_TABLE] == 0
    assert counts[DOCUMENTS_TABLE] == 0
    assert counts[REQUESTS_TABLE] == 0
    assert any(ASSIGNMENTS_TABLE.startswith(prefix) for prefix in DOMAIN_TABLE_PREFIXES)
    with sqlite3.connect(db_path) as conn:
        plan = " ".join(
            str(row)
            for row in conn.execute(
                f"EXPLAIN QUERY PLAN SELECT * FROM {ASSIGNMENTS_TABLE} "
                "WHERE wb_supply_cache_key=? ORDER BY assignment_sequence DESC",
                ("supply:41000001",),
            ).fetchall()
        )
        assert "wb_supply_ff_origin_by_supply" in plan
        for statement in (
            f"UPDATE {ASSIGNMENTS_TABLE} SET reason='x' WHERE assignment_id=?",
            f"DELETE FROM {ASSIGNMENTS_TABLE} WHERE assignment_id=?",
        ):
            try:
                conn.execute(statement, (assignment_id,))
            except sqlite3.IntegrityError as exc:
                assert "immutable" in str(exc) or "append-only" in str(exc)
            else:
                raise AssertionError("origin assignment audit must be immutable")


if __name__ == "__main__":
    main()
